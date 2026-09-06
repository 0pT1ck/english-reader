"""In-process event bus.

The point of this module is stated in architecture rule 6: **new features
should attach by subscribing, not by editing existing flows.**

Most features in this project are shaped like "when X happens, also do Y".
Reading finishes → update the ability estimate, update sense states, and later
(P8) update streak counters and weekly reports. Without a bus, each of those
additions means editing the reading flow, and that file slowly becomes the
place where every feature has a finger. With one, the reading flow only ever
announces what happened.

Handlers are synchronous by design. Everything they realistically do here is
SQLite work, which is synchronous anyway; a handler that needs to make a network
call should hand off to a thread itself rather than force the whole bus to be
async. Keeping it synchronous also means an event emitted inside a database
transaction behaves predictably.

Event names use the same dotted, stable-identifier convention as log events —
``article.ingested``, ``reading.finished`` — for the same reason: they are
matched by code and filtered by humans, so they must not drift.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.core.logging import get_logger

log = get_logger("core.events")

Handler = Callable[["Event"], None]


@dataclass(frozen=True)
class Event:
    """What happened, and everything a subscriber might need to react to it.

    ``payload`` is intentionally a plain dict rather than a typed object per
    event: subscribers are written after the emitter, often in a later phase,
    and a loose payload lets a new subscriber read a field the original author
    never anticipated needing.
    """

    name: str
    payload: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


_subscribers: dict[str, list[tuple[int, str, Handler]]] = defaultdict(list)


def subscribe(event_name: str, handler: Handler, *, owner: str = "?", priority: int = 100) -> None:
    """Attach a handler to an event.

    ``owner`` is the module name, recorded so that a failing handler can be
    identified in the logs without a stack trace archaeology session.

    ``priority`` orders handlers when order genuinely matters — lower runs
    first. Prefer not to rely on it: handlers that depend on each other's
    ordering are a sign the work belongs in one handler.
    """
    _subscribers[event_name].append((priority, owner, handler))
    _subscribers[event_name].sort(key=lambda item: item[0])


def emit(event_name: str, **payload: Any) -> None:
    """Announce that something happened.

    Every subscriber runs even if an earlier one raised. A failing subscriber is
    logged as an ERROR (which also triggers alerting and DEBUG flush) but never
    propagates: the feature that emitted the event did its job, and one broken
    listener must not undo it. This matters most for the reading flow — a bug in
    a statistics subscriber should never lose the record that you read an
    article.
    """
    event = Event(name=event_name, payload=payload)
    handlers = _subscribers.get(event_name, [])

    if not handlers:
        # `event` is the logger's own first parameter, so the context key has to
        # be something else — passing event= here raised TypeError and took down
        # whatever emitted an event nobody had subscribed to yet.
        log.debug("event.unhandled", f"事件 {event_name} 没有订阅者", emitted=event_name)
        return

    for _priority, owner, handler in handlers:
        try:
            handler(event)
        except Exception:  # noqa: BLE001 - isolation is the whole point
            log.exception(
                "event.handler.failed",
                f"事件 {event_name} 的订阅者执行失败，其余订阅者继续",
                event=event_name,
                owner=owner,
                handler=getattr(handler, "__qualname__", repr(handler)),
            )


def subscriber_summary() -> dict[str, list[str]]:
    """Which modules listen to what — shown on the admin status page.

    Useful when a feature silently stops working: the first question is whether
    its subscriber is actually registered.
    """
    return {
        name: [f"{owner}:{getattr(h, '__qualname__', repr(h))}" for _p, owner, h in handlers]
        for name, handlers in sorted(_subscribers.items())
    }


def clear_subscribers() -> None:
    """Drop all subscriptions. Only for tests and script entry points."""
    _subscribers.clear()
