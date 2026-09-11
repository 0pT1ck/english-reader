"""Generating articles through the API instead of by hand.

The P1a loop was deliberately manual — prompt out, paste into a chat window,
paste the reply back — because the question then was whether the approach worked
at all, and machinery would only have got in the way. That question is answered,
so the loop can be closed.

One article per job item, each with its own seed so the target words differ.
Everything else is the existing pieces in their existing roles: the prompt
builder picks the words, the parser survives whatever wrapping the model adds,
and the checker reports without judging. Nothing here decides whether a draft is
good — over-generating and picking the best is a later step, and building it
before there is anything to choose between would be the wrong order again.
"""

from __future__ import annotations

import random
from typing import Any

from backend.core import runtime_config
from backend.core.logging import get_logger
from backend.modules.generation import pipeline
from backend.modules.llm import jobs
from backend.modules.llm.providers import Provider

log = get_logger("generation.jobs")


def _tiers(key: str) -> list[str]:
    return [t for t in str(runtime_config.get(key)).replace(",", " ").split() if t]


def preferred_provider() -> str | None:
    """Which provider writes the articles, if it differs from the default.

    Writing and data-wrangling need different things from a model, and the same
    prompt shows it: 3.64% out-of-syllabus from the fast model that handles the
    sense sets, 0.32% from a strong one. The batch runner's default stays
    pointed at the cheap model, so generation names its own.
    """
    chosen = str(runtime_config.get("gen_provider")).strip()
    return chosen or None


def _plan(params: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    count = max(1, int(params.get("count", 4)))
    scheme = str(params.get("scheme", "anchor"))
    topic = params.get("topic")
    # A fixed base seed keeps a job reproducible; the offset keeps the articles
    # within one job from all teaching the same eight words.
    base = int(params.get("seed", random.randrange(1, 10**6)))  # noqa: S311
    return [
        (f"#{i + 1}", {"seed": base + i * 977, "scheme": scheme, "topic": topic})
        for i in range(count)
    ]


def _run(provider: Provider, payload: dict[str, Any], params: dict[str, Any]):
    """One article. The seven steps live in :mod:`.pipeline`, shared with the
    daily supply task — a second copy of them here is exactly how one judgement
    becomes two different judgements (坑 §5.1)."""
    written = pipeline.write_one(
        provider,
        seed=payload["seed"],
        scheme=payload.get("scheme", "anchor"),
        topic=payload.get("topic"),
        note="api",
    )
    return jobs.ItemOutcome(
        result=written.raw,
        tokens_in=written.tokens_in,
        tokens_out=written.tokens_out,
    )


def register() -> None:
    jobs.register_worker(jobs.Worker(
        kind="generate_article",
        title="生成文章",
        description="按当前提示词生成文章，自动解析入库并出体检报告。"
        "参数：count 篇数、scheme 约束方案、topic 主题",
        plan=_plan,
        run_item=_run,
        default_provider=preferred_provider,
    ))
