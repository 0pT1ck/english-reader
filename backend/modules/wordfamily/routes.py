"""Admin API and page for word families."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response

from backend.admin.templating import render, require_page_auth
from backend.core import auth
from backend.core.errors import InvalidRequest
from backend.core.logging import get_logger, trace
from backend.modules.vocabulary import repository as dictionary
from backend.modules.wordfamily import affixes, build, derive, repository

log = get_logger("wordfamily")

admin_router = APIRouter(dependencies=[Depends(auth.require_admin)])
pages_router = APIRouter()


@admin_router.get("/wordfamily/stats", summary="词族与词缀统计")
async def stats() -> dict[str, Any]:
    return {"affixes": repository.affix_stats(), "families": repository.family_stats()}


@admin_router.post("/wordfamily/import-affixes", summary="导入词根词缀表")
async def import_affixes(force: Annotated[bool, Query()] = False) -> dict[str, Any]:
    with trace():
        return affixes.import_all(force_download=force)


@admin_router.post("/wordfamily/run-rules", summary="跑一遍规则分解")
async def run_rules(
    ceiling: Annotated[int, Query(ge=1000, le=200000)] = build.FREQUENCY_CEILING,
) -> dict[str, Any]:
    """Free and idempotent — existing rows are left alone, so a model's
    judgement is never overwritten by re-running this."""
    if not dictionary.is_imported():
        raise InvalidRequest("词典尚未导入，请先运行导入脚本")
    with trace():
        return build.run(ceiling)


@admin_router.get("/wordfamily/word/{word}", summary="查一个词的构词分解")
async def one_word(word: str) -> dict[str, Any]:
    word = word.lower()
    family = repository.family_of(word)
    return {
        "word": word,
        "family": family,
        "affix_gloss": (
            repository.affix_gloss(family["affix"], family["affix_kind"]) if family else ""
        ),
        "rule_says": derive.analyse(word).__dict__ if derive.analyse(word) else None,
        "members": repository.members_of(word),
        "tags": sorted(dictionary.tags_of(word)),
    }


@admin_router.get("/wordfamily/families", summary="列出词族")
async def list_families(
    grade: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    from backend.core.db import get_connection

    clause = "WHERE grade = ?" if grade else ""
    params: tuple[Any, ...] = (grade, limit) if grade else (limit,)
    rows = get_connection("content").execute(
        f"SELECT member, root, affix, affix_kind, grade, breakdown_zh, source"
        f" FROM word_families {clause} ORDER BY member LIMIT ?",
        params,
    ).fetchall()
    return {"count": len(rows), "families": [dict(r) for r in rows]}


@pages_router.get("/admin/wordfamily", response_class=HTMLResponse)
async def page(request: Request) -> Response:
    if (redirect := require_page_auth(request)) is not None:
        return redirect
    payload = await list_families(limit=60)
    return render(
        request,
        "wordfamily.html",
        stats=await stats(),
        families=payload["families"],
        dictionary_ready=dictionary.is_imported(),
    )
