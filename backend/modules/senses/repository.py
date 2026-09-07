"""Reads and writes for the sense tables."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from backend.core import events
from backend.core.db import get_connection

TOPICS = (
    "社会", "科技", "文化", "日常生活", "环境", "经济", "教育", "健康", "心理", "历史", "通用",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store_senses(headword: str, senses: list[dict[str, Any]], *, model: str = "") -> int:
    """Replace one word's sense set.

    Replace rather than merge: a sense set is a single judgement about how a
    word divides up, and half of an old division mixed with half of a new one
    is not a division of anything.

    **Replacing changes the sense ids**, and from P2 onwards other things point
    at them — the contextual annotation on every occurrence of the word, the
    learner's marks, the per-sense study state. Nothing here knows about those,
    and nothing should. So the change is announced on the event bus and whoever
    holds a reference repairs itself; without that announcement, topping up one
    word's senses silently orphans every annotation of it, with no error and no
    log (architecture rule 6: new features subscribe, they do not edit this).

    **This path is for topping up one word. It is the wrong path for replacing
    the whole inventory**, which is planned — these sense sets are model-written
    and will be rebuilt from a real lexicographic source. Called in a loop over
    every headword, the subscriber would reset all 95,058 contextual
    annotations, discarding the 4.6M input tokens the corpus pass cost, silently
    and in the time it takes to click a button.

    A replacement should instead keep the old rows (``senses_p1b`` is the
    precedent) and build an old-sense → new-sense map, one call per headword.
    That is about 7,200 calls against 95,058 tokens re-annotated — an order of
    magnitude cheaper — and it carries the exam frequencies across instead of
    throwing them away. See the main design document, §F4.
    """
    existing = [row["id"] for row in get_connection("content").execute(
        "SELECT id FROM senses WHERE headword = ?", (headword,)
    ).fetchall()]

    conn = get_connection("content")
    conn.execute("DELETE FROM senses WHERE headword = ?", (headword,))
    conn.executemany(
        "INSERT INTO senses (headword, ordinal, concept_en, gloss_zh, pos, topic,"
        " covers, source, model, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                headword,
                index,
                sense["concept_en"],
                json.dumps(sense["gloss_zh"], ensure_ascii=False),
                sense.get("pos"),
                sense.get("topic"),
                # Which source senses this one accounts for. Present only on the
                # rebuilt set; the P1b set had no checklist to point at.
                sense.get("covers"),
                sense.get("source"),
                model,
                _now(),
            )
            for index, sense in enumerate(senses, start=1)
        ],
    )
    conn.commit()

    if existing:
        events.emit("senses.replaced", headword=headword,
                    previous_ids=existing, count=len(senses))
    return len(senses)


def senses_of(headword: str) -> list[dict[str, Any]]:
    try:
        rows = get_connection("content").execute(
            "SELECT * FROM senses WHERE headword = ? ORDER BY ordinal",
            (headword.lower(),),
        ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        sense = dict(row)
        try:
            sense["gloss_zh"] = json.loads(sense["gloss_zh"])
        except (TypeError, json.JSONDecodeError):
            sense["gloss_zh"] = [sense["gloss_zh"]]
        out.append(sense)
    return out


def pending_words(limit: int = 20000) -> list[str]:
    """Target words with no sense set yet — the job's input.

    The screening verdict is deliberately ignored. It judged English polysemy by
    counting commas in a Chinese gloss, and measuring it against an external
    inventory showed that 65% of the words it dismissed as "simple" are in fact
    polysemous — ``bank``, ``come``, ``do`` and ``be`` among them. Building
    everything costs about twice the tokens and removes both the threshold and
    the blind spot. The table is kept for reference, not for filtering.
    """
    rows = get_connection("content").execute(
        "SELECT s.headword FROM sense_screening s"
        " LEFT JOIN senses n ON n.headword = s.headword"
        " WHERE n.id IS NULL"
        " GROUP BY s.headword ORDER BY s.headword LIMIT ?",
        (limit,),
    ).fetchall()
    return [row["headword"] for row in rows]


def add_examples(sense_id: int, examples: list[dict[str, str]], *, source: str = "llm") -> int:
    conn = get_connection("content")
    conn.executemany(
        "INSERT INTO sense_examples (sense_id, text_en, gloss_zh, source, created_at)"
        " VALUES (?,?,?,?,?)",
        [
            (sense_id, item["text_en"], item.get("gloss_zh"), source, _now())
            for item in examples
        ],
    )
    conn.commit()
    return len(examples)


def examples_of(sense_id: int) -> list[dict[str, Any]]:
    try:
        rows = get_connection("content").execute(
            "SELECT id, text_en, gloss_zh, source FROM sense_examples"
            " WHERE sense_id = ? ORDER BY id",
            (sense_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def stats() -> dict[str, Any]:
    try:
        conn = get_connection("content")
        words = conn.execute(
            "SELECT COUNT(DISTINCT headword) AS n FROM senses"
        ).fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM senses").fetchone()["n"]
        examples = conn.execute(
            "SELECT COUNT(*) AS n FROM sense_examples"
        ).fetchone()["n"]
        by_topic = {
            row["topic"] or "未标": row["n"]
            for row in conn.execute(
                "SELECT topic, COUNT(*) AS n FROM senses GROUP BY topic ORDER BY n DESC"
            )
        }
    except sqlite3.Error:
        return {"words": 0, "senses": 0, "examples": 0, "by_topic": {}}
    return {
        "words": int(words),
        "senses": int(total),
        "examples": int(examples),
        "per_word": round(total / words, 2) if words else 0,
        "by_topic": by_topic,
    }


def words_by_topic(topic: str, limit: int = 200) -> list[str]:
    """Target words whose main sense belongs to a topic.

    This is what lets a batch of target words be picked around a subject instead
    of at random — the fix for models having to force eight unrelated words into
    one article.
    """
    rows = get_connection("content").execute(
        "SELECT DISTINCT headword FROM senses WHERE topic = ? AND ordinal = 1"
        " ORDER BY headword LIMIT ?",
        (topic, limit),
    ).fetchall()
    return [row["headword"] for row in rows]
