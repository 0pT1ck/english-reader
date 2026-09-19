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
from typing import Any, NamedTuple

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

#: What a study record can be about. Enforced by a CHECK constraint too — a typo
#: at one call site would otherwise create a third kind of item that every query
#: silently skips.
ITEM_TYPES = ("word", "phrase")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Articles
# --------------------------------------------------------------------------- #


def create_article(source: str, source_ref: str, title: str, body: str, *,
                   topic: str | None = None, summary_zh: str | None = None) -> int:
    """Reserve a row before analysis starts, so a crash leaves a visible failure.

    Returns the existing id when this source has already been ingested — a paper
    must never be ingested twice, because the learner may already have marks
    pointing at the first copy.
    """
    conn = get_connection("content")
    existing = conn.execute(
        "SELECT id FROM reading_articles WHERE source = ? AND source_ref = ?",
        (source, source_ref),
    ).fetchone()
    if existing:
        return int(existing["id"])

    cursor = conn.execute(
        "INSERT INTO reading_articles (source, source_ref, title, body, status,"
        " topic, summary_zh, prepared_at, created_at)"
        " VALUES (?,?,?,?,'pending',?,?,?,?)",
        (source, source_ref, title, body, topic, summary_zh, _now(), _now()),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def set_status(article_id: int, status: str, detail: str | None = None) -> None:
    conn = get_connection("content")
    conn.execute(
        "UPDATE reading_articles SET status = ?, status_detail = ? WHERE id = ?",
        (status, detail, article_id),
    )
    conn.commit()


def store_difficulty(article_id: int, measures: dict[str, Any]) -> None:
    conn = get_connection("content")
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
    row = get_connection("content").execute(
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
    # 「待学」= 这篇的目标词里**还没进入学习流程**的个数。
    #
    # 判据是标记，不是「有没有 study_states 行」——读完一篇文章会给它遇见过的
    # 每个词都留一行（记的是遇见次数），所以按行的有无来数，读过一篇之后这个
    # 数就会莫名其妙地归零。进复习队列只有标记一条路，所以数的就是标记。
    #
    # 第二个条件管的是已经学出师的词：它们标记还留着，但不该再算「待学」。
    #
    # **真题恒为 0**，因为它们的 target_count 本来就是 0——真题不为教任何词而写。
    rows = get_connection("content").execute(
        "SELECT a.*, p.percent, p.sentence_seq,"
        " (SELECT COUNT(DISTINCT t.headword) FROM reading_tokens t"
        "  WHERE t.article_id = a.id AND t.is_target = 1) AS target_count,"
        " (SELECT COUNT(DISTINCT t.headword) FROM reading_tokens t"
        "  WHERE t.article_id = a.id AND t.is_target = 1"
        "    AND NOT EXISTS (SELECT 1 FROM study_marks m"
        "                    WHERE m.learner_id = ? AND m.item_type = 'word'"
        "                      AND m.item_key = t.headword)"
        "    AND NOT EXISTS (SELECT 1 FROM study_states s"
        "                    WHERE s.learner_id = ? AND s.item_type = 'word'"
        "                      AND s.item_key = t.headword AND s.pool <> 'new')"
        " ) AS pending_count"
        " FROM reading_articles a"
        " LEFT JOIN reading_progress p ON p.article_id = a.id AND p.learner_id = ?"
        f" WHERE {' AND '.join(where)}",
        [learner_id, learner_id, learner_id, *params],
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
    rows = get_connection("content").execute(
        "SELECT id, source, source_ref, title, status, status_detail FROM reading_articles"
        " WHERE status NOT IN ('ready') ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def ingested_refs(source: str) -> set[str]:
    rows = get_connection("content").execute(
        "SELECT source_ref FROM reading_articles WHERE source = ?", (source,)
    ).fetchall()
    return {r["source_ref"] for r in rows}


def recount_exam_frequency() -> dict[str, int]:
    """Count how often each sense actually occurs in the exam corpus.

    The free half of annotating the exam papers. Once every content word in 452
    past papers carries a sense id, 考频 is a GROUP BY — and with it something
    the design wanted since day one becomes a number rather than a belief:
    which meaning of a word the exams actually use. `address` reads 地址 twice
    against 着手解决 thirty-eight times.

    Lives here rather than in the senses module because the annotation is this
    module's output and only the ``learning`` connection has both databases
    attached. The columns were declared by senses in P0 and left at 0 ever
    since; this is what finally fills them.

    **Tokens inside a confirmed phrase do not count.** The annotator swept the
    corpus before phrases existed, so where the text said ``account for`` it
    could only pick one of ``account``'s own senses — and that inflated
    「account 的『占／构成』出现 22 次」with occurrences that were never about
    that word. Roughly 4.5% of the annotations are affected. Re-running this
    after a phrase scan is what corrects the count.
    """
    conn = get_connection("content")
    counts = conn.execute(
        "SELECT t.sense_id, COUNT(*) AS n FROM reading_tokens t"
        " JOIN reading_articles a ON a.id = t.article_id"
        " WHERE a.source != 'generated' AND t.sense_id > 0 AND t.in_phrase = 0"
        " GROUP BY t.sense_id"
    ).fetchall()

    conn.execute("UPDATE senses SET exam_frequency = 0, is_exam_key = 0")
    conn.executemany(
        "UPDATE senses SET exam_frequency = ? WHERE id = ?",
        [(int(r["n"]), int(r["sense_id"])) for r in counts],
    )
    conn.commit()

    # `is_exam_key` is deliberately left at 0, and the idea behind it — flagging
    # a "familiar word in an obscure sense" — was dropped on 2026-09-07.
    #
    # The label does not survive its own data. `address` means 着手解决 in 38 of
    # its 41 exam occurrences and 地址 in 2: that sense is not obscure, it is the
    # dominant one in written English. Calling it obscure takes the learner's
    # first impression as the baseline instead of the language. And the only
    # available rule — "a sense the model ranked second or later that still
    # appears in the exams" — flagged 2629 senses, 19% of all of them, including
    # `well` 好, `even` 甚至 and `make` 使得, because `ordinal` is a model's
    # guess at commonness and these sense sets are temporary anyway.
    #
    # What replaces it is the count and each sense's share of the word's exam
    # occurrences: facts, side by side, with the reader drawing the conclusion.
    # The column stays declared and empty per architecture rule 5.
    scored = int(conn.execute(
        "SELECT COUNT(*) FROM senses WHERE exam_frequency > 0").fetchone()[0])
    excluded = int(conn.execute(
        "SELECT COUNT(*) FROM reading_tokens t JOIN reading_articles a ON a.id = t.article_id"
        " WHERE a.source != 'generated' AND t.sense_id > 0 AND t.in_phrase = 1"
    ).fetchone()[0])
    log.info(
        "senses.exam_frequency.recounted",
        f"按 452 篇真题的标注结果算出考频：{scored} 个义项在真题里出现过；"
        f"落在词组里的 {excluded} 个 token 没有计入",
        scored=scored, excluded_in_phrase=excluded,
    )
    return {"senses_with_frequency": scored, "exam_key_senses": 0,
            "excluded_in_phrase": excluded,
            "articles_counted": int(conn.execute(
                "SELECT COUNT(*) FROM reading_articles WHERE source != 'generated'"
                " AND status = 'ready'").fetchone()[0])}


def scores_by_source() -> dict[str, list[float]]:
    """Composite scores grouped by exam, for the calibration check."""
    rows = get_connection("content").execute(
        "SELECT source, difficulty_score FROM reading_articles"
        " WHERE difficulty_score IS NOT NULL"
    ).fetchall()
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(row["source"], []).append(float(row["difficulty_score"]))
    return grouped


def delete_article(article_id: int) -> bool:
    conn = get_connection("content")
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
    conn = get_connection("content")
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
    rows = get_connection("content").execute(
        "SELECT * FROM reading_sentences WHERE article_id = ? ORDER BY seq", (article_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def tokens_of(article_id: int) -> list[dict[str, Any]]:
    rows = get_connection("content").execute(
        "SELECT * FROM reading_tokens WHERE article_id = ? ORDER BY seq", (article_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def unannotated_tokens(article_id: int) -> list[dict[str, Any]]:
    """Content words still waiting for a contextual sense.

    ``sense_id IS NULL`` means "not annotated"; ``0`` means "annotated, and this
    word has no sense set". Keeping those apart is what makes a retry ask only
    for what is actually missing.
    """
    rows = get_connection("content").execute(
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
    conn = get_connection("content")
    scope = " AND {alias}.{column} = ?"
    args: list[Any] = [headword] if headword else []

    def where(alias: str, column: str = "item_key") -> str:
        return scope.format(alias=alias, column=column) if headword else ""

    affected_articles = [
        row["article_id"] for row in conn.execute(
            "SELECT DISTINCT t.article_id FROM reading_tokens t"
            " LEFT JOIN senses c ON c.id = t.sense_id"
            f" WHERE t.sense_id > 0 AND c.id IS NULL{where('t', 'headword')}", args
        ).fetchall()
    ]
    tokens = conn.execute(
        "UPDATE reading_tokens SET sense_id = NULL, sense_ordinal = NULL"
        " WHERE sense_id > 0 AND sense_id NOT IN (SELECT id FROM senses)"
        + (" AND headword = ?" if headword else ""), args
    ).rowcount

    # Collapse to the word-level slot, merging rather than colliding with a row
    # that may already be there.
    marks = 0
    for row in conn.execute(
        "SELECT w.id, w.learner_id, w.item_key, w.kind FROM study_marks w"
        " LEFT JOIN senses c ON c.id = w.sense_id"
        f" WHERE w.item_type = 'word' AND w.sense_id > 0 AND c.id IS NULL{where('w')}", args
    ).fetchall():
        conn.execute(
            "UPDATE OR REPLACE study_marks SET sense_id = 0 WHERE id = ?", (row["id"],)
        )
        marks += 1

    states = 0
    for row in conn.execute(
        "SELECT s.* FROM study_states s LEFT JOIN senses c ON c.id = s.sense_id"
        f" WHERE s.item_type = 'word' AND s.sense_id > 0 AND c.id IS NULL{where('s')}", args
    ).fetchall():
        existing = conn.execute(
            "SELECT * FROM study_states WHERE learner_id = ? AND item_type = 'word'"
            " AND item_key = ? AND sense_id = 0",
            (row["learner_id"], row["item_key"]),
        ).fetchone()
        if existing is None:
            conn.execute("UPDATE study_states SET sense_id = 0 WHERE id = ?", (row["id"],))
        else:
            # Keep the earlier introduction and the sum of encounters: both rows
            # describe the same word being met, just cut differently.
            conn.execute(
                "UPDATE study_states SET encounters = encounters + ?,"
                " introduced_at = MIN(COALESCE(introduced_at, ?), COALESCE(?, introduced_at)),"
                " pool = CASE WHEN pool = 'new' THEN ? ELSE pool END, updated_at = ?"
                " WHERE id = ?",
                (row["encounters"], row["introduced_at"], row["introduced_at"],
                 row["pool"], _now(), existing["id"]),
            )
            conn.execute("DELETE FROM study_states WHERE id = ?", (row["id"],))
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
    conn = get_connection("content")
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

    Tokens inside a confirmed phrase are excluded, and that is most of what used
    to make this report hard to read: ``get up``, ``known as`` and ``at least``
    were its most frequent entries, and no sense of ``get`` or ``least`` will
    ever cover them. They were never gaps in a sense set — they were phrases the
    annotator had no way to see. What is left is the real shortfall.
    """
    rows = get_connection("content").execute(
        "SELECT t.headword, t.surface, t.article_id, s.text AS sentence,"
        " a.title, a.source FROM reading_tokens t"
        " JOIN reading_sentences s ON s.id = t.sentence_id"
        " JOIN reading_articles a ON a.id = t.article_id"
        " WHERE t.sense_id = -1 AND t.in_phrase = 0 ORDER BY t.headword LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_token_sense(token_id: int, sense_id: int, ordinal: int | None) -> None:
    conn = get_connection("content")
    conn.execute(
        "UPDATE reading_tokens SET sense_id = ?, sense_ordinal = ? WHERE id = ?",
        (sense_id, ordinal, token_id),
    )


def commit() -> None:
    get_connection("content").commit()


def annotation_progress(article_id: int) -> tuple[int, int]:
    """(annotated, total) content tokens."""
    row = get_connection("content").execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN sense_id IS NOT NULL THEN 1 ELSE 0 END)"
        " AS done FROM reading_tokens WHERE article_id = ? AND kind = 'content'",
        (article_id,),
    ).fetchone()
    return int(row["done"] or 0), int(row["total"] or 0)


# --------------------------------------------------------------------------- #
# Marks — three levels counting the absence of one
#
# One table for both kinds of item. A word sense and a phrase are the same thing
# to everything downstream: something the learner said they do not know, which
# review scheduling will one day order into a single list. Two tables would mean
# writing that ordering twice.
# --------------------------------------------------------------------------- #


def set_mark(learner_id: int, item_key: str, sense_id: int, kind: str, *,
             item_type: str = "word", article_id: int | None = None,
             sentence_id: int | None = None, token_id: int | None = None) -> None:
    if kind not in MARK_KINDS or item_type not in ITEM_TYPES:
        return
    conn = get_connection("events")
    # An item is either unknown or fuzzy, never both: marking it one clears the
    # other, otherwise "I worked it out" would sit alongside "I don't know it".
    other = "fuzzy" if kind == "unknown" else "unknown"
    conn.execute(
        "DELETE FROM study_marks WHERE learner_id = ? AND item_type = ? AND item_key = ?"
        " AND sense_id = ? AND kind = ?",
        (learner_id, item_type, item_key, sense_id, other),
    )
    conn.execute(
        "INSERT OR IGNORE INTO study_marks (learner_id, item_type, item_key, sense_id,"
        " kind, article_id, sentence_id, token_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (learner_id, item_type, item_key, sense_id, kind, article_id, sentence_id,
         token_id, _now()),
    )
    conn.commit()


def clear_mark(learner_id: int, item_key: str, sense_id: int, kind: str | None = None,
               *, item_type: str = "word") -> None:
    conn = get_connection("events")
    sql = ("DELETE FROM study_marks WHERE learner_id = ? AND item_type = ?"
           " AND item_key = ? AND sense_id = ?")
    params: list[Any] = [learner_id, item_type, item_key, sense_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    conn.execute(sql, params)
    conn.commit()


def marks_for(learner_id: int, keys: set[str], *,
              item_type: str = "word") -> dict[tuple[str, int], str]:
    """Every mark on these items, keyed by (item_key, sense_id).

    Deliberately fetched per *item*, not per (item, sense): the tap panel has to
    be able to say "you marked another sense of this word". Without that line
    the learner sees "unmarked" on a word they know they marked, and concludes
    the app forgot.
    """
    if not keys:
        return {}
    placeholders = ",".join("?" * len(keys))
    rows = get_connection("events").execute(
        f"SELECT item_key, sense_id, kind FROM study_marks"  # noqa: S608 - count-built
        f" WHERE learner_id = ? AND item_type = ? AND item_key IN ({placeholders})",
        [learner_id, item_type, *keys],
    ).fetchall()
    return {(r["item_key"], int(r["sense_id"])): r["kind"] for r in rows}


# --------------------------------------------------------------------------- #
# Sense states — where each sense stands now
# --------------------------------------------------------------------------- #


def touch_state(learner_id: int, item_key: str, sense_id: int, *,
                item_type: str = "word", pool: str | None = None,
                article_id: int | None = None, sentence_id: int | None = None,
                encounters: int = 0) -> None:
    """Create or update one item's standing.

    ``introduced_*`` is written once and never overwritten: it records where the
    item was first met, which is what the review card's original sentence comes
    from.
    """
    conn = get_connection("events")
    conn.execute(
        "INSERT OR IGNORE INTO study_states (learner_id, item_type, item_key, sense_id,"
        " updated_at) VALUES (?,?,?,?,?)",
        (learner_id, item_type, item_key, sense_id, _now()),
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
    params.extend([learner_id, item_type, item_key, sense_id])
    conn.execute(
        f"UPDATE study_states SET {', '.join(sets)}"  # noqa: S608 - fragments are literals
        " WHERE learner_id = ? AND item_type = ? AND item_key = ? AND sense_id = ?",
        params,
    )
    conn.commit()


def demote_if_unmarked(learner_id: int, item_key: str, sense_id: int, *,
                       item_type: str = "word") -> bool:
    """Send an item back to the 'new' pool once nothing marks it any more.

    The counterpart of the rule that only the learner's own signal puts
    something in the review queue: withdraw the signal and it comes back out.
    Without this, unmarking leaves a ``reviewing`` row with no mark behind it —
    which is precisely the state the acceptance check for that invariant looks
    for, and it would be there because of an undo rather than a bug.
    """
    conn = get_connection("events")
    still = conn.execute(
        "SELECT 1 FROM study_marks WHERE learner_id = ? AND item_type = ?"
        " AND item_key = ? AND sense_id = ?",
        (learner_id, item_type, item_key, sense_id),
    ).fetchone()
    if still:
        return False
    cursor = conn.execute(
        "UPDATE study_states SET pool = 'new', updated_at = ? WHERE learner_id = ?"
        " AND item_type = ? AND item_key = ? AND sense_id = ? AND pool = 'reviewing'",
        (_now(), learner_id, item_type, item_key, sense_id),
    )
    conn.commit()
    return bool(cursor.rowcount)


def states_for(learner_id: int, keys: set[str], *,
               item_type: str = "word") -> dict[tuple[str, int], dict]:
    if not keys:
        return {}
    placeholders = ",".join("?" * len(keys))
    rows = get_connection("events").execute(
        f"SELECT * FROM study_states WHERE learner_id = ?"  # noqa: S608 - count-built
        f" AND item_type = ? AND item_key IN ({placeholders})",
        [learner_id, item_type, *keys],
    ).fetchall()
    return {(r["item_key"], int(r["sense_id"])): dict(r) for r in rows}


def state_counts(learner_id: int = 1) -> dict[str, int]:
    conn = get_connection("events")
    rows = conn.execute(
        "SELECT pool, COUNT(*) AS n FROM study_states WHERE learner_id = ? GROUP BY pool",
        (learner_id,),
    ).fetchall()
    counts = {r["pool"]: int(r["n"]) for r in rows}
    for kind in MARK_KINDS:
        counts[f"marked_{kind}"] = int(conn.execute(
            "SELECT COUNT(*) FROM study_marks WHERE learner_id = ? AND kind = ?",
            (learner_id, kind),
        ).fetchone()[0])
    counts["marked_phrases"] = int(conn.execute(
        "SELECT COUNT(*) FROM study_marks WHERE learner_id = ? AND item_type = 'phrase'",
        (learner_id,),
    ).fetchone()[0])
    return counts


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #


def save_progress(learner_id: int, article_id: int, sentence_seq: int, percent: float,
                  finished: bool = False) -> None:
    conn = get_connection("events")
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
    """How far through this article the learner is. Three keys, always the same.

    This used to be ``dict(row)``, which meant a reader who had opened the
    article got ``learner_id``, ``article_id`` and ``updated_at`` as well, and
    one who had not got three keys — the same field being sometimes present is
    worse than either shape on its own, because nothing can be written against
    it. Narrowed on 2026-09-12 while declaring the response types: those three
    were server bookkeeping that leaked, nothing read them, and ``learner_id``
    in particular contradicts the invariant that identity is derived from the
    device token rather than carried around in payloads.
    """
    row = get_connection("events").execute(
        "SELECT sentence_seq, percent, finished_at FROM reading_progress"
        " WHERE learner_id = ? AND article_id = ?",
        (learner_id, article_id),
    ).fetchone()
    return dict(row) if row else {"sentence_seq": 0, "percent": 0.0, "finished_at": None}


def mark_article_read(article_id: int) -> None:
    conn = get_connection("content")
    conn.execute(
        "UPDATE reading_articles SET read_at = COALESCE(read_at, ?) WHERE id = ?",
        (_now(), article_id),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Client events
# --------------------------------------------------------------------------- #


#: How long a half-finished event has to sit before a retry may take it over.
#: Same reasoning and same number as ``llm.jobs.STALE_MINUTES``: a row with no
#: verdict is either a process that died mid-apply or a request still running,
#: and only the clock tells them apart.
EVENT_STALE_MINUTES = 5


class RecordedEvent(NamedTuple):
    """Where a reported event stands after being written down.

    ``retry`` marks the case this type exists for: the key is already on file,
    but the earlier attempt never took effect, so the caller should apply it
    again rather than wave it through as a duplicate.
    """

    id: int
    retry: bool


def record_event(idem_key: str, device_id: int, learner_id: int, event_type: str,
                 payload: dict[str, Any], occurred_at: str | None) -> RecordedEvent | None:
    """Store one reported event. ``None`` means it already landed — skip it.

    Deduplication is the unique index doing the work, not a lookup — two
    uploads racing each other would both pass a check-then-insert.

    **Why a duplicate is not always finished business.** Handling an event is
    two steps, storing it and applying it, and they can disagree: an apply that
    raises leaves the row on file with an error against it. The client, which
    only ever hears "duplicate", then deletes the event from its outbox
    believing it landed — and nothing retries it, ever. A marked word would
    simply never reach the review queue, with no error and no log line. So a
    duplicate whose earlier attempt failed comes back with ``retry`` set.

    **Applying twice is the risk on the other side**, and the two events that
    would hurt are already guarded: finishing an article is refused once
    ``read_at`` is set (that is the bug where `colony` reached 55 encounters in
    one 430-word article), and marking touches state with a zero encounter
    delta. A stale half-finished row is treated the same way, but only after
    ``EVENT_STALE_MINUTES`` — before that it may be a request still in flight,
    and taking it over would be the double-apply this paragraph is about.
    """
    conn = get_connection("events")
    try:
        cursor = conn.execute(
            "INSERT INTO client_events (idem_key, device_id, learner_id, type, payload,"
            " occurred_at, received_at) VALUES (?,?,?,?,?,?,?)",
            (idem_key, device_id, learner_id, event_type,
             json.dumps(payload, ensure_ascii=False), occurred_at, _now()),
        )
        conn.commit()
        return RecordedEvent(int(cursor.lastrowid or 0), retry=False)
    except sqlite3.IntegrityError:
        # The duplicate is expected — an offline client retries whatever it is
        # unsure about. What is *not* optional is the rollback: a failed INSERT
        # leaves the implicit transaction open, and an open write transaction
        # holds the database lock for every later writer. Returning early
        # without it strands the lock until the connection happens to commit
        # something else, which shows up much later as an unexplained
        # "database is locked" in a completely unrelated request.
        conn.rollback()

    row = conn.execute(
        "SELECT id, processed_at, error, received_at FROM client_events WHERE idem_key = ?",
        (idem_key,),
    ).fetchone()
    if row is None:
        # The unique index fired but the row is gone: someone deleted it between
        # the two statements. Nothing sensible to retry.
        return None

    event_id = int(row["id"])
    if row["error"] is not None:
        log.info(
            "event.retry.after_failure",
            f"事件 {idem_key} 上次执行失败过，这次重新执行",
            idem_key=idem_key, event_type=event_type, previous_error=str(row["error"])[:200],
        )
        return RecordedEvent(event_id, retry=True)

    if row["processed_at"] is None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=EVENT_STALE_MINUTES)
        ).isoformat(timespec="seconds")
        if (row["received_at"] or "") < cutoff:
            log.warning(
                "event.retry.after_interruption",
                f"事件 {idem_key} 收下了却没有执行结果，超过 {EVENT_STALE_MINUTES} 分钟，重新执行",
                idem_key=idem_key, event_type=event_type, received_at=row["received_at"],
            )
            return RecordedEvent(event_id, retry=True)
        # Too fresh to judge — another request may be working through it right
        # now, and taking it over would apply the same event twice.
        return None

    return None


def events_after(learner_id: int, after: int, limit: int) -> list[dict[str, Any]]:
    """一个学习者的事件，序号大于 ``after`` 的那些，按序号升序。

    **`client_events.id` 就是那个全序序号。** P9 §6 的细则 ① 要一个「服务端收到即
    分配的单调序号」——而这张表从 P2 起就是 ``AUTOINCREMENT``，它一直是。
    新建一套会得到第二个顺序，然后两个顺序说反话。

    **序号会有缺口**，那是对的:按 ``learner_id`` 过滤之后别人的事件不在里面，
    而缺口对「大于某个号」这种游标毫无影响。

    **payload 原样返回，不解释。** 服务端对学习记录只有两种关系:生文需要的
    那一小撮信号，和它不解释的存档（`phase-9.html` §2）。这是后者。
    """
    rows = get_connection("events").execute(
        "SELECT id, idem_key, type, payload, occurred_at, received_at"
        " FROM client_events WHERE learner_id = ? AND id > ?"
        " ORDER BY id LIMIT ?",
        (learner_id, int(after), int(limit)),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            # 存进去的不是 JSON。**报出来而不是跳过**:一条读不出来的事件会让
            # 重放少算一笔，而跳过它的话没人知道少了什么。
            payload = {}
            log.warning(
                "event.payload.unreadable",
                f"事件 {row['idem_key']} 的 payload 解不开，按空对象下发",
                idem_key=row["idem_key"], sequence=int(row["id"]),
            )
        out.append({
            "sequence": int(row["id"]),
            "idem_key": row["idem_key"],
            "type": row["type"],
            "payload": payload,
            "occurred_at": row["occurred_at"],
            "received_at": row["received_at"],
        })
    return out


def latest_event_sequence(learner_id: int) -> int:
    """这个学习者最大的那个序号。0 ＝ 一条都没有。"""
    row = get_connection("events").execute(
        "SELECT COALESCE(MAX(id), 0) AS n FROM client_events WHERE learner_id = ?",
        (learner_id,),
    ).fetchone()
    return int(row["n"] or 0)


def finish_event(event_id: int, error: str | None = None) -> None:
    conn = get_connection("events")
    conn.execute(
        "UPDATE client_events SET processed_at = ?, error = ? WHERE id = ?",
        (_now(), error, event_id),
    )
    conn.commit()


def recent_events(limit: int = 100) -> list[dict[str, Any]]:
    rows = get_connection("events").execute(
        "SELECT * FROM client_events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["payload"] = _loads(item.get("payload"))
        out.append(item)
    return out


def stats() -> dict[str, Any]:
    conn = get_connection("content")

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
