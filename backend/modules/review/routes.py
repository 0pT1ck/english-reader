"""Endpoints for review.

Two surfaces that never touch, same as reading: ``/v1/client`` behind a device
token, ``/v1/admin`` behind the admin credential.

**This is the "new endpoint" P2 promised.** ``docs/phase-2.html`` §6 refused to
guess what a review card looks like and said the shape would arrive as its own
endpoint rather than as fields bolted onto the article response. This is it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from backend.admin.templating import render, require_page_auth
from backend.core import auth, runtime_config
from backend.core.db import get_connection
from backend.core.logging import get_logger
from backend.modules.reading import repository as reading_repository
from backend.modules.review import (
    clock, repository, sentences, session, translate,
)
from backend.modules.progress import module as progress_module
from backend.modules.review.contract import (
    AnswersResponse,
    SentencePoolResponse,
    SpellingResponse,
    SpellingsResponse,
)

log = get_logger("review.routes")

client_router = APIRouter()
admin_router = APIRouter(dependencies=[Depends(auth.require_admin)])
pages_router = APIRouter()

DeviceId = Annotated[int, Depends(auth.require_device)]


def _learner(device_id: int) -> int:
    return auth.learner_for_device(device_id)


# `_payload` 没有了:今天的复习队列不再由服务端组装（P9 §11）。


# --------------------------------------------------------------------------- #
# Client surface
# --------------------------------------------------------------------------- #

class AnswerIn(BaseModel):
    queue_id: int = Field(
        default=0,
        description="今天这一轮的排队号。**不是义项 id。**"
        "**P9 起可以是 0**:队列由设备自己组，而这个号是服务端那张表的行号、"
        "每天重建——带身份（下面三个字段）来的作答只被记下来，服务端不再算一遍",
    )
    passed: bool
    revealed: int = Field(
        default=0, ge=0,
        description="开了几级提示。**入站不设上限**，理由见下——"
        "服务端在解释它的时候 clamp 到 `MAX_REVEAL`",
    )
    # **上限 2026-09-19 从这里去掉了，而它是一次真的事故。**
    #
    # 原本写的是 `le=session.MAX_REVEAL`。而 P7 在 2026-09-13 把三级提示压成
    # 一级，`MAX_REVEAL` 从 3 变成 1——**于是服务端把自己的存档变成了自己
    # 收不下的东西**：`client_events` 里有 2 条 P7 之前记下的 `revealed=2`。
    #
    # 在 P9 之前没人会把老事件重新发上来，所以它一直没发作。§6 做了双向同步
    # 之后，设备把这些事件拉下来又推回来，那 2 条就让整批 118 条永远进不来
    # （422，而且是 pydantic 在 handler 之前拒的，逐条裁决那套完全没机会跑）。
    #
    # **教训不是「上限写错了」，是「入站字段的取值范围不许收紧」**——
    # 那等于事后宣布一批已经收下的事实为非法，而铁律 5 说的「不能改字段含义」
    # 正是这件事。上限属于**解释**这一侧:`grade` 那边照旧按 `MAX_REVEAL` 封顶。
    sentence_id: int | None = None
    #: "That was trivial." Only honoured on a round with no misses — the server
    #: checks, so a client cannot talk its way into a longer interval.
    easy: bool = False
    item_type: str | None = Field(
        default=None, description="word / phrase。**P9 加的**:见 item_key")
    item_key: str | None = Field(
        default=None,
        description="被考的那个词。**P9 加的**——日志里不许出现只有别处才解释得了的"
        "标识符:`queue_id` 是服务端那张表的行号，换台设备重放就指不到任何东西了",
    )
    sense_id: int | None = Field(default=None, description="义项 id。词组恒为 0")


@client_router.get("/sentences", summary="在学的那些词的句子（不分池）",
                   response_model=SentencePoolResponse)
async def sentence_pool(device_id: DeviceId) -> dict[str, Any]:
    '''你在学的每个词，连它的全部句子——**服务端不分池**。

    **P9 §11:那条线把两件事分开了。** 造句子要钱、要模型、要 24 小时醒着，
    是工厂的活；而「这一句该当考题还是当提示」取决于你读完过哪些文章、
    见过哪些句子——那是学习记录，现在在设备上。所以这里原样全给，
    由客户端分（`ERCore/SentencePool`，三条规则逐条镜像 `sentences.split_pools`）。

    **依据是你上报的词池快照**（§7），不是服务端自己推的。服务端对学习记录只有
    两种关系:生文需要的那一小撮信号，和它不解释的存档——这里用的是前者，
    而它连解释都不算:直接用。**所以还没报过快照的设备会拿到空列表**，
    而响应里的 `reported_at` 为空正是在说这件事:不是「你没在学任何词」，
    是「服务端还不知道」。

    **只给词，不给词组。** 复习有意跳过词组（`verify_phase3` 2.2），
    而句子池本来也是按词与义项建的——词组拿不到句子。

    **它取代了 `/reviews`，而不是补充它。** 那个端点带着队列、方向、权重、进度，
    全是学习状态；它在同一个 Phase 删掉了（§11）。这一个只带内容。
    '''
    learner_id = auth.learner_for_device(device_id)
    status = progress_module.snapshot_status(learner_id)
    rows = get_connection("events").execute(
        "SELECT item_key, sense_id FROM learner_pool"
        " WHERE learner_id = ? AND item_type = 'word' AND pool != 'new'"
        " ORDER BY item_key, sense_id",
        (learner_id,),
    ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        item_key, sense_id = str(row["item_key"]), int(row["sense_id"])
        items.append({
            "item_type": "word",
            "item_key": item_key,
            "sense_id": sense_id,
            "word": session.word_of(item_key),
            "sense": session.sense_of(sense_id),
            # **不分池。** `_rows` 是那张表的原样读取，而 `split_pools` 是
            # 它上面那层判断——搬走的正是那一层。
            "sentences": [sentences.as_card(s)
                          for s in sentences._rows(item_key, sense_id)],
        })

    log.info(
        "review.sentences.served",
        f"下发了 {len(items)} 个在学词的句子池",
        learner_id=learner_id, items=len(items),
        reported_at=status.get("reported_at"),
    )
    return {
        "learner": auth.learner_profile(learner_id),
        "reported_at": status.get("reported_at"),
        "items": items,
    }


class SpellingIn(BaseModel):
    item_key: str
    typed: str


class AnswerItem(AnswerIn):
    idem_key: str = Field(min_length=8, max_length=128, description="客户端生成的幂等键")
    occurred_at: str | None = Field(default=None, description="客户端时钟，可能不准")


class UnusableItem(BaseModel):
    """一条验不过去的补报。**留着它，好让它有资格被逐条拒绝。**

    这个类型存在的理由是一次事故（2026-09-19）。这个端点的契约写的是
    「a failure in the middle stops nothing — the one that failed is reported
    with its key so the client can decide」，而 **pydantic 的校验发生在
    handler 之前**——一条字段越界就让整批 500 条一起 422，
    逐条裁决那套设计完全没机会跑。**注释描述的是意图，实现从来没跟上**
    （坑 §7.1 的同一个形状，而那段注释是我自己刚写下的）。

    实际发作的样子:设备把老事件拉下来又推回去，其中 2 条是 P7 之前记的
    `revealed=2`，于是那 118 条作答**永远**进不来，手机一直转圈。

    所以列表的元素类型是「一条作答 **或** 一条验不过去的东西」。
    后者只需要认得出是哪一条（`idem_key`），好把 `failed` 连同理由回给客户端;
    认不出的话连拒绝都没法逐条拒绝，那就只能整批砸掉，也就回到了原点。
    """

    model_config = {"extra": "allow"}

    idem_key: str | None = Field(
        default=None, description="能认出是哪一条就够了，其余字段不做要求")


class AnswersIn(BaseModel):
    """A batch of answers that happened while the device was on its own.

    ``idem_key`` is generated by the client, not the server: the client is the
    only party that knows whether this is the same answer it failed to upload
    ten minutes ago on a train. Same reasoning, same field name and same table
    as the reading events — one event store, not two (see ``client_events``,
    whose own comment anticipated this phase).

    **元素是个联合类型，那是有意的**（2026-09-19）:见 :class:`UnusableItem`。
    一条坏的只挡住自己，不挡它后面的——那是这个端点从第一天就承诺的事，
    而在这之前它做不到。
    """

    answers: list[AnswerItem | UnusableItem] = Field(
        default_factory=list, max_length=500)


@client_router.post("/reviews/answers", summary="批量上报作答（离线补报用）",
                    response_model=AnswersResponse)
async def report_answers(device_id: DeviceId, body: AnswersIn) -> dict[str, Any]:
    """Replay a day's answers in order, skipping anything already recorded.

    **Why this exists at all.** ``/reviews`` has always been offline-shaped —
    every card ships with its sentences — but the answer path was one online
    POST per question with no key and no queue, so review failed on every
    question without a connection while reading carried on fine. That broke
    架构铁律 2 for half the product, and 读后抽查 being dropped left review as
    the only place the learner produces anything.

    **Order matters and is the client's.** A review session is a state machine:
    passing 看词想义 unlocks 看义想词, failing it locks it again. So the batch is
    applied in the order given, not concurrently, and a failure in the middle
    stops nothing — the remaining answers still apply, and the one that failed
    is reported with its key so the client can decide.

    **The single-answer endpoint is gone (P9 §11).** It was the one route that
    wrote learning state on the user's thumb — one POST per question, no key,
    no batch — and the whole point of this phase is that the device owns that
    state. 铁律 5「只增不减」was relaxed here on purpose and only here, for the
    state/sync half of the contract: the client that used it is the Web review
    page, which went away in the same phase. **Everything else stays additive.**
    """
    learner_id = _learner(device_id)
    accepted = duplicates = failed = 0
    results: list[dict[str, Any]] = []

    for item in body.answers:
        # **验不过去的那一条:逐条拒绝，带着理由。**
        # 在 2026-09-19 之前这里到不了——pydantic 在 handler 之前就把整批砸掉了，
        # 而这个端点的契约承诺的恰恰是「一条坏的只挡住自己」。
        if isinstance(item, UnusableItem):
            failed += 1
            reason = "这一条的字段验不过去，其余照常收下"
            results.append({"idem_key": item.idem_key or "?", "status": "failed",
                            "reason": reason})
            log.warning(
                "review.answer.unusable",
                f"补报里有一条验不过去:{item.idem_key or '没有幂等键'}",
                idem_key=item.idem_key,
                # **原样记下来**，因为「哪个字段不对」只有这里看得到，
                # 而客户端拿到的只有「这一条没收」。
                fields=sorted(item.model_dump().keys()),
            )
            continue

        payload = item.model_dump()
        record = reading_repository.record_event(
            item.idem_key, device_id, learner_id, "review.answered",
            payload, item.occurred_at,
        )
        if record is None:
            duplicates += 1
            results.append({"idem_key": item.idem_key, "status": "duplicate"})
            continue

        # **作答只存下来，不解释**（P9 §11）。
        #
        # 那条线说学习状态由设备算。设备从 P9 起自己组队列、自己排期，
        # 所以它发来的作答**没有队列号**——那个号是服务端那张表的行号，
        # 每天重建，日志里引它就不是可重放的日志了（§16 ⑥）。
        #
        # 所以这里什么都不算，只把事件记下来:它已经由 `record_event` 存进
        # `client_events`，而那正是多设备的会合点。**服务端不再重复算一遍**——
        # 算了也没人看，而两边各算一遍才是真会分家的做法。
        #
        # **带队列号、不带身份的那一支删掉了**，连同它背后的 `session.answer`。
        # 它是这条路上最后一处「服务端跟着用户的拇指改学习状态」，留着就等于
        # 那条线只画了一半。这样的一条补报现在报 failed 而不是静默丢掉——
        # 「没人认得这条」必须说出来，见「踩过的坑」§6.6。
        if not item.item_key:
            reason = "没有 item_key：P9 起作答按身份上报，队列号这条路已经删了"
            reading_repository.finish_event(record.id, reason)
            failed += 1
            results.append({"idem_key": item.idem_key, "status": "failed",
                            "reason": reason})
            log.warning(
                "review.answer.rejected",
                f"补报的一条作答没有身份，收不下：{item.idem_key}",
                idem_key=item.idem_key, queue_id=item.queue_id,
            )
            continue
        reading_repository.finish_event(record.id)
        accepted += 1
        results.append({"idem_key": item.idem_key, "status": "accepted"})

    log.info(
        "review.answers.replayed",
        f"补报 {len(body.answers)} 条作答：接受 {accepted}、重复 {duplicates}、失败 {failed}",
        accepted=accepted, duplicates=duplicates, failed=failed,
    )
    return {
        "accepted": accepted,
        "duplicates": duplicates,
        "failed": failed,
        "results": results,
    }


@client_router.post("/reviews/spelling", summary="上报一次拼写",
                    response_model=SpellingResponse)
async def report_spelling(device_id: DeviceId, body: SpellingIn) -> dict[str, Any]:
    """Recorded, never scheduled on.

    决定 13 keeps spelling out of the scheduler entirely: it is an optional
    reinforcement, and letting it move due dates would mix a productive-recall
    difficulty into a recognition track. What is stored is what was typed, not
    just whether it was right — that is the whole value of the record.
    """
    return _apply_spelling(_learner(device_id), body.item_key, body.typed)


def _apply_spelling(learner_id: int, item_key: str, typed: str) -> dict[str, Any]:
    """One spelling attempt, recorded. Shared by the single and batch paths.

    **2026-09-18 (P9 §11): it no longer decides anything.** It used to look up
    today's session, compare against `session.spelling_words`, and mark the day's
    spelling round finished — all of which is learning state, and all of which
    the device now works out for itself (`ERCore/ReviewDay.spellingWords()`).
    The row is written with no session: there are no sessions any more, and a
    made-up one would be a fact the server invented.

    What is still decided here is one thing, and it belongs here: **what counts
    as correct.** A rule written twice is a rule that will disagree with itself,
    and this is the copy the archive is built from.
    """
    typed = typed.strip()
    correct = typed.lower() == item_key.strip().lower()
    repository.record_spelling(learner_id, None, item_key, item_key, typed, correct)
    return {"correct": correct, "expected": item_key}


class SpellingItem(SpellingIn):
    idem_key: str = Field(min_length=8, max_length=128, description="客户端生成的幂等键")
    occurred_at: str | None = Field(default=None, description="客户端时钟，可能不准")


class SpellingsIn(BaseModel):
    """**元素同样是联合类型**，理由见 :class:`UnusableItem`。

    这一路还没出过事，而形状和作答那一路一模一样:一条字段验不过去就整批 422，
    而这个端点的契约写的也是「the same per-item verdict as the answers batch」。
    **等它出事再改，就是等一次「手机一直转圈」**——那一次已经付过了。
    """

    spellings: list[SpellingItem | UnusableItem] = Field(
        default_factory=list, max_length=500)


@client_router.post("/reviews/spellings", summary="批量上报拼写（离线补报用）",
                    response_model=SpellingsResponse)
async def report_spellings(device_id: DeviceId, body: SpellingsIn) -> dict[str, Any]:
    """The offline path for spelling, which P4 gave answers and not this.

    決定 12 of P4 added a batch endpoint for answers because "地铁里每答一题都
    失败" broke 架构前提 2 for half the product. Spelling was out of scope that
    day and kept its one-at-a-time online POST, so the last step of the day was
    still the one step that needed a network. Same store, same idempotency key,
    same per-item verdict as the answers batch — a client that already speaks
    that one needs no new vocabulary here.

    **Order does not matter here**, unlike answers: each attempt is an
    independent record, and spelling never touches the scheduler (决定 13). They
    are applied in the order given anyway, because replaying in sequence is
    what the client's outbox does.

    Re-applying an attempt whose first try failed writes a second row in
    `spelling_attempts`. That is a duplicate record rather than a wrong one —
    nothing reads the table to make a decision — and the alternative is the
    attempt being silently dropped.
    """
    learner_id = _learner(device_id)
    accepted = duplicates = failed = 0
    results: list[dict[str, Any]] = []

    for item in body.spellings:
        if isinstance(item, UnusableItem):
            failed += 1
            results.append({"idem_key": item.idem_key or "?", "status": "failed",
                            "reason": "这一条的字段验不过去，其余照常收下"})
            log.warning("review.spelling.unusable",
                        f"补报里有一条拼写验不过去:{item.idem_key or '没有幂等键'}",
                        idem_key=item.idem_key)
            continue

        record = reading_repository.record_event(
            item.idem_key, device_id, learner_id, "review.spelled",
            item.model_dump(), item.occurred_at,
        )
        if record is None:
            duplicates += 1
            results.append({"idem_key": item.idem_key, "status": "duplicate"})
            continue
        try:
            outcome = _apply_spelling(learner_id, item.item_key, item.typed)
        except Exception as exc:  # noqa: BLE001 - one bad entry must not sink the batch
            reading_repository.finish_event(record.id, str(exc))
            failed += 1
            results.append({"idem_key": item.idem_key, "status": "failed",
                            "reason": str(exc)})
            log.warning(
                "review.spelling.failed",
                f"补报的一条拼写没能记下：{exc}",
                idem_key=item.idem_key, item_key=item.item_key,
            )
            continue
        reading_repository.finish_event(record.id)
        accepted += 1
        results.append({"idem_key": item.idem_key, "status": "accepted",
                        "result": outcome})

    log.info(
        "review.spellings.replayed",
        f"补报 {len(body.spellings)} 条拼写：接受 {accepted}、重复 {duplicates}、失败 {failed}",
        accepted=accepted, duplicates=duplicates, failed=failed,
    )
    return {"accepted": accepted, "duplicates": duplicates,
            "failed": failed, "results": results}


# --------------------------------------------------------------------------- #
# Admin surface
# --------------------------------------------------------------------------- #

# **「今天的复习队列」那个管理端点 2026-09-18 删了**（P9 §11）:
# 队列由设备自己组，服务端组一个出来只会和设备手上那份说反话。
# 要看学习者在学什么，看 `/v1/admin/progress/pool`——那是设备**上报**的那一份。


@admin_router.post("/review/sentences/generate", summary="给缺句子的义项生成例句")
async def admin_generate(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    job_id = sentences.start_for(
        payload.get("senses"),
        learner_id=int(payload.get("learner_id", 1)),
        provider_id=payload.get("provider_id"),
    )
    return {"job_id": job_id}


@admin_router.post("/review/sentences/translate", summary="给句子配中文")
async def admin_translate(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Queue the translation batch.

    ``limit`` caps it — used to spot-check a small batch before letting the
    whole pool run, which is the only cheap way to find out whether the prompt
    aligns the target word correctly.
    """
    payload = payload or {}
    limit = payload.get("limit")
    job_id = translate.start(
        limit=int(limit) if limit else None,
        provider_id=payload.get("provider_id"),
    )
    return {"job_id": job_id, "pending": translate.pending_count()}


