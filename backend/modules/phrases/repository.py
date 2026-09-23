"""Storing the phrase inventory — P11.

**Re-import must not move a single id** (架构铁律 5). A phrase is recognised by
its text, a phrase sense by the provenance triple it was imported from
(dictionary, entry, block in document order). Anything already there is updated
in place; only genuinely new rows draw a number from the allocator. That is the
property P10 §5 paid for the hard way, and the reason "next time we change
dictionaries" is a mapping job rather than another total invalidation.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from backend.core.db import get_connection
from backend.core.logging import get_logger
from backend.modules.phrases import ids

log = get_logger("phrases.repository")

SOURCE_DICT = "collins-cobuild-2012"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    return get_connection("content")


def upsert_phrase(text: str, *, source_key: str, tier: str, level: str | None,
                  conn: sqlite3.Connection | None = None) -> int:
    conn = conn or _conn()
    row = conn.execute("SELECT id FROM phrase_list WHERE text = ?", (text,)).fetchone()
    if row:
        conn.execute(
            "UPDATE phrase_list SET source_key = ?, tier = ?, level = ? WHERE id = ?",
            (source_key, tier, level, int(row["id"])),
        )
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO phrase_list (text, source, source_key, tier, level, created_at)"
        " VALUES (?, 'collins', ?, ?, ?, ?)",
        (text, source_key, tier, level, _now()),
    )
    return int(cur.lastrowid)


def store_senses(phrase_id: int, senses: list[dict[str, Any]],
                 conn: sqlite3.Connection | None = None) -> tuple[int, int]:
    """Write one phrase's senses. Returns (kept, added).

    ``senses`` items carry ``source_head``/``source_block`` — the two halves of
    the provenance triple that vary — plus the printed content.
    """
    conn = conn or _conn()
    existing = {
        (str(r["source_head"]), int(r["source_block"])): int(r["id"])
        for r in conn.execute(
            "SELECT id, source_head, source_block FROM phrase_senses WHERE phrase_id = ?",
            (phrase_id,),
        )
    }
    fresh = [s for s in senses
             if (s["source_head"], s["source_block"]) not in existing]
    numbers = iter(ids.allocate(len(fresh), conn) if fresh else [])

    kept = added = 0
    for ordinal, sense in enumerate(senses, start=1):
        key = (sense["source_head"], sense["source_block"])
        columns = (
            sense.get("gloss_zh") or "", sense.get("concept_en") or "",
            sense.get("pos") or "", sense.get("pos_zh") or "",
            sense.get("register") or "", sense.get("pattern") or "",
            sense.get("source_ordinal"),
        )
        if key in existing:
            conn.execute(
                "UPDATE phrase_senses SET ordinal = ?, gloss_zh = ?, concept_en = ?,"
                " pos = ?, pos_zh = ?, register = ?, pattern = ?, source_ordinal = ?"
                " WHERE id = ?",
                (ordinal, *columns, existing[key]),
            )
            kept += 1
            continue
        conn.execute(
            "INSERT INTO phrase_senses (id, phrase_id, ordinal, gloss_zh, concept_en,"
            " pos, pos_zh, register, pattern, source_dict, source_head, source_block,"
            " source_ordinal, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (next(numbers), phrase_id, ordinal, *columns[:6], SOURCE_DICT,
             key[0], key[1], columns[6], _now()),
        )
        added += 1
    return kept, added


def store_candidate(text: str, *, tier: str, head: str, block: int, bold: str,
                    gloss_zh: str, level: str | None, control: bool = False,
                    conn: sqlite3.Connection | None = None) -> None:
    """A tier C/D match, waiting to be judged (㉑). Never overwrites a verdict."""
    conn = conn or _conn()
    conn.execute(
        "INSERT INTO phrase_candidates (text, tier, source_head, source_block, bold,"
        " gloss_zh, level, control, created_at) VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(text, source_head, source_block) DO UPDATE SET"
        " tier = excluded.tier, bold = excluded.bold, gloss_zh = excluded.gloss_zh",
        (text, tier, head, block, bold, gloss_zh, level, int(control), _now()),
    )


def store_excluded(rows: Iterable[tuple[str, str, str, str | None]],
                   conn: sqlite3.Connection | None = None) -> int:
    """Archive what only one side of the intersection carries (①)."""
    conn = conn or _conn()
    count = 0
    for text, source, reason, level in rows:
        conn.execute(
            "INSERT INTO phrase_excluded (text, source, reason, level, created_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(text, source) DO NOTHING",
            (text, source, reason, level, _now()),
        )
        count += 1
    return count


def store_collocations(sense_id: int, texts: list[str],
                       conn: sqlite3.Connection | None = None) -> int:
    """A word sense's usage patterns, **as Collins prints them** (⑨)."""
    conn = conn or _conn()
    added = 0
    for ordinal, text in enumerate(texts, start=1):
        conn.execute(
            "INSERT INTO sense_collocations (sense_id, ordinal, text, created_at)"
            " VALUES (?,?,?,?) ON CONFLICT(sense_id, text) DO UPDATE SET"
            " ordinal = excluded.ordinal",
            (sense_id, ordinal, text, _now()),
        )
        added += 1
    return added


