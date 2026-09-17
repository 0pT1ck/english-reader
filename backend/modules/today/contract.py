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
    """服务端定的那几个数，随今日包一起下发。**客户端只读它们。**

    **2026-09-17 加上排期那四个（P9）。** 在那之前客户端从不计算间隔，所以不需要
    它们；而 P9 把排期搬到了设备上（`phase-9.html` §4），它现在必须知道服务端配的
    是什么。**不许在客户端写一份默认值**：两边各用自己的默认，就会跑出不同的间隔，
    而且两边都在按自己的文档正常工作、没有东西会报错——这正是 swift-fsrs
    与 py-fsrs 默认参数不一致那次差点踩进去的形状（§16 ②）。
    """

    fresh_days: int = Field(description="「新备的」这个书架算几天之内")
    weight_decay: float = Field(description="复习答错之后权重乘以它")
    spelling_enabled: bool
    fsrs_parameters: list[float] = Field(
        default_factory=list,
        description="FSRS 的参数向量。**21 个是 FSRS-6，19 个是 FSRS-5**，"
        "下发的是服务端实际生效的那一组，不是「用默认」——"
        "两边的「默认」不是同一组数。",
    )
    fsrs_desired_retention: float = Field(
        default=0.9, description="目标可提取性"
    )
    fsrs_maximum_interval: int = Field(
        default=180,
        description="间隔上限（天）。FSRS 自己默认 36500，那是「记一辈子」的答案；"
        "这个项目对着一场有日期的考试，落在考试之后的间隔不是复习。",
    )
    fsrs_fuzz: bool = Field(
        default=True,
        description="抖动。开着让同一天学的一批词不会永远同一天回来；"
        "验收与向量一律关掉，否则两种语言的随机数种子不同就成了「分歧」。",
    )


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
