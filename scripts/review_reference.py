"""The review round's state machine, kept as an independent reference.

复习状态机 the review state machine / 参照实现 reference implementation

**Why this file exists in `scripts/` and not in the backend.** P9 drew the line
between the two sides: the server generates and delivers, the device learns.
A round of review — passing 看词想义 unlocks 看义想词, failing the second locks it
again — is learning state that moves with the user's thumb, so it left the
server together with ``session.answer`` (`phase-9.html` §11).

But `export_review_vectors.py` exists so the client's copy is checked against
something *other than itself*: hand-written expectations have no authority, and
two sides both "passing" may only mean both are wrong. Deleting the Python copy
outright would have quietly turned those vectors into a transcript of whatever
`ERCore/Review.swift` happened to do on the day they were exported.

So the rules moved here instead of disappearing — the same treatment
`scripts/fsrs_reference.py` got. **This is not on any request path**: nothing
under `backend/` imports it, and it touches no database.

**It is a transcription, not a rewrite.** Every line below came from the
deleted ``session.answer``; where the two could differ, this one is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

#: 看词想义 — given the word, recall the meaning.
WORD_TO_SENSE = 1
#: 看义想词 — given the meaning, recall the word. Unlocked by passing the first.
SENSE_TO_WORD = 2

#: How far the hints go. **One level as of P7**: 「不确定」 is a single tap, and
#: taking it costs a grade rather than being merely recorded.
MAX_REVEAL = 1

#: The floor a draw weight never goes below. Its purpose is the opposite of what
#: a floor usually does — it keeps a hopeless item *in* the pool.
WEIGHT_FLOOR = 0.0001


@dataclass(frozen=True)
class Round:
    """Where one item stands in today's review."""

    direction: int = WORD_TO_SENSE
    #: Both directions together. **This is the grade** — behaviour, not a
    #: self-rated difficulty.
    asks: int = 0
    misses: int = 0
    weight: float = 1.0
    done: bool = False
    #: "That was trivial." Claimed by the learner, honoured only on a round with
    #: no misses.
    easy: bool = False
    #: The most help taken on this item today, not the most recent: a hint on
    #: the first direction still counted, even if the second went unaided.
    revealed: int = 0


def answer(round_: Round, *, passed: bool, revealed: int = 0, easy: bool = False,
           weight_decay: float) -> Round:
    """Apply one answer. Pure — no database, no clock, no configuration.

    ``weight_decay`` is a parameter rather than a constant for the same reason
    the client takes it from the payload: it is a tuning knob the server owns.
    """
    if round_.done:
        raise ValueError("这个词今天已经过了")

    asks = round_.asks + 1
    misses = round_.misses + (0 if passed else 1)
    revealed_max = max(round_.revealed, max(0, min(MAX_REVEAL, int(revealed))))
    # Claimed, not inferred, and only on a clean round. A miss withdraws an
    # earlier claim: it plainly was not easy.
    claimed_easy = (round_.easy or easy) and misses == 0

    if passed and round_.direction == WORD_TO_SENSE:
        # Unlocked, then put back in the pool rather than asked straight away —
        # the gap is the point of asking the other way round.
        return replace(round_, direction=SENSE_TO_WORD, asks=asks, misses=misses,
                       easy=claimed_easy, revealed=revealed_max)
    if passed and round_.direction == SENSE_TO_WORD:
        return replace(round_, asks=asks, misses=misses, easy=claimed_easy,
                       revealed=revealed_max, done=True)
    # Failing either direction sends the item back to the first and re-locks
    # the second (决定 7).
    weight = max(WEIGHT_FLOOR, round_.weight * weight_decay)
    return replace(round_, direction=WORD_TO_SENSE, asks=asks, misses=misses,
                   easy=False, revealed=revealed_max, weight=weight)
