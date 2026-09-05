"""Vocabulary module endpoints.

Everything here is on the **admin** surface. There is no client-facing
vocabulary API yet, and adding one now would be building P2: clients do not read
articles until then, and the shape of what they receive (the daily bundle) is a
P2 decision.

What this does provide is the tool that verifies P0 actually works — paste
English in, see how every word resolves. That is verification item 2, and it is
done through the console so it needs no command line.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from backend.admin.templating import render, require_page_auth
from backend.core import auth
from backend.core.errors import NotReady
from backend.core.logging import get_logger
from backend.modules.vocabulary import analyzer, repository

log = get_logger("vocabulary")

admin_router = APIRouter(dependencies=[Depends(auth.require_admin)])
pages_router = APIRouter()


class AnalyzeRequest(BaseModel):
    text: str = Field(
        min_length=1,
        max_length=20000,
        description="要分析的英文文本",
    )


@admin_router.get("/vocabulary/status", summary="词典状态")
async def dictionary_status() -> dict[str, Any]:
    return repository.stats()


@admin_router.post("/vocabulary/analyze", summary="分析一段英文")
async def analyze_text(payload: AnalyzeRequest) -> dict[str, Any]:
    """Resolve every word in ``text`` to a headword, with dictionary data.

    This is the same analysis that will run at article ingest time in P1 — the
    single pass whose stored output makes every later read a table lookup.
    """
    try:
        sentences = analyzer.analyze(payload.text)
    except OSError as exc:
        raise NotReady(
            "语言模型尚未安装，无法进行词汇分析。请先执行模型下载步骤。"
        ) from exc

    summary = analyzer.summarise(sentences)
    log.info(
        "analyze.completed",
        f"分析了 {summary['words']} 个词",
        words=summary["words"],
        sentences=summary["sentences"],
        not_in_dictionary=summary["not_in_dictionary"],
    )

    return {
        "summary": summary,
        "dictionary_imported": repository.is_imported(),
        "sentences": [
            {
                "index": s.index,
                "text": s.text,
                "tokens": [asdict(t) for t in s.tokens],
            }
            for s in sentences
        ],
    }


@pages_router.get("/admin/vocabulary", response_class=HTMLResponse)
async def vocabulary_page(request: Request) -> Response:
    if (redirect := require_page_auth(request)) is not None:
        return redirect
    return render(
        request,
        "vocabulary.html",
        stats=repository.stats(),
        labels=repository.SYLLABUS_LABELS,
    )
