"""Local checks on a generated draft — a health report, not a verdict.

P1a deliberately sets no thresholds and rejects nothing. The standards are not
calibrated yet: the assumed-known word list is a guess, the syntax baseline was
just measured and not validated, and the out-of-syllabus count carries known
noise (hyphenated compounds get split, spelling variants are missed). Gating on
numbers in that state would reject good articles, pass bad ones, and leave no
way to tell whether the article or the threshold was at fault.

So the checker reports. **The first batch calibrates the checker as much as it
calibrates the prompt.**

Syntax analysis needs spaCy's dependency parser, which the vocabulary module
disables for speed — it only ever needed part-of-speech tags. Loading a second
pipeline here costs about 50 MB and keeps the two modules independent, which is
worth more than the memory on a 4 GB box.
"""

from __future__ import annotations

import json
import statistics
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.modules.vocabulary import analyzer, repository

log = get_logger("generation.checker")

_nlp = None
_lock = threading.Lock()

# Nominalisation is a hallmark of exam prose ("the implementation of the
# policy"). Suffix matching is crude but adequate for a comparative measure —
# both the baseline and the drafts are counted the same way.
NOMINAL_SUFFIXES = ("tion", "sion", "ment", "ness", "ity", "ance", "ence", "ism")

DEP_LABELS = {
    "relcl": "定语从句",
    "ccomp": "宾语从句",
    "xcomp": "非限定补语",
    "advcl": "状语从句",
    "acl": "分词/不定式修饰",
    "expl": "形式主语",
    "passive": "被动语态",
    "nominal": "名词化结构",
}


def nlp():
    """Full pipeline, parser included."""
    global _nlp
    if _nlp is None:
        with _lock:
            if _nlp is None:
                import spacy

                _nlp = spacy.load("en_core_web_sm", exclude=["ner"])
    return _nlp


@dataclass
class Report:
    words: int = 0
    sentences: int = 0
    avg_sentence: float = 0.0
    max_sentence: int = 0
    paragraphs: int = 0

    distinct_lemmas: int = 0
    proper_nouns: int = 0

    target_hits: dict[str, bool] = field(default_factory=dict)
    beyond: list[dict[str, Any]] = field(default_factory=list)
    beyond_rate: float = 0.0

    syntax: dict[str, float] = field(default_factory=dict)
    baseline: dict[str, float] = field(default_factory=dict)
    max_depth: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "words": self.words,
            "sentences": self.sentences,
            "avg_sentence": round(self.avg_sentence, 1),
            "max_sentence": self.max_sentence,
            "paragraphs": self.paragraphs,
            "distinct_lemmas": self.distinct_lemmas,
            "proper_nouns": self.proper_nouns,
            "target_hits": self.target_hits,
            "targets_used": sum(1 for v in self.target_hits.values() if v),
            "targets_total": len(self.target_hits),
            "beyond": self.beyond,
            "beyond_rate": round(self.beyond_rate, 2),
            "syntax": {k: round(v, 2) for k, v in self.syntax.items()},
            "baseline": {k: round(v, 2) for k, v in self.baseline.items()},
            "max_depth": self.max_depth,
        }


def _syntax_counts(doc) -> tuple[Counter, int]:
    """Count structures, and find the deepest dependency chain.

    Depth matters more than any single structure: three clauses side by side
    read far easier than three nested inside one another, and it is nesting
    that makes exam sentences hard.
    """
    counts: Counter[str] = Counter()
    deepest = 0

    for token in doc:
        dep = token.dep_
        if dep in ("relcl", "ccomp", "xcomp", "advcl", "acl"):
            counts[dep] += 1
        elif dep == "expl":
            counts["expl"] += 1
        if dep in ("nsubjpass", "auxpass"):
            counts["passive"] += 1
        if token.pos_ == "NOUN" and token.lemma_.lower().endswith(NOMINAL_SUFFIXES):
            counts["nominal"] += 1

        # Compare indices, not objects: spaCy Tokens are lightweight proxies,
        # so `node.head is node` can be false for the root and the walk never
        # terminates — which is how this silently reported a depth of 40.
        depth, node = 0, token
        while node.head.i != node.i and depth < 50:
            depth += 1
            node = node.head
        deepest = max(deepest, depth)

    return counts, deepest


def _per_thousand(counts: Counter, words: int) -> dict[str, float]:
    if not words:
        return {}
    return {key: 1000 * value / words for key, value in counts.items()}


# --------------------------------------------------------------------------- #
# Baseline from real exam papers
# --------------------------------------------------------------------------- #


def _baseline_path() -> Path:
    return get_settings().data_dir / "syntax_baseline.json"


