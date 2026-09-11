"""Batch jobs: run thousands of calls, survive interruption, stop at a budget.

The sense set needs roughly 280 calls, word families a few hundred more, and
the nightly article generation added later is the same shape of work. So this is
built once, as infrastructure, rather than as a script for the sense set.

Four properties are the reason it is not a for-loop:

* **Resumable.** Every unit of work is a row. Stopping the service at item 1,847
  and starting it again resumes at 1,848 — starting over would cost money.
* **Bounded.** A job carries a spend cap and pauses on reaching it, instead of
  discovering the bill afterwards. The cap is checked before each call, so a run
  can overshoot by at most the calls already in flight (concurrency minus one) —
  an exact cap is not possible when a call's cost is only known after it
  returns.
* **Visible.** What is running, how far along, what it has cost, what failed —
  all queryable while it runs.
* **Open.** Workers are registered by other modules. This file knows nothing
  about senses or word families; it knows how to run a list of items.

Workers are registered at import time, the same way modules register everything
else, so adding a new kind of batch work touches nothing here.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.core import events, runtime_config
from backend.core.db import get_connection
from backend.core.errors import InvalidRequest, NotFound
from backend.core.logging import get_logger, trace
from backend.modules.llm import client, parsing, providers
from backend.modules.llm.providers import Provider

log = get_logger("llm.jobs")

# Statuses that mean "not finished, and can be picked up again".
RESUMABLE = ("pending", "paused", "capped", "running")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Worker registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ItemOutcome:
    """What a worker produced for one item.

    ``result`` is stored verbatim for audit — when a batch turns out to have
    produced nonsense, the raw reply is the only way to tell a bad prompt from a
    bad parser.
    """

    result: str = ""
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass(frozen=True)
class Worker:
    """One kind of batch work.

    ``plan`` turns the job's parameters into the list of units; ``run_item``
    processes one unit and is responsible for persisting whatever domain data it
    produces. Keeping persistence inside the worker is what keeps this module
    free of any knowledge about senses, examples or word families.
    """

    kind: str
    title: str
    plan: Callable[[dict[str, Any]], list[tuple[str, dict[str, Any]]]]
    run_item: Callable[[Provider, dict[str, Any], dict[str, Any]], ItemOutcome]
    description: str = ""

    #: Which provider this kind of work wants, when the caller did not name one.
    #: Writing articles and sorting out sense sets need different things from a
    #: model — the same prompt gives 3.64% out-of-syllabus from the fast model
    #: that handles the data and 0.14% from a strong one — so "the default
    #: provider" is the wrong answer for at least one of them.
    default_provider: Callable[[], str | None] | None = None


_workers: dict[str, Worker] = {}


#: Three-state, because "use the model's default" is a real answer and not the
#: same as "force it on". Only the article writer has measured grounds to change
#: it away from the default (决定 19).
THINKING_CHOICES = ("default", "on", "off")


def provider_key(kind: str) -> str:
    return f"llm_provider_{kind}"


def thinking_key(kind: str) -> str:
    return f"llm_thinking_{kind}"


def register_worker(worker: Worker) -> None:
    """Register a worker and the two settings that belong to it.

    Declaring the settings here rather than in each module is what makes 决定 19
    hold for workers written later: adding a worker cannot forget to add its own
    switches, because it does not add them.
    """
    _workers[worker.kind] = worker

    # Seed the setting's default from the hint the worker was written with, so
    # the console shows the provider that is actually going to be used instead
    # of an empty box that silently means something else. Without this there
    # would be two places expressing one decision, and the older one would win
    # invisibly — which is how 坑 §5.1 started.
    try:
        inherited = (worker.default_provider() or "") if worker.default_provider else ""
    except Exception:  # noqa: BLE001 - a hint that cannot be read is just absent
        inherited = ""

    runtime_config.register(
        runtime_config.ConfigSpec(
            key=provider_key(worker.kind),
            default=inherited,
            value_type="str",
            title=f"「{worker.title}」用哪个提供商",
            description="留空＝用默认提供商。写提供商的 id，例如 shuai 或 dsflash。"
                        "写文章与整理数据要的是不同的模型，所以这里按任务分开设。",
            group="models",
            order=10,
        ),
        runtime_config.ConfigSpec(
            key=thinking_key(worker.kind),
            default="default",
            value_type="str",
            title=f"「{worker.title}」的思考开关",
            description="default 跟随模型自己、on 强制开、off 强制关。"
                        "关掉思考只在「写文章」这一处实测过（合格率 5/10 → 8/10、"
                        "每篇 61.6 秒 → 9.1 秒），别处改之前先拿数说话。",
            group="models",
            order=11,
        ),
    )


def configured_provider(kind: str) -> str | None:
    """The provider this kind of work is set to use, if any."""
    try:
        return str(runtime_config.get(provider_key(kind))).strip() or None
    except Exception:  # noqa: BLE001 - an unregistered worker has no setting yet
        return None


def configured_thinking(kind: str) -> bool | None:
    """``True`` / ``False`` to force it, ``None`` to leave the model alone."""
    try:
        value = str(runtime_config.get(thinking_key(kind))).strip().lower()
    except Exception:  # noqa: BLE001
        return None
    if value == "on":
        return True
    if value == "off":
        return False
    return None


def workers() -> list[Worker]:
    return sorted(_workers.values(), key=lambda w: w.title)


def running_entries() -> list["_Running"]:
    """The jobs running right now, so a caller can wait for them.

    Exposed rather than letting callers reach into the private dict: the daily
    supply task has to wait for annotation to finish before judging phrases,
    and waiting on the ``status`` column instead would mean waiting on a value
    the waiter's own work changes (坑 §7.2).
    """
    return list(_running.values())


def get_worker(kind: str) -> Worker:
    worker = _workers.get(kind)
    if worker is None:
        raise InvalidRequest("未知的任务类型", kind=kind)
    return worker


# --------------------------------------------------------------------------- #
# Job lifecycle
# --------------------------------------------------------------------------- #


@dataclass
class _Running:
    thread: threading.Thread
    stop: threading.Event = field(default_factory=threading.Event)


_running: dict[int, _Running] = {}
_registry_lock = threading.Lock()
# SQLite handles concurrency fine, but several worker threads updating the same
# job row is a needless write-lock fight; one short lock is cheaper than retries.
_write_lock = threading.Lock()


def create(
    kind: str,
    *,
    params: dict[str, Any] | None = None,
    provider_id: str | None = None,
    spend_cap: float | None = None,
    title: str | None = None,
) -> int:
    """Plan a job and store its items. Does not start it."""
    worker = get_worker(kind)
    params = params or {}
    # Precedence: what the caller asked for, then this worker's own setting,
    # then the hint the worker was written with, then the default provider.
    if not provider_id:
        provider_id = configured_provider(kind)
    if not provider_id and worker.default_provider is not None:
        provider_id = worker.default_provider()
    provider = providers.get(provider_id) if provider_id else providers.default_provider()

    units = worker.plan(params)
    if not units:
        raise InvalidRequest("这个任务没有需要处理的内容")

    if spend_cap is None:
        spend_cap = float(runtime_config.get("llm_spend_cap"))

    conn = get_connection("learning")
    cursor = conn.execute(
        "INSERT INTO llm_jobs (kind, title, provider_id, status, params, total,"
        " spend_cap, created_at, updated_at)"
        " VALUES (?,?,?,'pending',?,?,?,?,?)",
        (
            kind,
            title or worker.title,
            provider.id,
            json.dumps(params, ensure_ascii=False),
            len(units),
            spend_cap,
            _now(),
            _now(),
        ),
    )
    job_id = int(cursor.lastrowid or 0)
    conn.executemany(
        "INSERT INTO llm_job_items (job_id, seq, key, payload) VALUES (?,?,?,?)",
        [
            (job_id, seq, key, json.dumps(payload, ensure_ascii=False))
            for seq, (key, payload) in enumerate(units)
        ],
    )
    conn.commit()

    log.info(
        "llm.job.created",
        f"新建批次任务「{title or worker.title}」，共 {len(units)} 项",
        job_id=job_id,
        kind=kind,
        provider=provider.id,
        total=len(units),
        spend_cap=spend_cap,
    )
    return job_id


def get(job_id: int) -> dict[str, Any]:
    row = get_connection("learning").execute(
        "SELECT * FROM llm_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise NotFound("找不到这个批次任务")
    job = dict(row)
    try:
        job["params"] = json.loads(job["params"]) if job["params"] else {}
    except json.JSONDecodeError:
        job["params"] = {}
    job["live"] = job_id in _running
    return job


def listing(limit: int = 50) -> list[dict[str, Any]]:
    rows = get_connection("learning").execute(
        "SELECT id, kind, title, provider_id, status, total, done, failed,"
        " tokens_in, tokens_out, cost, spend_cap, created_at, updated_at, finished_at"
        " FROM llm_jobs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [{**dict(r), "live": r["id"] in _running} for r in rows]


def items(job_id: int, *, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    clause = "AND status = ?" if status else ""
    params: tuple[Any, ...] = (job_id, status, limit) if status else (job_id, limit)
    rows = get_connection("learning").execute(
        # The reply is truncated rather than omitted: on a failed item it is the
        # evidence, and the whole point of storing it is being able to read it
        # from the console instead of reproducing the call by hand.
        f"SELECT seq, key, status, tokens_in, tokens_out, cost, attempts, error,"
        f" substr(result, 1, 600) AS reply, updated_at"
        f" FROM llm_job_items WHERE job_id = ? {clause}"
        f" ORDER BY seq LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _set_status(job_id: int, status: str, **extra: Any) -> None:
    assignments = ["status = ?", "updated_at = ?"]
    values: list[Any] = [status, _now()]
    for key, value in extra.items():
        assignments.append(f"{key} = ?")
        values.append(value)
    values.append(job_id)
    with _write_lock:
        conn = get_connection("learning")
        conn.execute(f"UPDATE llm_jobs SET {', '.join(assignments)} WHERE id = ?", values)
        conn.commit()


def start(job_id: int) -> dict[str, Any]:
    """Run, or resume, a job on a background thread."""
    job = get(job_id)
    if job["status"] == "done":
        raise InvalidRequest("这个任务已经完成")

    with _registry_lock:
        if job_id in _running:
            return get(job_id)
        entry = _Running(thread=threading.Thread(
            target=_run, args=(job_id,), name=f"llm-job-{job_id}", daemon=True
        ))
        _running[job_id] = entry
        entry.thread.start()

    return get(job_id)


def set_spend_cap(job_id: int, spend_cap: float | None) -> dict[str, Any]:
    """Raise (or lift) a job's budget.

    Without this a capped job is a dead end: it pauses because it reached the
    limit, and pressing continue re-reaches it immediately. Raising the cap is
    the whole point of stopping at one — the job asks before spending more,
    rather than deciding for you in either direction.
    """
    get(job_id)  # 404 if it does not exist
    with _write_lock:
        conn = get_connection("learning")
        conn.execute(
            "UPDATE llm_jobs SET spend_cap = ?, updated_at = ?,"
            " error = CASE WHEN status = 'capped' THEN NULL ELSE error END,"
            " status = CASE WHEN status = 'capped' THEN 'paused' ELSE status END"
            " WHERE id = ?",
            (spend_cap, _now(), job_id),
        )
        conn.commit()
    log.info(
        "llm.job.cap.changed",
        f"批次任务 {job_id} 的花费上限改为 {spend_cap}",
        job_id=job_id, spend_cap=spend_cap,
    )
    return get(job_id)


def pause(job_id: int) -> dict[str, Any]:
    """Ask a running job to stop after the calls in flight finish."""
    with _registry_lock:
        entry = _running.get(job_id)
    if entry is None:
        _set_status(job_id, "paused")
        return get(job_id)
    entry.stop.set()
    return get(job_id)


def delete(job_id: int) -> bool:
    pause(job_id)
    conn = get_connection("learning")
    conn.execute("DELETE FROM llm_job_items WHERE job_id = ?", (job_id,))
    cursor = conn.execute("DELETE FROM llm_jobs WHERE id = ?", (job_id,))
    conn.commit()
    return bool(cursor.rowcount)


#: A job updates its row after every item, so a row that has not moved for this
#: long is not being worked on by anybody.
STALE_MINUTES = 5


def recover_interrupted() -> int:
    """Mark jobs that were running when the process died as paused.

    Called at startup. Without it a killed service leaves jobs looking active
    forever, and the console offers no way to restart them.

    Only *stale* rows are touched. This runs whenever anything imports the
    application — a maintenance script, a one-off query — and a plain "reset
    everything that says running" would then declare a job dead while another
    process is happily working through it, which is exactly what happened the
    first time a long job was run alongside a diagnostic script.
    """
    # The cutoff is built in Python, not by SQLite's datetime(). Both describe
    # the same instant, but `updated_at` is stored as ISO-8601 ("…T13:14:15+00:00")
    # while datetime('now') yields "… 13:14:15" with a space — and 'T' sorts
    # after ' ', so a same-day comparison between the two is always false.
    # Measured 2026-09-11: nothing interrupted today was ever recovered, only
    # things left over from a previous date, and the symptom was an article
    # stuck at `annotating` for good with nothing logged. 坑 §6.1.
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)
    ).isoformat(timespec="seconds")
    conn = get_connection("learning")
    cursor = conn.execute(
        "UPDATE llm_jobs SET status = 'paused', updated_at = ?"
        " WHERE status = 'running' AND updated_at < ?",
        (_now(), cutoff),
    )
    conn.commit()
    if cursor.rowcount:
        log.warning(
            "llm.job.recovered",
            f"有 {cursor.rowcount} 个批次任务在上次退出时仍在运行，已标记为暂停，可继续",
            jobs=cursor.rowcount,
        )
    return cursor.rowcount


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #


def _pending_items(job_id: int) -> list[dict[str, Any]]:
    """Items still to do — this is what makes a restart a resume.

    Failed items are included: a batch that failed on a rate limit should
    continue where it stopped once the limit clears.
    """
    rows = get_connection("learning").execute(
        "SELECT seq, key, payload, attempts FROM llm_job_items"
        " WHERE job_id = ? AND status != 'done' ORDER BY seq",
        (job_id,),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item["payload"]) if item["payload"] else {}
        except json.JSONDecodeError:
            item["payload"] = {}
        out.append(item)
    return out


def _record_item(job_id: int, seq: int, *, status: str, outcome: ItemOutcome | None,
                 cost: float, error: str | None, attempts: int,
                 raw: str | None = None) -> None:
    with _write_lock:
        conn = get_connection("learning")
        conn.execute(
            "UPDATE llm_job_items SET status = ?, result = ?, tokens_in = ?,"
            " tokens_out = ?, cost = ?, attempts = ?, error = ?, updated_at = ?"
            " WHERE job_id = ? AND seq = ?",
            (
                status,
                # On failure the raw reply is what makes the failure
                # diagnosable — "no JSON found" without the text that contained
                # no JSON forces the call to be reproduced by hand.
                (outcome.result if outcome else raw),
                (outcome.tokens_in if outcome else 0),
                (outcome.tokens_out if outcome else 0),
                cost,
                attempts,
                error,
                _now(),
                job_id,
                seq,
            ),
        )
        # Recomputed from the items rather than incremented. Incrementing drifts
        # as soon as a failed item is retried successfully — it would have been
        # counted once as a failure and again as a success — and a cost total
        # that quietly overstates itself is worse than no total at all. The
        # (job_id, status) index makes this cheap.
        conn.execute(
            "UPDATE llm_jobs SET"
            " done   = (SELECT COUNT(*) FROM llm_job_items"
            "           WHERE job_id = :id AND status = 'done'),"
            " failed = (SELECT COUNT(*) FROM llm_job_items"
            "           WHERE job_id = :id AND status = 'failed'),"
            " tokens_in  = (SELECT COALESCE(SUM(tokens_in), 0) FROM llm_job_items"
            "               WHERE job_id = :id),"
            " tokens_out = (SELECT COALESCE(SUM(tokens_out), 0) FROM llm_job_items"
            "               WHERE job_id = :id),"
            " cost = (SELECT COALESCE(SUM(cost), 0) FROM llm_job_items"
            "         WHERE job_id = :id),"
            " updated_at = :now"
            " WHERE id = :id",
            {"id": job_id, "now": _now()},
        )
        conn.commit()


def _spent(job_id: int) -> float:
    row = get_connection("learning").execute(
        "SELECT cost FROM llm_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return float(row["cost"]) if row else 0.0


def _run(job_id: int) -> None:
    """Body of the worker thread. Never raises out of here."""
    entry = _running.get(job_id)
    stop = entry.stop if entry else threading.Event()

    with trace() as tid:
        try:
            job = get(job_id)
            worker = get_worker(job["kind"])
            provider = providers.get(job["provider_id"])
            pending = _pending_items(job_id)

            _set_status(job_id, "running", error=None)
            log.info(
                "llm.job.started",
                f"批次任务 {job_id} 开始运行，剩余 {len(pending)} 项",
                job_id=job_id,
                trace=tid,
                remaining=len(pending),
                provider=provider.id,
            )

            cap = job["spend_cap"]
            concurrency = max(1, int(runtime_config.get("llm_concurrency")))
            attempts_allowed = max(1, int(runtime_config.get("llm_max_attempts")))
            capped = False

            def process(item: dict[str, Any]) -> None:
                nonlocal capped
                if stop.is_set() or capped:
                    return
                if cap and _spent(job_id) >= cap:
                    capped = True
                    return

                attempts = int(item["attempts"]) + 1
                try:
                    # Applied here, in the thread that will make the call, so
                    # every completion inside run_item inherits it without the
                    # worker having to know the setting exists.
                    with client.thinking(configured_thinking(worker.kind)):
                        outcome = worker.run_item(provider, item["payload"], job["params"])
                except client.LLMError as exc:
                    _record_item(job_id, item["seq"], status="failed", outcome=None,
                                 cost=0.0, error=str(exc), attempts=attempts)
                except parsing.ItemFailed as exc:
                    _record_item(job_id, item["seq"], status="failed", outcome=None,
                                 cost=0.0, error=str(exc), attempts=attempts, raw=exc.raw)
                except Exception as exc:  # noqa: BLE001 - one bad item must not kill the batch
                    log.exception(
                        "llm.job.item.failed",
                        f"任务 {job_id} 的第 {item['seq']} 项处理失败",
                        job_id=job_id,
                        seq=item["seq"],
                        key=item["key"],
                    )
                    _record_item(job_id, item["seq"], status="failed", outcome=None,
                                 cost=0.0, error=f"{type(exc).__name__}: {exc}",
                                 attempts=attempts)
                else:
                    cost = provider.cost_of(outcome.tokens_in, outcome.tokens_out)
                    _record_item(job_id, item["seq"], status="done", outcome=outcome,
                                 cost=cost, error=None, attempts=attempts)

            # The client already retries a single call; `attempts_allowed` is the
            # coarser outer limit that stops a permanently broken item from being
            # retried on every resume forever.
            runnable = [i for i in pending if int(i["attempts"]) < attempts_allowed]

            # Each call already carries its own timeout, so the pool needs none:
            # every task terminates, and leaving the block waits for them.
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                list(pool.map(process, runnable))

            remaining = get_connection("learning").execute(
                "SELECT COUNT(*) AS n FROM llm_job_items"
                " WHERE job_id = ? AND status != 'done'",
                (job_id,),
            ).fetchone()["n"]

            if capped:
                _set_status(job_id, "capped",
                            error=f"已达到花费上限 {cap}，任务暂停。提高上限后可继续。")
                log.warning(
                    "llm.job.capped",
                    f"批次任务 {job_id} 达到花费上限 {cap}，已暂停",
                    job_id=job_id, cap=cap, spent=round(_spent(job_id), 4),
                )
            elif stop.is_set():
                _set_status(job_id, "paused")
                log.info("llm.job.paused", f"批次任务 {job_id} 已暂停", job_id=job_id)
            elif remaining == 0:
                _set_status(job_id, "done", finished_at=_now(), error=None)
                log.info(
                    "llm.job.finished",
                    f"批次任务 {job_id} 全部完成，花费 {_spent(job_id):.4f}",
                    job_id=job_id, cost=round(_spent(job_id), 4),
                )
            else:
                _set_status(job_id, "paused",
                            error=f"还有 {remaining} 项未完成（多为失败重试次数用尽）")

            events.emit(
                "llm.job.finished",
                job_id=job_id,
                kind=job["kind"],
                status=get(job_id)["status"],
                cost=_spent(job_id),
            )
        except Exception as exc:  # noqa: BLE001 - the thread must not die silently
            log.exception("llm.job.crashed", f"批次任务 {job_id} 异常终止", job_id=job_id)
            _set_status(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            with _registry_lock:
                _running.pop(job_id, None)


def summary() -> dict[str, Any]:
    """Totals for the console header — and for answering 'what has this cost?'."""
    row = get_connection("learning").execute(
        "SELECT COUNT(*) AS jobs, COALESCE(SUM(cost), 0) AS cost,"
        " COALESCE(SUM(tokens_in), 0) AS tokens_in,"
        " COALESCE(SUM(tokens_out), 0) AS tokens_out FROM llm_jobs"
    ).fetchone()
    return {
        "jobs": row["jobs"],
        "cost": round(row["cost"], 4),
        "tokens_in": row["tokens_in"],
        "tokens_out": row["tokens_out"],
        "running": sorted(_running),
    }


def iter_results(job_id: int) -> Iterable[tuple[str, str]]:
    """(key, raw reply) for every finished item — used when re-parsing a batch."""
    rows = get_connection("learning").execute(
        "SELECT key, result FROM llm_job_items"
        " WHERE job_id = ? AND status = 'done' ORDER BY seq",
        (job_id,),
    ).fetchall()
    for row in rows:
        yield row["key"], row["result"] or ""
