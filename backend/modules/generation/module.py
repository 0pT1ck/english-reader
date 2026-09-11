"""Module declaration for generation (P1a).

Scope is deliberately narrow: build a prompt, parse what comes back, report on
it. No API calls, no gloss annotation, no over-generation or repair. Those are
P1c, and building them before the core assumption is verified would be doing the
work in the wrong order — if the generated English turns out to be unusable, a
pipeline built around it is wasted.
"""

from __future__ import annotations

from pathlib import Path

from backend.admin.templating import add_template_dir
from backend.core import runtime_config
from backend.core.registry import AdminPage, Module
from backend.core.tasks import Task
from backend.modules.generation import daily, routes, schema, workers

add_template_dir(Path(__file__).parent / "templates")

workers.register()

# Difficulty is a set of parameters, not a constant — re-aiming at a harder exam
# later means changing these values, nothing else.
#
# The two vocabulary parameters must stay separate: `assumed` is what the
# learner is taken to know, `allowed` is the ceiling the article may reach.
# Restricting generation to words already known would leave nothing to learn;
# new words belong in the gap between the two.
runtime_config.register(
    runtime_config.ConfigSpec(
        key="gen_assumed_tiers",
        default="zk gk",
        value_type="str",
        title="假定已掌握的词表层级",
        description="用于计算生词的基准。P1 阶段是固定配置，P3 起改由水平估计动态给出。"
        "可选：zk 中考 / gk 高考 / cet4 / cet6 / ky 考研。",
        group="generation",
        order=10,
    ),
    runtime_config.ConfigSpec(
        key="gen_allowed_tiers",
        default="zk gk cet4",
        value_type="str",
        title="允许使用的词表层级",
        description="文章用词的难度上限。必须比「假定已掌握」高出一层，"
        "否则文章里没有生词可学。",
        group="generation",
        order=11,
    ),
    runtime_config.ConfigSpec(
        key="gen_learn_tier",
        default="cet4",
        value_type="str",
        title="本阶段的学习目标层级",
        description="目标词从这一层挑选，句法基线也参照这一层的真题。",
        group="generation",
        order=12,
    ),
    runtime_config.ConfigSpec(
        key="gen_target_count",
        default=25,
        value_type="int",
        title="每篇的目标词数量",
        description="要求文章必须用上的学习目标词个数，也就是生词密度。"
        "这个数直接决定学完全部词汇要多久：7226 个目标词 ÷ 这个数 = 需要读多少篇。"
        "8 个时要 903 篇，25 个时要 289 篇。实测提到 25 个超纲率仍只有 0.29%。",
        group="generation",
        order=20,
    ),
    runtime_config.ConfigSpec(
        key="gen_targets_per_paragraph",
        default=5,
        value_type="int",
        title="每段的目标词数量",
        description="光有密度不够，还要管分布——二十五个生词分五段、每段五个是能读的，"
        "堆在开头两段就成了单词表。段数由「每篇目标词 ÷ 这个数」自动算出。",
        group="generation",
        order=21,
    ),
    runtime_config.ConfigSpec(
        key="gen_anchor_count",
        default=40,
        value_type="int",
        title="难度锚点词数量",
        description="锚点方案下，给模型作为难度标尺的词数。太少不足以定位，"
        "太多就退化成了词表。",
        group="generation",
        order=21,
    ),
    runtime_config.ConfigSpec(
        key="gen_provider",
        default="",
        value_type="str",
        title="生成文章用的提供商",
        description="留空则用默认提供商。写作和数据整理要的能力不一样："
        "同一套提示词下，deepseek-v4-flash 的超纲率 3.64%，claude-haiku-4-5 是 0.98%，"
        "gpt-5.5 是 0.32%。数据整理该用快模型，写文章不该。",
        group="generation",
        order=13,
    ),
    runtime_config.ConfigSpec(
        key="gen_length",
        default=350,
        value_type="int",
        title="目标篇长（词）",
        description="四级真题实测中位数 346，六级 445，考研 421。",
        group="generation",
        order=22,
    ),

    # --- 合格线：只有这三条会丢弃草稿（决定 4）------------------------------ #
    runtime_config.ConfigSpec(
        key="gen_max_beyond_rate",
        default=1.0,
        value_type="float",
        title="超纲率上限（%）",
        description="超过就丢弃。实测 gpt-5.5 与 deepseek-flash（关思考）的超纲率"
        "中位数都在 0.4% 上下，失败的那几篇都落在 1%–2.3%。",
        group="generation",
        order=30,
    ),
    runtime_config.ConfigSpec(
        key="gen_min_target_ratio",
        default=0.8,
        value_type="float",
        title="目标词命中下限（比例）",
        description="每篇要教的词里至少用上这个比例，否则丢弃。0.8 即 25 个里至少 20 个。"
        "实测 gpt-5.5 命中均值 24.4/25，deepseek 关思考 22.3/25。",
        group="generation",
        order=31,
    ),

    # --- 每日供给（决定 2、3、11）------------------------------------------ #
    runtime_config.ConfigSpec(
        key="gen_daily_count",
        default=3,
        value_type="int",
        title="每天备几篇",
        description="一次生成一篇、当场校验、够数为止。没读的不会删，会转入往期。",
        group="generation",
        order=40,
    ),
    runtime_config.ConfigSpec(
        key="gen_daily_attempt_cap",
        default=12,
        value_type="int",
        title="单次备稿的生成次数上限",
        description="这是熔断不是配额。实测合格率 60%–80%，凑够 3 篇期望 4–5 次；"
        "12 次还凑不够的概率在 0.3% 以下，真发生了基本可以断定是链路坏了，"
        "所以到顶就停并推送通知。",
        group="generation",
        order=41,
    ),
    runtime_config.ConfigSpec(
        key="gen_topup_enabled",
        default=True,
        value_type="bool",
        title="读完一篇就补库存",
        description="三层保险的第一层：读完触发，库存低于下限时补一篇。"
        "第二层是每天定时备稿，第三层是控制台手动触发。",
        group="generation",
        order=42,
    ),
    runtime_config.ConfigSpec(
        key="gen_stock_floor",
        default=3,
        value_type="int",
        title="库存下限（未读的生成文篇数）",
        description="低于这个数才补。只数生成的文章，真题不算——真题是现成的语料，不会用完。",
        group="generation",
        order=43,
    ),
)

MODULE = Module(
    name="generation",
    title="生成",
    description="按约束写文章、本地校验出体检报告，以及每天自动备稿",
    migrations=schema.MIGRATIONS,
    tasks=[
        Task(
            name="generation.daily",
            title="每天备稿",
            description="一次生成一篇、当场校验、够数为止。夜里跑完，早上打开就有。",
            run=daily.run_scheduled,
            schedule="04:00",
        )
    ],
    subscriptions={"article.finished": [daily.on_article_finished]},
    admin_router=routes.admin_router,
    admin_router_pages=routes.pages_router,
    admin_pages=[
        AdminPage(
            title="生成",
            path="/admin/generation",
            order=60,
            description="P1a 生成实验工作台",
        )
    ],
)
