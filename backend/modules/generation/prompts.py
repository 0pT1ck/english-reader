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
PROMPT_VERSION = "p1b-1"

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
    topic: str | None = None


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
    topic: str | None = None,
) -> tuple[list[str], str | None]:
    """Choose the words this article should teach, and the topic they suggest.

    Drawn from the tier being learned — the gap between "assumed known" and
    "allowed" — and from the middle of that tier by frequency.

    When sense sets are available the words are picked **around a topic**: three
    or four that share one, then general-purpose words to fill the slot count.
    Eight unrelated words (``anchor / humour / wagon / robe``) force a model to
    manufacture a scene that holds all of them, and the first round showed
    exactly that — contrived articles built around a list. A topic gives it a
    scene to write about instead.

    Falls back to a plain random draw when no topic data exists yet, so
    generation never depends on the sense sets having been built.
    """
    pool = _percentile_band(_tier_words_by_frequency(learn_tier, known_tiers), *TARGET_BAND)
    if exclude:
        pool = [w for w in pool if w not in exclude]

    rng = random.Random(seed)
    themed, chosen_topic = _themed_targets(pool, count, rng, topic)
    if themed:
        remaining = [w for w in pool if w not in set(themed)]
        filler = rng.sample(remaining, min(count - len(themed), len(remaining)))
        return sorted(themed + filler), chosen_topic

    return sorted(rng.sample(pool, min(count, len(pool)))), None


# How many of the target words share a topic. The rest are deliberately left
# general: an article whose every target word is on one subject reads like a
# glossary, and the incidental vocabulary is where much of the learning happens.
THEMED_SHARE = 0.45


def _themed_targets(
    pool: list[str], count: int, rng: random.Random, topic: str | None
) -> tuple[list[str], str | None]:
    """Pick the topic-sharing part of the target list, if topics are available."""
    try:
        from backend.modules.senses import repository as senses
    except ImportError:  # pragma: no cover - module not installed
        return [], None

    wanted = max(1, round(count * THEMED_SHARE))
    candidates = [topic] if topic else list(PREFERRED_TOPICS)
    rng.shuffle(candidates)

    available = set(pool)
    for candidate in candidates:
        if not candidate:
            continue
        words = [w for w in senses.words_by_topic(candidate, limit=500) if w in available]
        if len(words) >= wanted:
            return rng.sample(words, wanted), candidate
    return [], None


# Preference order for topics, from the design: these four come up most, the
# rest still appear but carry less weight.
PREFERRED_TOPICS = ("社会", "科技", "文化", "日常生活",
                    "环境", "经济", "教育", "健康", "心理", "历史")


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
    topic: str | None = None,
    targets_per_paragraph: int = 5,
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
        "**不要使用超出这个范围的词。**读这篇文章的人要把文中每一个生词都搞懂，",
        "一个学不会的词既学不到，还占掉注意力。",
        "拿不准某个词是否超范围时，换一个更常见的说法——",
        "用简单的词把事情说清楚，比用一个准确但超纲的词更有价值。",
        "人名、地名、机构名不受此限制。",
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

    per_paragraph = max(1, targets_per_paragraph)
    paragraphs = max(3, round(len(target_words) / per_paragraph))

    parts += [
        "",
        f"【必须自然地用上】以下 {len(target_words)} 个词是本文的学习目标，"
        "每个至少出现一次，要用在真实、恰当的语境里：",
        ", ".join(target_words),
        "",
        # Density and spread are one requirement, not two. A high count with the
        # words piled into the first paragraph is no easier to read than a word
        # list, and a low count means the reader must get through fifty running
        # words to meet one worth learning.
        f"**分布要均匀**：全文写 {paragraphs} 段左右，"
        f"每段自然地融入 {per_paragraph} 个左右的目标词。",
        "不要把大部分目标词堆在开头一两段，也不要有哪一段一个都没有。",
        "如果某几个词实在放不进同一个场景，就让文章的话题自然转折，",
        "而不是硬造一句话把它们塞进去。",
    ]

    if topic:
        parts += ["", f"【主题】{topic}。"]

    parts += [
        "",
        "【内容要求】",
        "- **必须写具体的事**：具体的人、具体的场景、具体的数字或细节。",
        "  宁可讲清楚一件事，也不要罗列三条空泛的道理。",
        "- 通篇抽象论述会让文章读起来像作文范文——这跟用词难易无关，",
        "  用最简单的词一样能写出有血有肉的文章。",
        "",
        "【文体要求】",
        "- 说明文或议论文，正式书面语体，不要口语化",
        "- 段落结构清晰，每段一个中心，段首点明",
        "- 逻辑连接词明确（转折、因果、递进都写出来）",
        "- 句式保持正常书面英语的复杂度，不要刻意简化成短句",
        "",
        "【句法要求】",
        # Two things were learned tuning this block, both the hard way.
        #
        # Numbers do not work. "Use passive no more than five or six times" and
        # "use at least one or two anticipatory subjects" both changed nothing —
        # the same deafness to counts that made the sense-merging prompt ignore
        # "aim for three to five items". Stating *when* a structure is right
        # moved the passive rate from 31 to 15.5 against a corpus baseline of 17.
        #
        # And the earlier complaint that this model never writes anticipatory
        # subjects was a measurement error, not a fact about the model: spaCy
        # labels the `it` in "It is clear that…" a plain nsubj, so the checker
        # was counting `there be` and calling it something else.
        "- 被动语态：**默认用主动，被动是例外**。只有这两种情况才用被动：动作的执行者不知道",
        "  （The bones were buried long ago.），或者执行者根本不重要、说出来反而啰嗦",
        "  （The samples were collected in March.）。",
        "  只要句子里能找到一个具体的人、机构或事物在做这件事，就把它放到主语位置上用主动。",
        "  写完通读一遍，把不属于上面两种情况的被动句全部改成主动。",
        "- 形式主语：It is clear that… / It has become difficult to… 这类句式适度使用即可，不要过量。",
        "- 名词化不要过量：能用动词说清楚的，就别硬凑成抽象名词",
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
    topic: str | None = None,
    targets_per_paragraph: int = 5,
) -> tuple[str, WordPlan]:
    """Pick the words and build the prompt in one call."""
    targets, chosen_topic = pick_target_words(
        learn_tier, target_count, known_tiers=assumed_tiers,
        seed=seed, exclude=exclude, topic=topic,
    )
    plan = WordPlan(
        target_words=targets,
        anchor_words=pick_anchor_words(learn_tier, anchor_count, known_tiers=assumed_tiers),
        allowed_count=count_words(allowed_tiers),
        known_count=count_words(assumed_tiers),
        topic=chosen_topic,
    )

    prompt = build_prompt(
        scheme=scheme,
        target_words=plan.target_words,
        anchor_words=plan.anchor_words,
        allowed_tiers=allowed_tiers,
        allowed_count=plan.allowed_count,
        length=length,
        topic=plan.topic,
        targets_per_paragraph=targets_per_paragraph,
    )

    log.debug(
        "prompt.built",
        f"生成提示词（{SCHEME_LABELS[scheme]}）",
        scheme=scheme,
        targets=plan.target_words,
        prompt_chars=len(prompt),
    )
    return prompt, plan
