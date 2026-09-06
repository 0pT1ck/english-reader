"""Reads and writes for ``content.db``'s affix and family tables."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from backend.core.db import get_connection
from backend.modules.wordfamily.derive import Derivation


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Affixes
# --------------------------------------------------------------------------- #


def store_affixes(rows: list[dict[str, Any]]) -> int:
    conn = get_connection("content")
    conn.executemany(
        # Meanings are merged, not replaced: several source entries collapse onto
        # one affix once the disambiguating index is stripped (al1 and al2 are
        # both -al), and each carries a real meaning worth keeping. The Chinese
        # gloss is never touched — re-importing must not throw away work that
        # was paid for.
        "INSERT INTO affixes (affix, kind, meaning_en, meaning_zh, forms_pos,"
        " examples, created_at) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(affix, kind) DO UPDATE SET"
        " meaning_en = CASE"
        "   WHEN excluded.meaning_en IS NULL OR excluded.meaning_en = '' THEN meaning_en"
        "   WHEN meaning_en IS NULL OR meaning_en = '' THEN excluded.meaning_en"
        "   WHEN instr(meaning_en, excluded.meaning_en) > 0 THEN meaning_en"
        "   ELSE meaning_en || '; ' || excluded.meaning_en END,"
        " forms_pos = COALESCE(forms_pos, excluded.forms_pos),"
        " examples = excluded.examples",
        [
            (
                row["affix"], row["kind"], row.get("meaning_en"),
                row.get("meaning_zh"), row.get("forms_pos"),
                json.dumps(row.get("examples") or [], ensure_ascii=False), _now(),
            )
            for row in rows
        ],
    )
    conn.commit()
    return len(rows)


def set_affix_gloss(affix: str, kind: str, meaning_zh: str) -> None:
    conn = get_connection("content")
    conn.execute(
        "UPDATE affixes SET meaning_zh = ? WHERE affix = ? AND kind = ?",
        (meaning_zh, affix, kind),
    )
    conn.commit()
    affix_gloss.cache_clear()


def affixes_without_gloss() -> list[dict[str, Any]]:
    """Affixes still needing a Chinese explanation — the LLM job's input."""
    rows = get_connection("content").execute(
        "SELECT affix, kind, meaning_en, examples FROM affixes"
        " WHERE kind IN ('prefix', 'suffix')"
        "   AND (meaning_zh IS NULL OR meaning_zh = '')"
        " ORDER BY kind, affix"
    ).fetchall()
    return [dict(row) for row in rows]


@lru_cache(maxsize=1024)
def affix_gloss(affix: str, kind: str | None = None) -> str:
    """Chinese explanation of one affix, for the breakdown shown to the reader.

    The hyphen is stripped, since ``-ity`` is stored as ``ity``. ``kind``
    matters more than it looks: several strings are both a prefix and a suffix —
    ``al`` is the assimilated ``ad-`` ("to, toward") *and* the adjective-forming
    ``-al`` — and answering with the wrong one puts a plainly wrong explanation
    in front of the reader.
    """
    bare = affix.strip("-")
    if kind is None:
        # Infer from the hyphen's position when the caller knows the shape but
        # not the label: "-ity" is a suffix, "un-" a prefix.
        if affix.startswith("-"):
            kind = "suffix"
        elif affix.endswith("-"):
            kind = "prefix"

    clause = "AND kind = ?" if kind else ""
    params: tuple[Any, ...] = (bare, kind) if kind else (bare,)
    try:
        row = get_connection("content").execute(
            f"SELECT meaning_zh, meaning_en FROM affixes WHERE affix = ? {clause}"
            " ORDER BY CASE WHEN meaning_zh IS NULL OR meaning_zh = '' THEN 1 ELSE 0 END"
            " LIMIT 1",
            params,
        ).fetchone()
    except sqlite3.Error:
        return ""
    if row is None:
        return ""
    return row["meaning_zh"] or row["meaning_en"] or ""


