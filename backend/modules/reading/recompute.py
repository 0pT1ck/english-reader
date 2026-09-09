"""Recompute the derived flags on articles already in the database.

重算 recompute / 超纲标记 out-of-syllabus flag / 难度画像 difficulty profile

Ingest is deliberately a one-way street: :mod:`.ingest` says so at length, and
the reason is that the token rows *are* the encounter record — re-analysing an
article would rewrite history that the learner's marks point at.

This module is the narrow exception, and it stays narrow on purpose. It
rewrites **only values derived from the dictionary** — the ``beyond`` flag and
the difficulty profile — and never touches a token's identity, its offsets, its
sense annotation or anything a mark could be attached to. Nothing here inserts
or deletes a row.

It exists because a syllabus rule was wrong for a whole phase: ``beyond`` was
computed from one spelling's tags alone, which called ``quickly``, ``teeth``
and ``short-lived`` out of syllabus. Fixing the rule is useless while 453
articles still carry the answers the old one gave, and re-ingesting them would
throw away the reading history to fix a boolean.

The safety property is counted, not asserted: the row count before and after
must match exactly, or the transaction rolls back — the same shape as the
learning-record migration in :mod:`.schema`, and for the same reason.
"""

from __future__ import annotations

from typing import Any

from backend.core.db import get_connection
from backend.core.logging import get_logger, trace
from backend.modules.reading import difficulty, repository
from backend.modules.vocabulary import analyzer, syllabus

log = get_logger("reading.recompute")


def _beyond_for(headword: str | None, surface: str, kind: str, is_proper: bool) -> int:
    """The ``beyond`` value a token would get today.

    Mirrors :func:`ingest._is_beyond` exactly, from stored columns rather than
    from a spaCy token — the classification (``kind``) was decided at ingest and
    is not revisited here.
    """
    if kind in ("nonword", "proper") or is_proper:
        return 0
    if not headword:
        return 1
    return 0 if syllabus.known(headword, surface, difficulty.WITHIN_CET6) else 1


def recompute_beyond() -> dict[str, Any]:
    """Rewrite ``reading_tokens.beyond`` for every stored token.

    Returns what changed, per direction, so the caller can report it rather
    than trust it.
    """
    conn = get_connection("learning")
    before = conn.execute("SELECT COUNT(*) FROM reading_tokens").fetchone()[0]

    rows = conn.execute(
        "SELECT id, headword, surface, kind, beyond FROM reading_tokens"
    ).fetchall()

    updates: list[tuple[int, int]] = []
    to_within = to_beyond = 0
    for row in rows:
        want = _beyond_for(row["headword"], row["surface"] or "", row["kind"], False)
        if want != row["beyond"]:
            updates.append((want, row["id"]))
            if want == 0:
                to_within += 1
            else:
                to_beyond += 1

    try:
        conn.executemany("UPDATE reading_tokens SET beyond = ? WHERE id = ?", updates)
        after = conn.execute("SELECT COUNT(*) FROM reading_tokens").fetchone()[0]
        if after != before:
            raise RuntimeError(
                f"重算前后 token 行数对不上：前 {before}，后 {after}。已回滚。"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log.info("reading.recompute.beyond", "超纲标记已重算",
             changed=len(updates), to_within=to_within, to_beyond=to_beyond, tokens=before)
    return {"tokens": before, "changed": len(updates),
            "to_within": to_within, "to_beyond": to_beyond}


def recompute_difficulty() -> dict[str, Any]:
    """Re-measure every article's difficulty profile.

    Runs the article body back through the same :func:`difficulty.measure` that
    ingest uses. Re-analysing rather than recomputing from the stored tokens is
    the point: a second implementation reading the same columns is how the two
    sides drifted apart in the first place.
    """
    conn = get_connection("learning")
    rows = conn.execute(
        "SELECT id, body FROM reading_articles WHERE body IS NOT NULL ORDER BY id"
    ).fetchall()

    done = 0
    for row in rows:
        sentences = analyzer.analyze(row["body"])
        measures = difficulty.measure(sentences)
        # Sense-derived indicators were folded in after ingest and are not
        # recoverable from the text; carry the stored value across so a
        # recompute never silently drops 考频 density.
        stored = repository.article_row(row["id"]).get("difficulty") or {}
        if isinstance(stored, dict) and "exam_key_pct" in stored:
            measures["exam_key_pct"] = stored["exam_key_pct"]
        repository.store_difficulty(row["id"], measures)
        done += 1

    log.info("reading.recompute.difficulty", "难度画像已重算", articles=done)
    return {"articles": done}


def recompute_all() -> dict[str, Any]:
    """Both, in the order that matters: flags first, then the profile."""
    with trace():
        flags = recompute_beyond()
        profile = recompute_difficulty()
    return {"beyond": flags, "difficulty": profile}
