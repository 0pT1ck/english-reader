"""The shared sense-number sequence — 决定 ⑭.

发号器 id allocator / 号段 number space / 高水位 high-water mark

**What this defends against.** ``review/session.py:sense_of`` takes one
argument — a bare ``sense_id`` — and so do the sentence pool and the tapped-word
panel. If phrase senses had their own AUTOINCREMENT, phrase sense 42 and word
sense 42 would be two meanings sharing one number, and the reader would be shown
the wrong Chinese **with nothing raising anywhere**. One number space makes the
same mistake produce an empty panel instead: visible, and therefore fixable.

**Both directions have to be closed.** Handing phrases numbers above the current
maximum is only half of it — ``senses`` is AUTOINCREMENT, so its counter would
happily walk straight into the range just handed out. Every allocation therefore
pushes ``sqlite_sequence`` for ``senses`` past the last number issued. That is
the line without which this file is decoration.

Retired senses count too (``senses_retired``): a mark made two months ago may
still point at one, so its number is taken even though the row left ``senses``.
"""

from __future__ import annotations

import sqlite3

from backend.core.db import get_connection
from backend.modules.phrases.schema import ALLOCATOR


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    value = row[0] if row else None
    return int(value or 0)


def high_water(conn: sqlite3.Connection) -> int:
    """The largest sense number anything has ever used."""
    return max(
        _scalar(conn, "SELECT seq FROM sqlite_sequence WHERE name = 'senses'"),
        _scalar(conn, "SELECT MAX(id) FROM senses"),
        _scalar(conn, "SELECT MAX(id) FROM senses_retired"),
        _scalar(conn, "SELECT MAX(id) FROM phrase_senses"),
        _scalar(conn, f"SELECT next - 1 FROM id_allocator WHERE name = '{ALLOCATOR}'"),
    )


def allocate(count: int, conn: sqlite3.Connection | None = None) -> list[int]:
    """Reserve ``count`` sense numbers that no word sense can ever be given.

    Returns them in order. Raises rather than returning a short list: a caller
    that got fewer numbers than rows would file the remainder under 0, which is
    the value meaning "this item has no sense set at all".
    """
    if count <= 0:
        return []
    conn = conn or get_connection("content")
    # **Whose transaction is this?** (踩过的坑 §6.2 asks the same question about
    # the other direction.) The importer calls this in the middle of writing a
    # phrase, so a connection already inside a transaction must not have a
    # second one started on it — and must not be committed out from under the
    # caller either. Only the outermost owner commits.
    owns = not conn.in_transaction
    if owns:
        conn.execute("BEGIN IMMEDIATE")
    try:
        start = high_water(conn) + 1
        last = start + count - 1
        conn.execute(
            "INSERT INTO id_allocator (name, next) VALUES (?, ?)"
            " ON CONFLICT(name) DO UPDATE SET next = excluded.next",
            (ALLOCATOR, last + 1),
        )
        # **The half that is easy to forget.** Without this the AUTOINCREMENT
        # counter for `senses` still sits below `last`, and the next imported
        # word sense is handed a number a phrase sense already holds.
        if conn.execute(
            "SELECT COUNT(*) FROM sqlite_sequence WHERE name = 'senses'"
        ).fetchone()[0]:
            conn.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = 'senses' AND seq < ?",
                (last, last),
            )
        else:
            conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('senses', ?)", (last,)
            )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    return list(range(start, last + 1))
