"""Module declaration for LLM access (P1b).

Brought forward from P1c because the sense set needs roughly 280 calls and the
word families a few hundred more — work the manual paste-it-into-a-chat loop
cannot carry. The manual workbench stays: it is still the way to try a model
that has no API, and the way to keep working when an API quota runs out.

Scope stops at "call a model and run batches of calls". What to ask for belongs
to the modules that need the answers.
"""

from __future__ import annotations

from pathlib import Path

from backend.admin.templating import add_template_dir
from backend.core import runtime_config
from backend.core.registry import AdminPage, Module
from backend.modules.llm import jobs, routes, schema

add_template_dir(Path(__file__).parent / "templates")

runtime_config.register(
    runtime_config.ConfigSpec(
        key="llm_spend_cap",
        default=30.0,
        value_type="float",
        title="单个批次任务的花费上限",
        description="一个批次累计花到这个数就自动暂停，等你确认后再继续。"
        "单位是提供商配置里的货币。因为一次调用的花费要等它返回才知道，"
        "实际可能超出一点点（最多是并发数减一次调用）。填 0 表示不限制——不建议。",
        group="llm",
        order=10,
    ),
    runtime_config.ConfigSpec(
        key="llm_concurrency",
        default=3,
        value_type="int",
        title="批次任务的并发数",
        description="同时进行的调用数。太高容易触发提供商的限流，反而更慢。",
        group="llm",
        order=20,
    ),
    runtime_config.ConfigSpec(
        key="llm_requests_per_minute",
        default=0,
        value_type="int",
        title="每分钟最多调用次数",
        description="中转服务通常有限流，超了会返回 429。快模型两秒就是一次调用，"
        "并发三个一分钟能发九十次，很容易撞上。填 0 表示不限制；"
        "填比对方额度略小的数（比如额度 20 就填 18）最稳。",
        group="llm",
        order=23,
    ),
    runtime_config.ConfigSpec(
        key="llm_timeout_seconds",
        default=120.0,
        value_type="float",
        title="单次调用超时（秒）",
        description="超过这个时间没有返回就算失败。长文本生成需要留够时间。",
        group="llm",
        order=21,
    ),
    runtime_config.ConfigSpec(
        key="llm_max_attempts",
        default=3,
        value_type="int",
        title="单项最多尝试次数",
        description="限流和网络错误会自动重试。用尽次数的任务项会留在失败列表里，"
        "可以在排除原因后手动继续。",
        group="llm",
        order=22,
    ),
)


def _on_startup() -> None:
    """A job that was running when the process died is not running now."""
    jobs.recover_interrupted()


MODULE = Module(
    name="llm",
    title="模型接入",
    description="提供商与密钥管理、批量任务调度、成本记录",
    migrations=schema.MIGRATIONS,
    admin_router=routes.admin_router,
    admin_router_pages=routes.pages_router,
    admin_pages=[
        AdminPage(
            title="模型",
            path="/admin/llm",
            order=55,
            description="提供商、密钥与批次任务",
        )
    ],
    on_startup=_on_startup,
)
