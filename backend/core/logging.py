"""Structured logging, built around one question: when the user says
"something's off", can the AI find out what happened?

Everything here follows from that. Three consequences worth understanding
before changing anything:

**Event names are stable identifiers, not prose.** ``article.generation.failed``
is filterable and countable; "生成失败了" is not. Human-readable detail goes in
``message`` (written in Chinese, since that is the user's language and models
read it fine); the identifier stays English and stable across releases.

**Technical logs and decision logs are different things.** Requests, exceptions
and timings are disposable operational noise and go to ``logs.db`` on a 30-day
rotation. *Decisions* — why this article, why these review items, why the
ability estimate moved — go to ``learning.db`` and are never pruned, because
"why did it do that three months ago" is a question only they can answer.

**DEBUG is captured but not written.** Recording DEBUG to disk permanently is
wasteful; enabling it only after a problem appears is useless, because by then
the problem is gone. So DEBUG records accumulate in memory per trace and are
flushed to disk only if that same trace produces an ERROR. The first occurrence
of a fault is therefore fully instrumented without the steady-state cost.

Terms: *trace* 追踪链 — one request or one background task, start to finish.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import traceback
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from backend.core.config import get_settings
from backend.core.db import Migration, get_connection

Level = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_LEVEL_ORDER: dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

MIGRATIONS = [
    Migration(
        version=1,
        name="technical log table",
        database="logs",
        apply="""
        CREATE TABLE IF NOT EXISTS logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT    NOT NULL,
            level      TEXT    NOT NULL,
            module     TEXT    NOT NULL,
            event      TEXT    NOT NULL,
            message    TEXT    NOT NULL,
            context    TEXT,
            trace_id   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_logs_ts       ON logs (ts);
        CREATE INDEX IF NOT EXISTS idx_logs_level_ts ON logs (level, ts);
        CREATE INDEX IF NOT EXISTS idx_logs_trace    ON logs (trace_id);
        CREATE INDEX IF NOT EXISTS idx_logs_event    ON logs (event);
        """,
    ),
    Migration(
        version=2,
        name="decision log table",
        database="learning",
        apply="""
        CREATE TABLE IF NOT EXISTS decisions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT    NOT NULL,
            kind       TEXT    NOT NULL,
            summary    TEXT    NOT NULL,
            inputs     TEXT,
            candidates TEXT,
            chosen     TEXT,
            reason     TEXT,
            trace_id   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_decisions_ts   ON decisions (ts);
        CREATE INDEX IF NOT EXISTS idx_decisions_kind ON decisions (kind, ts);
        """,
    ),
]

# --------------------------------------------------------------------------- #
# Trace context
# --------------------------------------------------------------------------- #

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)

# DEBUG records held in memory, keyed by trace. Bounded on both axes so a long
# running process cannot accumulate them without limit.
_MAX_TRACES_BUFFERED = 64
_MAX_RECORDS_PER_TRACE = 200
_debug_buffers: OrderedDict[str, deque[dict[str, Any]]] = OrderedDict()


def current_trace() -> str | None:
    """The trace this code is running inside, if any."""
    return _trace_id.get()


@contextmanager
def trace(trace_id: str | None = None) -> Iterator[str]:
    """Open a trace: one request, one scheduled task, one CLI invocation.

    Every log record emitted inside inherits the id, which is what makes
    "give me everything that happened during that one failure" a single query.
    """
    tid = trace_id or uuid.uuid4().hex[:16]
    token = _trace_id.set(tid)
    try:
        yield tid
    finally:
        _trace_id.reset(token)
        _debug_buffers.pop(tid, None)


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

_SENSITIVE_HINTS = ("secret", "token", "password", "api_key", "apikey", "authorization")


def _redact(value: Any, key: str = "") -> Any:
    """Strip anything that looks like a credential.

    Logs get exported, pasted into chat, and attached to diagnostic bundles.
    A leaked admin secret or LLM API key in any of those is a real incident, so
    redaction happens here — at the single choke point — rather than relying on
    every call site to remember.
    """
    if any(hint in key.lower() for hint in _SENSITIVE_HINTS):
        return "***redacted***"
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def _serialise(context: dict[str, Any]) -> str | None:
    if not context:
        return None
    cleaned = {k: _redact(v, k) for k, v in context.items()}
    try:
        return json.dumps(cleaned, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        # Never let a log record break the operation it was describing.
        return json.dumps({"_unserialisable": str(cleaned)[:2000]}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Alert hooks
# --------------------------------------------------------------------------- #

AlertHandler = Callable[[dict[str, Any]], None]
_alert_handlers: list[AlertHandler] = []


def add_alert_handler(handler: AlertHandler) -> None:
    """Register something to call on ERROR/CRITICAL — Bark push, for instance.

    Kept as a hook rather than a direct import so this module stays free of
    dependencies on runtime configuration and outbound HTTP. Notifications are a
    consumer of logging, not a part of it.
    """
    _alert_handlers.append(handler)


def _fire_alerts(record: dict[str, Any]) -> None:
    for handler in _alert_handlers:
        try:
            handler(record)
        except Exception:  # noqa: BLE001 - an alerting failure must never cascade
            print(
                f"[logging] alert handler failed: {traceback.format_exc()}",
                file=sys.stderr,
            )


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def _write_records(records: list[dict[str, Any]]) -> None:
    """Persist records to logs.db.

    Failures here are printed to stderr and swallowed. A logging subsystem that
    can take down the application is worse than no logging.
    """
    if not records:
        return
    try:
        conn = get_connection("logs")
        conn.executemany(
            "INSERT INTO logs (ts, level, module, event, message, context, trace_id)"
            " VALUES (:ts, :level, :module, :event, :message, :context, :trace_id)",
            records,
        )
        conn.commit()
    except sqlite3.Error:
        print(f"[logging] failed to persist: {traceback.format_exc()}", file=sys.stderr)


def _console(record: dict[str, Any]) -> None:
    """One readable line per record, for when someone is watching the terminal."""
    stream = sys.stderr if _LEVEL_ORDER[record["level"]] >= 40 else sys.stdout
    trace_part = f" [{record['trace_id']}]" if record["trace_id"] else ""
    print(
        f"{record['ts']} {record['level']:<8}{trace_part} "
        f"{record['module']}::{record['event']} — {record['message']}",
        file=stream,
    )


def _emit(
    level: Level,
    module: str,
    event: str,
    message: str,
    context: dict[str, Any],
) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "module": module,
        "event": event,
        "message": message,
        "context": _serialise(context),
        "trace_id": current_trace(),
    }

    threshold = _LEVEL_ORDER.get(get_settings().log_level.upper(), 20)

    if level == "DEBUG" and _LEVEL_ORDER["DEBUG"] < threshold:
        # Buffered, not written. Flushed only if this trace later fails.
        tid = record["trace_id"]
        if tid:
            buffer = _debug_buffers.get(tid)
            if buffer is None:
                buffer = deque(maxlen=_MAX_RECORDS_PER_TRACE)
                _debug_buffers[tid] = buffer
                while len(_debug_buffers) > _MAX_TRACES_BUFFERED:
                    _debug_buffers.popitem(last=False)
            _debug_buffers.move_to_end(tid)
            buffer.append(record)
        return

    if _LEVEL_ORDER[level] < threshold:
        return

    pending = [record]

    if _LEVEL_ORDER[level] >= 40:
        # Something failed. Flush this trace's buffered DEBUG records so the
        # first occurrence is fully instrumented — there may not be a second.
        tid = record["trace_id"]
        if tid and tid in _debug_buffers:
            pending = list(_debug_buffers.pop(tid)) + pending

    _write_records(pending)
    _console(record)

    if _LEVEL_ORDER[level] >= 40:
        _fire_alerts(record)


# --------------------------------------------------------------------------- #
# Public interface
# --------------------------------------------------------------------------- #


class Logger:
    """A logger bound to one module name.

    Usage::

        log = get_logger("vocabulary")
        log.info("dictionary.imported", "词典导入完成", entries=340_000)
        log.error("dictionary.import.failed", "词典导入失败", path=str(p))
    """

    __slots__ = ("module",)

    def __init__(self, module: str) -> None:
        self.module = module

    def debug(self, event: str, message: str, **context: Any) -> None:
        _emit("DEBUG", self.module, event, message, context)

    def info(self, event: str, message: str, **context: Any) -> None:
        _emit("INFO", self.module, event, message, context)

    def warning(self, event: str, message: str, **context: Any) -> None:
        _emit("WARNING", self.module, event, message, context)

    def error(self, event: str, message: str, **context: Any) -> None:
        _emit("ERROR", self.module, event, message, context)

    def critical(self, event: str, message: str, **context: Any) -> None:
        _emit("CRITICAL", self.module, event, message, context)

    def exception(self, event: str, message: str, **context: Any) -> None:
        """Log an ERROR with the current traceback attached."""
        context.setdefault("traceback", traceback.format_exc())
        _emit("ERROR", self.module, event, message, context)


def get_logger(module: str) -> Logger:
    return Logger(module)


def log_decision(
    kind: str,
    summary: str,
    *,
    inputs: dict[str, Any] | None = None,
    candidates: list[Any] | None = None,
    chosen: Any = None,
    reason: str = "",
) -> None:
    """Record an adaptive decision, with the evidence behind it.

    This is the project's distinguishing log. The system silently judges what to
    show the user every day; without a record of *what it knew and why it chose*,
    a complaint like "articles got harder this week" is undiagnosable.

    Written to ``learning.db`` and never pruned — it is closer to study data than
    to operational logging.
    """
    try:
        conn = get_connection("learning")
        conn.execute(
            "INSERT INTO decisions (ts, kind, summary, inputs, candidates, chosen,"
            " reason, trace_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                kind,
                summary,
                _serialise(inputs or {}),
                _serialise({"items": candidates}) if candidates is not None else None,
                _serialise({"value": chosen}) if chosen is not None else None,
                reason,
                current_trace(),
            ),
        )
        conn.commit()
    except sqlite3.Error:
        print(f"[logging] decision not recorded: {traceback.format_exc()}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #


def prune_logs(retention_days: int) -> int:
    """Delete technical logs older than ``retention_days``. Returns rows removed.

    Decision logs are deliberately untouched.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    conn = get_connection("logs")
    cursor = conn.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount
