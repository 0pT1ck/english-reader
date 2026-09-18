"""One day's review: what is in play, what to ask next, what an answer does.

会话 session / 当天池 today bucket / 到期池 due bucket / 解锁 unlock

The shape, from 决定 7–19:

* the day's items go into one pool; each draw is **weighted random**, and a
  failure halves that item's weight so a stuck word stops blocking the rest
  without ever being let off;
* an item is asked in the direction it has reached — 看词想义 first, and only
  passing that unlocks 看义想词. Failing 看义想词 locks it again and sends the
  item back to 看词想义;
* passing 看词想义 **puts the item back in the pool** rather than asking the
  second direction straight away. Asking immediately would test the answer just
  read, which is worth nothing; a few items later is a real recall. The random
  draw gives this for free;
* **the day ends when the pool is empty, and only then** (决定 15). Halving the
  weight is a way of yielding, not an exit. The cost is accepted: a bad day can
  be dragged out by one stubborn word, and in exchange "today's review is done"
  means it.

The unlock lives on the queue row, not on ``study_states`` — the plan had put it
on the study record, but it turned out to be session state: 决定 7 re-earns it
every round, and a durable column would have implied it survives the day.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from backend.core import auth, runtime_config
from backend.core.db import get_connection
from backend.core.errors import InvalidRequest, NotFound
from backend.core.logging import get_logger
from backend.modules.review import clock, repository, scheduler, sentences

log = get_logger("review.session")

WORD_TO_SENSE = 1      # 看词想义
SENSE_TO_WORD = 2      # 看义想词

#: How far the hints go. **One level as of P7** (2026-09-13): 「不确定」 is a
#: single tap that shows the whole hint and turns the three buttons into two.
#: Three levels were a ladder the interface never wanted, and the middle rung
#: of the 看义想词 ladder handed over the answer a level early.
#:
#: So ``revealed`` is now 0 or 1 — and as of the same date it is no longer only
#: recorded: taking the hint **costs a grade** (``scheduler.rating_for``),
#: because a hint that costs nothing is a hint everyone takes first.
#:
#: 2026-09-17 订正: 这里原本写的是 "caps the grade at Hard"，而 scheduler 那边
#: 写的是 "costs a grade"——两句话说的是两回事，只是在 ``Good`` 上结果相同所以
#: 没人发现。**降一档才是这一条**；封顶是 ``capped`` 那一条（当天刚标的词）。
MAX_REVEAL = 1


def _now(now: datetime | None = None) -> datetime:
    """The module's single entry point for the current time.

    Everything that compares against a due date, decides which day a session
    belongs to, or is handed to the scheduler comes through here — which is what
    lets the simulated clock move all of them together. See :mod:`.clock`.
    """
    return now or clock.now()


# --------------------------------------------------------------------------- #
# Building the day
# --------------------------------------------------------------------------- #

def collect(learner_id: int, now: datetime) -> list[dict[str, Any]]:
    """What belongs in today's pool, and which bucket each item is in.

    Three cases, and the third is 决定 18 — the day "模糊" stops being a label
    that only gets recorded:

    * due by the schedule                            → ``due``
    * never scheduled, marked **today** as 不认识     → ``today``, asked today
    * never scheduled, marked **today** as 模糊       → ``today`` as well, but
      **capped**: P3 决定 18 kept these until tomorrow because "you have just
      read it and of course you know it". P7 overturned that for one reason —
      the card said 3 when you had marked 5, and nothing explained why. The
      original argument was not wrong though, so the answer still does not
      count for full marks: see ``capped`` on the queue row.
    * never scheduled, marked on an earlier day       → ``due``, it is overdue
    """
    today = repository.today(now)
    marked_today = {
        (r["item_key"], r["sense_id"]): (r.get("mark_kind") or "unknown")
        for r in repository.marked_on(learner_id, today)
    }

    chosen: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for row in repository.due_items(learner_id, now):
        key = (row["item_key"], row["sense_id"])
        seen.add(key)
        chosen.append({**row, "bucket": "due"})

    # Items that have never been scheduled at all: everything marked before P3,
    # plus everything marked since the last session ran.
    rows = get_connection("learning").execute(
        "SELECT * FROM study_states WHERE learner_id = ? AND item_type = 'word'"
        "  AND pool = 'reviewing' AND due_at IS NULL",
        (learner_id,),
    ).fetchall()
    for row in rows:
        row = dict(row)
        key = (row["item_key"], row["sense_id"])
        if key in seen:
            continue
        kind = marked_today.get(key)
        seen.add(key)
        chosen.append({
            **row,
            "bucket": "today" if kind else "due",
            # Only the fuzzy ones: 不认识 was always asked the same day (决定 18's
            # other half) and P3 accepted a full grade for it.
            "capped": 1 if kind == "fuzzy" else 0,
        })

    return chosen


def ensure(learner_id: int, now: datetime | None = None) -> dict[str, Any]:
    """Today's session, created and filled if it does not exist yet."""
    now = _now(now)
    session = repository.open_session(learner_id, repository.today(now))
    items = collect(learner_id, now)
    if items:
        repository.enqueue(session["id"], items)
    return session


