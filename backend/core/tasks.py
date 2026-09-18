"""Scheduled work owned by modules.

定时任务 scheduled task / 周期 schedule / 补跑 catch-up

The registry's own docstring has promised modules "its own background tasks"
since Phase 0, and until now there was no slot for one: a module could declare
tables, routes, pages, subscriptions and a startup hook, but nothing that runs
at four in the morning. Phase 4 needs exactly that (主文档 §H asks for three
layers of safety net, and the middle one is a fixed time), so the slot is added
here rather than bolted onto the one feature that happened to need it first.

**Two shapes of schedule, and no more** (决定 14). ``"04:00"`` means every day at
that wall-clock time; ``"30m"`` and ``"6h"`` mean every so often. A cron
expression would cover more cases than this project has, and a scheduling
library would be a dependency plus a cross-platform surface for a need this
small.

**Wall clock, not UTC.** Timestamps are stored in UTC like everywhere else in
the project, but ``"04:00"`` is read as *local* 4am, because that is what
someone setting it means. Reading it as UTC would silently put the nightly job
at noon.

**Restart must neither skip nor repeat** (验收 A2). The next due time lives in
the database, not in memory:

* nothing scheduled yet -> the first due time is the next occurrence, so
  installing a module at 10am does not immediately fire its 4am job;
* due time already passed while the service was down -> it runs once on the
  next tick, no matter how many occurrences were missed, because the next due
  time is recomputed from now rather than advanced occurrence by occurrence;
* finished -> the next due time is written before anything else can look at it.

**A failing task must not take the loop with it.** Every run is wrapped; the
exception is logged at ERROR, which is what puts it on Bark (see
``core.notifications`` — the alert handler is attached to logging, so a task
does not need to know that push notifications exist).
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.core import runtime_config
from backend.core.db import Migration, get_connection
from backend.core.logging import get_logger, trace

log = get_logger("core.tasks")

#: How often the loop wakes up. A daily task therefore fires within half a
#: minute of its time, which is as precise as anything here needs to be.
TICK_SECONDS = 30

MIGRATIONS = [
    Migration(
        version=1,
        name="scheduled task state",
        database="ops",
        apply="""
        CREATE TABLE IF NOT EXISTS task_runs (
            name             TEXT    PRIMARY KEY,

            -- When this task should next run. The whole point of persisting it:
            -- a restart has to be able to tell "missed while down" from
            -- "already done", and memory cannot.
            next_due_at      TEXT,

            last_started_at  TEXT,
            last_finished_at TEXT,
            -- ok | failed | running
            last_status      TEXT,
            last_error       TEXT,
            last_elapsed_ms  INTEGER,
            last_trace_id    TEXT,

            runs             INTEGER NOT NULL DEFAULT 0,
            failures         INTEGER NOT NULL DEFAULT 0,
            updated_at       TEXT    NOT NULL
        );
        """,
    ),
]


# --------------------------------------------------------------------------- #
# Declaration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Task:
    """One piece of scheduled work, declared by the module that owns it.

    ``name`` is an identifier, not prose (日志规范), and it is also the stem of
    the two settings this task gets automatically — so it must be stable.
    """

    name: str
    title: str
    run: Callable[[], Any]
    #: Default schedule. ``"04:00"`` daily, or ``"30m"`` / ``"6h"`` periodic.
    #: Only the default: the value actually used comes from the setting below,
    #: because 参数一律配置化.
    schedule: str
    description: str = ""


_tasks: dict[str, Task] = {}
_running: set[str] = set()
_lock = threading.Lock()


def slug(name: str) -> str:
    """Config-key form of a task name: ``generation.daily`` -> ``generation_daily``."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def schedule_key(name: str) -> str:
    return f"task_{slug(name)}_schedule"


def enabled_key(name: str) -> str:
    return f"task_{slug(name)}_enabled"


def register(*tasks: Task) -> None:
    """Declare tasks and their two settings. Called once per module at install."""
    for task in tasks:
        if task.name in _tasks and _tasks[task.name] is not task:
            log.warning(
                "task.duplicate",
                f"任务 {task.name} 被重复登记，后一次覆盖前一次",
                task=task.name,
            )
        parse_schedule(task.schedule)  # fail loudly at registration, not at 4am
        _tasks[task.name] = task
        runtime_config.register(
            runtime_config.ConfigSpec(
                key=enabled_key(task.name),
                default=True,
                value_type="bool",
                title=f"启用「{task.title}」",
                description=task.description or f"定时任务 {task.name}",
                group="tasks",
                order=10,
            ),
            runtime_config.ConfigSpec(
                key=schedule_key(task.name),
                default=task.schedule,
                value_type="str",
                title=f"「{task.title}」的周期",
                description="每天某时写 04:00；每隔一段时间写 30m 或 6h。",
                group="tasks",
                order=11,
            ),
        )
        log.debug("task.registered", f"任务 {task.name} 已登记", task=task.name)


