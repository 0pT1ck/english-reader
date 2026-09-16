"""Data access for review.

Reads three tables it does not own — ``study_states``, ``study_marks`` and the
article/sentence tables — because they belong to ``reading`` and review is a
consumer of the study record, not a second copy of it. The same shape as
``reading`` reading the sense sets from ``senses``.
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
# Study state — owned by reading, updated here when a round settles
# --------------------------------------------------------------------------- #

def state_of(learner_id: int, item_type: str, item_key: str, sense_id: int) -> dict[str, Any] | None:
    row = get_connection("learning").execute(
        "SELECT * FROM study_states WHERE learner_id=? AND item_type=? AND item_key=?"
        " AND sense_id=?",
        (learner_id, item_type, item_key, sense_id),
    ).fetchone()
    return dict(row) if row else None


def save_state(learner_id: int, item_type: str, item_key: str, sense_id: int,
               fields: dict[str, Any]) -> None:
    """Write back the memory columns. Never touches ``pool``.

    That omission is the P2 invariant: what pool an item sits in is decided by
    the learner's own signal — marking it, and as of 2026-09-11 nothing else —
    and reviewing is not that signal. A review moves the due date, not the pool.
    """
    assert "pool" not in fields, "复习不改变词池位置，那是 P2 的不变量"
    columns = ", ".join(f"{k} = ?" for k in fields)
    get_connection("learning").execute(
        f"UPDATE study_states SET {columns}, updated_at = ?"
        " WHERE learner_id=? AND item_type=? AND item_key=? AND sense_id=?",
        (*fields.values(), now_iso(), learner_id, item_type, item_key, sense_id),
    )
    get_connection("learning").commit()


def due_items(learner_id: int, when: datetime, *, limit: int = 500) -> list[dict[str, Any]]:
    """Items the scheduler says are due, words only.

    Phrases are excluded here and only here — 决定 17 keeps them out of P3, and
    doing it in one query rather than at every call site is what makes the
    acceptance check able to assert "deliberately skipped" rather than
    "happened not to appear".
    """
    rows = get_connection("learning").execute(
        "SELECT * FROM study_states"
        " WHERE learner_id = ? AND item_type = 'word' AND pool = 'reviewing'"
        "   AND due_at IS NOT NULL AND due_at <= ?"
        " ORDER BY due_at LIMIT ?",
        (learner_id, when.isoformat(timespec="seconds"), limit),
    ).fetchall()
    return [dict(r) for r in rows]


def marked_on(learner_id: int, day: str) -> list[dict[str, Any]]:
    """Items marked on this day that are still in the review pool, words only.

    The latest mark per item wins, because a mark can be withdrawn and remade —
    and the withdrawal already moved the item back to ``new`` (the P2 invariant
    fixed during P2y), so the pool check below is what enforces it here.
    """
    rows = get_connection("learning").execute(
        """
        SELECT s.*, (
            SELECT m.kind FROM study_marks m
             WHERE m.learner_id = s.learner_id AND m.item_type = s.item_type
               AND m.item_key = s.item_key AND m.sense_id = s.sense_id
             ORDER BY m.id DESC LIMIT 1
        ) AS mark_kind
        FROM study_states s
        WHERE s.learner_id = ? AND s.item_type = 'word' AND s.pool = 'reviewing'
          AND EXISTS (
              SELECT 1 FROM study_marks m
               WHERE m.learner_id = s.learner_id AND m.item_type = s.item_type
                 AND m.item_key = s.item_key AND m.sense_id = s.sense_id
                 AND substr(m.created_at, 1, 10) = ?
          )
        """,
        (learner_id, day),
    ).fetchall()
    return [dict(r) for r in rows]


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
    conn = get_connection("learning")
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
    rows = get_connection("learning").execute(
        "SELECT article_id FROM reading_progress"
        " WHERE learner_id = ? AND finished_at IS NOT NULL",
        (learner_id,),
    ).fetchall()
    return {int(r[0]) for r in rows}


# --------------------------------------------------------------------------- #
# Sessions and queue
# --------------------------------------------------------------------------- #

def session_for(learner_id: int, day: str) -> dict[str, Any] | None:
    """Today's review session, or None before it is opened.

    Columns named rather than ``SELECT *``: this row is echoed to clients inside
    ``progress``, and ``learner_id`` has no business travelling in a payload —
    identity is derived from the device token. Narrowed on 2026-09-12 while
    declaring the response types, along with the same leak in
    ``reading.repository.progress_of``.
    """
    row = get_connection("learning").execute(
        "SELECT id, day, started_at, finished_at, spelling_at FROM review_sessions"
        " WHERE learner_id = ? AND day = ?",
        (learner_id, day),
    ).fetchone()
    return dict(row) if row else None


def open_session(learner_id: int, day: str) -> dict[str, Any]:
    conn = get_connection("learning")
    conn.execute(
        "INSERT OR IGNORE INTO review_sessions (learner_id, day, started_at)"
        " VALUES (?,?,?)",
        (learner_id, day, now_iso()),
    )
    conn.commit()
    return session_for(learner_id, day)  # type: ignore[return-value]


def enqueue(session_id: int, items: list[dict[str, Any]]) -> int:
    conn = get_connection("learning")
    added = 0
    for item in items:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO review_queue"
            " (session_id, item_type, item_key, sense_id, bucket, capped)"
            " VALUES (?,?,?,?,?,?)",
            (session_id, item["item_type"], item["item_key"], item["sense_id"],
             item["bucket"], int(item.get("capped") or 0)),
        )
        added += cursor.rowcount
    conn.commit()
    return added


def queue_rows(session_id: int, *, open_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM review_queue WHERE session_id = ?"
    if open_only:
        sql += " AND done_at IS NULL"
    rows = get_connection("learning").execute(sql + " ORDER BY id", (session_id,)).fetchall()
    return [dict(r) for r in rows]


def close_open_queue_rows(learner_id: int, *, item_type: str, item_key: str,
                          sense_id: int, now: datetime | None = None) -> int:
    """Close today's unanswered rows for one item. Returns how many.

    Used when a mark is withdrawn: the item leaves the pool, so today's question
    has nothing behind it any more.

    **Only today's, and only the unanswered ones.** A row that was already
    answered is a record of something that happened and stays exactly as it is;
    future sessions are not touched because they will be rebuilt from the pool,
    which no longer contains this item.
    """
    session = session_for(learner_id, today(now))
    if session is None:
        return 0
    conn = get_connection("learning")
    cursor = conn.execute(
        "UPDATE review_queue SET done_at = ? WHERE session_id = ? AND item_type = ?"
        " AND item_key = ? AND sense_id = ? AND done_at IS NULL",
        (now_iso(), session["id"], item_type, item_key, sense_id),
    )
    conn.commit()
    if cursor.rowcount and not queue_rows(session["id"], open_only=True):
        # Withdrawing the last open item finishes the day, same as answering it
        # would have — otherwise the session stays open forever with nothing in
        # it to answer.
        finish_session(session["id"])
    return int(cursor.rowcount)


def update_queue(queue_id: int, **fields: Any) -> None:
    columns = ", ".join(f"{k} = ?" for k in fields)
    conn = get_connection("learning")
    conn.execute(f"UPDATE review_queue SET {columns} WHERE id = ?", (*fields.values(), queue_id))
    conn.commit()


def mark_spelled(session_id: int) -> None:
    """Stamp that the spelling pass happened. Idempotent."""
    conn = get_connection("learning")
    conn.execute(
        "UPDATE review_sessions SET spelling_at = ? WHERE id = ? AND spelling_at IS NULL",
        (now_iso(), session_id),
    )
    conn.commit()


def spelled_keys(session_id: int) -> set[str]:
    """Which words already have an attempt recorded for this session."""
    rows = get_connection("learning").execute(
        "SELECT DISTINCT item_key FROM spelling_attempts WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    return {r["item_key"] for r in rows}


def finish_session(session_id: int) -> None:
    conn = get_connection("learning")
    conn.execute("UPDATE review_sessions SET finished_at = ? WHERE id = ? AND finished_at IS NULL",
                 (now_iso(), session_id))
    conn.commit()


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #

def record_answer(learner_id: int, session_id: int, item: dict[str, Any], *,
                  direction: int, sentence_id: int | None, revealed: int,
                  result: str, rating: int | None = None,
                  interval_days: float | None = None) -> int:
    conn = get_connection("learning")
    cursor = conn.execute(
        "INSERT INTO review_history (learner_id, session_id, item_type, item_key, sense_id,"
        " direction, sentence_id, revealed, result, asked_at, rating, interval_d)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (learner_id, session_id, item["item_type"], item["item_key"], item["sense_id"],
         direction, sentence_id, revealed, result, now_iso(), rating, interval_days),
    )
    conn.commit()
    return int(cursor.lastrowid)


def sentences_used_today(session_id: int) -> set[int]:
    rows = get_connection("learning").execute(
        "SELECT DISTINCT sentence_id FROM review_history"
        " WHERE session_id = ? AND sentence_id IS NOT NULL",
        (session_id,),
    ).fetchall()
    return {int(r[0]) for r in rows}


def record_spelling(learner_id: int, session_id: int | None, item_key: str,
                    expected: str, typed: str, correct: bool) -> None:
    """Write only. Nothing in P3 reads this table — see the schema docstring."""
    conn = get_connection("learning")
    conn.execute(
        "INSERT INTO spelling_attempts (learner_id, session_id, item_key, expected,"
        " typed, correct, created_at) VALUES (?,?,?,?,?,?,?)",
        (learner_id, session_id, item_key, expected, typed, int(correct), now_iso()),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Overview — one row per marked item, everything that decided its schedule
# --------------------------------------------------------------------------- #

def overview(learner_id: int, *, limit: int = 500) -> list[dict[str, Any]]:
    """Every marked item with the whole story behind its next due date.

    Exists because "the schedule looks wrong" is otherwise unfalsifiable: you
    can see that a word is coming back in eleven days but not why. Here the
    reason is on the same row — how many rounds, how many misses, what the last
    round scored, and what the memory state is now.

    Architecture rule 8 puts this on the console rather than in the reading UI:
    the console is the diagnostic channel and is meant to outlive the web
    reader.
    """
    conn = get_connection("learning")
    rows = conn.execute(
        """
        SELECT s.*,
               (SELECT m.kind FROM study_marks m
                 WHERE m.learner_id = s.learner_id AND m.item_type = s.item_type
                   AND m.item_key = s.item_key AND m.sense_id = s.sense_id
                 ORDER BY m.id DESC LIMIT 1)              AS mark_kind,
               (SELECT m.created_at FROM study_marks m
                 WHERE m.learner_id = s.learner_id AND m.item_type = s.item_type
                   AND m.item_key = s.item_key AND m.sense_id = s.sense_id
                 ORDER BY m.id DESC LIMIT 1)              AS marked_at,
               (SELECT h.rating FROM review_history h
                 WHERE h.learner_id = s.learner_id AND h.item_type = s.item_type
                   AND h.item_key = s.item_key AND h.sense_id = s.sense_id
                   AND h.rating IS NOT NULL
                 ORDER BY h.id DESC LIMIT 1)              AS last_rating,
               (SELECT COUNT(*) FROM review_history h
                 WHERE h.learner_id = s.learner_id AND h.item_type = s.item_type
                   AND h.item_key = s.item_key AND h.sense_id = s.sense_id
                   AND h.result = 'fail')                 AS total_misses,
               (SELECT COUNT(*) FROM review_sentences r
                 WHERE r.item_type = s.item_type AND r.item_key = s.item_key
                   AND r.sense_id = s.sense_id)           AS pool_total,
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
         ORDER BY (s.due_at IS NULL), s.due_at, s.item_key
         LIMIT ?
        """,
        (learner_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def sentences_of(item_type: str, item_key: str, sense_id: int) -> list[dict[str, Any]]:
    rows = get_connection("learning").execute(
        "SELECT r.*, a.title AS article_title, a.source AS article_source"
        "  FROM review_sentences r"
        "  LEFT JOIN reading_articles a ON a.id = r.article_id"
        " WHERE r.item_type = ? AND r.item_key = ? AND r.sense_id = ?"
        " ORDER BY r.source, r.id",
        (item_type, item_key, sense_id),
    ).fetchall()
    return [dict(r) for r in rows]
