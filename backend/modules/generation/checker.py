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
    #: How many target words landed in each paragraph, in order. Clustering is
    #: as much a defect as the wrong total: twenty-five new words are learnable
    #: spread five to a paragraph and unreadable piled into the opening.
    targets_per_paragraph: list[int] = field(default_factory=list)
    beyond: list[dict[str, Any]] = field(default_factory=list)
    beyond_rate: float = 0.0
    # Words outside the syllabus that are B-grade derivations of words inside
    # it. Reported apart from `beyond` because they are not violations — the
    # design treats them as light target words worth a third of a slot.
    derived: list[dict[str, Any]] = field(default_factory=list)
    derived_rate: float = 0.0

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
            "targets_per_paragraph": self.targets_per_paragraph,
            "beyond": self.beyond,
            "beyond_rate": round(self.beyond_rate, 2),
            "derived": self.derived,
            "derived_rate": round(self.derived_rate, 2),
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
            # `there is / there are` only. spaCy reserves `expl` for that.
            counts["there_be"] += 1

        # The anticipatory `it` — "It is clear that…", "It has become difficult
        # to…" — is what exam prose is full of and what a model avoids, but
        # spaCy labels that `it` an ordinary nsubj, not an expletive. Counting
        # `expl` and calling it 形式主语 measured `there be` instead, which sent
        # three rounds of prompt changes chasing a number that was never
        # describing the thing being changed.
        if (
            token.lower_ == "it"
            and token.dep_ == "nsubj"
            and any(
                child.dep_ in ("ccomp", "csubj", "xcomp", "acomp", "attr", "advcl")
                for child in token.head.children
            )
        ):
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


