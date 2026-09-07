"""What the client actually receives, and what its events actually change.

Two responsibilities, kept together because they are two halves of one contract:
assembling a reading payload, and applying the events that come back from it.

**Everything a reader needs for one article arrives in one response.** Tapping a
word, marking it, scrolling — none of it makes a request. That is the offline
shape of architecture rule 2, honoured at the level P2 can honour it: the page
itself is not offline-capable (no service worker — the web reader is a
development tool that P7's native client replaces), but the protocol is, and
events queue locally and upload in batches with idempotency keys.

**Glosses are sent once per headword, not once per occurrence.** A 450-word
article has around 250 content tokens over maybe 180 distinct words; inlining a
sense list per token would multiply the payload for nothing.
"""

from __future__ import annotations

from typing import Any

from backend.core import auth, runtime_config
from backend.core.db import get_connection
from backend.core.errors import InvalidRequest
from backend.core.logging import get_logger
from backend.modules.reading import difficulty, ingest, repository
from backend.modules.senses import repository as senses
from backend.modules.vocabulary import repository as dictionary

log = get_logger("reading.service")

EVENT_TYPES = (
    "article.opened",
    "article.progress",
    "article.finished",
    "word.tapped",
    "word.marked",
    "word.unmarked",
)


def capabilities() -> dict[str, bool]:
    """Which reserved slots actually carry values yet.

    A null field alone is ambiguous — ``root_known: null`` could mean "this word
    has no root" or "the ability estimate does not exist". The client renders
    those two differently (nothing at all, versus "功能待开发"), so the
    distinction has to be in the contract rather than guessed from the value.

    Every flag here is false because the feature behind it belongs to a later
    phase. The fields are present now because architecture rule 5 forbids
    changing a field's meaning later, and sideloaded clients update late.
    """
    annotated_exams = int(get_connection("learning").execute(
        "SELECT COUNT(*) FROM reading_articles WHERE source != 'generated'"
        " AND status = 'ready'"
    ).fetchone()[0])
    return {
        # P3: ability estimate. Fills learner.level, difficulty.for_you,
        # token.root_known.
        "level_estimate": False,
        # P5: per-sense memory parameters and due dates.
        "memory_state": False,
        # A generation task nobody has written yet: one line saying what a
        # proper noun is.
        "proper_noun_notes": False,
        # Layer ③ of the gloss. Turns true once the exam corpus has been
        # annotated end to end and 考频 is actually counted — until then the
        # field is absent rather than zero, because a zero here would read as
        # "never appears in the exams", which is a different claim.
        "exam_frequency": annotated_exams >= 400,
    }


# --------------------------------------------------------------------------- #
# Library
# --------------------------------------------------------------------------- #


def library(learner_id: int, *, shelf: str = "fresh", source: str | None = None,
            sort: str = "composite", descending: bool = False,
            limit: int = 500) -> dict[str, Any]:
    if sort not in difficulty.SORTABLE:
        raise InvalidRequest("不支持这个排序方式", sort=sort,
                             supported=sorted(difficulty.SORTABLE))
    rows = repository.list_articles(
        learner_id=learner_id, shelf=shelf, source=source,
        sort=sort, descending=descending, limit=limit,
    )
    return {
        "learner": auth.learner_profile(learner_id),
        "capabilities": capabilities(),
        "shelf": shelf,
        "sort": sort,
        "descending": descending,
        "sortable": difficulty.SORTABLE,
        "fresh_days": int(runtime_config.get("reading_fresh_days")),
        "articles": [
            {
                "id": row["id"],
                "title": row["title"],
                "source": row["source"],
                "source_label": row["source_label"],
                "word_count": row["word_count"],
                "sentence_count": row["sentence_count"],
                "prepared_at": row["prepared_at"],
                "read_at": row["read_at"],
                "percent": row.get("percent") or 0.0,
                # How many words this article was written to teach. 0 for exam
                # papers — they were not written to teach anything, so finishing
                # one records only what the reader marked by hand.
                "target_count": row.get("target_count") or 0,
                "difficulty": row["difficulty"],
                "difficulty_score": row["difficulty_score"],
                # P3 fills this in; the column exists so the client's list can
                # be written against it now and never touched again.
                "difficulty_for_you": None,
            }
            for row in rows
        ],
    }


# --------------------------------------------------------------------------- #
# One article
# --------------------------------------------------------------------------- #


