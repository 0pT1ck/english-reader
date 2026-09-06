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

import json
import random
from datetime import datetime, timezone
from typing import Any

from backend.core import runtime_config
from backend.core.db import get_connection
from backend.core.logging import get_logger
from backend.modules.generation import checker, parser, prompts
from backend.modules.llm import client, jobs
from backend.modules.llm.parsing import ItemFailed
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
    text, plan = prompts.plan_and_build(
        scheme=payload["scheme"],
        assumed_tiers=_tiers("gen_assumed_tiers"),
        allowed_tiers=_tiers("gen_allowed_tiers"),
        learn_tier=str(runtime_config.get("gen_learn_tier")),
        target_count=int(runtime_config.get("gen_target_count")),
        anchor_count=int(runtime_config.get("gen_anchor_count")),
        length=int(runtime_config.get("gen_length")),
        seed=payload["seed"],
        topic=payload.get("topic"),
        targets_per_paragraph=int(runtime_config.get("gen_targets_per_paragraph")),
    )

    completion = client.complete(
        provider,
        [{"role": "user", "content": text}],
        max_tokens=2500,
        # Higher than the data-building jobs on purpose: this is writing, and
        # eight drafts at temperature 0.2 would read like eight copies.
        temperature=0.8,
    )

    try:
        article = parser.parse(completion.text)
    except parser.ParseError as exc:
        raise ItemFailed(f"无法解析这段回复：{exc}", raw=completion.text) from exc

    report = checker.check(
        article.body,
        target_words=plan.target_words,
        allowed_tiers=_tiers("gen_allowed_tiers"),
        exam=str(runtime_config.get("gen_learn_tier")),
    )

    conn = get_connection("learning")
    conn.execute(
        "INSERT INTO generation_drafts (title, body, model, scheme, prompt_version,"
        " word_set, target_words, prompt, report, created_at, note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            article.title,
            article.body,
            provider.model,
            payload["scheme"],
            prompts.PROMPT_VERSION,
            plan.topic or "",
            json.dumps(plan.target_words, ensure_ascii=False),
            text,
            json.dumps(report.as_dict(), ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "api",
        ),
    )
    conn.commit()

    log.info(
        "draft.generated",
        f"{provider.model} 生成一篇（{report.words} 词，超纲 {len(report.beyond)}，"
        f"目标词 {sum(report.target_hits.values())}/{len(report.target_hits)}）",
        model=provider.model,
        words=report.words,
        beyond=len(report.beyond),
        beyond_rate=round(report.beyond_rate, 2),
    )

    return jobs.ItemOutcome(
        result=completion.text,
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
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