def for_article(article_id: int) -> list[dict[str, Any]]:
    """Every phrase occurrence in one article, with the phrase's senses.

    **Only occurrences that carry a sense go out** (`sense_id > 0`). The other
    two values are not phrases to render: ``NULL`` has not been asked about yet,
    and ``-1`` is the annotator saying *these are two ordinary words here*
    (P11 §7b — ``He ran into the room``). The number is -1 rather than 0 because
    this database already spends those two values on exactly these meanings for
    word tokens (`annotate.NO_SENSE_FITS`), and one table using them the other
    way round is how a filter ends up shipping the rows it meant to drop.
    """
    conn = _conn()
    rows = conn.execute(
        "SELECT o.phrase, o.phrase_id, o.surface, o.start_seq, o.end_seq, o.sense_id"
        " FROM reading_phrases o WHERE o.article_id = ?"
        "   AND o.sense_id > 0"
        " ORDER BY o.start_seq",
        (article_id,),
    ).fetchall()
    if not rows:
        return []

    senses: dict[int, list[dict[str, Any]]] = {}
    ids = sorted({int(r["phrase_id"]) for r in rows if r["phrase_id"]})
    placeholders = ",".join("?" * len(ids))
    raw: dict[int, list[Any]] = {}
    for row in conn.execute(
        "SELECT id, phrase_id, ordinal, gloss_zh, concept_en, pos_zh, register,"  # noqa: S608
        f" exam_frequency FROM phrase_senses WHERE phrase_id IN ({placeholders})"
        " ORDER BY phrase_id, ordinal", ids,
    ):
        raw.setdefault(int(row["phrase_id"]), []).append(row)

    for phrase_id, group in raw.items():
        # 占这个词组全部真题出现的百分之几。**跟单词那边同一个算法**——
        # 「78%」回答得了「考的是不是这个意思」，而一个光秃秃的 38 回答不了。
        total = sum(int(r["exam_frequency"] or 0) for r in group)
        senses[phrase_id] = [{
            "id": int(r["id"]),
            "ordinal": int(r["ordinal"]),
            "gloss_zh": r["gloss_zh"],
            "concept_en": r["concept_en"],
            "pos_zh": r["pos_zh"],
            "register_label": r["register"] or None,
            # 整块缺席而不是为零：零会被读成「真题里从没出现过」，那是另一回事。
            "exam": ({"frequency": int(r["exam_frequency"] or 0),
                      "share": (round(int(r["exam_frequency"] or 0) / total * 100, 1)
                                if total else None),
                      # **恒为 false，跟单词那边一样**（跨 Phase 不变量）。
                      # 「熟词僻义」这个说法本身站不住：`address` 的「着手解决」
                      # 在真题里 38 次、「地址」2 次，那不是僻义，是主流用法。
                      "is_exam_key": False}
                     if total else None),
        } for r in group]

    return [{
        "phrase": str(r["phrase"]),
        "phrase_id": int(r["phrase_id"] or 0),
        "surface": str(r["surface"]),
        "start_seq": int(r["start_seq"]),
        "end_seq": int(r["end_seq"]),
        "sense_id": int(r["sense_id"]),
        "senses": senses.get(int(r["phrase_id"] or 0), []),
    } for r in rows]


