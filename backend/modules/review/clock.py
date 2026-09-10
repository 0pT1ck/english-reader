"""What "now" means to the review module — and how to fast-forward it.

模拟时钟 simulated clock / 跳到第二天 jump to the next day

Review is scheduled in **days**, so trying it out honestly means waiting days.
That is the one thing guaranteed not to happen: acceptance item M1 asks for
three to five days of real use, and without a way to skip ahead the first
verification of the schedule would come a week after the code was written.

So the module reads its clock from here, and here alone. One offset, stored in
the settings so it survives a restart, added to the real clock:

* every ``due_at`` comparison,
* which day a session belongs to,
* the ``now`` handed to the scheduler,
* the timestamps written into the history.

All of them shift together, which is the whole point — a clock that moved some
of those and not others would produce a state that could never occur in real
use, and then the bug being hunted would be in the test rig.

**It is a development tool and it lies about the date, so it announces itself.**
The review page shows a banner whenever the offset is non-zero, and
``verify_phase3.py`` fails if acceptance is attempted with the clock moved —
passing acceptance in a simulated future would prove nothing about tomorrow.

Scope is deliberately this module. Reading still writes marks at the real time,
which is what makes the simulation faithful: jump forward a day and yesterday's
marks are exactly that, yesterday's.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.core import runtime_config
from backend.core.logging import get_logger

log = get_logger("review.clock")

KEY = "review_clock_offset_days"


def offset_days() -> int:
    try:
        return int(runtime_config.get(KEY) or 0)
    except (TypeError, ValueError):
        return 0


def now() -> datetime:
    """The current time as the review module sees it."""
    return datetime.now(timezone.utc) + timedelta(days=offset_days())


def today() -> str:
    return now().date().isoformat()


def advance(days: int = 1) -> int:
    """Move the simulated clock. Returns the new offset."""
    value = max(0, offset_days() + int(days))
    runtime_config.set(KEY, value)
    log.info("review.clock.advanced", "模拟时钟前进",
             days=int(days), offset_days=value, simulated_now=now().isoformat())
    return value


def reset() -> int:
    runtime_config.set(KEY, 0)
    log.info("review.clock.reset", "模拟时钟已归零")
    return 0


def status() -> dict:
    return {
        "offset_days": offset_days(),
        "simulated_now": now().isoformat(timespec="seconds"),
        "real_now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "simulated": offset_days() != 0,
    }