def affix_stats() -> dict[str, int]:
    try:
        conn = get_connection("content")
        total = conn.execute("SELECT COUNT(*) AS n FROM affixes").fetchone()["n"]
        glossed = conn.execute(
            "SELECT COUNT(*) AS n FROM affixes WHERE meaning_zh IS NOT NULL AND meaning_zh != ''"
        ).fetchone()["n"]
        by_kind = {
            row["kind"]: row["n"]
            for row in conn.execute("SELECT kind, COUNT(*) AS n FROM affixes GROUP BY kind")
        }
    except sqlite3.Error:
        return {"total": 0, "glossed": 0}
    return {"total": int(total), "glossed": int(glossed), **by_kind}


# --------------------------------------------------------------------------- #
# Families
# --------------------------------------------------------------------------- #


def store_families(derivations: list[Derivation], *, source: str = "rule") -> int:
    """Insert derivations, leaving any existing row for the same pair alone.

    Existing rows win because a later pass is usually a model correcting the
    rules; re-running the rule pass must not overwrite that judgement.
    """
    conn = get_connection("content")
    conn.executemany(
        "INSERT OR IGNORE INTO word_families (member, root, affix, affix_kind,"
        " grade, source, created_at) VALUES (?,?,?,?,?,?,?)",
        [
            (d.member, d.root, d.affix, d.affix_kind, d.grade, source or d.source, _now())
            for d in derivations
        ],
    )
    conn.commit()
    family_of.cache_clear()
    transparent_root.cache_clear()
    return len(derivations)


def set_grade(member: str, root: str, grade: str, *, breakdown_zh: str | None = None,
              source: str = "llm") -> None:
    conn = get_connection("content")
    conn.execute(
        "UPDATE word_families SET grade = ?, breakdown_zh = COALESCE(?, breakdown_zh),"
        " source = ? WHERE member = ? AND root = ?",
        (grade, breakdown_zh, source, member, root),
    )
    conn.commit()
    family_of.cache_clear()
    transparent_root.cache_clear()


@lru_cache(maxsize=20000)
def family_of(member: str) -> dict[str, Any] | None:
    """The breakdown for one word, if we have one."""
    try:
        row = get_connection("content").execute(
            "SELECT member, root, affix, affix_kind, grade, breakdown_zh, source"
            " FROM word_families WHERE member = ? ORDER BY id LIMIT 1",
            (member.lower(),),
        ).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


@lru_cache(maxsize=20000)
def transparent_root(member: str) -> str | None:
    """The root of a grade-A derivation, or ``None``.

    Grade A is the one the vocabulary checker cares about: knowing the root
    means the derived form is not a new word, so it must not be counted as one.
    """
    family = family_of(member.lower())
    if family and family["grade"] == "A":
        return str(family["root"])
    return None


def members_of(root: str, limit: int = 50) -> list[dict[str, Any]]:
    try:
        rows = get_connection("content").execute(
            "SELECT member, affix, grade FROM word_families WHERE root = ?"
            " ORDER BY member LIMIT ?",
            (root.lower(), limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def ungraded_for_review(limit: int = 5000) -> list[dict[str, Any]]:
    """Rule-derived families a model has not looked at yet.

    Only prefixes and the B grade are worth reviewing: A-grade suffixes are
    grammar and need no judgement, and the rules never emit C.
    """
    rows = get_connection("content").execute(
        "SELECT member, root, affix, affix_kind, grade FROM word_families"
        " WHERE source = 'rule' AND (affix_kind = 'prefix' OR grade = 'B')"
        " ORDER BY member LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def family_stats() -> dict[str, Any]:
    try:
        conn = get_connection("content")
        total = conn.execute("SELECT COUNT(*) AS n FROM word_families").fetchone()["n"]
        by_grade = {
            row["grade"]: row["n"]
            for row in conn.execute(
                "SELECT grade, COUNT(*) AS n FROM word_families GROUP BY grade"
            )
        }
        by_source = {
            row["source"]: row["n"]
            for row in conn.execute(
                "SELECT source, COUNT(*) AS n FROM word_families GROUP BY source"
            )
        }
    except sqlite3.Error:
        return {"total": 0, "by_grade": {}, "by_source": {}}
    return {"total": int(total), "by_grade": by_grade, "by_source": by_source}


def clear_caches() -> None:
    family_of.cache_clear()
    transparent_root.cache_clear()
    affix_gloss.cache_clear()