@admin_router.get("/review/sentences/translate", summary="还有多少句子没配中文")
async def admin_translate_status() -> dict[str, Any]:
    conn = get_connection("content")
    total = int(conn.execute("SELECT COUNT(*) FROM review_sentences").fetchone()[0])
    done = int(conn.execute(
        "SELECT COUNT(*) FROM review_sentences WHERE text_zh IS NOT NULL").fetchone()[0])
    aligned = int(conn.execute(
        "SELECT COUNT(*) FROM review_sentences WHERE zh_start IS NOT NULL").fetchone()[0])
    return {"total": total, "translated": done, "aligned": aligned,
            "pending": total - done}


@admin_router.get("/review/pool", summary="句子池的覆盖情况")
async def admin_pool(learner_id: int = Query(1), limit: int = Query(200, ge=1, le=2000)
                     ) -> dict[str, Any]:
    """**照上报的词池快照数，不照服务端自己那份存档**（P9 §7）。

    句子是工厂的活:哪个词的池子不够深，决定的是今晚生成什么。而「哪些词在学」
    从 P9 起是设备算出来、上报上来的一个值——服务端不再自己推。

    **没报过快照时给空**，并把 `reported` 说出来。**空不等于「都够了」**:
    看不出这两者差别的页面，会在设备一周没同步的时候显示一片绿。
    """
    target = int(runtime_config.get("review_pool_target"))
    status = progress_module.snapshot_status(learner_id)
    rows = get_connection("content").execute(
        """
        SELECT p.item_key, p.sense_id,
               (SELECT COUNT(*) FROM review_sentences r
                 WHERE r.item_key = p.item_key AND r.sense_id = p.sense_id) AS total,
               (SELECT COUNT(*) FROM review_sentences r
                 WHERE r.item_key = p.item_key AND r.sense_id = p.sense_id
                   AND r.source = 'generated') AS generated
          FROM learner_pool p
         WHERE p.learner_id = ? AND p.item_type = 'word' AND p.pool = 'reviewing'
         ORDER BY total ASC LIMIT ?
        """,
        (learner_id, limit),
    ).fetchall()
    items = [dict(r) for r in rows]
    return {
        "target": target,
        "reported": status["reported"],
        "reported_at": status.get("reported_at"),
        "items": items,
        "short": [i for i in items if i["total"] < target],
    }


