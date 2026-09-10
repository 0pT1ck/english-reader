"""When does this item come back — the only place that decides it.

调度 scheduling / 记忆状态 memory state / 评分 grade

Wraps FSRS (`py-fsrs`, MIT) and nothing else. **A pure function**: it reads a
stored state and a grade, returns a new state and a due time. No database, no
network, no clock of its own — the caller passes ``now``, which is what lets the
acceptance suite inject a time and check three days of schedule without waiting
three days.

**Two configuration choices are load-bearing, and both are deliberate.**

``learning_steps=()`` — FSRS ships with its own same-day repetition (show again
in 1 minute, then 10). We already do same-day repetition, through the weighted
pool in :mod:`.session`, and running both would count each day twice. The rule
that follows and must not be broken: **one day is one review**, and the grade
says how many times it took. Measured consequence: with the steps off, a card
leaves ``Learning`` on its first review and lands two days out rather than ten
minutes out.

``relearning_steps=()`` — same reason, for lapses. A side effect worth knowing:
``State.Relearning`` then never occurs, so a stored ``fsrs_state`` is only ever
Learning or Review.

``enable_fuzzing`` stays **on** in normal use. It scatters intervals by a few
percent, which is how Anki stops a batch of cards learned on the same day from
coming back on the same day forever — and 主文档 §M records that pile-up as an
open problem for us too. Fuzzing does not solve it, but it is free and it helps.
The acceptance suite turns it off so runs are reproducible.

Grade mapping, from 决定 11 as revised on 2026-09-09. The signal is **how many
times the round was missed today** — not how many questions were asked, which
was the first implementation and was wrong: a round is two questions, so a
flawless one counted as 2 and a single stumble as 4.

    没失误 → Good（默认）      失误一次 → Hard
    失误两次及以上 → Again      Easy 只在你自己说「太简单了」时才给

**What is deliberately *not* here:** any weighting of "how many rounds this item
has taken over its lifetime". FSRS's difficulty already accumulates exactly
that, and adding our own would count it twice. 决定 11 says so in as many words.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fsrs import Card, Rating, Scheduler, State

from backend.core import runtime_config

#: Misses in the day → rating. **Misses, not questions asked.** A round is two
#: questions by construction (both directions), so counting questions made a
#: flawless round score 2 and a single stumble score 4 — which graded the
#: stumble as Again, the worst there is, and made Easy unreachable.
MISS_RATING = {0: Rating.Good, 1: Rating.Hard}
WORST_RATING = Rating.Again

#: Easy is **claimed by the learner, never inferred.** FSRS separates "I got it"
#: (Good) from "that was trivial" (Easy), and telling them apart needs a measure
#: of effort — how fast, how sure — that this project does not collect. Mapping
#: every clean pass to Easy is what made intervals run 8 → 66 → 180 days: it
#: says "trivial" every time the learner merely succeeds. So the default is
#: Good, and there is a button for the person who actually knows.
EASY_RATING = Rating.Easy

#: Field names as stored. Named for what they mean, not for FSRS — swapping
#: schedulers must not need a migration. See the migration note in
#: ``reading/schema.py``.
STATE_FIELDS = (
    "stability", "difficulty", "due_at", "last_review_at",
    "reps", "lapses", "fsrs_state", "fsrs_step",
)


@dataclass(frozen=True)
class Outcome:
    """What one review did to one item."""

    state: dict[str, Any]
    due_at: datetime
    interval_days: float
    rating: Rating


def _parameters() -> list[float] | None:
    raw = runtime_config.get("fsrs_parameters")
    if not raw:
        return None
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if isinstance(raw, list) and raw:
        return [float(x) for x in raw]
    return None


def scheduler(*, fuzz: bool | None = None) -> Scheduler:
    """The configured scheduler.

    Built per call rather than cached: it is cheap, and a cached one would go on
    using the old parameters after they are changed in the console.
    """
    kwargs: dict[str, Any] = {
        "learning_steps": (),
        "relearning_steps": (),
        "enable_fuzzing": bool(runtime_config.get("fsrs_fuzz")) if fuzz is None else fuzz,
        "desired_retention": float(runtime_config.get("fsrs_desired_retention")),
        # FSRS defaults this to 36500 days, which is the right answer for
        # "remember it for life" and the wrong one here: this project aims at an
        # exam with a date on it, and an interval that lands after the exam is
        # not a review. Measured before capping: the third review of a word came
        # due 397 days out.
        "maximum_interval": int(runtime_config.get("fsrs_maximum_interval")),
    }
    params = _parameters()
    if params:
        kwargs["parameters"] = params
    return Scheduler(**kwargs)


def rating_for(misses: int, *, easy: bool = False) -> Rating:
    """Grade from how many times the item was missed today.

    ``easy`` is only honoured on a clean round: claiming a word was trivial
    after failing it twice is not a claim about anything.
    """
    misses = max(0, int(misses))
    if easy and misses == 0:
        return EASY_RATING
    return MISS_RATING.get(misses, WORST_RATING)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def to_card(state: dict[str, Any] | None) -> Card:
    """Rebuild the scheduler's card from stored columns.

    A row with no memory state yet — every item marked before P3 — becomes a
    fresh card, which is the right answer: it has never been reviewed.
    """
    if not state or state.get("stability") is None:
        return Card()
    return Card(
        state=State(int(state.get("fsrs_state") or State.Review.value)),
        step=state.get("fsrs_step"),
        stability=float(state["stability"]),
        difficulty=float(state["difficulty"]),
        due=_parse(state.get("due_at")),
        last_review=_parse(state.get("last_review_at")),
    )


def from_card(card: Card, *, reps: int, lapses: int) -> dict[str, Any]:
    """Stored columns from the scheduler's card."""
    return {
        "stability": card.stability,
        "difficulty": card.difficulty,
        "due_at": card.due.isoformat(timespec="seconds") if card.due else None,
        "last_review_at": (card.last_review.isoformat(timespec="seconds")
                           if card.last_review else None),
        "reps": reps,
        "lapses": lapses,
        "fsrs_state": int(card.state.value),
        "fsrs_step": card.step,
    }


def review(state: dict[str, Any] | None, misses: int, now: datetime,
           *, easy: bool = False, fuzz: bool | None = None) -> Outcome:
    """One item, one day's worth of asking, resolved into the next due date.

    ``misses`` is how many times the day's round was failed. ``now`` is passed
    in rather than read from the clock so that the schedule can be tested
    without waiting.
    """
    rating = rating_for(misses, easy=easy)
    card = to_card(state)
    updated, _log = scheduler(fuzz=fuzz).review_card(card, rating, now)

    reps = int((state or {}).get("reps") or 0) + 1
    lapses = int((state or {}).get("lapses") or 0) + (1 if rating is WORST_RATING else 0)
    stored = from_card(updated, reps=reps, lapses=lapses)
    interval = ((updated.due - now).total_seconds() / 86400) if updated.due else 0.0
    return Outcome(state=stored, due_at=updated.due, interval_days=interval, rating=rating)
