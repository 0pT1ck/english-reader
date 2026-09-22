"""Shared pieces of the client contract.

契约 contract / 预留位置 reserved slot / 能力声明 capabilities

`learner` and `capabilities` are echoed by every client response, across three
modules. Declaring them three times would guarantee they drift, and a drifting
contract is worse than an undeclared one: the generated Swift would carry three
near-identical types with nothing to say which is right.

**Why declare types now, after five phases of `dict[str, Any]`.** The phone
client is generated from `/openapi.json`, and an untyped response generates
`[String: Any]` — the same as generating nothing. 主文档 §M settled the method:
not a second, hand-written contract document (two documents drift, and when
they do you cannot tell which is wrong) but types on the responses, so the code
says it itself.

**The rule these models must not break** is 架构铁律 5 — fields may be added,
never removed, never given a new meaning. FastAPI filters a response down to
the declared model, so *omitting* a field here deletes it from the wire, with
nothing raised. Every model in this project is therefore written against the
dict the service layer actually returns, not against the field list it ought to
have. `verify_phase5` compares the two on live data, because a rule nobody
checks is a rule that lasts until the next edit.

**Optional means nullable, not "might be missing".** A field declared `X | None`
generates `X?` in Swift and makes every reader handle the nil case. Used where
the value genuinely can be null it carries real information; used out of
laziness it spreads null checks through a client that can never hit them. So
nothing here is optional unless the service can actually return null for it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Learner(BaseModel):
    """Who this response is about, echoed at the top of every client response.

    Lets a client notice it is looking at someone else's cache and drop it. The
    learner is derived from the device token — no learner id appears in a path.
    """

    id: int
    name: str
    level: int | None = Field(
        default=None,
        description="词汇量估计。水平评估（主文档 §A）尚未实现，所以现在恒为 null——"
        "由 capabilities.level_estimate 区分「没数据」和「没实现」。",
    )


class Capabilities(BaseModel):
    """Which reserved slots actually carry values yet.

    A null alone is ambiguous: `root_known: null` could mean "this word has no
    root" or "the ability estimate does not exist", and a client renders those
    two differently — nothing at all, versus 功能待开发. The distinction belongs
    in the contract rather than being guessed from the value.
    """

    level_estimate: bool = Field(
        description="水平估计。它填的是 learner.level、difficulty_for_you 和 root_known"
    )
    memory_state: bool = Field(description="每个义项的记忆状态与到期时间")
    proper_noun_notes: bool = Field(description="专有名词的一句话说明")
    exam_frequency: bool = Field(
        description="真题考频。为假时 exam 整块缺席而不是为零——零会被读成「真题里从没出现过」，那是另一回事"
    )
    phrases: bool = Field(
        description="词组识别跑过没有。为假时，空的 phrases 列表意思是「还没看过」而不是「这篇没有词组」"
    )
    phrase_senses: bool = Field(
        default=False,
        description="词组有没有义项集（P11）。为假时词组只有一个整体，"
        "空的 senses 意思是「服务端还没导」而不是「这个词组只有一个意思」",
    )
    collocations: bool = Field(
        default=False,
        description="义项的搭配（用法）导进来没有。**P11 只存只发，复习里一处不用**——"
        "怎么用是界面 Phase 的事，那时这一位已经是真的了，不用再等一轮服务端发版",
    )


class ItemState(BaseModel):
    """One item's position in the three-bucket pool, and how often it was met.

    Shared because a word's sense and a phrase carry the same two facts.
    """

    pool: str = Field(description="new 没学过 / reviewing 在学 / known 已掌握")
    encounters: int = Field(description="遇见次数。读完一篇 +1，词池位置不动")
