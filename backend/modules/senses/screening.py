"""The free half of the work: decide which words need a sense set at all.

Better than half, as it turns out. Of roughly six thousand target words, more
than three thousand have one part of speech and a handful of glosses that all
say the same thing — ``against``, ``happen``, ``area``, ``fact``. Sending those
to a model would be paying to be told they have one meaning.

The signal is the structure of ECDICT's own translation field. It marks part of
speech per line, so a word whose glosses span three parts of speech is almost
certainly carrying distinct concepts, while a single-part-of-speech word with
three near-synonymous glosses is almost certainly not.

This is a filter, not a judgement. It decides *who gets asked*; how many senses
a word actually has is the model's answer, and a word wrongly filtered out here
can be topped up individually from the console for a fraction of a cent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from backend.core.db import get_connection
from backend.core.logging import get_logger

log = get_logger("senses.screening")

# Every syllabus up to and including the targets — the secondary-school ones
# too, deliberately.
#
# It is tempting to screen only the words being learned, but that removes
# exactly the words this system exists to teach a second time. `present`,
# `spring`, `bear`, `run` and `set` carry no CET tag: they are all secondary
# vocabulary, "already known". Knowing them at the word level says nothing about
# knowing 呈交 for `present` or 承受 for `bear` — and a familiar word used in an
# unfamiliar sense is what CET reading tests hardest. A sense set built only
# from the unknown words would have nothing to say about any of them.
TARGET_TAGS = ("zk", "gk", "cet4", "cet6", "ky")

# Thresholds. A single-part-of-speech word needs this many distinct glosses
# before it is worth asking about — below it, the glosses are near-synonyms of
# one concept rather than separate senses.
MANY_GLOSSES = 7

_POS_MARKER = re.compile(r"^\s*([a-z]{1,4})\.")
_GLOSS_SPLIT = re.compile(r"[,，;；]")

CATEGORY_LABELS = {
    "multi_pos": "多词性 · 强候选",
    "dual_pos": "双词性 · 待判",
    "many_glosses": "单词性多义",
    "simple": "简单 · 跳过",
}


@dataclass(frozen=True)
class Verdict:
    headword: str
    category: str
    pos_count: int
    gloss_items: int

    @property
    def needs_senses(self) -> bool:
        return self.category != "simple"


def profile(translation: str | None) -> tuple[int, int]:
    """How many parts of speech and how many distinct glosses a word carries."""
    parts: set[str] = set()
    items = 0
    for line in (translation or "").split("\n"):
        match = _POS_MARKER.match(line)
        if match:
            parts.add(match.group(1))
        body = _POS_MARKER.sub("", line)
        items += len([piece for piece in _GLOSS_SPLIT.split(body) if piece.strip()])
    return len(parts), items


def classify(headword: str, translation: str | None) -> Verdict:
    pos_count, gloss_items = profile(translation)
    if pos_count >= 3:
        category = "multi_pos"
    elif pos_count == 2:
        category = "dual_pos"
    elif gloss_items >= MANY_GLOSSES:
        category = "many_glosses"
    else:
        category = "simple"
    return Verdict(headword, category, pos_count, gloss_items)


def target_words() -> list[tuple[str, str | None]]:
    tag_clause = " OR ".join(f"tags LIKE '%{tag}%'" for tag in TARGET_TAGS)
    rows = get_connection("dictionary").execute(
        f"SELECT headword, translation FROM words"
        f" WHERE ({tag_clause}) AND frq > 0 ORDER BY headword"
    ).fetchall()
    return [(row["headword"], row["translation"]) for row in rows]


def run() -> dict[str, int]:
    """Screen every target word and store the verdicts."""
    verdicts = [classify(word, translation) for word, translation in target_words()]

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = get_connection("content")
    conn.executemany(
        "INSERT INTO sense_screening (headword, category, pos_count, gloss_items,"
        " needs_senses, screened_at) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(headword) DO UPDATE SET category = excluded.category,"
        " pos_count = excluded.pos_count, gloss_items = excluded.gloss_items,"
        " needs_senses = excluded.needs_senses, screened_at = excluded.screened_at",
        [
            (v.headword, v.category, v.pos_count, v.gloss_items,
             1 if v.needs_senses else 0, now)
            for v in verdicts
        ],
    )
    conn.commit()

    counts: dict[str, int] = {}
    for verdict in verdicts:
        counts[verdict.category] = counts.get(verdict.category, 0) + 1
    counts["total"] = len(verdicts)
    counts["needs_senses"] = sum(1 for v in verdicts if v.needs_senses)

    log.info(
        "senses.screened",
        f"粗筛完成：{counts['total']} 个目标词，其中 {counts['needs_senses']} 个需要建义项集",
        **counts,
    )
    return counts


def stats() -> dict[str, int]:
    try:
        rows = get_connection("content").execute(
            "SELECT category, COUNT(*) AS n FROM sense_screening GROUP BY category"
        ).fetchall()
    except Exception:  # noqa: BLE001 - before the table exists
        return {}
    counts = {row["category"]: row["n"] for row in rows}
    counts["total"] = sum(counts.values())
    counts["needs_senses"] = counts["total"] - counts.get("simple", 0)
    return counts
