"""Writing one article, end to end — the step both callers share.

生成 generate / 校验 check / 合格线 bar / 草稿 draft

Two things now ask for an article: the batch job the console drives, and the
daily supply task that runs at four in the morning. They want the same seven
steps (pick words -> build prompt -> call the model -> parse -> check -> store
-> report), so the steps live here and neither owns them.

That is not tidiness. The one rule this project has paid for four times over is
that **a judgement implemented twice ends up being two different judgements**
(坑 §5.1: three callers each decided "is this word beyond the syllabus" their
own way, and only one was right). Article generation is about to have a second
caller; this file is the reason it will not become a second pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.core import runtime_config
from backend.core.db import get_connection
from backend.core.logging import get_logger
from backend.modules.generation import checker, parser, prompts
from backend.modules.llm import client, jobs
from backend.modules.llm.providers import Provider

log = get_logger("generation.pipeline")

#: The job kind these settings belong to — the article writer's provider and
#: thinking switch (决定 19).
KIND = "generate_article"


def tiers(key: str) -> list[str]:
    return [t for t in str(runtime_config.get(key)).replace(",", " ").split() if t]


@dataclass
class Written:
    """One article as it came back, with the verdict already attached."""

    draft_id: int
    title: str
    body: str
    target_words: list[str]
    topic: str
    report: Any
    ok: bool
    #: Why it was rejected, in the reader's language. Empty when it passed.
    failures: list[str]
    #: Recorded but never a reason to reject — see :func:`verdict`.
    notes: list[str]
    tokens_in: int
    tokens_out: int
    raw: str


def verdict(report: Any) -> tuple[list[str], list[str]]:
    """``(failures, notes)`` for one checked article.

    **Three things reject a draft** (决定 4), all of them table lookups with no
    model in the loop: too many words outside the syllabus, too few of the target
    words actually used, or a paragraph that teaches nothing.

    **Length is recorded, never rejected** (决定 5). Measured 2026-09-11: drafts
    run 413–521 words, so the per-exam length bands in 主文档 §I would throw away
    every one of them while they are fine on everything that matters.

    **Sentence length is not measured here at all** (2026-09-11). It was, briefly,
    as another recorded note — but it measures how closely the prose resembles an
    exam paper, and that is not what these articles are for: **the point is
    learning the words**. The syntax picture is still computed by the checker and
    stored with every draft, for anyone who wants to look; it just is not a
    verdict, not even a soft one.
    """
    failures: list[str] = []
    notes: list[str] = []

    max_beyond = float(runtime_config.get("gen_max_beyond_rate"))
    min_hit_ratio = float(runtime_config.get("gen_min_target_ratio"))

    hits = sum(1 for v in report.target_hits.values() if v)
    total = len(report.target_hits)

    if report.beyond_rate > max_beyond:
        failures.append(f"超纲率 {report.beyond_rate:.2f}% 超过 {max_beyond}%")
    if total and hits / total < min_hit_ratio:
        failures.append(f"目标词只用了 {hits}/{total}")
    per_paragraph = report.targets_per_paragraph or []
    if per_paragraph and min(per_paragraph) == 0:
        failures.append(f"有段落一个目标词都没有：{per_paragraph}")

    wanted = int(runtime_config.get("gen_length"))
    if report.words and abs(report.words - wanted) / wanted > 0.15:
        notes.append(f"篇长 {report.words} 词，目标 {wanted}")

    return failures, notes


def write_one(
    provider: Provider,
    *,
    seed: int,
    scheme: str = "anchor",
    topic: str | None = None,
    exclude: set[str] | None = None,
    note: str = "api",
) -> Written:
    """Write, check and store exactly one draft. Never raises on a bad draft.

    A draft that fails the bar is still stored: it cost money, it is evidence
    about the model, and the console's comparison view reads these rows. What
    the caller does with a failure is the caller's business.
    """
    text, plan = prompts.plan_and_build(
        scheme=scheme,
        assumed_tiers=tiers("gen_assumed_tiers"),
        allowed_tiers=tiers("gen_allowed_tiers"),
        learn_tier=str(runtime_config.get("gen_learn_tier")),
        target_count=int(runtime_config.get("gen_target_count")),
        anchor_count=int(runtime_config.get("gen_anchor_count")),
        length=int(runtime_config.get("gen_length")),
        seed=seed,
        exclude=exclude,
        topic=topic,
        targets_per_paragraph=int(runtime_config.get("gen_targets_per_paragraph")),
    )

    # Applied here so both callers get it: the batch runner already sets this
    # around each item, and the daily task does not go through the batch runner
    # at all. Setting it twice to the same value is harmless; missing it on one
    # of the two paths would mean the switch silently does nothing at 4am.
    with client.thinking(jobs.configured_thinking(KIND)):
        completion = client.complete(
            provider,
            [{"role": "user", "content": text}],
            max_tokens=2500,
            # Higher than the data-building jobs on purpose: this is writing, and
            # eight drafts at temperature 0.2 would read like eight copies.
            temperature=0.8,
        )

    article = parser.parse(completion.text)
    report = checker.check(
        article.body,
        target_words=plan.target_words,
        allowed_tiers=tiers("gen_allowed_tiers"),
        exam=str(runtime_config.get("gen_learn_tier")),
    )
    failures, notes = verdict(report)

    conn = get_connection("learning")
    cursor = conn.execute(
        "INSERT INTO generation_drafts (title, body, model, scheme, prompt_version,"
        " word_set, target_words, prompt, report, created_at, note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            article.title,
            article.body,
            provider.model,
            scheme,
            prompts.PROMPT_VERSION,
            plan.topic or "",
            json.dumps(plan.target_words, ensure_ascii=False),
            text,
            json.dumps(report.as_dict(), ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            note,
        ),
    )
    conn.commit()
    draft_id = int(cursor.lastrowid or 0)

    hits = sum(1 for v in report.target_hits.values() if v)
    log.info(
        "draft.generated",
        f"{provider.model} 生成一篇（{report.words} 词，超纲 {report.beyond_rate:.2f}%，"
        f"目标词 {hits}/{len(report.target_hits)}）"
        + ("" if not failures else "，不合格：" + "、".join(failures)),
        draft_id=draft_id,
        model=provider.model,
        words=report.words,
        beyond_rate=round(report.beyond_rate, 2),
        target_hits=hits,
        passed=not failures,
    )

    return Written(
        draft_id=draft_id,
        title=article.title or "",
        body=article.body,
        target_words=list(plan.target_words),
        topic=plan.topic or "",
        report=report,
        ok=not failures,
        failures=failures,
        notes=notes,
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
        raw=completion.text,
    )
