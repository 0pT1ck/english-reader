"""Admin API and page for sense sets."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response

from backend.admin.templating import render, require_page_auth
from backend.core import auth
from backend.core.errors import InvalidRequest
from backend.core.logging import get_logger, trace
from backend.modules.senses import repository, screening, validation
from backend.modules.vocabulary import repository as dictionary

log = get_logger("senses")

admin_router = APIRouter(dependencies=[Depends(auth.require_admin)])
pages_router = APIRouter()


@admin_router.get("/senses/stats", summary="义项集统计")
async def stats() -> dict[str, Any]:
    return {"senses": repository.stats(), "screening": screening.stats()}


@admin_router.post("/senses/screen", summary="跑一遍粗筛")
async def run_screening() -> dict[str, Any]:
    if not dictionary.is_imported():
        raise InvalidRequest("词典尚未导入，请先运行导入脚本")
    with trace():
        return screening.run()


@admin_router.get("/senses/word/{word}", summary="查一个词的义项集")
async def one_word(
    word: str,
    usages: Annotated[bool, Query(description="附上真题里的实际用例")] = False,
) -> dict[str, Any]:
    word = word.lower()
    senses = repository.senses_of(word)
    for sense in senses:
        sense["examples"] = repository.examples_of(sense["id"])
    entry = dictionary.lookup(word)
    return {
        "word": word,
        "senses": senses,
        "dictionary": (entry["translation"] if entry else None),
        "tags": sorted(dictionary.tags_of(word)),
        "usages": validation.sample_usages(word) if usages else [],
    }


@admin_router.post("/senses/word/{word}/build", summary="给单个词补建义项")
async def build_one(word: str, provider_id: Annotated[str | None, Query()] = None
                    ) -> dict[str, Any]:
    """Queue one word. The escape hatch for what the coarse filter got wrong."""
    from backend.modules.llm import jobs

    word = word.lower().strip()
    if not dictionary.lookup(word):
        raise InvalidRequest("词典里没有这个词", word=word)

    job_id = jobs.create(
        "sense_build",
        params={"words": [word]},
        provider_id=provider_id,
        title=f"补建义项：{word}",
    )
    jobs.start(job_id)
    return jobs.get(job_id)


@admin_router.get("/senses/validate", summary="三层验证")
async def validate() -> dict[str, Any]:
    with trace():
        return validation.run_all()


@admin_router.get("/senses/topics", summary="按主题列词")
async def by_topic(
    topic: Annotated[str, Query()],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    return {"topic": topic, "words": repository.words_by_topic(topic, limit)}


@pages_router.get("/admin/senses", response_class=HTMLResponse)
async def page(request: Request) -> Response:
    if (redirect := require_page_auth(request)) is not None:
        return redirect

    from backend.core.db import get_connection

    try:
        rows = get_connection("content").execute(
            "SELECT headword, ordinal, concept_en, gloss_zh, topic FROM senses"
            " ORDER BY headword, ordinal LIMIT 60"
        ).fetchall()
        samples = [dict(row) for row in rows]
    except Exception:  # noqa: BLE001 - before the table exists
        samples = []

    return render(
        request,
        "senses.html",
        stats=repository.stats(),
        screening=screening.stats(),
        labels=screening.CATEGORY_LABELS,
        samples=samples,
        dictionary_ready=dictionary.is_imported(),
    )
