"""Data access for the reading loop.

Everything the client surface needs is assembled here so the routes stay thin
and the SQL stays in one place. Reads cross into ``content`` (sense sets, word
families) and ``dict`` (the dictionary) freely — both are attached to the
``learning`` connection, so a join is just a join.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.core import runtime_config
from backend.core.db import get_connection
from backend.core.errors import NotFound
from backend.core.logging import get_logger
from backend.modules.reading import difficulty

log = get_logger("reading.repository")

SOURCES = ("generated", "cet4", "cet6", "kaoyan")
SOURCE_LABELS = {
    "generated": "生成",
    "cet4": "四级真题",
    "cet6": "六级真题",
    "kaoyan": "考研真题",
}

MARK_KINDS = ("unknown", "fuzzy")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Articles
# --------------------------------------------------------------------------- #


def create_article(source: str, source_ref: str, title: str, body: str) -> int:
    """Reserve a row before analysis starts, so a crash leaves a visible failure.

    Returns the existing id when this source has already been ingested — a paper
    must never be ingested twice, because the learner may already have marks
    pointing at the first copy.
    """
    conn = get_connection("learning")
    existing = conn.execute(
        "SELECT id FROM reading_articles WHERE source = ? AND source_ref = ?",
        (source, source_ref),
    ).fetchone()
    if existing:
        return int(existing["id"])

    cursor = conn.execute(
        "INSERT INTO reading_articles (source, source_ref, title, body, status,"
        " prepared_at, created_at) VALUES (?,?,?,?,'pending',?,?)",
        (source, source_ref, title, body, _now(), _now()),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def set_status(article_id: int, status: str, detail: str | None = None) -> None:
    conn = get_connection("learning")
    conn.execute(
        "UPDATE reading_articles SET status = ?, status_detail = ? WHERE id = ?",
        (status, detail, article_id),
    )
    conn.commit()


def store_difficulty(article_id: int, measures: dict[str, Any]) -> None:
    conn = get_connection("learning")
    conn.execute(
        "UPDATE reading_articles SET difficulty = ?, difficulty_score = ?,"
        " word_count = ?, sentence_count = ? WHERE id = ?",
        (
            json.dumps(measures, ensure_ascii=False),
            difficulty.score(measures),
            int(measures.get("word_count", 0)),
            int(measures.get("sentence_count", 0)),
            article_id,
        ),
    )
    conn.commit()


def article_row(article_id: int) -> dict[str, Any]:
    row = get_connection("learning").execute(
        "SELECT * FROM reading_articles WHERE id = ?", (article_id,)
    ).fetchone()
    if row is None:
        raise NotFound("找不到这篇文章")
    item = dict(row)
    item["difficulty"] = _loads(item.get("difficulty"))
    return item


def _loads(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


def fresh_cutoff() -> str:
    days = int(runtime_config.get("reading_fresh_days"))
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def list_articles(
    *,
    learner_id: int = 1,
    shelf: str = "all",
    source: str | None = None,
    sort: str = "composite",
    descending: bool = False,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """The library listing.

    ``shelf`` splits what was prepared recently from the back catalogue. Unread
    articles are never deleted — an article nobody opened today is not waste,
    it is one whose turn has not come — but a year of them would bury the few
    that are new, so the fresh shelf is bounded by ``reading_fresh_days``.
    """
    where = ["a.status = 'ready'"]
    params: list[Any] = []

    if shelf == "fresh":
        where.append("a.prepared_at >= ? AND a.read_at IS NULL")
        params.append(fresh_cutoff())
    elif shelf == "archive":
        where.append("(a.prepared_at < ? OR a.read_at IS NOT NULL)")
        params.append(fresh_cutoff())
    elif shelf == "unread":
        where.append("a.read_at IS NULL")
    elif shelf == "read":
        where.append("a.read_at IS NOT NULL")

    if source:
        where.append("a.source = ?")
        params.append(source)

    # How many words this article was *written to teach*. It is not a promise
    # about the ledger — nothing enters the review queue by being read, only by
    # being marked — it says what the generator aimed at, which is what makes a
    # generated article different from an exam paper (which aimed at nothing and
    # reads 0).
    rows = get_connection("learning").execute(
        "SELECT a.*, p.percent, p.sentence_seq,"
        " (SELECT COUNT(DISTINCT t.headword) FROM reading_tokens t"
        "  WHERE t.article_id = a.id AND t.is_target = 1) AS target_count"
        " FROM reading_articles a"
        " LEFT JOIN reading_progress p ON p.article_id = a.id AND p.learner_id = ?"
        f" WHERE {' AND '.join(where)}",
        [learner_id, *params],
    ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        item["difficulty"] = _loads(item.get("difficulty"))
        item["source_label"] = SOURCE_LABELS.get(item["source"], item["source"])
        items.append(item)

    items.sort(key=lambda i: _sort_key(i, sort), reverse=descending)
    return items[:limit]


def _sort_key(item: dict[str, Any], sort: str) -> Any:
    """Sorting puts unmeasured articles last regardless of direction."""
    if sort == "prepared_at":
        return item.get("prepared_at") or ""
    if sort == "composite":
        value = item.get("difficulty_score")
    else:
        value = item.get("difficulty", {}).get(sort)
    return float("inf") if value is None else float(value)


def pending_articles(limit: int = 50) -> list[dict[str, Any]]:
    rows = get_connection("learning").execute(
        "SELECT id, source, source_ref, title, status, status_detail FROM reading_articles"
        " WHERE status NOT IN ('ready') ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def ingested_refs(source: str) -> set[str]:
    rows = get_connection("learning").execute(
        "SELECT source_ref FROM reading_articles WHERE source = ?", (source,)
    ).fetchall()
    return {r["source_ref"] for r in rows}


def scores_by_source() -> dict[str, list[float]]:
    """Composite scores grouped by exam, for the calibration check."""
    rows = get_connection("learning").execute(
        "SELECT source, difficulty_score FROM reading_articles"
        " WHERE difficulty_score IS NOT NULL"
    ).fetchall()
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(row["source"], []).append(float(row["difficulty_score"]))
    return grouped


def delete_article(article_id: int) -> bool:
    conn = get_connection("learning")
    cursor = conn.execute("DELETE FROM reading_articles WHERE id = ?", (article_id,))
    conn.commit()
    return bool(cursor.rowcount)


# --------------------------------------------------------------------------- #
# Sentences and tokens
# --------------------------------------------------------------------------- #


def store_sentences(article_id: int, sentences: list[dict[str, Any]],
                    tokens_by_sentence: list[list[dict[str, Any]]]) -> None:
    """Write the whole analysed article in one transaction.

    All or nothing: a half-stored article would look ingested while missing
    tokens, and the tap panel would silently show nothing for those words.
    """
    conn = get_connection("learning")
    conn.execute("DELETE FROM reading_sentences WHERE article_id = ?", (article_id,))
    conn.execute("DELETE FROM reading_tokens WHERE article_id = ?", (article_id,))

    for sentence, tokens in zip(sentences, tokens_by_sentence, strict=True):
        cursor = conn.execute(
            "INSERT INTO reading_sentences (article_id, seq, text, char_start, char_end)"
            " VALUES (?,?,?,?,?)",
            (article_id, sentence["seq"], sentence["text"],
             sentence["char_start"], sentence["char_end"]),
        )
        sentence_id = int(cursor.lastrowid or 0)
        conn.executemany(
            "INSERT INTO reading_tokens (article_id, sentence_id, seq, surface, lemma,"
            " headword, pos, char_start, char_end, kind, is_target, beyond, derived_root,"
            " derived_affix, sense_id, sense_ordinal)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (article_id, sentence_id, t["seq"], t["surface"], t.get("lemma"),
                 t.get("headword"), t.get("pos"), t["char_start"], t["char_end"],
                 t["kind"], int(t.get("is_target", 0)), int(t.get("beyond", 0)),
                 t.get("derived_root"), t.get("derived_affix"),
                 t.get("sense_id"), t.get("sense_ordinal"))
                for t in tokens
            ],
        )
    conn.commit()


def sentences_of(article_id: int) -> list[dict[str, Any]]:
    rows = get_connection("learning").execute(
        "SELECT * FROM reading_sentences WHERE article_id = ? ORDER BY seq", (article_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def tokens_of(article_id: int) -> list[dict[str, Any]]:
    rows = get_connection("learning").execute(
        "SELECT * FROM reading_tokens WHERE article_id = ? ORDER BY seq", (article_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def unannotated_tokens(article_id: int) -> list[dict[str, Any]]:
    """Content words still waiting for a contextual sense.

    ``sense_id IS NULL`` means "not annotated"; ``0`` means "annotated, and this
    word has no sense set". Keeping those apart is what makes a retry ask only
    for what is actually missing.
    """
    rows = get_connection("learning").execute(
        "SELECT t.*, s.text AS sentence_text, s.seq AS sentence_seq"
        " FROM reading_tokens t JOIN reading_sentences s ON s.id = t.sentence_id"
        " WHERE t.article_id = ? AND t.kind = 'content' AND t.sense_id IS NULL"
        " ORDER BY t.seq",
        (article_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def repair_dangling_senses(headword: str | None = None) -> dict[str, Any]:
    """Re-point anything left hanging by a sense set being regenerated.

    Sense ids are not stable: ``store_senses`` deletes and re-inserts, so
    topping up one word's senses invalidates every id anyone recorded. Three
    kinds of reference exist, and each wants different treatment:

    * **token annotations** go back to NULL, which puts them in the queue to be
      annotated again against the new sense set. The article stays readable
      meanwhile; that word simply shows its dictionary gloss.
    * **marks** and **study state** collapse to sense 0, the word-level slot.
      The learner did mark this word and did meet it, and that must not be lost
      just because the senses were re-cut — only *which* sense is gone, and
      guessing a mapping from an old ordinal to a new one would invent data.

    Runs scoped to one word from the event handler, or over everything as a
    repair tool. Idempotent.
    """
    conn = get_connection("learning")
    scope = " AND {alias}.headword = ?"
    args: list[Any] = [headword] if headword else []

    def where(alias: str) -> str:
        return scope.format(alias=alias) if headword else ""

    affected_articles = [
        row["article_id"] for row in conn.execute(
            "SELECT DISTINCT t.article_id FROM reading_tokens t"
            " LEFT JOIN content.senses c ON c.id = t.sense_id"
            f" WHERE t.sense_id > 0 AND c.id IS NULL{where('t')}", args
        ).fetchall()
    ]
    tokens = conn.execute(
        "UPDATE reading_tokens SET sense_id = NULL, sense_ordinal = NULL"
        " WHERE sense_id > 0 AND sense_id NOT IN (SELECT id FROM content.senses)"
        + (" AND headword = ?" if headword else ""), args
    ).rowcount

    # Collapse to the word-level slot, merging rather than colliding with a row
    # that may already be there.
    marks = 0
    for row in conn.execute(
        "SELECT w.id, w.learner_id, w.headword, w.kind FROM word_marks w"
        " LEFT JOIN content.senses c ON c.id = w.sense_id"
        f" WHERE w.sense_id > 0 AND c.id IS NULL{where('w')}", args
    ).fetchall():
        conn.execute(
            "UPDATE OR REPLACE word_marks SET sense_id = 0 WHERE id = ?", (row["id"],)
        )
        marks += 1

    states = 0
    for row in conn.execute(
        "SELECT s.* FROM sense_states s LEFT JOIN content.senses c ON c.id = s.sense_id"
        f" WHERE s.sense_id > 0 AND c.id IS NULL{where('s')}", args
    ).fetchall():
        existing = conn.execute(
            "SELECT * FROM sense_states WHERE learner_id = ? AND headword = ? AND sense_id = 0",
            (row["learner_id"], row["headword"]),
        ).fetchone()
        if existing is None:
            conn.execute("UPDATE sense_states SET sense_id = 0 WHERE id = ?", (row["id"],))
        else:
            # Keep the earlier introduction and the sum of encounters: both rows
            # describe the same word being met, just cut differently.
            conn.execute(
                "UPDATE sense_states SET encounters = encounters + ?,"
                " introduced_at = MIN(COALESCE(introduced_at, ?), COALESCE(?, introduced_at)),"
                " pool = CASE WHEN pool = 'new' THEN ? ELSE pool END, updated_at = ?"
                " WHERE id = ?",
                (row["encounters"], row["introduced_at"], row["introduced_at"],
                 row["pool"], _now(), existing["id"]),
            )
            conn.execute("DELETE FROM sense_states WHERE id = ?", (row["id"],))
        states += 1

    conn.commit()

    if tokens or marks or states:
        log.warning(
            "senses.references.repaired",
            f"义项集变了，修复了 {tokens} 处标注、{marks} 条标记、{states} 条掌握状态；"
            f"涉及 {len(affected_articles)} 篇文章，需要重跑标注",
            headword=headword, tokens=tokens, marks=marks, states=states,
            articles=affected_articles[:20],
        )
    return {"tokens": tokens, "marks": marks, "states": states,
            "articles": affected_articles}


def reset_declined(article_id: int) -> int:
    """Put "no candidate sense fitted" tokens back in the queue.

    Used by the console's re-annotate action: once a missing sense has been
    added to the sense set, the word deserves another go.
    """
    conn = get_connection("learning")
    cursor = conn.execute(
        "UPDATE reading_tokens SET sense_id = NULL WHERE article_id = ? AND sense_id = -1",
        (article_id,),
    )
    conn.commit()
    return cursor.rowcount


def declined_tokens(limit: int = 200) -> list[dict[str, Any]]:
    """Every word the annotator could not fit to a sense, with its sentence.

    This is the raw material for the 漏义项报告 the sense design asks for: a
    word whose sense set does not cover a use that actually occurs in the exam
    corpus is a gap worth filling, and the annotator finds them for free.
    """
    rows = get_connection("learning").execute(
        "SELECT t.headword, t.surface, t.article_id, s.text AS sentence,"
        " a.title, a.source FROM reading_tokens t"
        " JOIN reading_sentences s ON s.id = t.sentence_id"
        " JOIN reading_articles a ON a.id = t.article_id"
        " WHERE t.sense_id = -1 ORDER BY t.headword LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_token_sense(token_id: int, sense_id: int, ordinal: int | None) -> None:
    conn = get_connection("learning")
    conn.execute(
        "UPDATE reading_tokens SET sense_id = ?, sense_ordinal = ? WHERE id = ?",
        (sense_id, ordinal, token_id),
    )


def commit() -> None:
    get_connection("learning").commit()


def annotation_progress(article_id: int) -> tuple[int, int]:
    """(annotated, total) content tokens."""
    row = get_connection("learning").execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN sense_id IS NOT NULL THEN 1 ELSE 0 END)"
        " AS done FROM reading_tokens WHERE article_id = ? AND kind = 'content'",
        (article_id,),
    ).fetchone()
    return int(row["done"] or 0), int(row["total"] or 0)


# --------------------------------------------------------------------------- #
# Marks — three levels counting the absence of one
# --------------------------------------------------------------------------- #


def set_mark(learner_id: int, headword: str, sense_id: int, kind: str, *,
             article_id: int | None = None, sentence_id: int | None = None,
             token_id: int | None = None) -> None:
    if kind not in MARK_KINDS:
        return
    conn = get_connection("learning")
    # A word is either unknown or fuzzy, never both: marking it one clears the
    # other, otherwise "I worked it out" would sit alongside "I don't know it".
    other = "fuzzy" if kind == "unknown" else "unknown"
    conn.execute(
        "DELETE FROM word_marks WHERE learner_id = ? AND headword = ? AND sense_id = ?"
        " AND kind = ?",
        (learner_id, headword, sense_id, other),
    )
    conn.execute(
        "INSERT OR IGNORE INTO word_marks (learner_id, headword, sense_id, kind,"
        " article_id, sentence_id, token_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (learner_id, headword, sense_id, kind, article_id, sentence_id, token_id, _now()),
    )
    conn.commit()


def clear_mark(learner_id: int, headword: str, sense_id: int, kind: str | None = None) -> None:
    conn = get_connection("learning")
    if kind:
        conn.execute(
            "DELETE FROM word_marks WHERE learner_id = ? AND headword = ? AND sense_id = ?"
            " AND kind = ?",
            (learner_id, headword, sense_id, kind),
        )
    else:
        conn.execute(
            "DELETE FROM word_marks WHERE learner_id = ? AND headword = ? AND sense_id = ?",
            (learner_id, headword, sense_id),
        )
    conn.commit()


def marks_for_headwords(learner_id: int, headwords: set[str]) -> dict[tuple[str, int], str]:
    """Every mark on these words, keyed by (headword, sense_id).

    Deliberately fetched per *headword*, not per (headword, sense): the tap
    panel has to be able to say "you marked another sense of this word".
    Without that line the learner sees "unmarked" on a word they know they
    marked, and concludes the app forgot.
    """
    if not headwords:
        return {}
    placeholders = ",".join("?" * len(headwords))
    rows = get_connection("learning").execute(
        f"SELECT headword, sense_id, kind FROM word_marks"  # noqa: S608 - count-built
        f" WHERE learner_id = ? AND headword IN ({placeholders})",
        [learner_id, *headwords],
    ).fetchall()
    return {(r["headword"], int(r["sense_id"])): r["kind"] for r in rows}


# --------------------------------------------------------------------------- #
# Sense states — where each sense stands now
# --------------------------------------------------------------------------- #


def touch_state(learner_id: int, headword: str, sense_id: int, *, pool: str | None = None,
                article_id: int | None = None, sentence_id: int | None = None,
                encounters: int = 0) -> None:
    """Create or update one sense's standing.

    ``introduced_*`` is written once and never overwritten: it records where the
    word was first taught, which is what the review card's original sentence
    comes from.
    """
    conn = get_connection("learning")
    conn.execute(
        "INSERT OR IGNORE INTO sense_states (learner_id, headword, sense_id, updated_at)"
        " VALUES (?,?,?,?)",
        (learner_id, headword, sense_id, _now()),
    )
    sets = ["encounters = encounters + ?", "updated_at = ?"]
    params: list[Any] = [encounters, _now()]
    if pool:
        sets.append("pool = ?")
        params.append(pool)
    if article_id is not None:
        sets.append("introduced_at = COALESCE(introduced_at, ?)")
        params.append(_now())
        sets.append("introduced_article_id = COALESCE(introduced_article_id, ?)")
        params.append(article_id)
        sets.append("introduced_sentence_id = COALESCE(introduced_sentence_id, ?)")
        params.append(sentence_id)
    params.extend([learner_id, headword, sense_id])
    conn.execute(
        f"UPDATE sense_states SET {', '.join(sets)}"  # noqa: S608 - fragments are literals
        " WHERE learner_id = ? AND headword = ? AND sense_id = ?",
        params,
    )
    conn.commit()


def states_for_headwords(learner_id: int, headwords: set[str]) -> dict[tuple[str, int], dict]:
    if not headwords:
        return {}
    placeholders = ",".join("?" * len(headwords))
    rows = get_connection("learning").execute(
        f"SELECT * FROM sense_states WHERE learner_id = ?"  # noqa: S608 - count-built
        f" AND headword IN ({placeholders})",
        [learner_id, *headwords],
    ).fetchall()
    return {(r["headword"], int(r["sense_id"])): dict(r) for r in rows}


def state_counts(learner_id: int = 1) -> dict[str, int]:
    rows = get_connection("learning").execute(
        "SELECT pool, COUNT(*) AS n FROM sense_states WHERE learner_id = ? GROUP BY pool",
        (learner_id,),
    ).fetchall()
    counts = {r["pool"]: int(r["n"]) for r in rows}
    counts["marked_unknown"] = int(get_connection("learning").execute(
        "SELECT COUNT(*) FROM word_marks WHERE learner_id = ? AND kind = 'unknown'",
        (learner_id,),
    ).fetchone()[0])
    counts["marked_fuzzy"] = int(get_connection("learning").execute(
        "SELECT COUNT(*) FROM word_marks WHERE learner_id = ? AND kind = 'fuzzy'",
        (learner_id,),
    ).fetchone()[0])
    return counts


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #


def save_progress(learner_id: int, article_id: int, sentence_seq: int, percent: float,
                  finished: bool = False) -> None:
    conn = get_connection("learning")
    conn.execute(
        "INSERT INTO reading_progress (learner_id, article_id, sentence_seq, percent,"
        " finished_at, updated_at) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT (learner_id, article_id) DO UPDATE SET"
        # Progress only moves forward: an event arriving late from an offline
        # queue must not drag the position back to where it was an hour ago.
        "   sentence_seq = MAX(sentence_seq, excluded.sentence_seq),"
        "   percent = MAX(percent, excluded.percent),"
        "   finished_at = COALESCE(finished_at, excluded.finished_at),"
        "   updated_at = excluded.updated_at",
        (learner_id, article_id, sentence_seq, percent,
         _now() if finished else None, _now()),
    )
    conn.commit()


def progress_of(learner_id: int, article_id: int) -> dict[str, Any]:
    row = get_connection("learning").execute(
        "SELECT * FROM reading_progress WHERE learner_id = ? AND article_id = ?",
        (learner_id, article_id),
    ).fetchone()
    return dict(row) if row else {"sentence_seq": 0, "percent": 0.0, "finished_at": None}


def mark_article_read(article_id: int) -> None:
    conn = get_connection("learning")
    conn.execute(
        "UPDATE reading_articles SET read_at = COALESCE(read_at, ?) WHERE id = ?",
        (_now(), article_id),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Client events
# --------------------------------------------------------------------------- #


def record_event(idem_key: str, device_id: int, learner_id: int, event_type: str,
                 payload: dict[str, Any], occurred_at: str | None) -> int | None:
    """Store one reported event. Returns ``None`` when it is a duplicate.

    Deduplication is the unique index doing the work, not a lookup — two
    uploads racing each other would both pass a check-then-insert.
    """
    conn = get_connection("learning")
    try:
        cursor = conn.execute(
            "INSERT INTO client_events (idem_key, device_id, learner_id, type, payload,"
            " occurred_at, received_at) VALUES (?,?,?,?,?,?,?)",
            (idem_key, device_id, learner_id, event_type,
             json.dumps(payload, ensure_ascii=False), occurred_at, _now()),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    except sqlite3.IntegrityError:
        # The duplicate is expected — an offline client retries whatever it is
        # unsure about. What is *not* optional is the rollback: a failed INSERT
        # leaves the implicit transaction open, and an open write transaction
        # holds the database lock for every later writer. Returning early
        # without it strands the lock until the connection happens to commit
        # something else, which shows up much later as an unexplained
        # "database is locked" in a completely unrelated request.
        conn.rollback()
        return None


def finish_event(event_id: int, error: str | None = None) -> None:
    conn = get_connection("learning")
    conn.execute(
        "UPDATE client_events SET processed_at = ?, error = ? WHERE id = ?",
        (_now(), error, event_id),
    )
    conn.commit()


def recent_events(limit: int = 100) -> list[dict[str, Any]]:
    rows = get_connection("learning").execute(
        "SELECT * FROM client_events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["payload"] = _loads(item.get("payload"))
        out.append(item)
    return out


def stats() -> dict[str, Any]:
    conn = get_connection("learning")

    def count(sql: str, *params: Any) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    by_source = {
        row["source"]: int(row["n"])
        for row in conn.execute(
            "SELECT source, COUNT(*) AS n FROM reading_articles WHERE status = 'ready'"
            " GROUP BY source"
        ).fetchall()
    }
    return {
        "articles": count("SELECT COUNT(*) FROM reading_articles"),
        "ready": count("SELECT COUNT(*) FROM reading_articles WHERE status = 'ready'"),
        "read": count("SELECT COUNT(*) FROM reading_articles WHERE read_at IS NOT NULL"),
        "by_source": by_source,
        "sentences": count("SELECT COUNT(*) FROM reading_sentences"),
        "tokens": count("SELECT COUNT(*) FROM reading_tokens"),
        "annotated": count("SELECT COUNT(*) FROM reading_tokens WHERE sense_id IS NOT NULL"),
        "events": count("SELECT COUNT(*) FROM client_events"),
        **state_counts(),
    }