def _glossary(learner_id: int, tokens: list[dict[str, Any]]) -> dict[str, Any]:
    headwords = {t["headword"] for t in tokens if t.get("headword")}
    marks = repository.marks_for_headwords(learner_id, headwords)
    states = repository.states_for_headwords(learner_id, headwords)

    glossary: dict[str, Any] = {}
    for headword in sorted(headwords):
        entry = dictionary.lookup(headword)
        sense_list = senses.senses_of(headword)
        # Same eligibility rule the ingest pass used, so what the reader sees
        # and what was stored on the token can never disagree.
        family = ingest.derivation_for(headword)

        glossary[headword] = {
            "headword": headword,
            "phonetic": entry["phonetic"] if entry else None,
            # Layer ②: the dictionary's comma pile, verbatim. Kept even though
            # layer ① is better, because it is what the learner falls back to
            # when a word has no sense set.
            "translation": entry["translation"] if entry else None,
            "tags": entry["tags"] if entry else None,
            "frq": entry["frq"] if entry else None,
            "senses": [
                {
                    "id": s["id"],
                    "ordinal": s["ordinal"],
                    # Layer ① proper: the English concept definition, written
                    # with words already known, so looking a word up is itself
                    # reading input rather than a switch into Chinese.
                    "concept_en": s["concept_en"],
                    "gloss_zh": s["gloss_zh"],
                    # Layer ③. Absent, not zero, until the exam corpus has been
                    # annotated — see capabilities().
                    "exam": (
                        {
                            "frequency": s.get("exam_frequency", 0),
                            "is_exam_key": bool(s.get("is_exam_key")),
                        }
                        if s.get("exam_frequency") else None
                    ),
                    # P5's slot. Whole object null rather than invented fields.
                    "memory": None,
                }
                for s in sense_list
            ],
            "derivation": (
                {
                    "root": family["root"],
                    "affix": family["affix"],
                    "breakdown_zh": family["breakdown_zh"],
                    # P3: whether the learner already knows the root. Until then
                    # the breakdown is shown for every eligible derived word.
                    "root_known": None,
                }
                if family else None
            ),
            # Every mark on this headword, keyed by sense id — including senses
            # other than the one on screen. Without this the tap panel cannot
            # say "you marked another sense of this word", and the learner reads
            # an unmarked word they know they marked as the app forgetting.
            "marks": {
                str(sense_id): kind
                for (word, sense_id), kind in marks.items() if word == headword
            },
            "states": {
                str(sense_id): {"pool": state["pool"], "encounters": state["encounters"]}
                for (word, sense_id), state in states.items() if word == headword
            },
        }
    return glossary


def article(learner_id: int, article_id: int) -> dict[str, Any]:
    row = repository.article_row(article_id)
    if row["status"] != "ready":
        # Not an error: lazy ingest means the client may well ask for something
        # still being prepared. It needs to be told to wait, with enough detail
        # to show progress rather than a spinner of unknown length.
        done, total = repository.annotation_progress(article_id)
        return {
            "learner": auth.learner_profile(learner_id),
            "capabilities": capabilities(),
            "article": {"id": article_id, "title": row["title"], "status": row["status"]},
            "preparing": {
                "status": row["status"],
                "detail": row["status_detail"],
                "annotated": done,
                "total": total,
            },
        }

    sentences = repository.sentences_of(article_id)
    tokens = repository.tokens_of(article_id)
    sentence_seq = {s["id"]: s["seq"] for s in sentences}

    return {
        "learner": auth.learner_profile(learner_id),
        "capabilities": capabilities(),
        "article": {
            "id": row["id"],
            "title": row["title"],
            # The original text, sent alongside the tokens rather than instead
            # of them: token offsets index into it, so a client can render the
            # exact spacing and paragraphing without re-tokenising anything.
            "body": row["body"],
            "source": row["source"],
            "source_label": repository.SOURCE_LABELS.get(row["source"], row["source"]),
            "word_count": row["word_count"],
            "sentence_count": row["sentence_count"],
            "difficulty": row["difficulty"],
            "difficulty_score": row["difficulty_score"],
            "difficulty_for_you": None,
            "read_at": row["read_at"],
            # C10: out-of-syllabus words are called out in generated text
            # ("outside your range, no need to learn this") but deliberately
            # not in exam papers, where meeting an unknown word is the skill
            # being practised.
            "mark_beyond": row["source"] == "generated",
        },
        "sentences": [
            {"seq": s["seq"], "text": s["text"],
             "char_start": s["char_start"], "char_end": s["char_end"]}
            for s in sentences
        ],
        "tokens": [
            {
                "seq": t["seq"],
                "surface": t["surface"],
                "sentence_seq": sentence_seq.get(t["sentence_id"], 0),
                "char_start": t["char_start"],
                "char_end": t["char_end"],
                "kind": t["kind"],
                "is_target": bool(t["is_target"]),
                "beyond": bool(t["beyond"]),
                "headword": t["headword"],
                "sense_id": t["sense_id"],
                "sense_ordinal": t["sense_ordinal"],
                # Proper nouns: the design wants one line explaining what the
                # place or person is. Generating those is a task nobody has
                # written, so the slot is here and empty rather than absent.
                "note": None,
            }
            for t in tokens
        ],
        "glossary": _glossary(learner_id, tokens),
        "progress": repository.progress_of(learner_id, article_id),
    }


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


