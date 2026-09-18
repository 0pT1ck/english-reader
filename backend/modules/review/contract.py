"""Response models for the review endpoints.

双向 both directions / 提示分级 graded hints / 结算 settlement / 权重 weight

**The offline shape is the whole point of `/reviews`.** Every card ships with
its sentences and its hint already attached, so a client can work through the
day without another request (架构前提 2). That is why these models are large:
the size is the feature.

**What the client is allowed to compute.** 架构前提 1 says client-side business
logic is forbidden, and the offline requirement makes one exception unavoidable
— the draw and the state transitions have to exist on the device or the day
cannot be finished without a network. The rule the client follows is: mirror
the few transitions written into this contract, never invent one, and let the
server overwrite the result when the answers are reported. `weight_decay` is in
the payload for exactly this reason: the *rules* come from the server, and what
the client does is pick from a bag.

Read the note at the top of `core/contract.py` before changing anything here.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from backend.core.contract import Learner


class SenseCard(BaseModel):
    """One sense, as a review card shows it."""

    id: int
    headword: str
    ordinal: int
    pos: str | None = None
    concept_en: str | None = None
    gloss_zh: list[str] | str | None = None
    exam_frequency: int | None = Field(default=None, description="真题考频")


class SentenceCard(BaseModel):
    """A sentence the item appears in.

    Two pools, and which one a sentence lands in is not a property of the
    sentence: 决定 of P3 puts the sentence you originally *read* it in into the
    hint slot, and asks with one you have not seen. So the same row is a
    question for one learner and a hint for another.
    """

    id: int
    text: str
    blank_start: int = Field(description="要挖空的区间，在 text 里的字符下标")
    blank_end: int
    surface: str = Field(description="被挖掉的那个词在句子里的原样")
    first_letter: str = Field(
        description="看义想词的第一级提示。**在服务端算**——什么算首字母是一条规则，"
        "而规则按架构前提 1 留在这边"
    )
    source: str | None = Field(default=None, description="generated 生成的 / corpus 语料里的")
    sentence_id: int | None = Field(
        default=None,
        description="这一句在文章里的那个 id（`reading_sentences.id`）。"
        "**P9 加的**:考句／提示的划分搬到客户端之后，客户端要判「这一句你见过吗」，"
        "而它手上「见过」的那个集合是按这个 id 记的（标记事件带着它）。"
        "**和上面那个 `id` 不是一回事**——那个是句子池自己的行号。"
        "生成的句子没有它（它们不出自任何文章）",
    )
    article_id: int | None = Field(
        default=None,
        description="提示可以说出处，也留着将来跳回原文。生成的句子没有——"
        "而生成的句子本来就不会当提示",
    )
    article_title: str | None = Field(
        default=None, description="提示上那句「——文章a」。join 出来的，不另存一份"
    )
    text_zh: str | None = Field(
        default=None,
        description="整句中文翻译，**看中文想英文那个方向的题面**。还没翻到的是 null。"
        "这跟「句子里不许出现中文」那条禁令不冲突：禁的是英文题面里混中文",
    )
    zh_start: int | None = Field(
        default=None,
        description="目标词在中文里的位置，给高亮用。**null 是正当答案**——"
        "有些词在中文里没有能单独拎出来的片段，那时不高亮、整句照显。"
        "高亮错位置比不高亮糟：它会把「估计」从中间劈开，而没有东西会报错",
    )
    zh_end: int | None = None


class WordSense(BaseModel):
    """One sense of the word being tested, for the reveal screen's 常见释义 list."""

    id: int
    ordinal: int
    pos: str | None = Field(default=None, description="词性。说明用，从不决定义项怎么分")
    concept_en: str | None = None
    gloss_zh: list[str] | str | None = None
    exam_frequency: int = 0
    share: float | None = Field(
        default=None,
        description="占这个词全部真题出现的百分之几。总数为零时是 null。"
        "**客户端按它降序排，绝不按 ordinal**——那个序号是模型猜的",
    )