# --------------------------------------------------------------------------- #
# Asking
# --------------------------------------------------------------------------- #

def _weights(rows: list[dict[str, Any]]) -> list[float]:
    return [max(0.0001, float(r["weight"])) for r in rows]


def next_question(learner_id: int, *, now: datetime | None = None,
                  rng: random.Random | None = None) -> dict[str, Any] | None:
    """Draw one item and build the question for it. ``None`` when the day is done."""
    now = _now(now)
    rng = rng or random
    session = ensure(learner_id, now)
    open_rows = repository.queue_rows(session["id"], open_only=True)
    if not open_rows:
        repository.finish_session(session["id"])
        return None

    row = rng.choices(open_rows, weights=_weights(open_rows), k=1)[0]
    finished = repository.finished_article_ids(learner_id)
    seen = repository.seen_sentence_ids(learner_id)
    used = repository.sentences_used_today(session["id"])
    sentence = sentences.pick_question(
        row["item_key"], row["sense_id"], finished=finished, exclude=used,
        seen_sentence_ids=seen, rng=rng
    )
    hint = sentences.pick_hint(row["item_key"], row["sense_id"], finished=finished,
                               seen_sentence_ids=seen, rng=rng)

    return {
        "session_id": session["id"],
        "queue_id": row["id"],
        "item": {"item_type": row["item_type"], "item_key": row["item_key"],
                 "sense_id": row["sense_id"]},
        "bucket": row["bucket"],
        "direction": int(row["step"]),
        "asks": int(row["asks"]),
        "sentence": sentences.as_card(sentence) if sentence else None,
        "hint_sentence": sentences.as_card(hint) if hint else None,
        "remaining": len(open_rows),
    }


def answer(learner_id: int, queue_id: int, *, passed: bool, revealed: int = 0,
           sentence_id: int | None = None, easy: bool = False,
           now: datetime | None = None) -> dict[str, Any]:
    """Record one answer and move the item along.

    Settlement — handing the day's ask-count to the scheduler — happens exactly
    once per item, when 看义想词 passes and the item leaves the pool.
    """
    now = _now(now)
    row = get_connection("learning").execute(
        "SELECT * FROM review_queue WHERE id = ?", (queue_id,)
    ).fetchone()
    if row is None:
        raise NotFound("这个复习条目不存在")
    row = dict(row)
    if row["done_at"]:
        raise InvalidRequest("这个词今天已经过了")

    item = {"item_type": row["item_type"], "item_key": row["item_key"],
            "sense_id": row["sense_id"]}
    direction = int(row["step"])
    asks = int(row["asks"]) + 1
    misses = int(row["misses"] or 0) + (0 if passed else 1)
    decay = float(runtime_config.get("review_weight_decay"))

    # Claimed, not inferred, and only on a clean round — see scheduler.EASY_RATING.
    claimed_easy = bool(row["easy"]) or (easy and misses == 0)
    # The most help taken on this item today, not the most recent: a hint on the
    # first direction still counted, even if the second went unaided.
    revealed_max = max(int(row["revealed"] or 0), max(0, min(MAX_REVEAL, int(revealed))))
    capped = bool(row["capped"])

    settled: dict[str, Any] | None = None

    if passed and direction == WORD_TO_SENSE:
        # Unlock the second direction, then put it back — see the module note on
        # why it is not asked straight away.
        repository.update_queue(queue_id, step=SENSE_TO_WORD, asks=asks,
                                easy=int(claimed_easy), revealed=revealed_max)
    elif passed and direction == SENSE_TO_WORD:
        repository.update_queue(queue_id, asks=asks, easy=int(claimed_easy),
                                revealed=revealed_max, done_at=repository.now_iso())
        settled = settle(learner_id, item, misses=misses, easy=claimed_easy,
                         revealed=revealed_max, capped=capped, now=now)
    else:
        weight = max(0.0001, float(row["weight"]) * decay)
        # Failing the second direction locks it again (决定 7). A miss also
        # withdraws any earlier "that was easy": it plainly was not.
        repository.update_queue(queue_id, step=WORD_TO_SENSE, asks=asks, misses=misses,
                                easy=0, revealed=revealed_max, weight=weight)

    repository.record_answer(
        learner_id, row["session_id"], item,
        direction=direction, sentence_id=sentence_id,
        revealed=max(0, min(MAX_REVEAL, int(revealed))),
        result="pass" if passed else "fail",
        rating=int(settled["rating"]) if settled else None,
        interval_days=settled["interval_days"] if settled else None,
    )

    if not repository.queue_rows(row["session_id"], open_only=True):
        repository.finish_session(row["session_id"])

    return {"done": settled is not None, "asks": asks, "misses": misses,
            "easy": claimed_easy, "settled": settled}


