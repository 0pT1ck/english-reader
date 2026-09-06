"""Three checks on a finished sense set, all of them free.

None of them needs a model, which is the point: a validation pass that costs as
much as the generation it validates would not get run.

1. **Shape.** A word with eight senses is not a word with eight senses; it is a
   granularity failure. Words with none at all are the other failure.
2. **Definition vocabulary.** The English concept definition exists so that
   reading it is itself reading practice. A definition containing words harder
   than the word being defined fails at its one job.
3. **Exam coverage.** Every target word is checked against 452 real papers. A
   word the papers use but our set has no plausible sense for is a gap; a word
   the papers never use at all is worth knowing about too. Full sense-level
   annotation of the corpus is a later phase — this is the spot check, not the
   statistics.
"""

from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.core.config import get_settings
from backend.core.db import get_connection
from backend.modules.senses import repository
from backend.modules.vocabulary import repository as dictionary

# A sensible ceiling for how finely a single word divides. Past this, the model
# has started splitting on Chinese wording rather than on English concepts.
MAX_SENSES = 5

# The definition may use words this common. Rank rather than a word list: the
# top 2,000 by frequency is the same idea and needs no extra data.
DEFINITION_VOCABULARY_RANK = 2000

_WORD = re.compile(r"[a-zA-Z][a-zA-Z'-]*")


@lru_cache(maxsize=20000)
def _too_hard(word: str) -> bool:
    """Whether one word in a definition is beyond the reader.

    Inflections have to be resolved first. ``ideas``, ``things``, ``helps`` and
    ``caring`` are not headwords, so a raw lookup calls every one of them hard
    and reports two thirds of all definitions as offenders — a false alarm that
    would send us chasing a model problem that does not exist.
    """
    word = word.lower()
    # The possessive is not a different word, and `cannot` is two.
    stripped = word[:-2] if word.endswith("'s") else word
    if stripped == "cannot":
        stripped = "can"

    for candidate in (stripped, dictionary.resolve_surface(stripped)):
        if not candidate:
            continue
        entry = dictionary.lookup(candidate)
        if entry is None:
            continue
        if dictionary.tags_of(candidate) & {"zk", "gk"}:
            return False
        rank = entry["frq"] or entry["bnc"] or 0
        if 0 < rank <= DEFINITION_VOCABULARY_RANK:
            return False
    return True


def check_shape() -> dict[str, Any]:
    rows = get_connection("content").execute(
        "SELECT headword, COUNT(*) AS n FROM senses GROUP BY headword"
    ).fetchall()
    counts = Counter(row["n"] for row in rows)
    too_many = [row["headword"] for row in rows if row["n"] > MAX_SENSES]
    missing = repository.pending_words(limit=100000)
    return {
        "words": len(rows),
        "distribution": dict(sorted(counts.items())),
        "too_many": sorted(too_many)[:100],
        "too_many_count": len(too_many),
        "missing_count": len(missing),
        "missing": missing[:100],
    }


def check_definitions(limit: int = 100000) -> dict[str, Any]:
    rows = get_connection("content").execute(
        "SELECT id, headword, concept_en FROM senses ORDER BY headword LIMIT ?",
        (limit,),
    ).fetchall()

    offenders: list[dict[str, Any]] = []
    hard_words: Counter[str] = Counter()
    for row in rows:
        hard = sorted({
            word.lower() for word in _WORD.findall(row["concept_en"])
            if len(word) > 2 and _too_hard(word)
        })
        if hard:
            hard_words.update(hard)
            offenders.append(
                {"headword": row["headword"], "concept_en": row["concept_en"], "hard": hard}
            )

    return {
        "checked": len(rows),
        "offenders": offenders[:100],
        "offenders_count": len(offenders),
        "rate": round(100 * len(offenders) / len(rows), 1) if rows else 0,
        "common_offenders": hard_words.most_common(30),
    }


@lru_cache(maxsize=1)
def _corpus_counts() -> Counter[str]:
    """How often each surface form appears across the exam corpus.

    Surface counts rather than lemmas on purpose: this is a coverage check, and
    running the whole corpus through the parser to answer "does this word ever
    appear" would cost minutes for no extra accuracy.
    """
    counts: Counter[str] = Counter()
    root = get_settings().data_dir / "exam_papers"
    if not root.exists():
        return counts
    for path in root.rglob("*.txt"):
        counts.update(word.lower() for word in _WORD.findall(path.read_text(encoding="utf-8")))
    return counts


def check_exam_coverage(sample: int = 40) -> dict[str, Any]:
    counts = _corpus_counts()
    rows = get_connection("content").execute(
        "SELECT DISTINCT headword FROM senses ORDER BY headword"
    ).fetchall()
    words = [row["headword"] for row in rows]

    unseen = []
    for word in words:
        if counts.get(word, 0):
            continue
        # Inflected forms count: `analyse` may only ever appear as `analysed`.
        forms = get_connection("dictionary").execute(
            "SELECT surface FROM word_forms WHERE headword = ?", (word,)
        ).fetchall()
        if not any(counts.get(form["surface"], 0) for form in forms):
            unseen.append(word)

    return {
        "papers": _paper_count(),
        "words_with_senses": len(words),
        "never_in_papers": len(unseen),
        "never_in_papers_sample": unseen[:sample],
    }


def _paper_count() -> int:
    root: Path = get_settings().data_dir / "exam_papers"
    return len(list(root.rglob("*.txt"))) if root.exists() else 0


def sample_usages(word: str, limit: int = 5) -> list[str]:
    """Real sentences from the papers, for eyeballing a sense set against use."""
    pattern = re.compile(rf"\b{re.escape(word)}\w*\b", re.IGNORECASE)
    found: list[str] = []
    root = get_settings().data_dir / "exam_papers"
    if not root.exists():
        return found
    for path in sorted(root.rglob("*.txt")):
        for sentence in re.split(r"(?<=[.!?])\s+", path.read_text(encoding="utf-8")):
            if pattern.search(sentence):
                found.append(" ".join(sentence.split())[:300])
                if len(found) >= limit:
                    return found
    return found


def run_all() -> dict[str, Any]:
    return {
        "shape": check_shape(),
        "definitions": check_definitions(),
        "exam": check_exam_coverage(),
    }
