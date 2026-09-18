"""Endpoints for review.

Two surfaces that never touch, same as reading: ``/v1/client`` behind a device
token, ``/v1/admin`` behind the admin credential.

**This is the "new endpoint" P2 promised.** ``docs/phase-2.html`` §6 refused to
guess what a review card looks like and said the shape would arrive as its own
endpoint rather than as fields bolted onto the article response. This is it.
"""

from __future__ import annotations

from datetime import datetime, timezone
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
    calendar, clock, repository, scheduler, sentences, session, translate,
)
from backend.modules.progress import module as progress_module
from backend.modules.review.contract import (
    AnswerResponse,
    AnswersResponse,
    CalendarResponse,
    ReviewDayResponse,
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


#: The day's review, assembled. Lives in :mod:`.session` now that something
#: other than this file needs it — 今日包 composes it alongside the articles.
_payload = session.day_payload


# --------------------------------------------------------------------------- #
# Client surface
# --------------------------------------------------------------------------- #

@client_router.get("/reviews", summary="今天要复习的全部内容",
                   response_model=ReviewDayResponse)
async def reviews(device_id: DeviceId) -> dict[str, Any]:
    return _payload(_learner(device_id))


class AnswerIn(BaseModel):
    queue_id: int = Field(
        default=0,
        description="今天这一轮的排队号。**不是义项 id。**"
        "**P9 起可以是 0**:队列由设备自己组，而这个号是服务端那张表的行号、"
        "每天重建——带身份（下面三个字段）来的作答只被记下来，服务端不再算一遍",
    )
    passed: bool
    revealed: int = Field(default=0, ge=0, le=session.MAX_REVEAL)
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

    **和 `/reviews` 的关系**:那个端点现在还在（老客户端要它，铁律 5），
    但它带着队列、方向、权重、进度——全是学习状态。这一个只带内容。
    '''
    learner_id = auth.learner_for_device(device_id)
    status = progress_module.snapshot_status(learner_id)
    rows = get_connection("learning").execute(
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


@client_router.get("/reviews/calendar", summary="打卡日历与连续天数",
                   response_model=CalendarResponse)
async def reviews_calendar(device_id: DeviceId, days: int = Query(7, ge=1, le=60)
                           ) -> dict[str, Any]:
    """The last ``days`` days and the streak.

    **A new endpoint rather than fields on `/reviews`.** 跨 Phase 不变量 only
    allows obvious shapes to be reserved in place; a list of days is not one, so
    it arrives as its own endpoint the way the invariant says complex additions
    should.
    """
    learner_id = _learner(device_id)
    return {"learner": auth.learner_profile(learner_id),
            **calendar.calendar(learner_id, days=days)}


@client_router.post("/reviews/answer", summary="上报一次作答",
                    response_model=AnswerResponse)
async def report_answer(device_id: DeviceId, body: AnswerIn) -> dict[str, Any]:
    learner_id = _learner(device_id)
    result = session.answer(
        learner_id, body.queue_id,
        passed=body.passed, revealed=body.revealed, sentence_id=body.sentence_id,
        easy=body.easy,
    )
    return {**result, "progress": session.progress(learner_id)}


class SpellingIn(BaseModel):
    item_key: str
    typed: str


class AnswersIn(BaseModel):
    """A batch of answers that happened while the device was on its own.

    ``idem_key`` is generated by the client, not the server: the client is the
    only party that knows whether this is the same answer it failed to upload
    ten minutes ago on a train. Same reasoning, same field name and same table
    as the reading events — one event store, not two (see ``client_events``,
    whose own comment anticipated this phase).
    """

    answers: list["AnswerItem"] = Field(default_factory=list, max_length=500)


class AnswerItem(AnswerIn):
    idem_key: str = Field(min_length=8, max_length=128, description="客户端生成的幂等键")
    occurred_at: str | None = Field(default=None, description="客户端时钟，可能不准")


AnswersIn.model_rebuild()


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

    The single-answer endpoint stays exactly as it was. 架构铁律 5 is only
    additive, and a client written against it keeps working untouched.
    """
    learner_id = _learner(device_id)
    accepted = duplicates = failed = 0
    results: list[dict[str, Any]] = []

    for item in body.answers:
        payload = item.model_dump()
        record = reading_repository.record_event(
            item.idem_key, device_id, learner_id, "review.answered",
            payload, item.occurred_at,
        )
        if record is None:
            duplicates += 1
            results.append({"idem_key": item.idem_key, "status": "duplicate"})
            continue

        # **按身份来的作答:只存下来，不解释**（P9 §11）。
        #
        # 那条线说学习状态由设备算。设备从 P9 起自己组队列、自己排期，
        # 所以它发来的作答**没有队列号**——那个号是服务端这张表的行号，
        # 每天重建，日志里引它就不是可重放的日志了（§16 ⑥）。
        #
        # 所以这一支什么都不算，只把事件记下来:它已经由 `record_event` 存进
        # `client_events`，而那正是多设备的会合点。**服务端不再重复算一遍**——
        # 算了也没人看，而两边各算一遍才是真会分家的做法。
        #
        # 旧那一支（带真队列号的）留着不动:Web 复习页和老客户端要它，
        # 而铁律 5 只增不减。它随那两样一起走（P10）。
        if not item.queue_id and item.item_key:
            reading_repository.finish_event(record.id)
            accepted += 1
            results.append({"idem_key": item.idem_key, "status": "accepted"})
            continue
        # `record.retry` means the earlier attempt never took effect. Answering
        # is not idempotent the way the reading events are — `asks` climbs, and
        # `asks` is the grade — so it is worth saying why a second attempt is
        # nevertheless the right call. Every way `session.answer` refuses (queue
        # row missing, item already done today) raises before it writes
        # anything, so a failed attempt left the item where it was. The residual
        # risk is a database error partway through, which would count one extra
        # ask on one item; the alternative is the failure this whole change
        # exists to remove — an answer that is silently never applied, which no
        # amount of later review can repair because nothing knows it happened.
        try:
            outcome = session.answer(
                learner_id, item.queue_id,
                passed=item.passed, revealed=item.revealed,
                sentence_id=item.sentence_id, easy=item.easy,
            )
        except Exception as exc:  # noqa: BLE001 - one bad answer must not sink the batch
            reading_repository.finish_event(record.id, str(exc))
            failed += 1
            results.append({"idem_key": item.idem_key, "status": "failed",
                            "reason": str(exc)})
            log.warning(
                "review.answer.failed",
                f"补报的一条作答没能应用：{exc}",
                idem_key=item.idem_key, queue_id=item.queue_id,
            )
            continue
        reading_repository.finish_event(record.id)
        accepted += 1
        results.append({"idem_key": item.idem_key, "status": "accepted",
                        "result": outcome})

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
        "progress": session.progress(learner_id),
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

    One function rather than two copies of four lines: what counts as correct is
    a rule, and a rule written twice is a rule that will disagree with itself.
    """
    session_row = repository.session_for(learner_id, repository.today())
    typed = typed.strip()
    correct = typed.lower() == item_key.strip().lower()
    session_id = session_row["id"] if session_row else None
    repository.record_spelling(learner_id, session_id, item_key, item_key, typed, correct)

    # **拼完了要留下痕迹。**`spelling_at` 这一列从 P3 建表起就在，而在
    # 2026-09-16 之前**没有任何代码写过它**——于是 `spelling_available` 只看
    # 「条目都做完了」，拼过多少遍都照样提示，每次还从第一个词拼起。
    # 真机上就是这么发现的：account 拼了好几遍，每次进去还在。
    if session_id is not None:
        expected = {w["item_key"] for w in session.spelling_words(learner_id)}
        if expected and expected <= repository.spelled_keys(session_id):
            repository.mark_spelled(session_id)
    return {"correct": correct, "expected": item_key}


class SpellingItem(SpellingIn):
    idem_key: str = Field(min_length=8, max_length=128, description="客户端生成的幂等键")
    occurred_at: str | None = Field(default=None, description="客户端时钟，可能不准")


class SpellingsIn(BaseModel):
    spellings: list[SpellingItem] = Field(default_factory=list, max_length=500)


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

@admin_router.get("/review/today", summary="今天的复习队列")
async def admin_today(learner_id: int = Query(1)) -> dict[str, Any]:
    return _payload(learner_id)


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
    conn = get_connection("learning")
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
    target = int(runtime_config.get("review_pool_target"))
    rows = get_connection("learning").execute(
        """
        SELECT s.item_key, s.sense_id,
               (SELECT COUNT(*) FROM review_sentences r
                 WHERE r.item_key = s.item_key AND r.sense_id = s.sense_id) AS total,
               (SELECT COUNT(*) FROM review_sentences r
                 WHERE r.item_key = s.item_key AND r.sense_id = s.sense_id
                   AND r.source = 'generated') AS generated
          FROM study_states s
         WHERE s.learner_id = ? AND s.item_type = 'word' AND s.pool = 'reviewing'
         ORDER BY total ASC LIMIT ?
        """,
        (learner_id, limit),
    ).fetchall()
    items = [dict(r) for r in rows]
    return {
        "target": target,
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


RATING_NAME = {1: "Again 没记住", 2: "Hard 磕绊", 3: "Good 答对", 4: "Easy 太简单"}
MARK_NAME = {"unknown": "不认识", "fuzzy": "模糊"}


def _band(stability: float | None) -> str:
    """A plain-language read of the memory strength.

    Purely a rendering of ``stability`` — the number is right there beside it,
    and no new concept is being introduced. Bands exist because "S=1.2" says
    nothing to a person and "撑一两天" does.
    """
    if stability is None:
        return "还没复习过"
    if stability < 3:
        return "刚开始，撑一两天"
    if stability < 14:
        return "有印象，撑一两周"
    if stability < 60:
        return "记住了，撑一两个月"
    return "稳固"


@admin_router.get("/review/words", summary="所有标记过的词：评级、下次复习、出处、句子")
async def admin_words(learner_id: int = Query(1), limit: int = Query(500, ge=1, le=2000),
                      ) -> dict[str, Any]:
    now = clock.now()
    out = []
    for row in repository.overview(learner_id, limit=limit):
        due = row["due_at"]
        days = None
        if due:
            days = round((datetime.fromisoformat(due) - now).total_seconds() / 86400, 1)
        out.append({
            **row,
            "sense": session.sense_of(row["sense_id"]),
            "mark_label": MARK_NAME.get(row["mark_kind"] or "", row["mark_kind"]),
            "rating_label": RATING_NAME.get(row["last_rating"] or 0, "还没结算过"),
            "band": _band(row["stability"]),
            "due_in_days": days,
            "overdue": bool(due and days is not None and days <= 0),
        })
    return {"count": len(out), "clock": clock.status(), "items": out}


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
    rows = get_connection("learning").execute(
        "SELECT * FROM review_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return {"history": [dict(r) for r in rows]}


@admin_router.get("/review/schedule-preview", summary="调度预览：这次评分之后什么时候再来")
async def admin_preview(item_key: str, sense_id: int = Query(0),
                        learner_id: int = Query(1)) -> dict[str, Any]:
    """What each grade would do to this item, without changing anything.

    A diagnostic, and the quickest way to see whether the scheduler is wired up
    the way the plan says: the four intervals must come out Easy > Good > Hard >
    Again.
    """
    state = repository.state_of(learner_id, "word", item_key, sense_id)
    now = clock.now()
    out = {}
    for misses in (0, 1, 2):
        outcome = scheduler.review(state, misses, now, fuzz=False)
        out[misses] = {
            "rating": outcome.rating.name,
            "interval_days": round(outcome.interval_days, 2),
            "due_at": outcome.due_at.isoformat(timespec="seconds") if outcome.due_at else None,
        }
    out["easy"] = {
        "rating": (e := scheduler.review(state, 0, now, easy=True, fuzz=False)).rating.name,
        "interval_days": round(e.interval_days, 2),
        "due_at": e.due_at.isoformat(timespec="seconds") if e.due_at else None,
    }
    return {"item_key": item_key, "sense_id": sense_id, "state": state, "by_misses": out}


# --------------------------------------------------------------------------- #
# Page — a development-period tool, per architecture rule 8
# --------------------------------------------------------------------------- #

WEB_REVIEW_DEVICE = "Web 复习页（开发期）"


def _web_review_token() -> str:
    """A device token for the development review page.

    Same reasoning as the reading page: served under the admin session it could
    have skipped the client contract entirely, and the client contract is the
    thing that has to be right. So it registers as a device and goes through
    ``/v1/client`` like any other client.
    """
    token = runtime_config.get("review_web_token")
    if token:
        return str(token)
    issued = auth.create_device(WEB_REVIEW_DEVICE)
    runtime_config.set("review_web_token", issued)
    return issued


@pages_router.get("/admin/review/words", response_class=HTMLResponse)
async def words_page(request: Request) -> Response:
    if (redirect := require_page_auth(request)) is not None:
        return redirect
    response = render(request, "review_words.html")
    response.headers["Cache-Control"] = "no-store"
    return response


@pages_router.get("/admin/review", response_class=HTMLResponse)
async def review_page(request: Request) -> Response:
    if (redirect := require_page_auth(request)) is not None:
        return redirect
    response = render(request, "review.html",
                      token=_web_review_token(),
                      target=int(runtime_config.get("review_pool_target")))
    # The page carries its own logic, and during development that logic changes
    # several times a day. A cached copy looks exactly like a bug that was not
    # fixed — which is how half an hour went into a bug that had already been
    # fixed but not reloaded.
    response.headers["Cache-Control"] = "no-store"
    return response
