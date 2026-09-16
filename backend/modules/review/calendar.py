"""打卡日历 the review calendar, and the streak.

日历 calendar / 打卡 a day completed / 连续 streak

**Four states, and the third one is the reason this file exists.** A day is
green when both pools were emptied, red when there was work and it was left,
and grey when only one of the two was finished — or when there was nothing to
do at all. That last case matters: **a day the system had no work for must not
be marked as a day you failed.**

**The streak counts green days only**, and today not being finished yet does
not break it — you may be about to do it. That is the one piece of leniency
here, and it is deliberate: a counter that resets at midnight before you have
had the chance to act is measuring the clock, not you.

**个人的连续天数做，打卡分享不做。** 主文档 §J rules out social features,
leaderboards and streak-sharing. A private counter is not one of those — it is
not shown to anyone and compares you to nobody — but the two sit close enough
together that the line is written down here rather than left to be re-derived.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from backend.core.db import get_connection
from backend.modules.review import clock, repository

#: What a day can be. The client colours them; naming them here keeps the rule
#: on this side (架构铁律 1) — "was this day completed" is not a rendering
#: question.
COMPLETE = "complete"    # 两项都做完了
PARTIAL = "partial"      # 只做完一项，或者那天压根没活
MISSED = "missed"        # 有活，一项都没做完
UNKNOWN = "unknown"      # 那天没开过 App，重建不出来


def _day_rows(learner_id: int, since: str) -> dict[str, dict[str, dict[str, int]]]:
    """``{day: {bucket: {"total": n, "done": n}}}`` for every day with a session."""
    rows = get_connection("learning").execute(
        """
        SELECT s.day                                     AS day,
               q.bucket                                  AS bucket,
               COUNT(*)                                  AS total,
               SUM(CASE WHEN q.done_at IS NULL THEN 0 ELSE 1 END) AS done
          FROM review_sessions s
          LEFT JOIN review_queue q ON q.session_id = s.id
         WHERE s.learner_id = ? AND s.day >= ?
         GROUP BY s.day, q.bucket
        """,
        (learner_id, since),
    ).fetchall()

    out: dict[str, dict[str, dict[str, int]]] = {}
    for row in rows:
        day = out.setdefault(row["day"], {})
        # The LEFT JOIN gives one row with a NULL bucket when the session held
        # nothing at all — a real state, and not the same as no session.
        bucket = row["bucket"] or "_empty"
        day[bucket] = {"total": int(row["total"] or 0), "done": int(row["done"] or 0)}
    return out


def _verdict(buckets: dict[str, dict[str, int]] | None) -> str:
    """One day's colour, from its two pools."""
    if buckets is None:
        # No session row. **Not reconstructible**: the queue is built when the
        # day is opened, so a day never opened has no record of how much work it
        # would have held. Reported as its own state rather than guessed at.
        return UNKNOWN

    # **Only pools that actually had work get a vote.** An empty pool counts as
    # completed for "was the day finished", but it must not count as *progress* —
    # the first version let it, and a day with 13 due items and nothing done came
    # back grey instead of red, because the empty 今日学习 pool voted "finished".
    worked = []
    for bucket in ("today", "due"):
        counts = buckets.get(bucket)
        if counts and counts["total"] > 0:
            worked.append(counts["done"] >= counts["total"])

    if not worked:
        return PARTIAL                        # 那天两项都是空的，没活可做
    if all(worked):
        return COMPLETE                       # 有活的那些全做完了（空桶不拖后腿）
    if any(worked):
        return PARTIAL                        # 做完一项，另一项还欠着
    return MISSED


def calendar(learner_id: int, *, days: int = 7, now: Any = None) -> dict[str, Any]:
    """The last ``days`` days up to today, plus the streak.

    **Today comes from the simulated clock** (决定 5), not from the wall clock:
    stepping ``offset_days`` forward to test scheduling has to move the calendar
    with it, or what the test shows is not what the app would show.
    """
    today = date.fromisoformat(repository.today(now or clock.now()))
    span = max(1, min(int(days), 60))
    start = today - timedelta(days=span - 1)
    rows = _day_rows(learner_id, start.isoformat())

    entries = []
    for offset in range(span):
        day = start + timedelta(days=offset)
        key = day.isoformat()
        entries.append({
            "day": key,
            "status": _verdict(rows.get(key)),
            "is_today": day == today,
        })

    # **The streak gets its own query, not these rows.** Reusing a seven-day
    # window would silently cap the streak at seven — and it would look right.
    return {"days": entries, "streak": streak(learner_id, today=today)}


def streak(learner_id: int, *, today: date, rows: dict | None = None) -> int:
    """Consecutive completed days ending today.

    **Today not being finished does not break it.** You may be about to do it,
    and a counter that resets before you have had the chance is measuring the
    clock rather than you. So counting starts at today when today is green, and
    at yesterday otherwise.
    """
    # Long enough to be worth showing, bounded so one query cannot walk years.
    look_back = 400
    rows = rows if rows is not None else _day_rows(
        learner_id, (today - timedelta(days=look_back)).isoformat())

    cursor = today
    if _verdict(rows.get(today.isoformat())) != COMPLETE:
        cursor = today - timedelta(days=1)

    count = 0
    while count < look_back:
        if _verdict(rows.get(cursor.isoformat())) != COMPLETE:
            break
        count += 1
        cursor -= timedelta(days=1)
    return count


__all__ = ["COMPLETE", "PARTIAL", "MISSED", "UNKNOWN", "calendar", "streak"]
