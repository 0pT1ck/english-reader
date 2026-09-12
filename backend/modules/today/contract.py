"""Response model for 今日包.

今日包 today's package / 离线形状 offline shape

One response the client works from all day with no network (架构前提 2). It is
a composer: the articles come from reading's builder and the review block is
review's own payload, whole and unaltered — so a client that already speaks
those two needs no third parser.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from backend.core.contract import Capabilities, Learner
from backend.modules.reading.contract import ArticleResponse
from backend.modules.review.contract import ReviewDayResponse


class TodaySettings(BaseModel):
    fresh_days: int = Field(description="「新备的」这个书架算几天之内")
    weight_decay: float = Field(description="复习答错之后权重乘以它")
    spelling_enabled: bool


class TodayResponse(BaseModel):
    """当天的文章与复习，一次拿全。

    **全文内联，不是清单**（P4 决定 8）。实测一篇约 300 KB，三篇不到 1 MB——
    清单式的话联不上网就只剩清单，那就不叫今日包了。
    """

    learner: Learner
    capabilities: Capabilities
    day: str | None = Field(default=None, description="复习那一天的日期")
    articles: list[ArticleResponse] = Field(
        description="今天的正课。**正好 3 篇**，由配置项 today_article_count 决定"
    )
    extra_articles: list[ArticleResponse] = Field(
        description="**永远是空的。**加餐已于 2026-09-12 取消——往期本身就是加餐，"
        "往列表下面翻就有。字段留着不删是因为铁律 5 只增不减；"
        "把 today_extra_count 改回非零就能恢复",
    )
    reviews: ReviewDayResponse = Field(
        description="跟 /v1/client/reviews 返回的是同一个对象，原样嵌在这里"
    )
    settings: TodaySettings
    excludes_exam_papers: bool = Field(
        description="**今日包 ≠ 今天能读的全部。**真题走自己的入口 "
        "/library?source=cet4|cet6|kaoyan。写在响应里而不只是写在文档里——"
        "一个以为这就是全部的客户端会静默地藏起 452 篇真题"
    )