class WordCard(BaseModel):
    """The whole word, as the reveal screen shows it.

    Duplicates what the article glossary carries, and that is the point:
    **the review payload has to stand on its own.** Article bodies are cleared
    one at a time (P5 决定 10) and the word may have been met months ago, so a
    client reaching into a cached article for this would lose half its card
    whenever the cache was tidied — silently.
    """

    headword: str
    phonetic: str | None = None
    translation: str | None = Field(
        default=None, description="词典那一堆逗号隔开的释义。义项集缺席时的退路"
    )
    senses: list[WordSense] = Field(default_factory=list)


class ReviewItem(BaseModel):
    queue_id: int = Field(description="上报作答时带上它。**不是义项 id**，是今天这一轮的排队号")
    item_type: str = Field(description="word 单词 / phrase 词组")
    item_key: str
    sense_id: int = Field(description="词组恒为 0——词组不是别的东西的一个义项")
    bucket: str = Field(description="today 今天刚标的 / due 到期该复习的")
    direction: int = Field(description="1 看词想义项，2 看义项想词。答对 1 解锁 2，答错 2 退回 1 并重新上锁")
    asks: int = Field(description="今天问过几次，两个方向合计。**它就是成绩**")
    misses: int
    weight: float = Field(description="抽题权重。答错减半，让卡住的词自己让路，但永不排除")
    done: bool
    word: WordCard | None = Field(
        default=None,
        description="被考的这个词的全部资料（音标、全部义项、考频占比）。揭晓屏用",
    )
    sense: SenseCard | None = None
    questions: list[SentenceCard] = Field(description="用来出题的句子，你没读到过的")
    hints: list[SentenceCard] = Field(description="提示用的句子，当初读到它的那一句")


class StudyItemSentences(BaseModel):
    '''一个在学的词，连它的句子——**不分池**。

    **P9 §11。** 分池（哪句当考题、哪句当提示）依赖「你读完过哪些文章、
    见过哪些句子」，那是学习记录；而造句子要钱、要模型，是内容生产。
    那条线把两件事分开了，所以这里原样全给，由客户端分（`ERCore/SentencePool`）。

    **和 `ReviewItem` 的差别正是那条线**：这里没有 `queue_id`、`bucket`、
    `direction`、`asks`、`weight`、`done`——那些全是学习状态，现在由设备重放算出来。
    '''

    item_type: str
    item_key: str
    sense_id: int
    word: WordCard | None = None
    sense: SenseCard | None = None
    sentences: list[SentenceCard] = Field(
        description="这个词（这个义项）的全部句子，**没有分池**。"
        "`source` 与 `sentence_id`／`article_id` 够客户端自己分"
    )


class SentencePoolResponse(BaseModel):
    '''在学的那些词的句子。

    **依据是你上报的词池快照**（§7），不是服务端自己推的——
    服务端对学习记录只有两种关系:生文需要的那一小撮信号，和它不解释的存档。
    这里用的是前者，而它连解释都不算:直接用。
    '''

    learner: Learner
    reported_at: str | None = Field(
        default=None,
        description="那份快照是什么时候的。**为空表示这台设备还没报过**——"
        "那时 `items` 也是空的，而那不是「你没在学任何词」，是「服务端还不知道」",
    )
    items: list[StudyItemSentences]


class ReviewSession(BaseModel):
    id: int
    day: str
    started_at: str
    finished_at: str | None = None
    spelling_at: str | None = None


class ReviewProgress(BaseModel):
    session: ReviewSession | None = None
    total: int
    done: int
    remaining: int
    buckets: dict[str, int] = Field(description="各桶各有几条")
    buckets_done: dict[str, int] = Field(
        default_factory=dict,
        description="各桶各做完了几条。**新开一个字段而不是改 `buckets` 的含义**——"
        "铁律 5 不许改字段含义。两张卡片上的「已复习/共」要的就是这两个数",
    )
    spelling_available: bool = Field(
        description="拼写这一轮开没开。当天复习全部走完之后才为真——"
        "它是强化选项，不进调度，拼错只记一笔"
    )