def collocations_for(sense_ids: list[int]) -> dict[int, list[str]]:
    """Usage patterns for these word senses, in the order Collins printed them."""
    if not sense_ids:
        return {}
    placeholders = ",".join("?" * len(sense_ids))
    out: dict[int, list[str]] = {}
    for row in _conn().execute(
        "SELECT sense_id, text FROM sense_collocations"  # noqa: S608
        f" WHERE sense_id IN ({placeholders}) ORDER BY sense_id, ordinal", sense_ids,
    ):
        out.setdefault(int(row["sense_id"]), []).append(str(row["text"]))
    return out


def senses_of_phrase(phrase: str) -> list[dict[str, Any]]:
    """Every sense of one phrase, in 柯林斯's order. For the reveal card (P12)."""
    rows = _conn().execute(
        "SELECT s.id, s.ordinal, s.gloss_zh, s.concept_en, s.pos, s.pos_zh,"
        " s.exam_frequency FROM phrase_senses s JOIN phrase_list p ON p.id = s.phrase_id"
        " WHERE p.text = ? ORDER BY s.ordinal",
        (phrase,),
    ).fetchall()
    return [dict(r) for r in rows]


def sense_by_id(sense_id: int) -> dict[str, Any] | None:
    """One phrase sense, in the shape a review card renders.

    Same keys as a word sense so that every caller that already knows how to
    show one of those needs no branch — ``headword`` carries the phrase text,
    because to a card that is what the item is called.
    """
    row = _conn().execute(
        "SELECT s.id, s.ordinal, s.gloss_zh, s.concept_en, s.pos, s.pos_zh,"
        " s.register, s.exam_frequency, p.text AS headword"
        " FROM phrase_senses s JOIN phrase_list p ON p.id = s.phrase_id"
        " WHERE s.id = ?",
        (sense_id,),
    ).fetchone()
    return dict(row) if row else None


def recount_exam_frequency() -> dict[str, int]:
    """How often each phrase sense actually occurs in the exam papers — 决定 ⑳.

    The mirror of the word-side recount, and free for the same reason: every
    occurrence already carries the sense the annotator chose, so 考频 is a
    GROUP BY. Generated articles are excluded — they were written to teach, so
    counting them would measure this project's own output rather than the exam.

    **Occurrences settled as "not a phrase here" (-1) do not count**, which is
    the whole point of asking: ``talk about`` is listed with one sense
    （「这才叫…；真是」）and the corpus is full of ordinary ``talked about``.
    Counting those would report a rare exclamation as one of the most frequent
    things in the exams.
    """
    conn = _conn()
    rows = conn.execute(
        "SELECT p.sense_id, COUNT(*) AS n FROM reading_phrases p"
        " JOIN reading_articles a ON a.id = p.article_id"
        " WHERE a.source != 'generated' AND p.sense_id > 0"
        " GROUP BY p.sense_id"
    ).fetchall()
    conn.execute("UPDATE phrase_senses SET exam_frequency = 0")
    conn.executemany(
        "UPDATE phrase_senses SET exam_frequency = ? WHERE id = ?",
        [(int(r["n"]), int(r["sense_id"])) for r in rows],
    )
    conn.commit()
    log.info("phrases.exam_frequency.recounted",
             f"{len(rows)} 条词组义项在真题里出现过", senses=len(rows))
    return {"senses_with_frequency": len(rows),
            "occurrences": sum(int(r["n"]) for r in rows)}


def stats() -> dict[str, int]:
    conn = _conn()

    def count(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "phrases": count("SELECT COUNT(*) FROM phrase_list"),
        "senses": count("SELECT COUNT(*) FROM phrase_senses"),
        "candidates": count("SELECT COUNT(*) FROM phrase_candidates"),
        "candidates_pending": count(
            "SELECT COUNT(*) FROM phrase_candidates WHERE verdict IS NULL"),
        "excluded": count("SELECT COUNT(*) FROM phrase_excluded"),
        "collocations": count("SELECT COUNT(*) FROM sense_collocations"),
    }