def registered() -> dict[str, Task]:
    return dict(_tasks)


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #

_DAILY = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_EVERY = re.compile(r"^(\d+)\s*([mh])$")


def parse_schedule(text: str) -> tuple[str, int, int]:
    """``("daily", hour, minute)`` or ``("every", minutes, 0)``.

    Raises ``ValueError`` on anything else, which is deliberate: a typo in the
    console should be refused when it is entered, not discovered by a job that
    silently never runs.
    """
    value = (text or "").strip()
    if m := _DAILY.match(value):
        return "daily", int(m.group(1)), int(m.group(2))
    if m := _EVERY.match(value):
        n = int(m.group(1))
        if n <= 0:
            raise ValueError(f"周期必须大于零：{text!r}")
        return "every", n * (60 if m.group(2) == "h" else 1), 0
    raise ValueError(f"看不懂的周期：{text!r}（应为 04:00 或 30m 或 6h）")


def next_due(schedule: str, after: datetime) -> datetime:
    """The first moment strictly after ``after`` when this schedule fires.

    ``after`` and the result are timezone-aware UTC; a daily time is resolved
    against the **local** clock, so "04:00" is 4am where the machine is.
    """
    kind, a, b = parse_schedule(schedule)
    if kind == "every":
        return after + timedelta(minutes=a)

    local = after.astimezone()
    candidate = local.replace(hour=a, minute=b, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment else None


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def state_of(name: str) -> dict[str, Any] | None:
    row = get_connection("ops").execute(
        "SELECT * FROM task_runs WHERE name = ?", (name,)
    ).fetchone()
    return dict(row) if row else None


def _ensure_row(name: str, schedule: str, now: datetime) -> dict[str, Any]:
    """Row for this task, with a due time it can actually be judged against.

    Seeded to the *next* occurrence rather than to now: a module installed at
    10am should not immediately run its 4am job just because it has no history.

    **A row with no due time is repaired, not left alone.** That state is
    reachable and it is fatal: if the schedule cannot be parsed at the moment a
    run finishes, the next due time is written as NULL — and a task with a NULL
    due time is never due, so fixing the schedule afterwards would not bring it
    back. It would simply never run again, silently, forever. Measured
    2026-09-11: the nightly supply task sat dead for twenty minutes this way and
    nothing anywhere said so.
    """
    existing = state_of(name)
    if existing and existing.get("next_due_at"):
        return existing

    conn = get_connection("ops")
    upcoming = _iso(next_due(schedule, now))
    if existing:
        conn.execute(
            "UPDATE task_runs SET next_due_at = ?, updated_at = ? WHERE name = ?",
            (upcoming, _iso(now), name),
        )
        log.warning(
            "task.reseeded",
            f"任务 {name} 没有下次运行时间，已重排到 {upcoming}——"
            f"没有这个时间的任务永远不会再跑，而且不会报错",
            task=name, next_due_at=upcoming,
        )
    else:
        conn.execute(
            "INSERT OR IGNORE INTO task_runs (name, next_due_at, updated_at)"
            " VALUES (?,?,?)",
            (name, upcoming, _iso(now)),
        )
    conn.commit()
    return state_of(name) or {"name": name, "next_due_at": upcoming}


def due_tasks(now: datetime | None = None) -> list[str]:
    """Which tasks are due, in declaration order. Pure read — nothing runs."""
    now = now or _now()
    ready: list[str] = []
    for name, task in _tasks.items():
        if not bool(runtime_config.get(enabled_key(name))):
            continue
        try:
            schedule = str(runtime_config.get(schedule_key(name)))
            parse_schedule(schedule)
        except ValueError:
            log.error(
                "task.schedule.invalid",
                f"任务 {name} 的周期设置看不懂，这个任务不会运行",
                task=name,
                schedule=runtime_config.get(schedule_key(name)),
            )
            continue
        row = _ensure_row(name, schedule, now)
        due = _parse(row.get("next_due_at"))
        if due is not None and due <= now:
            ready.append(name)
    return ready


def status() -> list[dict[str, Any]]:
    """Everything the admin page shows, assembled from what is installed."""
    out = []
    for name, task in _tasks.items():
        try:
            schedule = str(runtime_config.get(schedule_key(name)))
        except Exception:  # noqa: BLE001
            schedule = task.schedule
        row = state_of(name) or {}
        out.append({
            "name": name,
            "title": task.title,
            "description": task.description,
            "schedule": schedule,
            "enabled": bool(runtime_config.get(enabled_key(name))),
            "running": name in _running,
            "next_due_at": row.get("next_due_at"),
            "last_started_at": row.get("last_started_at"),
            "last_finished_at": row.get("last_finished_at"),
            "last_status": row.get("last_status"),
            "last_error": row.get("last_error"),
            "last_elapsed_ms": row.get("last_elapsed_ms"),
            "last_trace_id": row.get("last_trace_id"),
            "runs": row.get("runs") or 0,
            "failures": row.get("failures") or 0,
        })
    return out


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #

def run_now(name: str) -> dict[str, Any]:
    """Run one task in this thread, whatever its schedule says.

    Used by the console's 手动触发 button and by the acceptance script. The
    schedule is still advanced afterwards, so a manual run at 3am does not leave
    the 4am run pending an hour later.
    """
    task = _tasks.get(name)
    if task is None:
        raise KeyError(f"没有这个任务：{name}")

    with _lock:
        if name in _running:
            return {"name": name, "status": "running", "note": "已经在跑了，这次跳过"}
        _running.add(name)

    conn = get_connection("ops")
    started = _now()
    try:
        schedule = str(runtime_config.get(schedule_key(name)))
    except Exception:  # noqa: BLE001
        schedule = task.schedule

    _ensure_row(name, schedule, started)
    conn.execute(
        "UPDATE task_runs SET last_started_at = ?, last_status = 'running',"
        " last_error = NULL, updated_at = ? WHERE name = ?",
        (_iso(started), _iso(started), name),
    )
    conn.commit()

    with trace() as trace_id:
        log.info("task.started", f"任务开始：{task.title}", task=name, trace=trace_id)
        status_text, error = "ok", None
        try:
            task.run()
        except Exception as exc:  # noqa: BLE001 - one task must not stop the rest
            status_text, error = "failed", f"{type(exc).__name__}: {exc}"
            # ERROR is what reaches Bark; the task does not need to know that.
            log.exception(
                "task.failed",
                f"任务失败：{task.title}",
                task=name,
                trace=trace_id,
            )
        finished = _now()
        elapsed = int((finished - started).total_seconds() * 1000)

        try:
            upcoming = next_due(schedule, finished)
        except ValueError:
            upcoming = None

        conn.execute(
            "UPDATE task_runs SET last_finished_at = ?, last_status = ?, last_error = ?,"
            " last_elapsed_ms = ?, last_trace_id = ?, next_due_at = ?,"
            " runs = runs + 1, failures = failures + ?, updated_at = ?"
            " WHERE name = ?",
            (_iso(finished), status_text, error, elapsed, trace_id, _iso(upcoming),
             1 if status_text == "failed" else 0, _iso(finished), name),
        )
        conn.commit()

        if status_text == "ok":
            log.info(
                "task.finished",
                f"任务完成：{task.title}（{elapsed / 1000:.1f} 秒）",
                task=name, trace=trace_id, elapsed_ms=elapsed,
                next_due_at=_iso(upcoming),
            )

    with _lock:
        _running.discard(name)

    return {"name": name, "status": status_text, "elapsed_ms": elapsed,
            "error": error, "next_due_at": _iso(upcoming), "trace_id": trace_id}


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #

_loop_task: "asyncio.Task[None] | None" = None


async def _loop() -> None:
    """Wake every tick, run what is due. Blocking work goes to a thread."""
    log.info(
        "task.loop.started",
        f"定时任务调度已启动，每 {TICK_SECONDS} 秒检查一次",
        tasks=sorted(_tasks),
    )
    try:
        while True:
            try:
                for name in due_tasks():
                    await asyncio.to_thread(run_now, name)
            except Exception:  # noqa: BLE001 - the loop outlives any single failure
                log.exception("task.loop.failed", "调度循环这一轮出错，下一轮继续")
            await asyncio.sleep(TICK_SECONDS)
    except asyncio.CancelledError:
        log.info("task.loop.stopped", "定时任务调度已停止")
        raise


def start_loop() -> None:
    """Start the scheduler. Called from the app lifespan, once."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    _loop_task = asyncio.create_task(_loop(), name="core.tasks.loop")


async def stop_loop() -> None:
    global _loop_task
    if _loop_task is None:
        return
    _loop_task.cancel()
    try:
        await _loop_task
    except asyncio.CancelledError:
        pass
    _loop_task = None
