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
    article_id: int | None = Field(
        default=None,
        description="最深一级提示可以跳回原文。生成的句子没有，客户端退回英文释义",
    )


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
    sense: SenseCard | None = None
    questions: list[SentenceCard] = Field(description="用来出题的句子，你没读到过的")
    hints: list[SentenceCard] = Field(description="提示用的句子，当初读到它的那一句")


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
