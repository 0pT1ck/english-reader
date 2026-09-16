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
PROMPT_VERSION = "p1b-2"

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


#: 从低到高。决定谁是用词上限，以及谁在上限之上。
TIER_ORDER = ["zk", "gk", "cet4", "cet6", "ky"]
TIER_FULL = {
    "zk": "中考", "gk": "高考", "cet4": "大学英语四级（CET-4）",
    "cet6": "大学英语六级（CET-6）", "ky": "考研",
}
TIER_SHORT = {"zk": "中考", "gk": "高考", "cet4": "四级", "cet6": "六级", "ky": "考研"}
#: 上限之下那一层的人——自查那句话要问的是「他认识吗」。
TIER_READER = {
    "zk": "初中毕业生", "gk": "高中毕业生", "cet4": "刚过四级的学生",
    "cet6": "刚过六级的学生", "ky": "考过研的学生",
}
#: 任何一层之上都还有这三个。它们不在 tags 体系里，但模型认得这三个名字。
BEYOND_ALL = ["雅思", "托福", "GRE"]

# 越界词的样本与替代说法。**不是清单，是「一类词长什么样」**——这一类的特征是
# 正式、抽象、拉丁词根，而模型感觉不到它们难，因为对它自己来说它们再平常不过。
#
# 这十六个词是 2026-09-16 从实测里挑的：gemini-3.8-flash-high 十篇草稿中越界最多的
# 那些，每个配一个四级里说得出的替代。**只对 cet4 成立**，换学习等级要重新测一批。
CET4_TRAPS = [
    "ambitious → eager；mechanism → way / system；transition → change；",
    "municipal → city；rigorous → strict / careful；infrastructure → roads and bridges；",
    "initially → at first；bias → unfair opinion；maritime → sea；historic → important；",
    "simulation → test run；logistic → practical；administrative → office；",
    "dismantle → take apart；precede → come before；skeptic → doubter。",
]

# 同一件事的第二种形状：**具体名词**。压住抽象词之后剩下的越界词全变成了
# storehouse / smokestack / townspeople 这类复合词，而「必须写具体的事」是另一条
# 硬要求——所以这一段给的是出口而不是禁令，否则模型会靠写空泛来守规矩。
CET4_CONCRETE = [
    "【具体的东西也要用常见词说】storehouse、smokestack、workroom、truckload、townspeople",
    "这类复合词看着简单，多数不在四级表里。",
    "**不要为了避开它们就把文章写空**——照旧写具体的人和事，只是把名字换成常见词组成的说法：",
    "a building where goods are kept、a tall chimney、a back room、a full truck、the people of the town。",
]


def _ceiling_block(allowed_tiers: list[str]) -> list[str]:
    """用词上限那一段——**讲成考试的名字，不要讲成词数**。

    2026-09-16 实测，同一个模型（gemini-3.8-flash-high）同样十个种子：
    原来那句「限制在最常见的约 4000 个英语单词以内」几乎没有约束力，超纲中位
    5.82%、十篇零合格；换成这里的说法之后 0.56%、七篇合格，与 gpt-5.5 在旧提示词
    下的 0.39% / 八篇基本持平。命中率没有赔进去（24.1 对 23.7）。

    为什么：词数是模型算不出来的东西（坑 §1.1，数量约束一概无感），而「六级词」
    「考研词」是它知道的类别。它写 mechanism、municipal、rigorous 不是因为不守规矩，
    是因为它感觉不到这些词难——直到你告诉它这些词属于哪一级。

    三段叠加，各自的实测份额（四种子小样本）：只讲考纲 2.12%、只给陷阱表 3.28%、
    只加自查 3.67%、三段合起来 0.88%。**它们互补，不是同一句话说三遍。**

    ``allowed_count`` 不再出现在提示词里，就是这次量出来的结论；参数留着，
    因为草稿表里存着它、控制台按它分组。
    """
    ranked = [t for t in TIER_ORDER if t in allowed_tiers] or ["cet4"]
    ceiling = ranked[-1]
    above = [TIER_SHORT[t] for t in TIER_ORDER[TIER_ORDER.index(ceiling) + 1:]] + BEYOND_ALL
    reader = TIER_READER[ranked[-2]] if len(ranked) > 1 else TIER_READER[ceiling]
    # 末尾是 GRE 时补一个空格，别让中英文挤在一起。
    above_text = "、".join(above) + (" " if above[-1].isascii() else "")
    count_text = "零一两三四五六七八九"[len(ranked)] if len(ranked) < 10 else str(len(ranked))

    parts = [
        f"【用词限制】这篇文章写给正在准备**{TIER_FULL[ceiling]}**的中国学生看。",
        f"**只用{TIER_SHORT[ceiling]}大纲以内的词**——也就是"
        f"{'、'.join(TIER_SHORT[t] for t in ranked)}这{count_text}份词汇表覆盖的词。",
        f"**凡是要到{above_text}才学到的词，一个都不要用。**",
        f"判断办法：这个词如果只在{above[0]}词汇书里见到，就换成{TIER_SHORT[ceiling]}里的同义说法。",
        "读这篇文章的人要把文中每一个生词都搞懂，一个学不会的词既学不到，还占掉注意力。",
        "拿不准某个词是否超范围时，换一个更常见的说法——",
        "用简单的词把事情说清楚，比用一个准确但超纲的词更有价值。",
        "人名、地名、机构名不受此限制。",
    ]

    # 陷阱表与具体名词那两段是拿 cet4 测出来的，例词也全是 cet4 的越界词。
    # 换等级时它们会说错话，所以只在四级这一档给出。
    if ceiling == "cet4":
        parts += [
            "【最容易踩的那一类】下面这些词看着平常，其实全部超出四级范围（六级或考研词）。",
            "它们不是一份完整清单，是**一类词的样子**：正式的、抽象的、拉丁词根的。",
            "写的时候遇到同一类的词就换成右边的说法：",
            *CET4_TRAPS,
        ]

    parts += [
        "【写完自查】交稿前把全文**逐词**过一遍，对每一个词问一句：",
        f"中国{reader}认识它吗？答案是「不一定」的，就换成他一定认识的说法。",
        "宁可用三个简单词把意思说出来，也不要留一个难词。",
        "目标词表里的词是例外——它们本来就是要学的，不要换掉。",
    ]

    if ceiling == "cet4":
        parts += [*CET4_CONCRETE, f"{TIER_SHORT[ceiling]}学生读得懂的具体，才叫具体。"]

    return parts


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
        *_ceiling_block(allowed_tiers),
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