def _finish_article(learner_id: int, article_id: int) -> dict[str, int]:
    """Record the reading. This is the moment the ledger is written.

    **Only on finish, never on ingest.** That single rule is what makes "words
    used in articles I never opened stay available for future articles" true —
    and if it is ever broken, nothing errors and nothing is logged; those words
    simply never come up again. Hence an explicit acceptance check for it.

    Target words enter the review pool with the sentence they were introduced
    in, which is what a review card's original sentence comes from later.
    Incidental words get an encounter only if they are already tracked: C6 is
    explicit that turning every unknown word into a task is the failure mode of
    vocabulary apps, so meeting a word in passing must not create a task.
    """
    # Recorded once. Finishing the same article again — clicking the button
    # twice, an offline queue replaying, a script re-running — must not add
    # another full set of encounters on top: `colony` came out at 55 encounters
    # in a 430-word article that way. `read_at` and `finished_at` are already
    # once-only (both COALESCE), so the ledger matches them. A genuine re-read
    # is E6's 旧文重读, which is P8 and will bring its own event.
    row = get_connection("learning").execute(
        "SELECT read_at, status FROM reading_articles WHERE id = ?", (article_id,)
    ).fetchone()

    # An article still being annotated has nothing to record yet, and marking it
    # read would be worse than doing nothing: the once-only guard below would
    # then refuse to record it ever again, and its target words would be lost
    # for good. Refuse the whole thing instead.
    if row is None or row["status"] != "ready":
        log.warning(
            "article.finished.not_ready",
            f"文章 {article_id} 还没标注完，不记账也不标记为已读",
            article_id=article_id, status=row["status"] if row else None,
        )
        return {"introduced": 0, "revisited": 0}

    already = row
    if already["read_at"]:
        log.info(
            "article.finished.again",
            f"文章 {article_id} 之前已经读完过，不重复记账",
            article_id=article_id,
        )
        return {"introduced": 0, "revisited": 0}

    # Content words, plus anything this article was written to teach whatever
    # its part of speech came out as. A target word can land on a token spaCy
    # tagged PROPN — the generator was asked for `latin` and the model wrote
    # `Latin` — and being a designated target is the stronger signal. Filtering
    # on kind alone silently dropped one target word in twenty-five.
    #
    # A NULL sense folds into 0, the word-level slot. That is only safe because
    # of the guard above: on a `ready` article every *content* token has been
    # annotated, so a NULL left over belongs to a token annotation never applies
    # to — a target word that came out tagged PROPN, for instance. Without the
    # guard the same fold produced a phantom second row for every target word of
    # an article finished before its annotation had run.
    rows = get_connection("learning").execute(
        "SELECT headword, COALESCE(sense_id, 0) AS sense_id, MAX(is_target) AS is_target,"
        " COUNT(*) AS n, MIN(sentence_id) AS first_sentence"
        " FROM reading_tokens WHERE article_id = ?"
        " AND (kind = 'content' OR is_target = 1)"
        " AND headword IS NOT NULL"
        " GROUP BY headword, COALESCE(sense_id, 0)",
        (article_id,),
    ).fetchall()

    known = repository.states_for_headwords(learner_id, {r["headword"] for r in rows})
    introduced = revisited = 0

    # **Nothing enters the review queue by being read.** Only the learner's own
    # signal puts a word there: a mark (「我不会」), or later a failed spot check
    # (D3, 「你以为你会」). Finishing an article records that words were *met*,
    # never that they were *needed*.
    #
    # Two versions were tried and both rejected. Exam papers adopted 25 unmet
    # syllabus words by frequency — that was an ability estimate, which is P3,
    # built badly. Generated articles introduced their declared target words —
    # which reads more defensible, since the article *was* written to teach
    # them, but it still asserts on the learner's behalf that reading equals
    # needing to review.
    #
    # The cost is real and worth stating: the coverage arithmetic in the design
    # (25 targets × 289 articles ≈ 3.2 months) assumed finishing an article
    # taught its targets. It now depends on how much the reader marks, and is no
    # longer predictable. What is gained is that every row in the ledger traces
    # to something the learner actually did.
    for row in rows:
        key = (row["headword"], int(row["sense_id"]))
        if key in known:
            repository.touch_state(
                learner_id, row["headword"], int(row["sense_id"]),
                encounters=int(row["n"]),
            )
            revisited += 1

    repository.mark_article_read(article_id)
    log.info(
        "article.finished",
        f"读完文章 {article_id}：{revisited} 个已跟踪的词又遇见一次"
        "（复习队列只由你的标记决定，读完本身不加词）",
        article_id=article_id, revisited=revisited,
    )
    return {"introduced": introduced, "revisited": revisited}


