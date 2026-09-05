"""Dictionary reads.

Thin on purpose: the dictionary is immutable reference data, so there is nothing
here but lookups and a small cache. Anything that writes to ``dictionary.db``
belongs in the import script, not in the running service.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from typing import Any

from backend.core.db import get_connection

# Syllabus tags as they appear in the imported data, ordered from easiest to
# hardest. Used to describe a word's level in the admin console.
SYLLABUS_LABELS = {
    "zk": "中考",
    "gk": "高考",
    "cet4": "CET-4",
    "cet6": "CET-6",
    "ky": "考研",
    "toefl": "托福",
    "ielts": "雅思",
    "gre": "GRE",
}

# The two syllabi this project actually targets.
TARGET_TAGS = ("cet4", "cet6")


@lru_cache(maxsize=20000)
def lookup(headword: str) -> dict[str, Any] | None:
    """Look up one headword.

    Cached because ingesting an article hits the same common words hundreds of
    times, and the dictionary never changes while the process runs.
    """
    try:
        row = get_connection("dictionary").execute(
            "SELECT headword, phonetic, definition, translation, pos_hint,"
            " collins, oxford, tags, bnc, frq, exchange"
            " FROM words WHERE headword = ?",
            (headword,),
        ).fetchone()
    except sqlite3.Error:
        # Dictionary not imported yet — a normal state before the import script
        # has been run, not an error worth raising.
        return None
    return dict(row) if row else None


@lru_cache(maxsize=20000)
def resolve_surface(surface: str) -> str | None:
    """Map an inflected form to its headword using the imported inflection table.

    A fallback for forms the NLP lemmatiser resolves to something the dictionary
    does not contain. Cannot disambiguate by itself — if a surface maps to
    several headwords this returns the most frequent one, which is why
    part-of-speech tagging remains the primary mechanism.
    """
    try:
        rows = get_connection("dictionary").execute(
            "SELECT f.headword, w.frq FROM word_forms f"
            " LEFT JOIN words w ON w.headword = f.headword"
            " WHERE f.surface = ?"
            " ORDER BY CASE WHEN w.frq IS NULL OR w.frq = 0 THEN 999999 ELSE w.frq END"
            " LIMIT 1",
            (surface,),
        ).fetchall()
    except sqlite3.Error:
        return None
    return rows[0]["headword"] if rows else None


def is_imported() -> bool:
    """Whether the dictionary has any content at all."""
    return entry_count() > 0


def entry_count() -> int:
    try:
        row = get_connection("dictionary").execute(
            "SELECT COUNT(*) AS n FROM words"
        ).fetchone()
        return int(row["n"])
    except sqlite3.Error:
        return 0


def stats() -> dict[str, Any]:
    """Coverage summary for the admin console.

    Answers the question P0 has to answer: is the dictionary actually loaded,
    and does it contain the syllabus data the later phases depend on.
    """
    conn = get_connection("dictionary")
    try:
        total = entry_count()
        forms = conn.execute("SELECT COUNT(*) AS n FROM word_forms").fetchone()["n"]
        senses = conn.execute("SELECT COUNT(*) AS n FROM senses").fetchone()["n"]
        families = conn.execute("SELECT COUNT(*) AS n FROM word_families").fetchone()["n"]

        by_tag = {}
        for tag in SYLLABUS_LABELS:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM words WHERE tags LIKE ?", (f"%{tag}%",)
            ).fetchone()
            by_tag[tag] = int(row["n"])

        with_freq = conn.execute(
            "SELECT COUNT(*) AS n FROM words WHERE frq > 0 OR bnc > 0"
        ).fetchone()["n"]
    except sqlite3.Error:
        return {
            "imported": False,
            "words": 0,
            "forms": 0,
            "senses": 0,
            "families": 0,
            "by_tag": {},
            "with_frequency": 0,
        }

    return {
        "imported": total > 0,
        "words": total,
        "forms": int(forms),
        "senses": int(senses),
        "families": int(families),
        "by_tag": by_tag,
        "with_frequency": int(with_freq),
    }


def clear_caches() -> None:
    """Drop lookup caches. Called after a dictionary import."""
    lookup.cache_clear()
    resolve_surface.cache_clear()
