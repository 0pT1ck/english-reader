"""今日包：打开就能用一天的那一份内容。

今日包 today's package / 选文 selection / 离线形状 offline shape

架构铁律 2 asks for one response the client can work from all day with no
network: the day's articles with every gloss already attached, and the day's
review with every sentence already attached. Until now the client had to browse
a library and pick for itself, which is fine on a desktop next to the server and
useless on a phone in a tunnel.

**Whole articles, inlined** (决定 8). Measured: one article is about 300 KB of
JSON, so three fit in under a megabyte — worth it, because a package of links is
just a list once the connection drops, and then it is not a package at all.

**No exam papers** (决定 9). They have their own entrance,
``/v1/client/library?source=cet4``, and they are a library rather than a supply.
The consequence has to be said out loud in the contract, because it is not
guessable: **今日包 is not everything available today.** A client that shows only
this is showing the generated articles and the review, which is the intent.

**The selection rule is deliberately temporary** (决定 17). With no ability
estimate there is nothing to select *for*: the rule is "the newest prepared
pieces you have not read". It is one function, and every choice it makes is
written to the decision log, so when §A finally lands there is exactly one place
to change and a record of what the old rule was doing.
"""

from __future__ import annotations

from typing import Any

from backend.core import auth, runtime_config
from backend.core.db import get_connection
from backend.core.logging import get_logger, log_decision
from backend.modules.reading import service as reading
from backend.modules.review import session as review

log = get_logger("today.service")


def choose_articles(learner_id: int, *, limit: int, extra: int) -> list[dict[str, Any]]:
    """Which articles today, and why — the one place that decides it.

    Returns rows in the order they should be offered: the main batch first, then
    the extras that unlock after it (主文档 §H). The extras are ordinary new
    articles now; the "tomorrow's due words" character they used to have went
    with E2 (归档 §G).
    """
    rows = get_connection("learning").execute(
        "SELECT id, title, source, word_count, sentence_count, prepared_at,"
        " difficulty, difficulty_score"
        " FROM reading_articles"
        " WHERE source = 'generated' AND status = 'ready' AND read_at IS NULL"
        " ORDER BY prepared_at DESC, id DESC LIMIT ?",
        (limit + extra,),
    ).fetchall()
    return [dict(r) for r in rows]


def package(learner_id: int) -> dict[str, Any]:
    """The whole day, in one response."""
    limit = int(runtime_config.get("today_article_count"))
    extra = int(runtime_config.get("today_extra_count"))

    chosen = choose_articles(learner_id, limit=limit, extra=extra)
    main = chosen[:limit]
    extras = chosen[limit:]

    # Each article is fetched through the same builder the single-article
    # endpoint uses, so the two can never drift into different shapes — the
    # client parses one thing whichever way it arrived.
    articles = [reading.article(learner_id, row["id"]) for row in main]
    extra_articles = [reading.article(learner_id, row["id"]) for row in extras]

    reviews = review.day_payload(learner_id)

    log_decision(
        "today.assembled",
        f"今日包：{len(articles)} 篇正课、{len(extra_articles)} 篇加餐、"
        f"{len(reviews.get('items') or [])} 条复习",
        inputs={
            "learner_id": learner_id,
            "wanted": limit,
            "extra": extra,
            "unread_stock": len(chosen),
        },
        candidates=[{"id": r["id"], "title": r["title"],
                     "prepared_at": r["prepared_at"]} for r in chosen],
        chosen=[r["id"] for r in main],
        reason="没有水平估计，按「新备未读优先」排；真题走自己的入口（决定 9、17）",
    )

    return {
        "learner": auth.learner_profile(learner_id),
        "capabilities": reading.capabilities(),
        "day": reviews.get("day"),
        "articles": articles,
        "extra_articles": extra_articles,
        # Review keeps its own shape, whole and unaltered: this is the same
        # object /v1/client/reviews returns, so a client that already speaks it
        # needs no second parser.
        "reviews": reviews,
        "settings": {
            "fresh_days": int(runtime_config.get("reading_fresh_days")),
            "weight_decay": float(runtime_config.get("review_weight_decay")),
            "spelling_enabled": bool(runtime_config.get("review_spelling")),
        },
        # Stated in the response, not only in the docs: a client that assumes
        # this is the whole day would silently hide 452 exam papers.
        "excludes_exam_papers": True,
    }