def settle(learner_id: int, item: dict[str, Any], *, misses: int, easy: bool = False,
           revealed: int = 0, capped: bool = False,
           now: datetime, fuzz: bool | None = None) -> dict[str, Any]:
    """Hand one finished round to the scheduler and store what comes back.

    ``revealed`` and ``capped`` are two ceilings on the grade, both of which can
    only lower it — see :func:`scheduler.rating_for` for what each one means.
    """
    state = repository.state_of(learner_id, item["item_type"], item["item_key"],
                                item["sense_id"])
    outcome = scheduler.review(state, misses, now, easy=easy,
                               revealed=revealed, capped=capped, fuzz=fuzz)
    repository.save_state(learner_id, item["item_type"], item["item_key"],
                          item["sense_id"], outcome.state)
    log.info("review.settled", "一个义项结算完毕",
             item=item["item_key"], sense_id=item["sense_id"], misses=misses, easy=easy,
             revealed=revealed, capped=capped,
             rating=int(outcome.rating), interval_days=round(outcome.interval_days, 2))
    return {
        "rating": int(outcome.rating),
        "rating_name": outcome.rating.name,
        "interval_days": round(outcome.interval_days, 2),
        "due_at": outcome.due_at.isoformat(timespec="seconds") if outcome.due_at else None,
    }


# --------------------------------------------------------------------------- #
# Progress and spelling
# --------------------------------------------------------------------------- #

def progress(learner_id: int, now: datetime | None = None) -> dict[str, Any]:
    now = _now(now)
    session = repository.session_for(learner_id, repository.today(now))
    if session is None:
        # `spelling_available` is stated here too, not left out. A field that
        # appears only on one branch cannot be written against — no session
        # means nothing is done, which means spelling is not on offer, and
        # saying so is more useful than saying nothing.
        return {"session": None, "total": 0, "done": 0, "remaining": 0,
                "buckets": {}, "buckets_done": {}, "spelling_available": False}
    rows = repository.queue_rows(session["id"])
    done = [r for r in rows if r["done_at"]]
    buckets: dict[str, int] = {}
    # **A second dict rather than changing what `buckets` holds.** 铁律 5 forbids
    # changing a field's meaning; a client written against "buckets is a count
    # per pool" must keep working. The two cards need 已复习/共, so the other
    # half arrives beside it.
    buckets_done: dict[str, int] = {}
    for r in rows:
        buckets[r["bucket"]] = buckets.get(r["bucket"], 0) + 1
        buckets_done.setdefault(r["bucket"], 0)
        if r["done_at"]:
            buckets_done[r["bucket"]] += 1
    return {
        "session": session,
        "total": len(rows),
        "done": len(done),
        "remaining": len(rows) - len(done),
        "buckets": buckets,
        "buckets_done": buckets_done,
        # 三个条件，第三个 2026-09-16 补：**这一轮拼过了就不再提示**。
        # 少了它，拼完回到主界面那一行还在，点进去又是全部的词。
        "spelling_available": bool(runtime_config.get("review_spelling"))
                              and len(rows) > 0 and len(done) == len(rows)
                              and not session.get("spelling_at"),
    }


