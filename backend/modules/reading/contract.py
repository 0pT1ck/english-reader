"""Response models for the reading endpoints.

释义层 gloss layer / 词族 word family / 词组 phrase / 遇见记录 encounter record

These describe what `service.py` already returns — they add types, not fields.
Read the note at the top of `core/contract.py` before changing anything here:
FastAPI deletes from the wire whatever a model omits, and 架构铁律 5 forbids
deletion.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from backend.core.contract import Capabilities, ItemState, Learner


# --------------------------------------------------------------------------- #
# Library
# --------------------------------------------------------------------------- #


class DifficultyProfile(BaseModel):
    """难度画像：这篇文章相对大纲有多难。

    Measured, not judged — every number here describes the text against the
    syllabus. What it is *for you* lives in `difficulty_for_you`, which needs
    the ability estimate and is therefore still null.

    **判超纲一定要走 `vocabulary/syllabus.py`**，别自己比对 `tags`：那个字段栽了
    四次，最贵的一次让 8,401 处超纲标记里有 3,005 处（35.8%）的词根其实就在纲内，
    而且它不只让数字难看——P3 的模型横评用坏规则算出来名次是反的。
    """

    word_count: int
    sentence_count: int
    content_word_count: int = Field(description="实词数，不含标点与虚词")
    mean_sentence_length: float
    beyond_cet4_pct: float = Field(description="超出四级大纲的实词占比")
    beyond_cet6_pct: float
    frequency_median: float = Field(description="词频排名的中位数，越大越生僻")
    frequency_p90: float
    rare_word_pct: float


class LibraryArticle(BaseModel):
    id: int
    title: str
    topic: str | None = Field(
        default=None,
        description="话题。**真题恒为 null**——它们那一格显示的是来源。"
        "库里已经攒下的生成文也是 null：2026-09-13 定不补",
    )
    summary_zh: str | None = Field(
        default=None,
        description="一句中文概括。**真题恒为 null**，那一行就空着"
    )
    source: str = Field(description="generated 生成文 / cet4 / cet6 / kaoyan")
    source_label: str
    word_count: int
    sentence_count: int
    prepared_at: str | None = None
    read_at: str | None = Field(default=None, description="读完的时刻。没读完是 null")
    percent: float = Field(description="读到百分之几")
    target_count: int = Field(
        description="这篇为教几个词而写。真题恒为 0——它们不为教任何词而写，"
        "读完只记下你亲手标记的东西"
    )
    pending_count: int = Field(
        default=0,
        description="「待学」：这篇的目标词里**还没进入学习流程**的个数。"
        "判据是标记，不是有没有词池行——读完一篇会给遇见过的每个词都留一行，"
        "按行数就会读完之后莫名归零。**标记了就不再计入**，不用等学会，"
        "所以这个数在阅读过程中会往下掉。真题恒为 0",
    )
    difficulty: DifficultyProfile | None = None
    difficulty_score: float | None = Field(
        default=None, description="把画像压成一个数，用来排序"
    )
    difficulty_for_you: float | None = Field(
        default=None,
        description="这篇对你的预期生词率。要水平估计才算得出来，所以现在恒为 null——"
        "看 capabilities.level_estimate",
    )


class LibraryResponse(BaseModel):
    learner: Learner
    capabilities: Capabilities
    shelf: str
    sort: str
    descending: bool
    sortable: dict[str, str] = Field(
        description="支持的排序方式：键是 sort 参数的值，值是中文标签。"
        "客户端照着它出选项——写死的话，服务端加一种排序客户端就看不见"
    )
    fresh_days: int = Field(description="「新备的」这个书架算几天之内")
    articles: list[LibraryArticle]


# --------------------------------------------------------------------------- #
# One article
# --------------------------------------------------------------------------- #


class SenseExam(BaseModel):
    """How often this sense appears in the exam corpus. Facts, no verdict.

    The label this replaced (熟词僻义) was retired on 2026-09-07: `address`
    means 着手解决 38 times against 地址 twice, so that sense is not obscure —
    it is the dominant one in written English. Calling it obscure takes the
    learner's first impression as the baseline instead of the language.
    """

    frequency: int
    share: float | None = Field(
        default=None, description="占这个词全部真题出现的百分之几。总数为零时是 null"
    )
    is_exam_key: bool = Field(
        description="永久为 false。规则本身成立，但它依赖可信的义项排序，"
        "而现在的序号是模型猜的（主文档 §F4）。留着而不是删掉，是因为铁律 5 不删字段"
    )


class Sense(BaseModel):
    id: int
    ordinal: int
    pos: str | None = Field(
        default=None,
        description="词性（n. / vt. / vi.）。**只是说明，从不决定义项怎么分**——名词的 address（地址）和动词的 address（写地址）是同一个概念。"
        "这一列 P1b 起就在库里，2026-09-13 才带进契约：手机端的点词面板要显示它",
    )
    concept_en: str | None = Field(
        default=None,
        description="用已知词写的英文概念定义——查词本身也是阅读输入，而不是切换到中文",
    )
    gloss_zh: list[str] | str | None = Field(default=None, description="中文释义")
    exam: SenseExam | None = Field(
        default=None,
        description="真题考频。整块缺席而不是为零，看 capabilities.exam_frequency",
    )
    memory: dict[str, Any] | None = Field(
        default=None,
        description="这个义项的记忆状态与到期时间。复习模块的位置，**整块为 null**——"
        "跨 Phase 不变量只允许留形状显而易见的位置，而记忆状态的形状不显而易见，"
        "所以留一个可整体为 null 的对象，而不是猜几个字段出来。"
        "由 capabilities.memory_state 说明它是「没数据」还是「没实现」",
    )


class Derivation(BaseModel):
    """How a derived word is built, shown instead of a new gloss to memorise."""

    root: str
    affix: str | None = None
    breakdown_zh: str | None = None
    root_known: bool | None = Field(
        default=None,
        description="你认不认得这个词根。要水平估计才知道，所以现在恒为 null——"
        "在那之前，每个合格的派生词都照样给拆解",
    )


class GlossaryEntry(BaseModel):
    headword: str
    phonetic: str | None = None
    translation: str | None = Field(
        default=None, description="词典那一堆逗号隔开的释义，原样。义项集缺席时的退路"
    )
    tags: str | None = Field(description="ECDICT 的分级标签。别直接比对它——判大纲走 syllabus.py")
    frq: int | None = Field(default=None, description="词频排名")
    senses: list[Sense]
    derivation: Derivation | None = None
    marks: dict[str, str] = Field(
        description="这个词上的全部标记，按义项 id。**包括屏幕上这个义项之外的**——"
        "没有它，点词面板说不出「你标过这个词的另一个义项」，"
        "而学习者会把一个自己明明标过的词读成「App 忘了」"
    )
    states: dict[str, ItemState] = Field(description="这个词各义项的词池位置，按义项 id")


class Phrase(BaseModel):
    """A phrase is an item in its own right.

    Marking `account for` records nothing against `account`: not knowing the
    phrase says nothing about whether the word inside it is known, and letting
    one mark stand for both would drop a word the reader understands perfectly
    well into the review queue.

    The span is sent; how to show it is the client's business — but 跨 Phase
    不变量 fixes one half of that: selection and marking happen to the phrase as
    a whole, on every client.
    """

    phrase: str
    surface: str = Field(description="原文里的样子，含中间的空格")
    start_seq: int
    end_seq: int
    translation: str | None = None
    definition: str | None = None
    mark: str | None = Field(default=None, description="unknown 不认识 / fuzzy 模糊，没标是 null")
    state: ItemState | None = None


class Sentence(BaseModel):
    seq: int
    text: str
    char_start: int
    char_end: int


class Token(BaseModel):
    seq: int
    surface: str
    sentence_seq: int
    char_start: int = Field(description="在 article.body 里的字符下标，闭开区间的开头")
    char_end: int
    kind: str = Field(description="word 实词 / punct 标点 / proper 专有名词 等")
    is_target: bool = Field(description="这篇为教它而写")
    beyond: bool = Field(description="超纲。只在生成文里标出来，看 article.mark_beyond")
    headword: str | None = Field(default=None, description="词形还原之后的词条。标点没有")
    sense_id: int | None = None
    sense_ordinal: int | None = None
    in_phrase: bool = Field(
        description="这个词是某个词组的一部分，**所以它自己的义项标注是在不知情的情况下做的**。"
        "在这儿显示这个词的释义就是在给错的意思，不只是没帮上忙——"
        "`account` 会读成「账目」，而句子说的是 `account for`"
    )
    note: str | None = Field(
        default=None,
        description="专有名词的一句话说明。生成它的任务还没人写，所以位置在这儿、空着，"
        "而不是没有这个字段",
    )


class ArticleBody(BaseModel):
    id: int
    title: str
    topic: str | None = None
    summary_zh: str | None = None
    body: str | None = Field(
        default=None,
        description="原文。跟 tokens 一起下发而不是二选一：token 的下标指进这里，"
        "客户端不用重新分词就能还原空格和分段。还没准备好时是 null",
    )
    source: str | None = None
    source_label: str | None = None
    word_count: int | None = None
    sentence_count: int | None = None
    difficulty: DifficultyProfile | None = None
    difficulty_score: float | None = None
    difficulty_for_you: float | None = Field(
        default=None, description="要水平估计，现在恒为 null"
    )
    read_at: str | None = None
    mark_beyond: bool | None = Field(
        default=None,
        description="这篇要不要标出超纲词。生成文标，**真题不标**——"
        "在真题里碰到生词正是要练的那件事",
    )
    status: str | None = Field(default=None, description="还没准备好时才有，说明卡在哪一步")


class Preparing(BaseModel):
    """Not an error: lazy ingest means asking for an unprepared article is normal.

    Enough detail to show progress rather than a spinner of unknown length.
    """

    status: str
    detail: str | None = None
    annotated: int = Field(description="已标注的实词数")
    total: int


class ArticleProgress(BaseModel):
    """How far through this article you are.

    Three keys whether or not a row exists. `progress_of` used to return the
    whole row when there was one, which leaked `learner_id`, `article_id` and
    `updated_at` — server bookkeeping, present only half the time, read by
    nothing. Narrowed on 2026-09-12 rather than declared, because declaring
    them would have written an accident into every future client's types.
    """

    sentence_seq: int
    percent: float
    finished_at: str | None = None


class ArticleResponse(BaseModel):
    """One response, then no further requests while reading.

    **Two shapes in one model.** An article still being prepared comes back with
    `preparing` filled and the reading fields null; a ready one has `preparing`
    null and the rest filled. A client checks `preparing` first. Declaring it as
    a union would generate a neater Swift enum, but it would also make the two
    shapes independently extensible — and the day someone adds a field to only
    one of them, the contract has two answers to the same question.
    """

    learner: Learner
    capabilities: Capabilities
    article: ArticleBody
    preparing: Preparing | None = Field(
        default=None, description="非空就说明还没准备好，正文那几块都是空的"
    )
    sentences: list[Sentence] | None = None
    tokens: list[Token] | None = None
    phrases: list[Phrase] | None = Field(
        default=None,
        description="确认过的词组。空列表在 capabilities.phrases 为假时意思是「还没看过」",
    )
    glossary: dict[str, GlossaryEntry] | None = Field(
        default=None, description="文中每个词的释义，按词条。**这就是离线也能点词查词的原因**"
    )
    progress: ArticleProgress | None = None


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


class EventResult(BaseModel):
    idem_key: str
    status: str = Field(description="accepted 收下了 / duplicate 重复 / failed 处理失败 / rejected 不认识")
    reason: str | None = None


class EventBatchResponse(BaseModel):
    """Duplicates are the protocol working, not a fault.

    An offline client retries whatever it is unsure about. What matters to the
    client is the per-item verdict: 决定 11 says it may delete an outbox file
    only for a key this list reports as landed — never on the strength of the
    HTTP status alone.
    """

    accepted: int
    duplicates: int
    failed: int
    results: list[EventResult]
