"""Admin API and console page for providers and batch jobs.

Admin surface only. There is no client-facing endpoint here and there should not
be one: a reading client never talks to a model, it reads what the server
already produced.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from backend.admin.templating import render, require_page_auth
from backend.core import auth
from backend.core.errors import InvalidRequest
from backend.core.logging import get_logger
from backend.modules.llm import client, jobs, providers, secrets_store

log = get_logger("llm")

admin_router = APIRouter(dependencies=[Depends(auth.require_admin)])
pages_router = APIRouter()


class ProviderIn(BaseModel):
    id: str = Field(min_length=1, max_length=31)
    label: str = ""
    kind: str = "openai"
    base_url: str
    model: str
    price_in: float = 0
    price_out: float = 0
    currency: str = "CNY"
    enabled: bool = True
    is_default: bool = False
    note: str = ""


class KeyIn(BaseModel):
    api_key: str = Field(min_length=8, description="只写入 data/secrets.json，不进备份")


class JobIn(BaseModel):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    provider_id: str | None = None
    spend_cap: float | None = None
    title: str | None = None


class CapIn(BaseModel):
    spend_cap: float | None = Field(default=None, description="留空表示不限制")


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


@admin_router.get("/llm/providers", summary="列出提供商")
async def list_providers() -> dict[str, Any]:
    return {
        "providers": [p.as_dict() for p in providers.all_providers()],
        "kinds": providers.KINDS,
        "presets": providers.PRESETS,
    }


@admin_router.put("/llm/providers/{provider_id}", summary="新增或修改提供商")
async def put_provider(provider_id: str, payload: ProviderIn) -> dict[str, Any]:
    if payload.id != provider_id:
        raise InvalidRequest("路径和请求体里的 id 不一致")
    return providers.upsert(**payload.model_dump()).as_dict()


@admin_router.delete("/llm/providers/{provider_id}", summary="删除提供商")
async def remove_provider(provider_id: str) -> dict[str, Any]:
    return {"deleted": providers.delete(provider_id)}


@admin_router.put("/llm/providers/{provider_id}/key", summary="设置 API 密钥")
async def put_key(provider_id: str, payload: KeyIn) -> dict[str, Any]:
    providers.get(provider_id)  # 404 rather than silently storing an orphan key
    secrets_store.set_key(provider_id, payload.api_key.strip())
    return {"provider": provider_id, "has_key": True}


@admin_router.delete("/llm/providers/{provider_id}/key", summary="删除 API 密钥")
async def remove_key(provider_id: str) -> dict[str, Any]:
    return {"provider": provider_id, "removed": secrets_store.delete_key(provider_id)}


@admin_router.post("/llm/providers/{provider_id}/test", summary="测试连通性")
async def test_provider(provider_id: str) -> dict[str, Any]:
    """Send the smallest useful request, and report what it cost.

    Deliberately a real completion rather than a models-list call: what needs
    proving is that this key can generate with this model through this proxy.
    """
    provider = providers.get(provider_id)
    try:
        result = client.complete(
            provider,
            [{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=16,
            temperature=0,
        )
    except client.LLMError as exc:
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "reply": result.text.strip()[:100],
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "cost": round(provider.cost_of(result.tokens_in, result.tokens_out), 6),
        "currency": provider.currency,
    }


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


@admin_router.get("/llm/jobs", summary="列出批次任务")
async def list_jobs(limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
    return {
        "jobs": jobs.listing(limit),
        "summary": jobs.summary(),
        "kinds": [
            {"kind": w.kind, "title": w.title, "description": w.description}
            for w in jobs.workers()
        ],
    }


@admin_router.post("/llm/jobs", summary="新建批次任务")
async def create_job(payload: JobIn) -> dict[str, Any]:
    job_id = jobs.create(
        payload.kind,
        params=payload.params,
        provider_id=payload.provider_id,
        spend_cap=payload.spend_cap,
        title=payload.title,
    )
    return jobs.get(job_id)


@admin_router.get("/llm/jobs/{job_id}", summary="任务详情")
async def job_detail(
    job_id: int,
    status: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    return {"job": jobs.get(job_id), "items": jobs.items(job_id, status=status)}


@admin_router.post("/llm/jobs/{job_id}/start", summary="开始或继续任务")
async def start_job(job_id: int) -> dict[str, Any]:
    return jobs.start(job_id)


@admin_router.put("/llm/jobs/{job_id}/spend-cap", summary="修改花费上限")
async def change_cap(job_id: int, payload: CapIn) -> dict[str, Any]:
    """A capped job resumes only after its budget is raised — that is the ask."""
    return jobs.set_spend_cap(job_id, payload.spend_cap)


@admin_router.post("/llm/jobs/{job_id}/pause", summary="暂停任务")
async def pause_job(job_id: int) -> dict[str, Any]:
    return jobs.pause(job_id)


@admin_router.delete("/llm/jobs/{job_id}", summary="删除任务")
async def delete_job(job_id: int) -> dict[str, Any]:
    return {"deleted": jobs.delete(job_id)}


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


@pages_router.get("/admin/llm", response_class=HTMLResponse)
async def page(request: Request) -> Response:
    if (redirect := require_page_auth(request)) is not None:
        return redirect
    return render(
        request,
        "llm.html",
        providers=[p.as_dict() for p in providers.all_providers()],
        kinds=providers.KINDS,
        presets=providers.PRESETS,
        job_kinds=[
            {"kind": w.kind, "title": w.title, "description": w.description}
            for w in jobs.workers()
        ],
        jobs=jobs.listing(30),
        summary=jobs.summary(),
    )
