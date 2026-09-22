"""Reading loop — P2.

One module, not two. Splitting ingest from reading was considered and rejected:
``reading_articles`` would then be owned by one module and queried by the other,
which is exactly the shared-table arrangement architecture rule 6 exists to
prevent. The ingest pipeline is an internal detail of reading, not a feature in
its own right.

What this module contributes, all from inside this directory:

* eight tables (articles, sentences, tokens, phrase occurrences, study marks, study
  states, progress, client events) via its own migrations
* the first ``/v1/client`` endpoints in the project
* admin endpoints and two console pages
* two batch workers: contextual sense annotation, and phrase judgement
* its own settings

Nothing outside ``backend/modules/reading/`` was edited to mount any of it,
except one column added to ``devices`` by core auth — that one belongs to auth
because auth owns the table.
"""

from __future__ import annotations

from pathlib import Path

from backend.admin.templating import add_template_dir
from backend.core import runtime_config
from backend.core.logging import get_logger
from backend.core.events import Event
from backend.core.registry import AdminPage, Module
from backend.modules.llm import jobs
from backend.modules.reading import annotate, repository, routes
from backend.modules.reading.schema import MIGRATIONS

log = get_logger("reading")

add_template_dir(Path(__file__).parent / "templates")


runtime_config.register(
    runtime_config.ConfigSpec(
        key="reading_fresh_days",
        default=3,
        value_type="int",
        title="「新备的」保留天数",
        description=(
            "文章备好后多少天内算「新备的」，之后转入「往期」。"
            "往期的文章不会删除，随时可以翻出来读——没读的不是废品，只是还没轮到。"
            "三天是个起点，觉得挑不过来就调小，觉得太快消失就调大。"
        ),
        group="reading",
        order=10,
    ),
    runtime_config.ConfigSpec(
        key="annotate_batch_words",
        default=35,
        value_type="int",
        title="语境标注每批词数",
        description=(
            "一次让模型判断多少个词的义项。批次太大，快模型会在输出的后半段漂移或偷懒；"
            "太小则调用次数成倍增加。切分点总是落在句子边界上。"
        ),
        group="reading",
        order=20,
    ),
    runtime_config.ConfigSpec(
        key="annotate_provider",
        default="",
        value_type="str",
        title="语境标注用哪个提供商",
        description=(
            "留空则用默认提供商。标注是数据整理，不是写作——按实测结论应当用快模型，"
            "跟义项集生成同一类，而不是写文章那个贵的。"
        ),
        group="reading",
        order=30,
    ),
    runtime_config.ConfigSpec(
        key="phrase_batch_size",
        default=20,
        value_type="int",
        title="词组判断每批处数",
        description=(
            "一次让模型判断多少处「这是不是词组」。每条只是一句话加一个序列，"
            "比义项标注短得多，所以批次可以大一些。"
        ),
        group="reading",
        order=35,
    ),
    runtime_config.ConfigSpec(
        key="difficulty_weights",
        default={
            "beyond_cet4_pct": 0.25,
            "beyond_cet6_pct": 0.25,
            "frequency_p90": 0.25,
            "rare_word_pct": 0.25,
            "exam_key_pct": 0.0,
        },
        value_type="json",
        title="综合难度分的权重",
        description=(
            "实测结论：难度几乎全在词汇上，句长与从句密度区分不了三个等级，因此不进综合分。"
            "改完可以调 /v1/admin/reading/calibration 立刻验证——"
            "一组算不出「四级 < 六级 ≈ 考研」的权重就是错的。"
        ),
        group="reading",
        order=40,
    ),
    runtime_config.ConfigSpec(
        key="library_default_sort",
        default="composite",
        value_type="str",
        title="文章列表默认排序",
        description="composite 综合分 / beyond_cet4_pct 超四级词比例 / prepared_at 备稿时间 等。",
        group="reading",
        order=50,
    ),
    runtime_config.ConfigSpec(
        key="progress_report_seconds",
        default=15,
        value_type="int",
        title="阅读进度上报间隔（秒）",
        description="节流用，避免每滚一屏就发一次请求。事件仍然先入本地队列，联网后批量上报。",
        group="reading",
        order=60,
    ),
)


def _register_workers() -> None:
    jobs.register_worker(annotate.WORKER)


def on_senses_replaced(event: Event) -> None:
    """Repair references when a word's sense set is regenerated.

    Sense ids are not stable across a regeneration, and P2 is the first phase to
    record them — on every annotated occurrence, on every mark, on every study
    state. The sense module has no business knowing any of that exists, so it
    announces the change and this reacts: annotations go back in the queue,
    marks and study state fall back to the word-level slot rather than dangling.

    Without this, topping up one word's senses quietly detaches every annotation
    of it. Nothing errors, nothing logs, and the gloss panel simply shows less
    than it did yesterday.
    """
    headword = str(event.get("headword") or "")
    if not headword:
        return
    result = repository.repair_dangling_senses(headword)
    if result["articles"]:
        log.info(
            "reading.reannotation.needed",
            f"「{headword}」的义项集变了，{len(result['articles'])} 篇文章需要重跑标注",
            headword=headword, articles=len(result["articles"]),
        )


MODULE = Module(
    name="reading",
    title="阅读",
    description="文章入库、点词查词、标记与遇见记录，以及第一套客户端接口",
    migrations=MIGRATIONS,
    client_router=routes.client_router,
    admin_router=routes.admin_router,
    admin_router_pages=routes.pages_router,
    admin_pages=[
        AdminPage(
            title="阅读",
            path="/admin/reading",
            order=50,
            description="文章库、入库管线与客户端事件",
        ),
    ],
    subscriptions={"senses.replaced": [on_senses_replaced]},
    on_startup=_register_workers,
)