def spelling_words(learner_id: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """The day's words for the optional spelling pass, **de-duplicated by word**.

    决定 13: several senses of one word spell the same, so the word appears once.
    """
    now = _now(now)
    session = repository.session_for(learner_id, repository.today(now))
    if session is None:
        return []
    seen: set[str] = set()
    out = []
    for row in repository.queue_rows(session["id"]):
        if not row["done_at"] or row["item_key"] in seen:
            continue
        seen.add(row["item_key"])
        out.append({"item_key": row["item_key"], "sense_id": row["sense_id"]})
    return out


def today_key(learner_id: int = 1) -> str:
    '''今天是哪天，按**模拟时钟**（P3 决定 5）。

    **只回一个日期，不碰任何学习状态。** 在这之前今日包是从 `day_payload` 里
    顺手拿到这个值的，而那个函数会 `ensure()`——也就是建会话、入队。
    于是每一次 `/today` 请求都在写学习状态，而那正是 P9 那条线禁止的事。

    时钟要用模拟的那个:往前拨 `offset_days` 去测排期，日历那一档和缓存有效性
    都得跟着动，否则测出来的不是 App 会显示的东西。
    '''
    del learner_id  # 目前所有学习者共用一个时钟，留着参数是为了将来分开
    return repository.today(clock.now())


def day_payload(learner_id: int, *, now: datetime | None = None) -> dict[str, Any]:
    """Everything today's review needs, in one response.

    Public because 今日包 composes it next to the day's articles (P4 决定 8);
    ``review.routes`` delegates here so there is one builder, not two.

    Architecture rule 2 asks for an offline shape: the client should be able to
    take this and work through the whole day without another request. So every
    item ships with its sentences and its hint already attached, rather than
    being fetched one question at a time.
    """
    now = now or clock.now()
    state = ensure(learner_id, now)
    rows = repository.queue_rows(state["id"])
    finished = repository.finished_article_ids(learner_id)
    seen = repository.seen_sentence_ids(learner_id)

    items = []
    for row in rows:
        questions, hints = sentences.split_pools(
            row["item_key"], row["sense_id"], finished, seen)
        items.append({
            "queue_id": row["id"],
            "item_type": row["item_type"],
            "item_key": row["item_key"],
            "sense_id": row["sense_id"],
            "bucket": row["bucket"],
            "direction": int(row["step"]),
            "asks": int(row["asks"]),
            "misses": int(row["misses"] or 0),
            "weight": float(row["weight"]),
            "done": bool(row["done_at"]),
            "sense": sense_of(row["sense_id"]),
            # The whole word, not just the sense being tested — the reveal
            # screen shows what P6's tap panel shows. **It has to come from
            # here**: the same data also sits in the article's glossary, but
            # article bodies are cleared per-article (P5 决定 10) and this word
            # may have been met two months ago. A review payload that leans on
            # a cached article is one that loses half its card, silently.
            # Measured: 392 bytes and 2.2 senses per word, 11.5 KB across a
            # 30-item day against a 1.2 MB package — under 1%.
            "word": word_of(row["item_key"]),
            "questions": [sentences.as_card(s) for s in questions],
            "hints": [sentences.as_card(s) for s in hints],
        })

    return {
        "learner": auth.learner_profile(learner_id),
        "day": state["day"],
        "session_id": state["id"],
        "finished_at": state["finished_at"],
        "weight_decay": float(runtime_config.get("review_weight_decay")),
        "spelling_enabled": bool(runtime_config.get("review_spelling")),
        "progress": progress(learner_id, now),
        "clock": clock.status(),
        "items": items,
    }


def word_of(headword: str) -> dict[str, Any] | None:
    """Everything the reveal screen shows about one word.

    Shares its shape with the reading glossary on purpose — the two screens show
    the same thing, and two shapes would drift.
    """
    import json

    from backend.modules.senses import repository as senses_repo
    from backend.modules.vocabulary import repository as dictionary

    sense_list = senses_repo.senses_of(headword)
    entry = dictionary.lookup(headword)
    if not sense_list and not entry:
        return None

    # The share, not the raw count: "78%" answers "is this the meaning the exam
    # actually tests" and a bare 38 does not. Computed the same way the reading
    # path computes it — one rule, not two.
    exam_total = sum(s.get("exam_frequency") or 0 for s in sense_list)

    out_senses = []
    for item in sense_list:
        gloss = item.get("gloss_zh")
        if isinstance(gloss, str):
            try:
                gloss = json.loads(gloss)
            except (TypeError, ValueError):
                gloss = [gloss]
        out_senses.append({
            "id": item["id"],
            "ordinal": item["ordinal"],
            "pos": item.get("pos"),
            "concept_en": item.get("concept_en"),
            "gloss_zh": gloss,
            "exam_frequency": item.get("exam_frequency") or 0,
            "share": (round((item.get("exam_frequency") or 0) / exam_total * 100, 1)
                      if exam_total else None),
        })

    return {
        "headword": headword,
        "phonetic": entry["phonetic"] if entry else None,
        # The dictionary's comma pile, as a fallback for words with no sense set.
        "translation": entry["translation"] if entry else None,
        "senses": out_senses,
    }


def sense_of(sense_id: int) -> dict[str, Any] | None:
    """One sense, as a card shows it. Public: routes renders it too."""
    if not sense_id:
        return None
    # **退休的义项也要找得到**（P9 §8）。一条两个月前的标记可能指着一个已经
    # 退休的义项——「退休而不是删除」换来的正是这个性质:它仍然指得到一个
    # 说得出话的义项，而不是指到空气，卡片因此不会突然变成一张空白。
    # 界面上列义项的地方照旧只列现行的（那些走 `senses_of`）。
    from backend.modules.senses import repository as senses_repo

    row = senses_repo.sense_by_id(sense_id)
    if row is None:
        return None
    import json
    item = dict(row)
    try:
        item["gloss_zh"] = json.loads(item["gloss_zh"] or "[]")
    except (TypeError, ValueError):
        item["gloss_zh"] = [str(item["gloss_zh"] or "")]
    return item