def _apply(learner_id: int, event_type: str, payload: dict[str, Any]) -> None:
    if event_type in ("article.opened", "word.tapped"):
        # Recorded verbatim in client_events and nothing more. Taps are the
        # implicit signal of A5; what reads them is P3, and inventing a
        # derived table for it now would be building P3.
        return

    if event_type == "article.progress":
        repository.save_progress(
            learner_id, int(payload["article_id"]),
            int(payload.get("sentence_seq", 0)), float(payload.get("percent", 0.0)),
        )
        return

    if event_type == "article.finished":
        article_id = int(payload["article_id"])
        repository.save_progress(learner_id, article_id,
                                 int(payload.get("sentence_seq", 0)), 100.0, finished=True)
        _finish_article(learner_id, article_id)
        return

    if event_type == "word.marked":
        repository.set_mark(
            learner_id, str(payload["headword"]).lower(), int(payload.get("sense_id") or 0),
            str(payload["kind"]),
            article_id=payload.get("article_id"),
            sentence_id=payload.get("sentence_id"),
            token_id=payload.get("token_id"),
        )
        # A marked word is tracked from now on, whether or not this article was
        # written to teach it — the learner said it matters.
        repository.touch_state(
            learner_id, str(payload["headword"]).lower(),
            int(payload.get("sense_id") or 0), pool="reviewing",
            article_id=payload.get("article_id"),
            sentence_id=payload.get("sentence_id"),
        )
        return

    if event_type == "word.unmarked":
        repository.clear_mark(
            learner_id, str(payload["headword"]).lower(),
            int(payload.get("sense_id") or 0), payload.get("kind"),
        )
        return

    raise InvalidRequest("未知的事件类型", type=event_type)


def ingest_events(device_id: int, learner_id: int,
                  batch: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply a batch of reported events. Duplicates are accepted, not errors.

    An offline client retries whatever it is unsure about, so a duplicate is the
    protocol working, not a fault. The idempotency key's unique index decides;
    a check-then-insert would let two concurrent uploads both through.
    """
    accepted = duplicates = failed = 0
    results = []

    for item in batch:
        key = str(item.get("idem_key") or "").strip()
        event_type = str(item.get("type") or "")
        if not key or event_type not in EVENT_TYPES:
            failed += 1
            results.append({"idem_key": key, "status": "rejected",
                            "reason": "缺少幂等键或事件类型不认识"})
            continue

        payload = item.get("payload") or {}
        event_id = repository.record_event(
            key, device_id, learner_id, event_type, payload, item.get("occurred_at"),
        )
        if event_id is None:
            duplicates += 1
            results.append({"idem_key": key, "status": "duplicate"})
            continue

        try:
            _apply(learner_id, event_type, payload)
        except Exception as exc:  # noqa: BLE001 - one bad event must not sink the batch
            repository.finish_event(event_id, str(exc))
            failed += 1
            results.append({"idem_key": key, "status": "failed", "reason": str(exc)})
            log.warning(
                "event.apply.failed",
                f"事件 {event_type} 处理失败：{exc}",
                event_type=event_type, idem_key=key,
            )
            continue

        repository.finish_event(event_id)
        accepted += 1
        results.append({"idem_key": key, "status": "accepted"})

    return {
        "accepted": accepted,
        "duplicates": duplicates,
        "failed": failed,
        "results": results,
    }
