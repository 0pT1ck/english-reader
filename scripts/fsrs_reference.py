"""FSRS 的参照实现——**只给导向量用，不在服务端跑**。

**2026-09-18 从 `backend/modules/review/scheduler.py` 移到这里**（P9 §11）。
排期搬到了客户端，所以服务端不再需要它；而**删掉它是另一回事，那件事不该做**：

向量（`client/Tests/ERCoreTests/Fixtures/scheduler-vectors.json`）的权威性
**全部来自「它出自另一份独立实现」**。删掉这份实现，Swift 那边就成了自证的——
而「两份都错得一样」正是这个项目一直担心的失败，P5 §17 专门写过它。
所以它留着，但**移出运行的那棵树**：服务端（跑着的那个应用）没有学习逻辑，
仓库里有一份参照实现，而它唯一的消费者是 `export_scheduler_vectors.py`。

由此 `pyproject.toml` 里的 `fsrs` 依赖也留着。要守的那条断言因此不是
「仓库不依赖 py-fsrs」，而是**「`backend/` 底下没有任何模块 import 它」**
——那才是有意义的那一条（`verify_phase3` 1.10）。

以下是原文，一字未改。

When does this item come back — the only place that decides it.

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

#: Grades worst-first. The two ceilings below both move a rating along this
#: list, but **they move it differently** — which is what a single shared
#: condition hid until the vectors enumerated it (phase-9.html §16).
GRADE_ORDER = (Rating.Again, Rating.Hard, Rating.Good, Rating.Easy)

#: Taking a hint never drops the grade below this. ``Again`` says "you had
#: forgotten it", which is a claim about recall — help taken is not forgetting.
HINT_FLOOR = Rating.Hard

#: The most a word marked today can score on that same day (P7 §3 的原话是
#: 「封顶为 Hard」——**封顶**，不是降档)。
CAPPED_CEILING = Rating.Hard

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


def rating_for(misses: int, *, easy: bool = False, revealed: int = 0,
               capped: bool = False) -> Rating:
    """Grade from how many times the item was missed today.

    ``easy`` is only honoured on a clean round: claiming a word was trivial
    after failing it twice is not a claim about anything.

    ``revealed`` — **taking a hint costs a grade** (P7 决定，2026-09-13). Until
    then the hint was free: reveal the context and the first letter, press
    「认识」, and the scheduler saw ``misses=0`` → ``Good`` and stretched the
    interval exactly as if you had recalled it unaided. FSRS's ``Hard`` already
    means "recalled, but with effort", which is what a hint says.

    ``capped`` — the same ceiling, for a different reason: a word marked
    今天 as 模糊 enters today's pool (P7 推翻 P3 决定 18), but you read it
    minutes ago, so answering it correctly proves nothing. Scheduling it as
    ``Good`` would inflate the first interval for **the most commonly used
    mark there is**. Capping keeps the review without believing it.

    **The two are not the same rule, and until 2026-09-17 the code treated them
    as one.** Written into a single ``revealed > 0 or capped`` condition they
    agree on ``Good`` — both land it on ``Hard`` — so the difference never
    showed. On ``Easy`` they disagree:

    * a hint **costs a grade** (P7 的原话: "taking a hint costs a grade"), so
      ``Easy`` becomes ``Good``;
    * today's mark **caps at Hard** (P7 §3 的原话: 「封顶为 Hard」), so ``Easy``
      becomes ``Hard``.

    And neither reached ``Easy`` at all: the ``easy`` branch returned before
    them, which made the line that lowered ``Easy`` unreachable — its presence
    being the evidence that it was meant to run. The grade enumeration exported
    for Phase 9 is what surfaced it (phase-9.html §16).

    **用户 2026-09-17 定的**: 「太简单了」是学习者的判断，提示不否决它——
    它藏在右上角的二级菜单里，点它的人知道自己在干什么，不存在误触；
    **但看了提示就降一档**。所以 ``Easy`` ＋ 提示 ＝ ``Good``，
    不是 ``Hard``（降一档，不是降两档），也不是 ``Easy``（提示不白拿）。

    Neither ceiling can turn a pass into a lapse: the hint stops at
    :data:`HINT_FLOOR`, and a cap never raises a grade.
    """
    misses = max(0, int(misses))
    rating = (EASY_RATING if easy and misses == 0
              else MISS_RATING.get(misses, WORST_RATING))
    if int(revealed) > 0:
        rating = _one_grade_lower(rating)
    if capped:
        rating = _at_most(rating, CAPPED_CEILING)
    return rating


def _one_grade_lower(rating: Rating) -> Rating:
    """One grade down, never below :data:`HINT_FLOOR`."""
    index = GRADE_ORDER.index(rating)
    floor = GRADE_ORDER.index(HINT_FLOOR)
    return rating if index <= floor else GRADE_ORDER[index - 1]


def _at_most(rating: Rating, ceiling: Rating) -> Rating:
    """The lower of the two — a cap never raises a grade."""
    return (rating if GRADE_ORDER.index(rating) <= GRADE_ORDER.index(ceiling)
            else ceiling)


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
           *, easy: bool = False, revealed: int = 0, capped: bool = False,
           fuzz: bool | None = None) -> Outcome:
    """One item, one day's worth of asking, resolved into the next due date.

    ``misses`` is how many times the day's round was failed. ``now`` is passed
    in rather than read from the clock so that the schedule can be tested
    without waiting.
    """
    rating = rating_for(misses, easy=easy, revealed=revealed, capped=capped)
    card = to_card(state)
    updated, _log = scheduler(fuzz=fuzz).review_card(card, rating, now)

    reps = int((state or {}).get("reps") or 0) + 1
    lapses = int((state or {}).get("lapses") or 0) + (1 if rating is WORST_RATING else 0)
    stored = from_card(updated, reps=reps, lapses=lapses)
    interval = ((updated.due - now).total_seconds() / 86400) if updated.due else 0.0
    return Outcome(state=stored, due_at=updated.due, interval_days=interval, rating=rating)