def baseline(exam: str = "cet4", sample: int = 40) -> dict[str, float]:
    """Structure frequencies in real papers, per thousand words.

    Computed once and cached to disk — parsing forty passages takes a while and
    the corpus does not change. Delete the file to recompute.
    """
    path = _baseline_path()
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if exam in cached:
                return cached[exam]
        except (OSError, json.JSONDecodeError):
            cached = {}
    else:
        cached = {}

    papers = sorted((get_settings().data_dir / "exam_papers" / exam).glob("*.txt"))
    if not papers:
        log.warning("baseline.missing", f"没有 {exam} 的真题语料，无法计算句法基线", exam=exam)
        return {}

    step = max(1, len(papers) // sample)
    papers = papers[::step][:sample]

    totals: Counter[str] = Counter()
    words = 0
    sentence_lengths: list[int] = []
    depths: list[int] = []

    for path_ in papers:
        doc = nlp()(path_.read_text(encoding="utf-8"))
        counts, deepest = _syntax_counts(doc)
        totals.update(counts)
        words += sum(1 for t in doc if t.is_alpha)
        depths.append(deepest)
        sentence_lengths += [sum(1 for t in s if t.is_alpha) for s in doc.sents]

    result = _per_thousand(totals, words)
    result["_avg_sentence"] = statistics.mean(sentence_lengths) if sentence_lengths else 0
    result["_max_depth"] = statistics.mean(depths) if depths else 0
    result["_papers"] = len(papers)

    cached[exam] = result
    try:
        path.write_text(json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    log.info(
        "baseline.computed",
        f"根据 {len(papers)} 篇 {exam} 真题计算了句法基线",
        exam=exam,
        papers=len(papers),
    )
    return result


# --------------------------------------------------------------------------- #
# Checking a draft
# --------------------------------------------------------------------------- #


def check(
    body: str,
    *,
    target_words: list[str],
    allowed_tiers: list[str],
    exam: str = "cet4",
) -> Report:
    """Produce the health report for one draft."""
    report = Report()
    doc = nlp()(body)

    words = [t for t in doc if t.is_alpha]
    report.words = len(words)
    report.paragraphs = len([p for p in body.split("\n\n") if p.strip()])

    lengths = [sum(1 for t in s if t.is_alpha) for s in doc.sents]
    lengths = [n for n in lengths if n]
    report.sentences = len(lengths)
    report.avg_sentence = statistics.mean(lengths) if lengths else 0
    report.max_sentence = max(lengths) if lengths else 0

    allowed = set(allowed_tiers)
    targets = {w.lower() for w in target_words}
    hits = {w: False for w in target_words}

    content_lemmas: set[str] = set()
    beyond: dict[str, dict[str, Any]] = {}
    content_total = 0

    for token in doc:
        if not token.is_alpha:
            continue
        if token.pos_ == "PROPN":
            report.proper_nouns += 1
            continue
        if token.pos_ not in ("NOUN", "VERB", "ADJ", "ADV"):
            continue

        # Same resolution strategy as article ingest, so a word counted as
        # out-of-range here is the same word the reader would see marked.
        lemma, _source = analyzer.resolve_lemma(token)
        content_lemmas.add(lemma)
        content_total += 1

        if lemma in targets:
            for original in target_words:
                if original.lower() == lemma:
                    hits[original] = True

        entry = repository.lookup(lemma)
        if entry is None:
            # The lemmatiser produced something the dictionary does not know —
            # usually a participle used adjectivally (cutting, rounded), where
            # it resolves to a form that is not a headword. The dictionary's own
            # inflection table maps the surface form back. Without this the word
            # is falsely counted as out-of-range, inflating the whole figure.
            fallback = repository.resolve_surface(token.text.lower())
            if fallback:
                lemma = fallback
                entry = repository.lookup(fallback)

        tags = set((entry["tags"] or "").split()) if entry else set()
        if not entry or not (tags & allowed):
            record = beyond.setdefault(
                lemma,
                {"lemma": lemma, "count": 0, "in_dictionary": entry is not None,
                 "tags": entry["tags"] if entry else None},
            )
            record["count"] += 1

    report.distinct_lemmas = len(content_lemmas)
    report.target_hits = hits
    # Target words are meant to be new, so they are not counted as violations —
    # they are the point of the article.
    report.beyond = sorted(
        (r for r in beyond.values() if r["lemma"] not in targets),
        key=lambda r: -r["count"],
    )
    report.beyond_rate = (
        100 * sum(r["count"] for r in report.beyond) / content_total if content_total else 0
    )

    counts, deepest = _syntax_counts(doc)
    report.syntax = _per_thousand(counts, report.words)
    report.max_depth = deepest
    report.baseline = baseline(exam)

    log.info(
        "draft.checked",
        f"校验完成：{report.words} 词，超出范围 {len(report.beyond)} 个词条，"
        f"目标词命中 {sum(hits.values())}/{len(hits)}",
        words=report.words,
        beyond=len(report.beyond),
        targets_used=sum(hits.values()),
    )
    return report