@admin_router.post("/review/clock", summary="模拟时钟：跳到下一天 / 归零")
async def admin_clock(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Move the review module's clock forward, or put it back.

    A development tool. Review is scheduled in days, so without this the first
    honest look at the schedule would be a week after writing it. It lies about
    the date on purpose, so it says so loudly: the page shows a banner while the
    offset is non-zero, and acceptance refuses to run in a simulated future.
    """
    payload = payload or {}
    if payload.get("reset"):
        clock.reset()
    else:
        clock.advance(int(payload.get("days", 1)))
    return clock.status()


@admin_router.get("/review/clock", summary="模拟时钟现在停在哪")
async def admin_clock_status() -> dict[str, Any]:
    return clock.status()


MARK_NAME = {"unknown": "不认识", "fuzzy": "模糊"}

#: 词池三档的中文。**这一列现在有两个来源**，见 `repository.overview`。
POOL_NAME = {"new": "还没标", "reviewing": "在学", "graduated": "学过了"}


@admin_router.get("/review/words", summary="标记过的词：标记、出处、句子池、设备报的词池")
async def admin_words(learner_id: int = Query(1), limit: int = Query(500, ge=1, le=2000),
                      ) -> dict[str, Any]:
    """**2026-09-18 改写（P9 §11）:评级、下次复习、记忆强度这三列没有了。**

    它们是服务端算的，而服务端不再算了——那几列自重构之日起没有被写过一次。
    留着它们比删掉糟得多:页面看上去信息齐全，报的却是一个冻结在某一天的数
    （「踩过的坑」§8 里那一整类）。要看排期得去设备上看,那是它算的。

    换上来的是服务端确实知道的:标记、在哪篇哪句遇见的、这个词编了几句，
    以及**设备报的词池和服务端存档各说什么**——两列并排放，对不上就看得见。
    """
    status = progress_module.snapshot_status(learner_id)
    out = []
    for row in repository.overview(learner_id, limit=limit):
        reported = row["pool_reported"]
        out.append({
            **row,
            "sense": session.sense_of(row["sense_id"]),
            "mark_label": MARK_NAME.get(row["mark_kind"] or "", row["mark_kind"]),
            "pool_archive_label": POOL_NAME.get(row["pool_archive"] or "", row["pool_archive"]),
            # 没报过快照时是 None，而 None 不该显示成「还没标」——
            # 「设备还没说」和「设备说它是 new」是两件事。
            "pool_reported_label": POOL_NAME.get(reported or "", reported) if reported else None,
            "pool_disagrees": bool(reported) and reported != row["pool_archive"],
            "short_of_target": int(row["pool_total"] or 0) < int(
                runtime_config.get("review_pool_target")),
        })
    return {"count": len(out), "clock": clock.status(),
            "snapshot": status, "items": out}


@admin_router.get("/review/words/{item_key}/sentences", summary="这个词编好的句子")
async def admin_word_sentences(item_key: str, sense_id: int = Query(0),
                               item_type: str = Query("word")) -> dict[str, Any]:
    rows = repository.sentences_of(item_type, item_key, sense_id)
    finished = repository.finished_article_ids(1)
    seen = repository.seen_sentence_ids(1)
    out = []
    for row in rows:
        card = sentences.as_card(row)
        is_hint = row["source"] == "corpus" and (
            row["article_id"] in finished or row["sentence_id"] in seen)
        out.append({**card,
                    "pool": "提示池（你读过这篇）" if is_hint else "考句池",
                    "article_title": row["article_title"],
                    "article_source": row["article_source"],
                    "model": row["model"]})
    return {"item_key": item_key, "sense_id": sense_id,
            "sense": session.sense_of(sense_id), "count": len(out), "sentences": out}


@admin_router.get("/review/history", summary="复习历史")
async def admin_history(limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    rows = get_connection("events").execute(
        "SELECT * FROM review_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return {"history": [dict(r) for r in rows]}


# **那个「四档评级各会排到哪天」的预览端点 2026-09-18 删了**（P9 §11）。
# 它是个诊断，而它靠服务端算一遍排期——那条线说服务端不算这个。
# 同一件事现在由 `scheduler-vectors.json` 回答，而且答得更细:
# 32 条评级映射 ＋ 15 个排期场景，逐条有明文依据。

# --------------------------------------------------------------------------- #
# Page — a development-period tool, per architecture rule 8
# --------------------------------------------------------------------------- #

WEB_REVIEW_DEVICE = "Web 复习页（开发期）"


@pages_router.get("/admin/review/words", response_class=HTMLResponse)
async def words_page(request: Request) -> Response:
    if (redirect := require_page_auth(request)) is not None:
        return redirect
    response = render(request, "review_words.html")
    response.headers["Cache-Control"] = "no-store"
    return response


# **Web 复习页 2026-09-18 删掉了**（P9 §11）。
#
# 它要能复习，就得在 JS 里再实现一遍学习引擎——而那正是那条线禁止的事。
# 铁律 8 本来就写着 Web 阅读页是「开发期测试工具，用完即弃」，复习页同理。
# 复习现在在手机上和 `ercli` 上，两者共用同一份 Core。
#
# **Web 阅读页还在**，它到这个 Phase 的最后一步才删——`verify_phase2` 的人工项
# M1 指着它，而那一项要等验收网搬完才改得动。
