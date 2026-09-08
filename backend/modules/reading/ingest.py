"""Turning a piece of text into something that can be read.

The pipeline runs **once**, at ingest, and its output is stored:

    原文 → 分句分词与词形还原 → 分类 → 语境释义标注 → 难度指标 → 入库，此后不可变

Tapping a word at read time is then a table lookup. That is not an optimisation;
it is why a NanoPi can serve this at all, and it is what C5 of the design means
by "阅读、点词、标记全程不需要任何语言分析".

Immutability matters for a second reason: the token rows *are* the encounter
record. Re-analysing an article would rewrite history the learner's marks point
at, so a regenerated article is a new article, never an edit of the old one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.core import events
from backend.core.config import get_settings
from backend.core.errors import InvalidRequest, NotFound
from backend.core.logging import get_logger, trace
from backend.modules.reading import difficulty, repository
from backend.modules.senses import repository as senses
from backend.modules.vocabulary import analyzer
from backend.modules.wordfamily import repository as families

log = get_logger("reading.ingest")

EXAM_SOURCES = ("cet4", "cet6", "kaoyan")


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def _classify(token: analyzer.TokenAnalysis) -> str:
    if not token.is_word:
        return "nonword"
    if token.is_proper_noun:
        return "proper"
    if token.is_content:
        return "content"
    # Function words are still tappable — C1 says "点击任意词" — they simply get
    # the dictionary gloss and never a sense or a mark.
    return "function"


def _is_beyond(token: analyzer.TokenAnalysis) -> bool:
    """Outside the CET-6 syllabus.

    Two traps in one line, both documented at length in :mod:`.difficulty`:

    * tags are a *levelled list*, so "within CET-6" means carrying **any** of
      zk / gk / cet4 / cet6, not "carrying cet6";
    * they are split between British and American spellings at random, so they
      have to be unioned across the pair — which is what
      :func:`difficulty.syllabus_tags` does and what this used to skip. ``labor`` was
      called out of syllabus 41 times in the corpus while ``labour`` sat in the
      CET-4 list.

    A word the dictionary does not have at all counts as beyond: there is no
    evidence it is in any syllabus, and saying nothing about it would let it
    pass as ordinary.
    """
    if not token.is_word or token.is_proper_noun:
        return False
    if not token.headword:
        return True
    return not (difficulty.syllabus_tags(token) & difficulty.WITHIN_CET6)


def derivation_for(headword: str | None) -> dict[str, Any] | None:
    """The word-formation breakdown to show for this word, or ``None``.

    The original design only shows a breakdown when the root is already known,
    so the learner sees ``nation + -ity`` rather than a new word to memorise.
    "Already known" needs the P3 ability estimate, so P2 shows it for every
    eligible derived word and reserves ``root_known`` in the contract as null.

    *Eligible* means grade C is excluded — the grades already say that showing a
    drifted derivation misleads — **and the entry was actually reviewed by a
    model**, not just produced by an affix rule.

    That second condition was added after auditing the rule-only tier, and the
    first version of this filter got it wrong. The assumption was that a purely
    grammatical suffix is exactly what rules get right, so grade A could stand
    on its own. A random sample of twenty-five rule-only grade-A entries says
    otherwise — about one in five is nonsense, and the failures are not the kind
    a rule can catch:

        forster  <- forst + -er     the "root" is not a word
        brunner  <- brunn + -er     nor is this one
        renoir   <- noir  + re-     a painter, cut in half
        remove   <- move  + re-     coincidental prefix, meaning unrelated
        repair   <- pair  + re-     same
        banner   <- ban   + -er     same

    Frequency filtering catches the first three and nothing else: ``move``,
    ``pair`` and ``ban`` are all common words. Telling ``remove <- move`` from
    ``revisit <- visit`` needs semantic judgement, which is what the review pass
    in P1b was for and why only 4997 of 10471 entries have one.

    So the trade is precision over recall: ``builder <- build`` and
    ``careful <- care`` stop being shown even though they are correct. That
    costs little — those are transparent enough that nobody needs the hint. The
    hint earns its place on ``likelihood``, ``archaeologist``, ``nationality``,
    and every one of those was reviewed.
    """
    if not headword:
        return None
    try:
        family = families.family_of(headword)
    except Exception:  # noqa: BLE001 - a missing family table must not break ingest
        return None
    if not family:
        return None

    root = family.get("root")
    if not root or root == headword:
        return None

    grade = (family.get("grade") or "").upper()
    if grade == "C":
        return None
    if family.get("source") != "llm":
        return None
    # The reviewer sometimes flags its own answer. Twenty-one entries say
    # 需确认 in their own breakdown — `army <- arm + -y` among them — and an
    # uncertain hint is worse than none.
    breakdown = family.get("breakdown_zh") or ""
    if "需确认" in breakdown:
        return None

    return {
        "root": root,
        "affix": family.get("affix"),
        "breakdown_zh": family.get("breakdown_zh"),
        "grade": grade,
    }


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #


def analyse_into(article_id: int, body: str, target_words: set[str]) -> dict[str, Any]:
    """Analyse, classify and store. Returns the difficulty measures."""
    sentences = analyzer.analyze(body)

    rows: list[dict[str, Any]] = []
    tokens_by_sentence: list[list[dict[str, Any]]] = []

    for sentence in sentences:
        rows.append({
            "seq": sentence.index,
            "text": sentence.text,
            "char_start": sentence.char_start,
            "char_end": sentence.char_end,
        })
        batch: list[dict[str, Any]] = []
        for token in sentence.tokens:
            kind = _classify(token)
            headword = token.headword
            derived = derivation_for(headword) if kind == "content" else None
            batch.append({
                "seq": token.index,
                "surface": token.text,
                "lemma": token.lemma,
                "headword": headword,
                "pos": token.pos,
                "char_start": token.char_start,
                "char_end": token.char_end,
                "kind": kind,
                "is_target": int(bool(headword) and headword in target_words),
                "beyond": int(_is_beyond(token)),
                "derived_root": derived["root"] if derived else None,
                "derived_affix": derived["affix"] if derived else None,
                # Left NULL: "not annotated yet", which is a different state
                # from 0, "annotated, and this word has no sense set".
                "sense_id": None,
                "sense_ordinal": None,
            })
        tokens_by_sentence.append(batch)

    repository.store_sentences(article_id, rows, tokens_by_sentence)

    measures = difficulty.measure(sentences)
    repository.store_difficulty(article_id, measures)
    return measures


def ingest(source: str, source_ref: str, title: str, body: str, *,
           target_words: set[str] | None = None) -> int:
    """Full ingest of one article, up to the point annotation takes over.

    Analysis and difficulty are synchronous — they are local and take under a
    second. Sense annotation calls a model, so it becomes a batch job and the
    article stays in ``annotating`` until it finishes.
    """
    if not body or not body.strip():
        raise InvalidRequest("文章正文是空的")

    article_id = repository.create_article(source, source_ref, title, body)
    current = repository.article_row(article_id)
    if current["status"] == "ready":
        return article_id

    with trace() as trace_id:
        repository.set_status(article_id, "analysing")
        try:
            measures = analyse_into(article_id, body, target_words or set())
        except Exception as exc:  # noqa: BLE001 - surface it, do not lose the article
            repository.set_status(article_id, "failed", f"分析失败：{exc}")
            log.exception(
                "article.analysis.failed",
                f"文章 {article_id} 分析失败",
                article_id=article_id, trace=trace_id,
            )
            raise

        repository.set_status(article_id, "annotating")
        log.info(
            "article.analysed",
            f"文章 {article_id} 已分析：{measures.get('sentence_count')} 句 /"
            f" {measures.get('word_count')} 词",
            article_id=article_id, source=source, trace=trace_id, **{
                k: v for k, v in measures.items()
                if k in ("beyond_cet4_pct", "beyond_cet6_pct", "rare_word_pct")
            },
        )
        events.emit("article.analysed", article_id=article_id, source=source)

    return article_id


def finalise_if_annotated(article_id: int) -> bool:
    """Flip to ``ready`` once every content token has a sense decision.

    Called after each annotation batch rather than from a job-completion hook,
    because a job that fails halfway should still leave the article visibly
    unfinished rather than silently ready.
    """
    done, total = repository.annotation_progress(article_id)
    if total and done < total:
        return False
    # Structural only — no model, so it costs nothing and the judgement can be
    # queued whenever. An article is readable before its phrases are judged;
    # they simply do not show until they are.
    try:
        from backend.modules.reading import phrases
        phrases.find_candidates(article_id)
    except Exception:  # noqa: BLE001 - a phrase scan must never block an article
        log.exception("phrases.scan.failed", f"文章 {article_id} 的词组扫描失败，不影响阅读",
                      article_id=article_id)
    repository.set_status(article_id, "ready")
    log.info("article.ready", f"文章 {article_id} 已就绪", article_id=article_id)
    events.emit("article.ready", article_id=article_id)
    return True


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


def exam_papers(source: str) -> list[dict[str, Any]]:
    """Every paper on disk for one exam, with whether it is already ingested."""
    if source not in EXAM_SOURCES:
        raise InvalidRequest("未知的真题来源", source=source)
    root = get_settings().data_dir / "exam_papers" / source
    if not root.exists():
        return []
    ingested = repository.ingested_refs(source)
    return [
        {"ref": path.name, "title": path.stem, "ingested": path.name in ingested}
        for path in sorted(root.glob("*.txt"))
    ]


def ingest_exam_paper(source: str, ref: str) -> int:
    if source not in EXAM_SOURCES:
        raise InvalidRequest("未知的真题来源", source=source)
    path: Path = get_settings().data_dir / "exam_papers" / source / ref
    # Refuse anything that escapes the exam directory rather than trusting the
    # caller's filename.
    if path.name != ref or not path.is_file():
        raise NotFound("找不到这份真题")
    body = path.read_text(encoding="utf-8", errors="ignore").strip()
    return ingest(source, ref, path.stem, body)


def _parse_target_words(raw: str | None) -> set[str]:
    """Read the target-word list a draft was generated from.

    The generation workbench stores it as a JSON array. The delimiter fallback
    is for anything hand-entered — and it matters more than it looks: getting
    this wrong produces no error at all, just an article with no target words,
    which then reads correctly and records nothing when finished.
    """
    if not raw:
        return set()
    text = raw.strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            return {str(w).strip().lower() for w in parsed if str(w).strip()}
    return {
        word.strip().lower()
        for word in text.replace("\n", ",").replace(" ", ",").split(",")
        if word.strip()
    }


def ingest_draft(draft_id: int) -> int:
    """Put a generated draft on the shelf.

    Manual by design: automatic over-generate-and-pick is P2x, and there are ten
    usable drafts, so an "入库" button is the honest amount of machinery.
    """
    from backend.core.db import get_connection

    row = get_connection("learning").execute(
        "SELECT id, title, body, target_words FROM generation_drafts WHERE id = ?",
        (draft_id,),
    ).fetchone()
    if row is None:
        raise NotFound("找不到这篇草稿")

    targets = _parse_target_words(row["target_words"])
    if not targets:
        log.warning(
            "draft.targets.missing",
            f"草稿 {draft_id} 没有解析出目标词，入库后这篇不会记账",
            draft_id=draft_id,
        )
    return ingest(
        "generated", f"draft-{draft_id}", row["title"] or f"草稿 {draft_id}",
        row["body"], target_words=targets,
    )


def sense_options(headword: str) -> list[dict[str, Any]]:
    """Candidate senses for one word, as the annotator and the client see them."""
    return senses.senses_of(headword)
