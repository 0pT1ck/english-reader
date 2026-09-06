"""The rule pass: derive families for the whole working vocabulary, for free.

Scope is every reasonably common word, not just the syllabus words. That is the
point — the words this fixes are precisely the ones *missing* from every
syllabus. ``carefully``, ``readiness`` and ``supervisor`` carry no tag, which is
why they were counted as beyond the syllabus in the first round; the syllabus
lists their roots and assumes the rest is grammar.

A frequency ceiling keeps the pass to the words a learner could actually meet.
Everything past it is technical vocabulary, proper nouns and noise.
"""

from __future__ import annotations

from backend.core.db import get_connection
from backend.core.logging import get_logger
from backend.modules.wordfamily import derive, repository

log = get_logger("wordfamily.build")

# Rank ceiling in either frequency list. 30,000 covers about 35,000 headwords —
# comfortably past anything a CET or postgraduate paper uses.
FREQUENCY_CEILING = 30_000


def candidate_words(ceiling: int = FREQUENCY_CEILING) -> list[str]:
    rows = get_connection("dictionary").execute(
        "SELECT headword FROM words"
        " WHERE (frq > 0 AND frq <= ?) OR (bnc > 0 AND bnc <= ?)"
        " ORDER BY headword",
        (ceiling, ceiling),
    ).fetchall()
    return [row["headword"] for row in rows]


def run(ceiling: int = FREQUENCY_CEILING) -> dict[str, int]:
    """Analyse every candidate and store what the rules find."""
    words = candidate_words(ceiling)
    derivations = derive.analyse_all(words)
    repository.store_families(derivations, source="rule")

    grades: dict[str, int] = {}
    for derivation in derivations:
        grades[derivation.grade] = grades.get(derivation.grade, 0) + 1

    log.info(
        "wordfamily.rules.done",
        f"规则分解完成：{len(words)} 个词里找到 {len(derivations)} 条派生关系",
        examined=len(words),
        found=len(derivations),
        **{f"grade_{k.lower()}": v for k, v in grades.items()},
    )
    return {"examined": len(words), "found": len(derivations), **grades}
