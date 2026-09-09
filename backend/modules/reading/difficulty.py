"""How hard an article is — the article, not the learner.

P2 can only describe the text. "How hard is this *for you*" needs the ability
estimate, which is P3; the slot for it is reserved in the client contract and
left null until then.

**The indicators were measured before being chosen, and the first attempt was
wrong.** Twenty-five papers from each of CET-4 / CET-6 / 考研 were scored, and
the question asked of every candidate indicator was: does it rise across the
three known levels? Result:

    指标            四级    六级   考研
    平均句长        19.0    22.0   19.6    ← no (CET-6 longest, 考研 back down)
    被动 / 从句密度        三者基本相同     ← no
    超四级词比例    10.2%   13.8%  13.5%   ← yes
    超六级词比例     7.6%    8.8%   9.5%   ← yes
    词频中位数       560     764    767    ← yes
    词频 90 分位    3888    5341   5415    ← yes
    生僻词比例       8.2%   10.6%  11.3%   ← yes

So difficulty here is vocabulary, and only vocabulary. Syntax indicators are
still computed and still sortable — they say something about a text — but they
are kept out of the composite, where they would only dilute the signal. This
also happens to confirm the project's founding premise: the learner's grammar
already covers every structure in the exam, and only the words are hard.

**The measurement error, recorded because it nearly shipped.** The first version
counted "words whose ``tags`` do not contain ``cet4``" and produced 58.2% /
57.1% / 56.9% — CET-4 papers coming out hardest. ECDICT's tags list *which word
lists a word appears on*, and the lists are levelled: ``the`` carries ``zk gk``
and no ``cet4`` at all. Within CET-4 level therefore means carrying **any** of
zk / gk / cet4. This is the second time a metric in this project measured
something other than what its name said, so: any new indicator gets checked
against a sample with a known answer before it is trusted, and the three exam
levels are that known answer.
"""

from __future__ import annotations

import json
import statistics
from typing import Any

from backend.core import runtime_config
from backend.core.logging import get_logger
from backend.modules.vocabulary import syllabus
from backend.modules.vocabulary.analyzer import SentenceAnalysis, TokenAnalysis

log = get_logger("reading.difficulty")

#: Re-exported so callers keep one import. The sets, and every rule for
#: reading them, live in :mod:`..vocabulary.syllabus` — this module measures
#: articles and does not get to have its own opinion about what a syllabus is.
WITHIN_CET4 = syllabus.WITHIN_CET4
WITHIN_CET6 = syllabus.WITHIN_CET6

#: Frequency rank above which a word counts as uncommon. Chosen because it sits
#: where the three exam levels separate cleanly (8.2 / 10.6 / 11.3 percent).
RARE_FREQUENCY_RANK = 5000

#: Each indicator mapped onto 0–100 by linear scaling between these bounds, so a
#: score describes one article on its own rather than its rank within whatever
#: batch happened to be measured with it. Bounds come from the measured spread
#: with headroom at both ends.
SCALES: dict[str, tuple[float, float]] = {
    "beyond_cet4_pct": (4.0, 25.0),
    "beyond_cet6_pct": (3.0, 20.0),
    "frequency_p90": (1500.0, 9000.0),
    "rare_word_pct": (3.0, 25.0),
    "exam_key_pct": (0.0, 12.0),
}

#: Equal weights across the vocabulary indicators. Verified to order the three
#: exam levels correctly, which is the acceptance test for any weighting: a set
#: of weights that cannot produce 四级 < 六级 ≈ 考研 is wrong, whatever it
#: looks like. Overridable from the console so the ordering can be re-checked
#: without a code change.
DEFAULT_WEIGHTS: dict[str, float] = {
    "beyond_cet4_pct": 0.25,
    "beyond_cet6_pct": 0.25,
    "frequency_p90": 0.25,
    "rare_word_pct": 0.25,
    # Stays 0 until the exam corpus has been annotated end to end; before that
    # the density is not measured, and a zero contribution is honest where an
    # invented one would not be.
    "exam_key_pct": 0.0,
}


def within(token: TokenAnalysis, tiers: frozenset[str]) -> bool:
    """Whether this occurrence counts as inside ``tiers``.

    A thin adapter: everything that makes the answer right — cumulative tags,
    spelling variants, the inflection table, grade-A derivations — lives in
    :mod:`..vocabulary.syllabus`, which is the only place that answers this
    question for the whole project.

    This function used to be ``syllabus_tags`` and returned tags, which meant
    every caller re-implemented the comparison and **each of them stopped at a
    different point**. Returning a boolean is what makes that impossible.
    """
    if not token.headword:
        return False
    return syllabus.within(token.headword, token.text, tiers)


