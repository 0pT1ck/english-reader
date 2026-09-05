"""Prompt construction, and the word selection that feeds it.

Three vocabulary-constraint schemes are built here so the first experiments can
compare them rather than assume one is right:

* ``anchor``      — a plain instruction plus ~40 boundary words as a yardstick
* ``whitelist``   — the complete allowed word list, several thousand words
* ``descriptive`` — the instruction alone, nothing else

The expectation is that ``anchor`` wins, because a model is not a constraint
solver: it does not check each word it writes against a list. Forty words are a
ruler it can feel; five thousand are a dictionary it cannot consult. But that is
a prediction, and the point of P1a is to test it instead of believing it.

**Two difficulty parameters, deliberately separate.** ``assumed known`` is what
the learner is taken to know already; ``allowed`` is the ceiling the article may
reach. If generation were restricted to words already known, the article would
contain nothing to learn — new words must land in the gap between the two. That
gap is where the target words come from, and shifting these two parameters is
how the whole system re-aims at a harder exam later.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

from backend.core.db import get_connection
from backend.core.logging import get_logger

log = get_logger("generation.prompts")

Scheme = Literal["anchor", "whitelist", "descriptive"]

# Bumped whenever the wording below changes, so a draft can always be traced
# back to the exact prompt that produced it.
PROMPT_VERSION = "p1a-1"

SCHEME_LABELS = {
    "anchor": "描述性约束 + 难度锚点",
    "whitelist": "完整词表",
    "descriptive": "纯描述性约束",
}


@dataclass
class WordPlan:
    """The vocabulary decisions behind one prompt."""

    target_words: list[str]
    anchor_words: list[str]
    allowed_count: int
    known_count: int


def _tier_clause(tiers: list[str]) -> tuple[str, list[str]]:
    """SQL fragment matching any of the given syllabus tags."""
    clause = " OR ".join("tags LIKE ?" for _ in tiers)
    return f"({clause})", [f"%{tier}%" for tier in tiers]


def count_words(tiers: list[str]) -> int:
    clause, params = _tier_clause(tiers)
    row = get_connection("dictionary").execute(
        f"SELECT COUNT(*) AS n FROM words WHERE {clause}", params
    ).fetchone()
    return int(row["n"])


def _tier_words_by_frequency(tier: str, known_tiers: list[str]) -> list[str]:
    """Words in ``tier`` that are *not* already in the assumed-known tiers.

    A word carries every syllabus it appears in — ``address`` is tagged
    ``zk gk cet4 cet6 ky`` all at once. So "words tagged cet4" is mostly words
    the learner already knows from secondary school, and selecting targets from
    it produces *secret*, *taste*, *wing*: nothing to learn.

    Subtracting the known tiers leaves what CET-4 actually adds, which is the
    only place a learning target can legitimately come from.
    """
    include, params = _tier_clause([tier])
    excludes = " AND ".join("tags NOT LIKE ?" for _ in known_tiers)
    where = f"{include} AND {excludes}" if known_tiers else include

    rows = get_connection("dictionary").execute(
        f"SELECT headword FROM words"
        f" WHERE {where} AND frq > 0 AND length(headword) > 3"
        f" ORDER BY frq",
        [*params, *[f"%{t}%" for t in known_tiers]],
    ).fetchall()
    return [r["headword"] for r in rows]


def _percentile_band(words: list[str], low: int, high: int) -> list[str]:
    return words[len(words) * low // 100 : len(words) * high // 100]


# Frequency bands, chosen from the actual distribution of CET-4 words.
#
# The trap here is treating "rare" as "hard". The rarest tenth of CET-4 is
# hasten, rouse, zealous, typhoon — uncommon but not difficult, and useless as
# either a learning target or a difficulty ceiling. The most common tenth
# (state, offer, action) is already known. What is worth teaching, and what
# meaningfully marks a ceiling, both sit in the middle.
TARGET_BAND = (20, 60)   # cancer, sample, scientific, monitor, maintenance
ANCHOR_BAND = (60, 85)   # insect, prohibit, interfere, vague, expenditure


def pick_target_words(
    learn_tier: str,
    count: int,
    *,
    known_tiers: list[str],
    seed: int | None = None,
    exclude: set[str] | None = None,
) -> list[str]:
    """Choose the words this article should teach.

    Drawn from the tier being learned — the gap between "assumed known" and
    "allowed" — and from the middle of that tier by frequency.
    """
    words = _percentile_band(_tier_words_by_frequency(learn_tier, known_tiers), *TARGET_BAND)
    if exclude:
        words = [w for w in words if w not in exclude]

    rng = random.Random(seed)
    return sorted(rng.sample(words, min(count, len(words))))


def pick_anchor_words(learn_tier: str, count: int, *, known_tiers: list[str]) -> list[str]:
    """Choose the words that show the model where the ceiling is.

    Taken from the harder end of the tier being learned, but stopping short of
    the rare tail. The message has to read as "roughly this hard, no harder" —
    which only works if every anchor word is one a reader would recognise as
    demanding rather than merely obscure.
    """
    band = _percentile_band(_tier_words_by_frequency(learn_tier, known_tiers), *ANCHOR_BAND)
    step = max(1, len(band) // count)
    return sorted(band[::step][:count])


def _whitelist(allowed_tiers: list[str], limit: int = 6000) -> list[str]:
    clause, params = _tier_clause(allowed_tiers)
    rows = get_connection("dictionary").execute(
        f"SELECT headword FROM words WHERE {clause} ORDER BY frq LIMIT ?",
        [*params, limit],
    ).fetchall()
    return [r["headword"] for r in rows]


def build_prompt(
    *,
    scheme: Scheme,
    target_words: list[str],
    anchor_words: list[str],
    allowed_tiers: list[str],
    allowed_count: int,
    length: int,
) -> str:
    """Assemble the text the user will paste into a chat window.

    Written in Chinese because the user reads it and may want to adjust it by
    hand; the instructions about English are equally clear either way.

    The manual workflow imposes a hard limit: the prompt has to be copyable in
    one go. That is what rules out pasting a five-thousand-word list under the
    ``anchor`` and ``descriptive`` schemes — and since both modes must share one
    prompt (otherwise the two produce different quality and the comparison is
    meaningless), it shapes the automatic path too.
    """
    parts: list[str] = [
        f"写一篇英语短文，约 {length} 词。",
        "",
        f"【用词限制】限制在最常见的约 {allowed_count} 个英语单词以内。",
    ]

    if scheme == "anchor":
        parts += [
            "",
            "【难度上限参照】不要使用比下列这些更难的词：",
            ", ".join(anchor_words),
        ]
    elif scheme == "whitelist":
        allowed = _whitelist(allowed_tiers)
        parts += [
            "",
            f"【允许使用的词表】只使用下列词汇及其变形（共 {len(allowed)} 个）：",
            ", ".join(allowed),
        ]

    parts += [
        "",
        "【必须自然地用上】以下词是本文的学习目标，每个至少出现一次，",
        "要用在真实、恰当的语境里，不要生硬堆砌：",
        ", ".join(target_words),
        "",
        "【文体要求】",
        "- 说明文或议论文，正式书面语体，不要口语化",
        "- 段落结构清晰，每段一个中心，段首点明",
        "- 逻辑连接词明确（转折、因果、递进都写出来）",
        "- 多用具体例子，少堆抽象名词",
        "- 句式保持正常书面英语的复杂度，不要刻意简化成短句",
        "",
        "【返回格式】严格按下面的格式返回，不要添加任何解释：",
        "",
        "===ARTICLE===",
        "TITLE: 你的标题",
        "",
        "正文第一段。",
        "",
        "正文第二段。",
        "===END===",
    ]

    return "\n".join(parts)


def plan_and_build(
    *,
    scheme: Scheme,
    assumed_tiers: list[str],
    allowed_tiers: list[str],
    learn_tier: str,
    target_count: int,
    anchor_count: int,
    length: int,
    seed: int | None = None,
    exclude: set[str] | None = None,
) -> tuple[str, WordPlan]:
    """Pick the words and build the prompt in one call."""
    plan = WordPlan(
        target_words=pick_target_words(
            learn_tier, target_count, known_tiers=assumed_tiers, seed=seed, exclude=exclude
        ),
        anchor_words=pick_anchor_words(learn_tier, anchor_count, known_tiers=assumed_tiers),
        allowed_count=count_words(allowed_tiers),
        known_count=count_words(assumed_tiers),
    )

    prompt = build_prompt(
        scheme=scheme,
        target_words=plan.target_words,
        anchor_words=plan.anchor_words,
        allowed_tiers=allowed_tiers,
        allowed_count=plan.allowed_count,
        length=length,
    )

    log.debug(
        "prompt.built",
        f"生成提示词（{SCHEME_LABELS[scheme]}）",
        scheme=scheme,
        targets=plan.target_words,
        prompt_chars=len(prompt),
    )
    return prompt, plan
