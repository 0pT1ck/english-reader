"""Generation workbench — the P1a loop, driven from the admin console.

The loop is: build a prompt → paste it into a chat window → paste the reply
back → read the report. No API keys, no retries, no error handling for network
failures. That is the point: P1a answers whether the approach works at all, and
the manual path reaches that answer with the least machinery in the way. It also
lets any model be tried, including ones with no API.

Everything is on the admin surface. There is no client-facing generation API and
adding one now would be building P2 — clients do not read articles yet, and the
shape of what they receive is a P2 decision.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from backend.admin.templating import render, require_page_auth
from backend.core import auth, runtime_config
from backend.core.db import get_connection
from backend.core.errors import InvalidRequest, NotFound
from backend.core.logging import get_logger
from backend.modules.generation import checker, parser, prompts
from backend.modules.vocabulary import repository

log = get_logger("generation")

admin_router = APIRouter(dependencies=[Depends(auth.require_admin)])
pages_router = APIRouter()

# Target-word sets are just fixed seeds. Round 1 uses set A across every model,
# round 2 uses set B — so each model's two pieces differ in content, while the
# models stay comparable to one another within a round.
WORD_SET_SEEDS = {"A": 1101, "B": 2202}


def _tiers(key: str) -> list[str]:
    return [t for t in str(runtime_config.get(key)).replace(",", " ").split() if t]


class DraftIn(BaseModel):
    response: str = Field(min_length=20, description="从 AI 复制回来的完整回复")
    model: str = Field(min_length=1, description="用了哪个模型")
    scheme: str = Field(default="anchor")
    word_set: str = Field(default="A")
    target_words: list[str] = Field(default_factory=list)
    prompt: str = Field(default="")
    note: str = Field(default="")


@admin_router.get("/generation/prompt", summary="生成提示词")
async def make_prompt(
    scheme: Annotated[str, Query()] = "anchor",
    word_set: Annotated[str, Query()] = "A",
    topic: Annotated[str | None, Query(description="指定主题；留空则自动挑一个")] = None,
) -> dict[str, Any]:
    """Build a prompt ready to be copied into a chat window."""
    if scheme not in prompts.SCHEME_LABELS:
        raise InvalidRequest("未知的词汇约束方案", scheme=scheme)
    if word_set not in WORD_SET_SEEDS:
        raise InvalidRequest("未知的目标词组", word_set=word_set)
    if not repository.is_imported():
        raise InvalidRequest("词典尚未导入，请先运行导入脚本")

    text, plan = prompts.plan_and_build(
        scheme=scheme,  # type: ignore[arg-type]
        assumed_tiers=_tiers("gen_assumed_tiers"),
        allowed_tiers=_tiers("gen_allowed_tiers"),
        learn_tier=str(runtime_config.get("gen_learn_tier")),
        target_count=int(runtime_config.get("gen_target_count")),
        anchor_count=int(runtime_config.get("gen_anchor_count")),
        length=int(runtime_config.get("gen_length")),
        seed=WORD_SET_SEEDS[word_set],
        topic=topic,
        targets_per_paragraph=int(runtime_config.get("gen_targets_per_paragraph")),
    )

    return {
        "prompt": text,
        "prompt_version": prompts.PROMPT_VERSION,
        "scheme": scheme,
        "scheme_label": prompts.SCHEME_LABELS[scheme],
        "word_set": word_set,
        "target_words": plan.target_words,
        "anchor_words": plan.anchor_words,
        "topic": plan.topic,
        "allowed_count": plan.allowed_count,
        "known_count": plan.known_count,
        "chars": len(text),
    }


@admin_router.post("/generation/drafts", summary="提交生成结果")
async def submit_draft(payload: DraftIn) -> dict[str, Any]:
    """Parse a chat reply, check it, and store it with its provenance."""
    try:
        article = parser.parse(payload.response)
    except parser.ParseError as exc:
        raise InvalidRequest(f"无法解析这段回复：{exc}") from exc

    report = checker.check(
        article.body,
        target_words=payload.target_words,
        allowed_tiers=_tiers("gen_allowed_tiers"),
        exam=str(runtime_config.get("gen_learn_tier")),
    )

    conn = get_connection("content")
    cursor = conn.execute(
        "INSERT INTO generation_drafts (title, body, model, scheme, prompt_version,"
        " word_set, target_words, prompt, report, created_at, note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            article.title,
            article.body,
            payload.model.strip(),
            payload.scheme,
            prompts.PROMPT_VERSION,
            payload.word_set,
            json.dumps(payload.target_words, ensure_ascii=False),
            payload.prompt,
            json.dumps(report.as_dict(), ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            payload.note,
        ),
    )
    conn.commit()

    log.info(
        "draft.stored",
        f"收录了 {payload.model} 的一篇草稿（{article.word_count} 词）",
        draft_id=cursor.lastrowid,
        model=payload.model,
        scheme=payload.scheme,
        word_set=payload.word_set,
    )

    return {
        "id": cursor.lastrowid,
        "title": article.title,
        "body": article.body,
        "warnings": article.warnings,
        "report": report.as_dict(),
    }


@admin_router.get("/generation/drafts", summary="列出草稿")
async def list_drafts(limit: Annotated[int, Query(ge=1, le=500)] = 100) -> dict[str, Any]:
    rows = get_connection("content").execute(
        "SELECT id, title, model, scheme, word_set, created_at, note,"
        " length(body) AS chars, report FROM generation_drafts"
        " ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()

    drafts = []
    for row in rows:
        item = dict(row)
        try:
            item["report"] = json.loads(item["report"]) if item["report"] else None
        except json.JSONDecodeError:
            item["report"] = None
        drafts.append(item)
    return {"count": len(drafts), "drafts": drafts}


@admin_router.get("/generation/drafts/{draft_id}", summary="草稿详情")
async def get_draft(draft_id: int) -> dict[str, Any]:
    row = get_connection("content").execute(
        "SELECT * FROM generation_drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    if row is None:
        raise NotFound("找不到这篇草稿")

    item = dict(row)
    for key in ("report", "target_words"):
        try:
            item[key] = json.loads(item[key]) if item[key] else None
        except json.JSONDecodeError:
            item[key] = None
    return item


@admin_router.delete("/generation/drafts/{draft_id}", summary="删除草稿")
async def delete_draft(draft_id: int) -> dict[str, Any]:
    conn = get_connection("content")
    cursor = conn.execute("DELETE FROM generation_drafts WHERE id = ?", (draft_id,))
    conn.commit()
    return {"deleted": bool(cursor.rowcount)}


@admin_router.get("/generation/comparison", summary="按模型汇总")
async def comparison() -> dict[str, Any]:
    """Aggregate the drafts by model — the table that decides which model wins.

    Averages only; the blind test supplies the judgement these numbers cannot.
    """
    rows = get_connection("content").execute(
        "SELECT model, scheme, report FROM generation_drafts WHERE report IS NOT NULL"
    ).fetchall()

    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        try:
            report = json.loads(row["report"])
        except json.JSONDecodeError:
            continue
        buckets.setdefault((row["model"], row["scheme"]), []).append(report)

    summary = []
    for (model, scheme), reports in sorted(buckets.items()):
        n = len(reports)
        summary.append(
            {
                "model": model,
                "scheme": scheme,
                "drafts": n,
                "avg_words": round(sum(r["words"] for r in reports) / n),
                "avg_sentence": round(sum(r["avg_sentence"] for r in reports) / n, 1),
                "avg_beyond": round(sum(len(r["beyond"]) for r in reports) / n, 1),
                "avg_beyond_rate": round(sum(r["beyond_rate"] for r in reports) / n, 2),
                "target_rate": round(
                    100 * sum(r["targets_used"] for r in reports)
                    / max(1, sum(r["targets_total"] for r in reports))
                ),
            }
        )
    return {"models": summary}


@pages_router.get("/admin/generation", response_class=HTMLResponse)
async def page(request: Request) -> Response:
    if (redirect := require_page_auth(request)) is not None:
        return redirect
    return render(
        request,
        "generation.html",
        schemes=prompts.SCHEME_LABELS,
        word_sets=list(WORD_SET_SEEDS),
        dictionary_ready=repository.is_imported(),
    )