def measure(sentences: list[SentenceAnalysis]) -> dict[str, Any]:
    """Every indicator for one article.

    Syntax indicators are included even though they do not feed the composite:
    they are sortable columns, and leaving them uncomputed would mean a schema
    change the first time someone wants to look at them.
    """
    tokens = [t for s in sentences for t in s.tokens]
    words = [t for t in tokens if t.is_word]
    # Proper nouns are excluded from every vocabulary measure for the same
    # reason they never count toward the unknown-word rate: an unfamiliar name
    # is not a vocabulary gap, and exam passages are full of them on purpose.
    content = [t for t in words if t.is_content and not t.is_proper_noun]

    result: dict[str, Any] = {
        "word_count": len(words),
        "sentence_count": len(sentences),
        "content_word_count": len(content),
        "mean_sentence_length": round(len(words) / len(sentences), 2) if sentences else 0.0,
    }

    if not content:
        return result

    beyond4 = sum(1 for t in content if not within(t, WITHIN_CET4))
    beyond6 = sum(1 for t in content if not within(t, WITHIN_CET6))
    result["beyond_cet4_pct"] = round(beyond4 / len(content) * 100, 2)
    result["beyond_cet6_pct"] = round(beyond6 / len(content) * 100, 2)

    ranks = [t.frq for t in content if t.frq]
    if ranks:
        ordered = sorted(ranks)
        result["frequency_median"] = statistics.median(ordered)
        result["frequency_p90"] = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
        result["rare_word_pct"] = round(
            sum(1 for r in ranks if r > RARE_FREQUENCY_RANK) / len(ranks) * 100, 2
        )

    return result


def add_sense_indicators(measures: dict[str, Any], *, exam_key_tokens: int,
                         annotated_tokens: int) -> dict[str, Any]:
    """Fold in what only exists once the article has been sense-annotated.

    Kept separate from :func:`measure` because annotation happens after ingest,
    and because ``exam_key_pct`` stays **absent, not zero**, until the exam
    corpus has been annotated in full and 考频 is actually counted. A zero here
    would read as "this sense never appears in the exams", which is a different
    claim from "nobody has counted yet" — and shipping a metric that means
    something other than its name is the mistake this module exists to avoid.
    """
    if annotated_tokens > 0:
        measures["exam_key_pct"] = round(exam_key_tokens / annotated_tokens * 100, 2)
    return measures


def weights() -> dict[str, float]:
    """Composite weights, from the console setting with the defaults as fallback."""
    raw = runtime_config.get("difficulty_weights")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if not isinstance(raw, dict) or not raw:
        return dict(DEFAULT_WEIGHTS)
    merged = dict(DEFAULT_WEIGHTS)
    for key, value in raw.items():
        if key in merged:
            try:
                merged[key] = float(value)
            except (TypeError, ValueError):
                log.warning(
                    "difficulty.weight.invalid",
                    f"配置里的难度权重 {key} 不是数字，用默认值",
                    key=key,
                )
    return merged


def _scaled(name: str, value: float) -> float:
    low, high = SCALES[name]
    if high <= low:
        return 0.0
    return max(0.0, min(100.0, (value - low) / (high - low) * 100.0))


def score(measures: dict[str, Any]) -> float | None:
    """Composite reference score, 0–100. ``None`` when nothing can be measured.

    Only indicators actually present contribute, and the weights are
    renormalised over those — so an article that has not been sense-annotated is
    scored on what is known rather than penalised for a missing number.
    """
    applied = weights()
    total_weight = 0.0
    accumulated = 0.0

    for name, weight in applied.items():
        if weight <= 0 or name not in measures or measures[name] is None:
            continue
        accumulated += _scaled(name, float(measures[name])) * weight
        total_weight += weight

    if total_weight <= 0:
        return None
    return round(accumulated / total_weight, 1)


#: Which indicators the client may sort by, and how each reads in the console.
#: ``composite`` is flagged so the interface can label it a reference figure:
#: it describes the text, never the reader, until P3 exists.
SORTABLE: dict[str, str] = {
    "composite": "综合参考分",
    "beyond_cet4_pct": "超四级词比例",
    "beyond_cet6_pct": "超六级词比例",
    "frequency_p90": "词频 90 分位",
    "rare_word_pct": "生僻词比例",
    "exam_key_pct": "非首义项占比",
    "word_count": "篇幅",
    "mean_sentence_length": "平均句长",
    "prepared_at": "备稿时间",
}

#: Indicators deliberately excluded from the composite, listed so the reason
#: survives: measured across three known difficulty levels, they do not move.
NOT_IN_COMPOSITE = ("word_count", "mean_sentence_length")
