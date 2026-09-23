"""What the server still answers about a word: its senses, and today's date.

会话 session / 当天池 today bucket / 到期池 due bucket / 解锁 unlock

**2026-09-18 (P9 §11): this module used to be the day itself.** It held the
pool, the weighted draw, the unlock, the settlement — 决定 7–19, roughly 480
lines of it. Every one of those is a decision made while the learner is looking
at a card, and P9 put that side of the line on the device (`phase-9.html` §4).
The transitions now live in `ERCore/Review.swift`, checked against an
independent Python transcription in `scripts/review_reference.py`; the intervals
live in `ERCore/Scheduler.swift`, checked against `scripts/fsrs_reference.py`.

What is left here is content and the clock — two things the device cannot get
anywhere else:

* ``word_of`` / ``sense_of`` — the dictionary and sense-set lookups the review
  cards render. Shared with the reading glossary on purpose: the two screens
  show the same thing, and two shapes would drift.
* ``today_key`` — which day it is **by the simulated clock**, so that winding
  the server forward to test a schedule moves the client's idea of "today" with
  it rather than leaving the two disagreeing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from backend.core.logging import get_logger
from backend.modules.review import clock, repository

log = get_logger("review.session")

#: 方向。**留着是因为契约里还写着它**（`ReviewItem.direction`，铁律 5），
#: 而客户端那份 `ReviewDirection` 用的是同样的两个数。
WORD_TO_SENSE = 1      # 看词想义
SENSE_TO_WORD = 2      # 看义想词

#: How far the hints go. **One level as of P7**: 「不确定」 is a single tap.
#: 服务端已经不按它算分了（那在 `ERCore/Scheduler` 里），但契约要校验上报上来的
#: 值在范围内，所以这个上限留在这边。
MAX_REVEAL = 1


def _now(now: datetime | None = None) -> datetime:
    """The module's single entry point for the current time.

    Everything that decides which day it is comes through here — which is what
    lets the simulated clock move all of it together. See :mod:`.clock`.
    """
    return now or clock.now()


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
            "pos_zh": item.get("pos_zh"),
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


def phrase_of(phrase: str) -> dict[str, Any] | None:
    """Everything the reveal screen shows about one phrase — ``WordCard``'s shape.

    P12 决定 ⑬: one card type for both. ``phonetic`` and ``translation`` stay
    null for a phrase; the dictionary comma pile is word-only, and 柯林斯 is the
    only source of phrase Chinese (P11). Share is computed exactly as for words.
    """
    from backend.modules.phrases import repository as phrase_repo

    rows = phrase_repo.senses_of_phrase(phrase)
    if not rows:
        return None
    exam_total = sum(int(r.get("exam_frequency") or 0) for r in rows)
    return {
        "headword": phrase,
        "phonetic": None,
        "translation": None,
        "senses": [{
            "id": r["id"],
            "ordinal": r["ordinal"],
            "pos": r.get("pos"),
            "pos_zh": r.get("pos_zh"),
            "concept_en": r.get("concept_en"),
            "gloss_zh": r["gloss_zh"],
            "exam_frequency": int(r.get("exam_frequency") or 0),
            "share": (round(int(r.get("exam_frequency") or 0) / exam_total * 100, 1)
                      if exam_total else None),
        } for r in rows],
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
        # **P11 决定 ⑭ 的另一半。** 词组义项和单词义项共用一个号段，所以一个
        # 查不到的号可能是词组的。没有这一层回落，词组复习卡的释义栏就是空的；
        # 有了它而**没有**共用号段，才是真正危险的那种——那时候查出来的会是
        # 另一个词的意思，不报错，只是中文错了。
        from backend.modules.phrases import repository as phrase_repo

        phrase = phrase_repo.sense_by_id(sense_id)
        if phrase is not None:
            return phrase
        return None
    import json
    item = dict(row)
    try:
        item["gloss_zh"] = json.loads(item["gloss_zh"] or "[]")
    except (TypeError, ValueError):
        item["gloss_zh"] = [str(item["gloss_zh"] or "")]
    return item