class ClockStatus(BaseModel):
    """The review clock, which can be moved for testing.

    In the payload because a client that quietly used the device clock while the
    server was running days ahead would disagree with it about what is due, and
    the disagreement would look like a scheduling bug.
    """

    offset_days: int
    simulated_now: str
    real_now: str
    simulated: bool


class ReviewDayResponse(BaseModel):
    """Everything today's review needs, in one response."""

    learner: Learner
    day: str
    session_id: int
    finished_at: str | None = None
    weight_decay: float = Field(
        description="答错之后权重乘以它。**规则来自服务端**，客户端只负责从袋子里摸"
    )
    spelling_enabled: bool
    progress: ReviewProgress
    clock: ClockStatus
    items: list[ReviewItem]


class CalendarDay(BaseModel):
    day: str
    status: str = Field(
        description="complete 两项都做完 / partial 只做完一项、或那天压根没活 / "
        "missed 有活但一项都没做完 / unknown 那天没开过 App，重建不出来。"
        "**partial 里那个「没活」很要紧**：系统没派活的日子不该判成你失败"
    )
    is_today: bool


class CalendarResponse(BaseModel):
    learner: Learner
    days: list[CalendarDay]
    streak: int = Field(
        description="连续多少天两项都做完。**今天还没做完不算断**——"
        "你可能正要去做，午夜就清零的计数器量的是时钟不是人"
    )


class Settlement(BaseModel):
    """What the scheduler decided when an item left the pool.

    Stored as well as returned, so an algorithm change can be replayed against
    real history instead of re-derived from assumptions.
    """

    rating: int
    rating_name: str
    interval_days: float
    due_at: str | None = None


class AnswerOutcome(BaseModel):
    """What one answer did to one item."""

    done: bool = Field(description="这个条目今天过了——两个方向都答对了")
    asks: int
    misses: int
    easy: bool = Field(
        description="「这个太简单了」。只在零失误的一轮才作数，**服务端自己判**，"
        "客户端说了不算"
    )
    settled: Settlement | None = Field(
        default=None, description="过了才有。没过是 null"
    )


class AnswerResponse(AnswerOutcome):
    progress: ReviewProgress


class AnswerResult(BaseModel):
    idem_key: str
    status: str = Field(description="accepted 收下了 / duplicate 重复 / failed 处理失败")
    reason: str | None = Field(default=None, description="failed 才有")
    result: AnswerOutcome | None = Field(
        default=None, description="accepted 才有。重复与失败都是 null"
    )


class AnswersResponse(BaseModel):
    """Replay of a day's answers, in the order the client sent them.

    **Order matters and it is the client's.** A review session is a state
    machine — passing 看词想义 unlocks 看义想词, failing it locks it again — so
    the batch is applied in sequence, not concurrently. A failure in the middle
    stops nothing: the rest still apply, and the one that failed comes back with
    its key so the client can decide.
    """

    accepted: int
    duplicates: int
    failed: int
    results: list[AnswerResult]
    progress: ReviewProgress


class SpellingResponse(BaseModel):
    """Recorded, never scheduled on.

    Spelling stays out of the scheduler entirely: it is optional reinforcement,
    and letting it move due dates would mix a productive-recall difficulty into
    a recognition track. What is stored is what was *typed*, not just whether it
    was right — that is the whole value of the record.
    """

    correct: bool
    expected: str


class SpellingResult(BaseModel):
    idem_key: str
    status: str = Field(description="accepted 收下了 / duplicate 重复 / failed 处理失败")
    reason: str | None = Field(default=None, description="failed 才有")
    result: SpellingResponse | None = Field(
        default=None, description="accepted 才有"
    )


class SpellingsResponse(BaseModel):
    """离线补报的一批拼写。

    No progress block, unlike the answers batch: spelling happens after the
    day's review is already finished, so there is nothing left for it to move.
    """

    accepted: int
    duplicates: int
    failed: int
    results: list[SpellingResult]
