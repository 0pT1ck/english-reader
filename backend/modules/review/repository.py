"""Data access for review.

Reads tables it does not own — ``study_states``, ``study_marks`` and the
article/sentence tables — because they belong to ``reading`` and review is a
consumer of the study record, not a second copy of it. The same shape as
``reading`` reading the sense sets from ``senses``.

**2026-09-18 (P9 §11): the write half is gone.** Sessions, the day's queue,
answer history and settled memory states were all written from here, and all of
them were learning state changing under the user's thumb. That moved to the
device (`phase-9.html` §4), so what is left is reading: what the learner has
marked, what they have finished, and which sentences exist for a word.

**The tables themselves stay**, with everything already in them. They are the
archive of what happened before the line was drawn, `review_history` still
backs the console's history page, and dropping tables is a data operation this
phase deliberately does not do (§13).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from backend.core.db import get_connection
from backend.modules.review import clock


def now_iso() -> str:
    """Timestamps written by review follow the simulated clock too.

    Otherwise a fast-forwarded session would leave history rows dated today
    while claiming to be tomorrow, and the record would not be replayable.
    """
    return clock.now().isoformat(timespec="seconds")


def today(now: datetime | None = None) -> str:
    return (now or clock.now()).date().isoformat()


# --------------------------------------------------------------------------- #
# What the learner has met — read-only, owned by reading
# --------------------------------------------------------------------------- #

def seen_sentence_ids(learner_id: int) -> set[int]:
    """Sentences the learner has certainly read, one by one.

    **Complements ``finished_article_ids``, and exists because that one is a
    proxy.** "Did you finish the article" stands in for "have you seen this
    sentence", and the two come apart in the most ordinary case there is: you
    mark a word *while reading*, so the sentence it was marked in has been seen
    — but the article may not be finished for hours, or ever.

    Left unhandled, that sentence counts as unseen and can be drawn as a
    question: you get asked with the very sentence you read five minutes ago.
    It looks like an easy question and lands as an inflated grade, and nothing
    anywhere reports it.

    Two sources, because either alone has a gap: ``study_marks`` records where
    each mark happened, and ``study_states.introduced_sentence_id`` remembers
    the first encounter even after a mark is withdrawn.
    """
    conn = get_connection("events")
    ids = {
        int(r[0]) for r in conn.execute(
            "SELECT DISTINCT sentence_id FROM study_marks"
            " WHERE learner_id = ? AND sentence_id IS NOT NULL", (learner_id,))
    }
    ids |= {
        int(r[0]) for r in conn.execute(
            "SELECT DISTINCT introduced_sentence_id FROM study_states"
            " WHERE learner_id = ? AND introduced_sentence_id IS NOT NULL", (learner_id,))
    }
    return ids


def finished_article_ids(learner_id: int) -> set[int]:
    """Articles the learner has read to the end.

    Decides which sentence pool a corpus sentence belongs to: one from an
    article you finished carries the memory of reading it, so it is a hint; one
    from an article you have not read is fair game as a question.
    """
    rows = get_connection("events").execute(
        "SELECT article_id FROM reading_progress"
        " WHERE learner_id = ? AND finished_at IS NOT NULL",
        (learner_id,),
    ).fetchall()
    return {int(r[0]) for r in rows}


# --------------------------------------------------------------------------- #
# Spelling — recorded, never interpreted
# --------------------------------------------------------------------------- #

def record_spelling(learner_id: int, session_id: int | None, item_key: str,
                    expected: str, typed: str, correct: bool) -> None:
    """Write only. Nothing in P3 reads this table — see the schema docstring."""
    conn = get_connection("events")
    conn.execute(
        "INSERT INTO spelling_attempts (learner_id, session_id, item_key, expected,"
        " typed, correct, created_at) VALUES (?,?,?,?,?,?,?)",
        (learner_id, session_id, item_key, expected, typed, int(correct), now_iso()),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Overview — one row per marked item, and what the two sides say about it
# --------------------------------------------------------------------------- #

def overview(learner_id: int, *, limit: int = 500) -> list[dict[str, Any]]:
    """Every marked item, as the server knows it.

    **2026-09-18 (P9 §11): what this row can and cannot say changed.** It used
    to carry the whole story behind a due date — rating, memory state, how many
    misses — because the server computed all of it. It does not any more, and
    the columns that held those numbers have not been written since. Serving
    them would have been the worst kind of wrong: a console that looks fully
    informed while quoting figures frozen on the day of the refactor.

    So the row now carries only what the server still has a claim to:

    * **the mark** — ``study_marks``, applied from the device's events;
    * **where it was met** — ``study_states.introduced_*``, same origin;
    * **how many sentences exist for it** — the factory's own business;
    * **which pool the device says it is in** — the reported snapshot (§7).

    ``pool_archive`` is kept beside ``pool_reported`` on purpose. They are two
    different things — one derived from the events the server has applied, the
    other computed by the device from the log it holds — and putting them side
    by side is what makes a disagreement visible. A single merged column would
    make a device that has not synced for a week look exactly like one that
    synced a minute ago.

    Architecture rule 8 puts this on the console rather than in a learning UI:
    the console is the diagnostic channel, and it outlived the web reader.
    """
    conn = get_connection("events")
    rows = conn.execute(
        """
        SELECT s.learner_id, s.item_type, s.item_key, s.sense_id,
               s.pool                                     AS pool_archive,
               s.introduced_article_id, s.introduced_sentence_id,
               (SELECT p.pool FROM learner_pool p
                 WHERE p.learner_id = s.learner_id AND p.item_type = s.item_type
                   AND p.item_key = s.item_key AND p.sense_id = s.sense_id)
                                                          AS pool_reported,
               (SELECT m.kind FROM study_marks m
                 WHERE m.learner_id = s.learner_id AND m.item_type = s.item_type
                   AND m.item_key = s.item_key AND m.sense_id = s.sense_id
                 ORDER BY m.id DESC LIMIT 1)              AS mark_kind,
               (SELECT m.created_at FROM study_marks m
                 WHERE m.learner_id = s.learner_id AND m.item_type = s.item_type
                   AND m.item_key = s.item_key AND m.sense_id = s.sense_id
                 ORDER BY m.id DESC LIMIT 1)              AS marked_at,
               (SELECT COUNT(*) FROM review_sentences r
                 WHERE r.item_type = s.item_type AND r.item_key = s.item_key
                   AND r.sense_id = s.sense_id)           AS pool_total,
               (SELECT COUNT(*) FROM review_sentences r
                 WHERE r.item_type = s.item_type AND r.item_key = s.item_key
                   AND r.sense_id = s.sense_id
                   AND r.source = 'generated')            AS pool_generated,
               a.title      AS from_title,
               a.source     AS from_source,
               sen.text     AS from_sentence
          FROM study_states s
          LEFT JOIN reading_articles  a   ON a.id   = s.introduced_article_id
          LEFT JOIN reading_sentences sen ON sen.id = s.introduced_sentence_id
         WHERE s.learner_id = ?
           AND EXISTS (SELECT 1 FROM study_marks m
                        WHERE m.learner_id = s.learner_id AND m.item_type = s.item_type
                          AND m.item_key = s.item_key AND m.sense_id = s.sense_id)
         ORDER BY s.item_key, s.sense_id
         LIMIT ?
        """,
        (learner_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def sentences_of(item_type: str, item_key: str, sense_id: int) -> list[dict[str, Any]]:
    rows = get_connection("content").execute(
        "SELECT r.*, a.title AS article_title, a.source AS article_source"
        "  FROM review_sentences r"
        "  LEFT JOIN reading_articles a ON a.id = r.article_id"
        " WHERE r.item_type = ? AND r.item_key = ? AND r.sense_id = ?"
        " ORDER BY r.source, r.id",
        (item_type, item_key, sense_id),
    ).fetchall()
    return [dict(r) for r in rows]
