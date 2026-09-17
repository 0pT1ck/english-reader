"""Preparing the day's articles, without anyone watching.

每日供给 daily supply / 库存 stock / 熔断 fuse / 记账 accounting

Until now the loop was: generate a batch, read the numbers yourself, press 入库
on the ones you like. Everything after that button is already automatic —
annotation runs, phrases get scanned, the article flips to ``ready``. So the
only two things missing were **who presses the first button** and **who takes
that look for you**.

**One at a time, until three pass** (决定 2). Not a batch to choose from: picking
the best of several is 过量生成择优, which is §B3 and not in this phase. Here a
draft either clears the bar or is dropped, and another is written.

**The cap is a fuse, not a quota** (决定 3). Measured pass rate is 60–80%, so
three articles normally take four or five attempts. Twelve attempts without
three passes does not mean bad luck — at 60% that happens 0.3% of days — it
means something broke: the model was swapped, the prompt was mangled, the
syllabus lookup regressed. So it stops and pushes, rather than writing all night.

**Nothing here touches ``study_states``.** Preparing articles is not studying
them; the words in a piece nobody has read are still unlearned (主文档 §B9, and
the invariant guarded by ``verify_phase2.py`` 7.1). The only thing this module
reads from the learner's record is which words to *avoid* teaching again.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from backend.core import runtime_config
from backend.core.db import get_connection
from backend.core.logging import get_logger, log_decision
from backend.modules.generation import pipeline
from backend.modules.llm import jobs, providers

log = get_logger("generation.daily")


@dataclass
class Outcome:
    """What one run of the task did, in the shape the console and logs want."""

    prepared: list[int] = field(default_factory=list)      # article ids
    drafts: list[int] = field(default_factory=list)        # draft ids that passed
    rejected: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0
    wanted: int = 0
    hit_cap: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "prepared": self.prepared,
            "drafts": self.drafts,
            "rejected": self.rejected,
            "attempts": self.attempts,
            "wanted": self.wanted,
            "hit_cap": self.hit_cap,
            "errors": self.errors,
        }


def words_in_progress() -> set[str]:
    """Words the learner is already working on, so they are not taught as new.

    决定 6. ``pick_target_words`` has taken an ``exclude`` argument since P1 and
    no caller ever filled it, so every batch could hand back a word already
    sitting in the review queue — teaching, with a full explanation, something
    the learner marked as known-unknown last week.

    Reads only; a word's pool is decided by the learner's own mark and by
    nothing else.

    **P9 改了来源:先看客户端上报的词池快照。** 学习记录搬到设备上之后，
    ``study_states`` 不再是那份记录（它只是过渡期还在维护的派生物），
    而快照是设备把「我在学哪些词」当作事实报上来的那一份（§7）。
    这是服务端与学习记录**唯一一种解释关系**，而它连解释都不算:直接用。

    **没有快照时回落到 ``study_states``，并且大声说出来。**
    静默回落会让「设备从没报过」看起来和「一个词都没在学」一样，
    于是生成把你正在学的词当生词再教一遍——**而那是静默的**，
    只表现为「这篇怎么全是我标过的词」。§11 拆掉 ``study_states`` 之后，
    这条回落一起删。
    """
    from backend.modules.progress import module as progress

    reported = progress.words_in_progress()
    if reported is not None:
        return reported

    log.warning(
        "generation.pool.snapshot_missing",
        "还没有任何设备报过词池快照，这一轮按 study_states 里的旧派生值排除",
    )
    rows = get_connection("learning").execute(
        "SELECT DISTINCT item_key FROM study_states"
        " WHERE item_type = 'word' AND pool != 'new'"
    ).fetchall()
    return {str(r["item_key"]).lower() for r in rows if r["item_key"]}


def stock() -> int:
    """How many prepared generated articles are still unread.

    The number the top-up watches. Exam papers are not counted: they are a
    library, not a supply — they were not written to teach anything and they
    never run out.
    """
    row = get_connection("learning").execute(
        "SELECT COUNT(*) AS n FROM reading_articles"
        " WHERE source = 'generated' AND status = 'ready' AND read_at IS NULL"
    ).fetchone()
    return int(row["n"] or 0)


def _provider():
    chosen = jobs.configured_provider(pipeline.KIND)
    return providers.get(chosen) if chosen else providers.default_provider()


def _resume_unfinished() -> list[int]:
    """Make sure everything already on the shelf is actually readable.

    An article is only readable once every content word has a sense decision,
    and there are two ways that stalls silently:

    * the service was killed mid-annotation — ``jobs`` marks the dead job as
      paused at startup, but nothing restarts it;
    * the article was re-analysed after its annotation was planned, so the job
      is holding token ids that no longer exist. Each batch then finds nothing
      to do and reports success, the job finishes "complete", and the article
      stays at ``annotating`` for good. Measured 2026-09-11 — the whole failure
      produces not one error line.

    So this heals **articles, not jobs**: anything prepared but not ready gets
    its annotation planned again from what is missing right now. Re-planning is
    free of charge for tokens that are already annotated — the planner only
    picks up what has no sense yet — so healing something that did not need it
    costs one query.

    Doing it at the start of a supply run rather than at startup is deliberate:
    annotation costs money and takes minutes, and four in the morning is when
    nobody minds.
    """
    from backend.modules.reading import annotate

    # Flip jobs whose process died into a state that can be picked up again.
    jobs.recover_interrupted()

    stuck = get_connection("learning").execute(
        "SELECT id, status FROM reading_articles"
        " WHERE source = 'generated' AND status != 'ready' ORDER BY id"
    ).fetchall()

    healed: list[int] = []
    for row in stuck:
        article_id = int(row["id"])
        try:
            if annotate.start_for(article_id) is not None:
                healed.append(article_id)
        except Exception:  # noqa: BLE001 - one stuck article must not stop the rest
            log.exception(
                "daily.heal.failed",
                f"文章 {article_id} 的标注没能续上",
                article_id=article_id,
            )
    if healed:
        log.warning(
            "daily.healed",
            f"有 {len(healed)} 篇文章备好了却还没标注完，已重新排上——"
            f"没标注完的文章打不开，而这件事本身不会报错",
            articles=healed,
        )
    return healed


def prepare(count: int | None = None, *, reason: str = "scheduled") -> Outcome:
    """Write articles until ``count`` of them pass, or the fuse blows.

    Returns rather than raises: a morning with two articles instead of three is
    a worse morning, not a failed service, and the caller (a scheduled task)
    logs and pushes on its own.
    """
    wanted = int(count if count is not None else runtime_config.get("gen_daily_count"))
    cap = int(runtime_config.get("gen_daily_attempt_cap"))
    _resume_unfinished()
    exclude = words_in_progress()
    provider = _provider()
    result = Outcome(wanted=wanted)

    # Different seeds inside one run, so the three pieces do not all teach the
    # same twenty-five words (决定 7). The words already used in this run are
    # excluded from the next one, which is the part the seed alone cannot do.
    used: set[str] = set()
    base = random.randrange(1, 10 ** 6)  # noqa: S311 - not a security decision

    log.info(
        "daily.started",
        f"开始备稿：目标 {wanted} 篇，上限 {cap} 次，排除在学词 {len(exclude)} 个",
        wanted=wanted, cap=cap, excluded=len(exclude), provider=provider.id,
        model=provider.model, reason=reason,
    )

    while len(result.prepared) < wanted and result.attempts < cap:
        result.attempts += 1
        seed = base + result.attempts * 977
        try:
            written = pipeline.write_one(
                provider,
                seed=seed,
                exclude=exclude | used,
                note=f"daily:{reason}",
            )
        except Exception as exc:  # noqa: BLE001 - one bad call must not end the run
            result.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception(
                "daily.attempt.failed",
                f"第 {result.attempts} 次生成没能完成，继续下一次",
                attempt=result.attempts,
            )
            continue

        if not written.ok:
            result.rejected.append({
                "draft_id": written.draft_id,
                "why": written.failures,
                "beyond_rate": round(written.report.beyond_rate, 2),
            })
            continue

        try:
            article_id = _shelve(written.draft_id)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"入库失败 draft {written.draft_id}: {exc}")
            log.exception(
                "daily.ingest.failed",
                f"草稿 {written.draft_id} 入库失败",
                draft_id=written.draft_id,
            )
            continue

        result.prepared.append(article_id)
        result.drafts.append(written.draft_id)
        used |= {w.lower() for w in written.target_words}
        if written.notes:
            log.info(
                "daily.note",
                f"文章 {article_id} 记一笔（不影响录用）：" + "、".join(written.notes),
                article_id=article_id, notes=written.notes,
            )

    result.hit_cap = len(result.prepared) < wanted and result.attempts >= cap
    _finish_articles(result.prepared)

    log_decision(
        "article.prepared",
        f"备稿 {len(result.prepared)}/{wanted} 篇，用了 {result.attempts} 次生成",
        inputs={
            "wanted": wanted,
            "cap": cap,
            "excluded_words": len(exclude),
            "provider": provider.id,
            "model": provider.model,
            "reason": reason,
            "stock_after": stock(),
        },
        candidates=[
            {"draft": r["draft_id"], "rejected_for": r["why"]} for r in result.rejected
        ],
        chosen=result.prepared,
        reason="一次一篇、当场校验、够数为止；不合格的丢弃不修补（§B3 的择优不在本阶段）",
    )

    if result.hit_cap:
        # ERROR is what reaches Bark. This is the one outcome that means
        # something is broken rather than merely unlucky.
        log.error(
            "daily.cap.reached",
            f"生成了 {result.attempts} 次仍然只有 {len(result.prepared)} 篇合格，已停手。"
            f"按实测合格率这几乎不会自然发生——检查模型、提示词与大纲规则",
            attempts=result.attempts, prepared=len(result.prepared), wanted=wanted,
            rejected=[r["why"] for r in result.rejected],
        )
    else:
        log.info(
            "daily.finished",
            f"备稿完成：{len(result.prepared)} 篇（{result.attempts} 次生成，"
            f"丢弃 {len(result.rejected)} 篇），库存 {stock()} 篇",
            prepared=result.prepared, attempts=result.attempts,
            rejected=len(result.rejected),
        )
    return result


def _finish_articles(article_ids: list[int]) -> None:
    """Wait out the annotation, then judge the phrases. Nobody is waiting at 4am.

    Two reasons this is not left to run on its own:

    * **An article is not readable until annotation finishes.** It runs on a
      background thread, and nothing else would notice if it died.
    * **Phrases are scanned structurally at ingest but not judged.** Judging is
      what decides whether ``account for`` in this sentence is a phrase or two
      ordinary words, and until it happens the phrase simply does not show —
      silently, with the article looking perfectly fine. Left undone it also
      turns ``verify_phase2.py`` 7.9 red from the next morning onwards, which is
      how this was noticed at all (2026-09-11: three prepared articles, twelve
      unjudged candidates, nothing in any log).
    """
    if not article_ids:
        return
    from backend.modules.reading import phrases

    # Annotation threads belong to the batch runner; wait for the ones that are
    # running rather than polling a status column, which is a value they write
    # (坑 §7.2: never wait on something you also change).
    for entry in list(jobs.running_entries()):
        entry.thread.join(timeout=600)

    try:
        outcome = phrases.scan_and_judge(
            article_ids, title=f"判断词组（备稿 {len(article_ids)} 篇）"
        )
        log.info(
            "daily.phrases",
            f"备好的 {len(article_ids)} 篇已排上词组判断：{outcome.get('waiting', 0)} 处",
            articles=article_ids, **{k: v for k, v in outcome.items() if k != "articles"},
        )
    except Exception:  # noqa: BLE001 - phrases never block the supply
        log.exception(
            "daily.phrases.failed",
            "词组判断没能排上——文章仍然可读，只是词组不会显示",
            articles=article_ids,
        )


def _shelve(draft_id: int) -> int:
    """Put a passing draft on the shelf and start its annotation.

    Imported here rather than at module load: generation and reading are
    separate modules and only this one path connects them, so the dependency
    stays visible and does not become an import cycle the day reading wants
    something from generation.
    """
    from backend.modules.reading import annotate, ingest

    article_id = ingest.ingest_draft(draft_id)
    annotate.start_for(article_id)
    return article_id


# --------------------------------------------------------------------------- #
# The three safety nets (主文档 §H)
# --------------------------------------------------------------------------- #

def run_scheduled() -> dict[str, Any]:
    """Net two: the fixed time. This is what the scheduled task calls."""
    return prepare(reason="scheduled").as_dict()


def on_article_finished(event: dict[str, Any]) -> None:
    """Net one: top up as soon as something is read.

    Subscribed rather than wired into the reading flow — architecture rule 6.
    Reading emits that an article was finished and knows nothing about supply;
    if this module is deleted, reading still works.

    Deliberately writes at most one article per event: this runs inside the
    request that reported the finish, and a learner who finishes a piece should
    not wait for three more to be written.
    """
    if not bool(runtime_config.get("gen_topup_enabled")):
        return
    floor = int(runtime_config.get("gen_stock_floor"))
    current = stock()
    if current >= floor:
        return
    log.info(
        "daily.topup",
        f"读完一篇后库存只剩 {current} 篇（下限 {floor}），补一篇",
        stock=current, floor=floor, article_id=event.get("article_id"),
    )
    prepare(count=1, reason="topup")
