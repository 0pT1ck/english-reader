"""Reading the Wiktionary checklist for one word.

The list has to be trimmed before a model sees it — ``run`` has 113 current
senses — and *how* it is trimmed decides what survives.

Two rules, both learned the hard way:

**Trim per part of speech, not across the whole list.** Sorting a word's senses
into one sequence puts every adjective sense before every verb sense, so the
first twenty-five senses of ``run`` are ``melted or molten``, ``cast in a
mould``, ``smuggled`` — and the verb everyone knows never makes the cut.

**Do not trim by topic label.** It looks like the obvious way to drop jargon,
and it drops ``bank``'s river bank, which Wiktionary files under
``geography hydrology``. Only the obsolete/archaic tags are safe to filter on,
and those are already marked at import.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from backend.core.db import get_connection

#: Senses offered per part of speech. Twelve covers every ordinary word — the
#: median word has six senses in total — and keeps the worst offenders inside a
#: prompt a model can actually attend to.
PER_POS_LIMIT = 12


@dataclass(frozen=True)
class Candidate:
    id: int
    pos: str
    gloss: str
    topics: str

    def as_line(self, number: int) -> str:
        # The topic is shown rather than filtered on: it is a useful hint that a
        # sense may be jargon, and the model is better placed than a rule to
        # decide whether this particular one still belongs in general prose.
        hint = f" [{self.topics}]" if self.topics else ""
        return f"{number}. ({self.pos}){hint} {self.gloss}"


def candidates(headword: str, *, per_pos: int = PER_POS_LIMIT) -> list[Candidate]:
    """Current senses for one word, trimmed evenly across parts of speech."""
    try:
        rows = get_connection("content").execute(
            "SELECT id, pos, gloss, topics FROM wiktionary_senses"
            " WHERE headword = ? AND is_dead = 0 ORDER BY pos, ordinal",
            (headword.lower(),),
        ).fetchall()
    except sqlite3.Error:
        return []

    by_pos: dict[str, list[Candidate]] = {}
    for row in rows:
        bucket = by_pos.setdefault(row["pos"] or "?", [])
        if len(bucket) < per_pos:
            bucket.append(
                Candidate(row["id"], row["pos"] or "?", row["gloss"], row["topics"] or "")
            )

    # Nouns and verbs first: that is where the senses a reader meets live, and
    # a model reads the top of a list more carefully than the bottom.
    order = {"noun": 0, "verb": 1, "adj": 2, "adv": 3}
    out: list[Candidate] = []
    for pos in sorted(by_pos, key=lambda p: order.get(p, 9)):
        out.extend(by_pos[pos])
    return out


def coverage_report(limit: int = 200) -> list[dict[str, Any]]:
    """Wiktionary senses that no sense of ours claims to cover.

    The missing-sense detector P1b had no way to build. It is a plain query
    rather than a judgement because every generated sense records which source
    senses it accounts for.
    """
    rows = get_connection("content").execute(
        """
        SELECT w.headword, w.pos, w.gloss, w.topics
        FROM wiktionary_senses w
        WHERE w.is_dead = 0
          AND EXISTS (SELECT 1 FROM senses s WHERE s.headword = w.headword)
          AND NOT EXISTS (
                SELECT 1 FROM senses s
                WHERE s.headword = w.headword
                  AND s.covers IS NOT NULL
                  AND ',' || replace(replace(replace(s.covers,'[',''),']',''),' ','') || ','
                      LIKE '%,' || w.id || ',%')
        ORDER BY w.headword, w.ordinal LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
