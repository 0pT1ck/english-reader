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
from backend.modules.generation import routes, schema

add_template_dir(Path(__file__).parent / "templates")

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
        default=8,
        value_type="int",
        title="每篇的目标词数量",
        description="要求文章必须用上的学习目标词个数。",
        group="generation",
        order=20,
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
        key="gen_length",
        default=350,
        value_type="int",
        title="目标篇长（词）",
        description="四级真题实测中位数 346，六级 445，考研 421。",
        group="generation",
        order=22,
    ),
)

MODULE = Module(
    name="generation",
    title="生成",
    description="P1a 生成实验：构造提示词、解析回复、本地校验出体检报告",
    migrations=schema.MIGRATIONS,
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