def _by_surface(token: Any) -> Any:
    """Last-resort lookup through the dictionary's own inflection table.

    Covers the case where the lemmatiser lands on something that is not a
    headword — usually a participle used adjectivally (``cutting``, ``rounded``).
    Without it those are falsely counted as out-of-range, which inflated the
    whole figure in the first round.
    """
    fallback = repository.resolve_surface(token.text.lower())
    return repository.lookup(fallback) if fallback else None


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
    derived: dict[str, dict[str, Any]] = {}
    content_total = 0

    # A hyphenated compound is one word that arrives as three tokens. Judged
    # whole where the dictionary knows it, part by part where it does not —
    # otherwise `socio-economic` contributes a bogus `socio` to the count.
    spans = analyzer.hyphenated_spans(doc)
    handled_spans: set[tuple[int, int]] = set()

    def record_beyond(lemma: str, entry: Any, surface: str) -> None:
        root = known_root(lemma, surface)
        bucket = derived if root else beyond
        # Report the form as written when the lemma is not a real word: a draft
        # containing `lighthearted` should say so, not `lighthearte`.
        label = lemma if entry is not None else surface
        record = bucket.setdefault(
            label,
            {"lemma": label, "count": 0, "in_dictionary": entry is not None,
             "tags": " ".join(sorted(repository.tags_of(lemma))) or None,
             "root": root},
        )
        record["count"] += 1

    def known_root(lemma: str, surface: str) -> str | None:
        """An in-range root this word is a B-grade derivation of.

        Kept apart from the out-of-syllabus count on purpose. A B-grade
        derivation is not a violation — the design treats it as a light target
        word: shown with its breakdown, worth a third of a slot. Counting
        ``cooperation`` as out of syllabus when ``cooperate`` is a CET-4 word
        would make the zero-out-of-syllabus goal unreachable and meaningless.
        """
        from backend.modules.wordfamily import repository as families

        for candidate in (lemma, surface):
            family = families.family_of(candidate)
            if family and family["grade"] == "B":
                root = str(family["root"])
                if repository.tags_of(root) & allowed:
                    return root
        return None

    def in_range(lemma: str, surface: str) -> bool:
        """Whether a word counts as inside the allowed vocabulary.

        Tags are unioned across spelling variants — ECDICT tags ``neighbour``
        cet4 and ``neighbor`` not, and to a reader they are the same word — and
        the surface form is checked too: ``data`` carries the syllabus tag while
        its lemma ``datum`` does not, and it is ``data`` that appears on the page.
        """
        if repository.tags_of(lemma) & allowed:
            return True
        if surface != lemma and repository.tags_of(surface) & allowed:
            return True
        # `planning` and `debating` are headwords in their own right, untagged,
        # so the lemmatiser never looks further — but the dictionary's own
        # inflection table maps them back to `plan` and `debate`. A reader who
        # knows the verb is not meeting a new word.
        base = repository.resolve_surface(surface)
        if base and repository.tags_of(base) & allowed:
            return True
        # Grade-A derivations are grammar, not vocabulary: a reader who knows
        # `careful` is not meeting a new word in `carefully`, and no syllabus
        # bothers to list it. Imported here rather than at module scope so the
        # checker still works if the word-family module is absent.
        from backend.modules.wordfamily import repository as families

        root = families.transparent_root(lemma) or families.transparent_root(surface)
        return bool(root and repository.tags_of(root) & allowed)

    def judge(lemma: str, entry: Any, surface: str) -> None:
        """Count one resolved word, and record it if it is out of range."""
        nonlocal content_total
        content_lemmas.add(lemma)
        content_total += 1

        if lemma in targets:
            for original in target_words:
                if original.lower() == lemma:
                    hits[original] = True

        if not entry or not in_range(lemma, surface):
            record_beyond(lemma, entry, surface)

    for token in doc:
        if not token.is_alpha:
            continue

        span = spans.get(token.i)
        if span is not None:
            if span in handled_spans:
                continue
            handled_spans.add(span)

            hyphenated, merged = analyzer.compound_forms(doc, span)

            # Judge the parts first: a compound every part of which is in range
            # is transparent to the reader (`well-timed`, `record-keeping`) even
            # when the dictionary happens not to tag the compound itself.
            parts = [
                part for part in list(doc)[span[0]:span[1]]
                if part.is_alpha and part.text.lower() not in analyzer.COMBINING_FORMS
            ]
            part_lemmas = []
            for part in parts:
                lemma, _source = analyzer.resolve_lemma(part)
                part_entry = repository.lookup(lemma) or _by_surface(part)
                if part_entry is not None:
                    lemma = part_entry["headword"]
                part_lemmas.append((lemma, part_entry, part.text.lower()))

            transparent = bool(part_lemmas) and all(
                entry is not None and in_range(lemma, surface)
                for lemma, entry, surface in part_lemmas
            )

            entry = repository.lookup(hyphenated) or repository.lookup(merged)
            if entry is not None or transparent:
                content_lemmas.add(hyphenated)
                content_total += 1
                if not transparent and not in_range(
                    entry["headword"], hyphenated  # type: ignore[index]
                ):
                    record_beyond(hyphenated, entry, hyphenated)
                continue

            # Opaque compound the dictionary does not know: judge the parts on
            # their own, skipping the bound morphemes, which are not words and
            # belong to no syllabus.
            for lemma, part_entry, surface in part_lemmas:
                judge(lemma, part_entry, surface)
            continue

        if token.pos_ == "PROPN":
            report.proper_nouns += 1
            continue
        if token.pos_ not in ("NOUN", "VERB", "ADJ", "ADV"):
            continue

        # Same resolution strategy as article ingest, so a word counted as
        # out-of-range here is the same word the reader would see marked.
        lemma, _source = analyzer.resolve_lemma(token)
        entry = repository.lookup(lemma) or _by_surface(token)
        if entry is not None:
            lemma = entry["headword"]
        judge(lemma, entry, token.text)

    report.distinct_lemmas = len(content_lemmas)
    report.target_hits = hits

    # Counted by re-reading the paragraphs rather than by tracking character
    # offsets through the token loop above: the loop already juggles compounds,
    # spelling variants and inflections, and paragraph position is independent
    # of all of it.
    for paragraph in [p for p in body.split("\n\n") if p.strip()]:
        found = 0
        for token in nlp()(paragraph):
            if not token.is_alpha:
                continue
            lemma, _source = analyzer.resolve_lemma(token)
            entry = repository.lookup(lemma) or _by_surface(token)
            if entry is not None:
                lemma = entry["headword"]
            if lemma in targets or token.text.lower() in targets:
                found += 1
        report.targets_per_paragraph.append(found)

    def summarise(bucket: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], float]:
        # Target words are meant to be new, so they are never violations — they
        # are the point of the article.
        rows = sorted(
            (r for r in bucket.values() if r["lemma"] not in targets),
            key=lambda r: -r["count"],
        )
        rate = 100 * sum(r["count"] for r in rows) / content_total if content_total else 0
        return rows, rate

    report.beyond, report.beyond_rate = summarise(beyond)
    report.derived, report.derived_rate = summarise(derived)

    counts, deepest = _syntax_counts(doc)
    report.syntax = _per_thousand(counts, report.words)
    report.max_depth = deepest
    report.baseline = baseline(exam)

    log.info(
        "draft.checked",
        f"校验完成：{report.words} 词，超纲 {len(report.beyond)} 个词条，"
        f"已知词根的派生词 {len(report.derived)} 个，"
        f"目标词命中 {sum(hits.values())}/{len(hits)}",
        words=report.words,
        beyond=len(report.beyond),
        derived=len(report.derived),
        targets_used=sum(hits.values()),
    )
    return report
