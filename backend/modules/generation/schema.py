"""Storage for P1a generation experiments.

These are *drafts*, not articles. The real article tables — with sentences,
tokens and per-occurrence records — belong to P2, when articles start being
read. Building them now would be building P2 during P1.

What this table exists for is provenance. A blind test tells us how many pieces
were spotted; without recording which model wrote each one, under which
constraint scheme, that result cannot be turned into a decision. Picking the
best model is the entire point of the first batch, so the columns that make
attribution possible are as important as the text itself.
"""

from __future__ import annotations

from backend.core.db import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="generation drafts",
        database="learning",
        apply="""
        CREATE TABLE IF NOT EXISTS generation_drafts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,

            -- content as returned by the model
            title          TEXT,
            body           TEXT    NOT NULL,

            -- provenance: which model, which vocabulary-constraint scheme,
            -- which prompt revision, and which target-word set (A or B).
            -- Round 1 varies the model with the scheme held fixed; round 2
            -- varies the scheme. Recording all four keeps both readable.
            model          TEXT    NOT NULL,
            scheme         TEXT    NOT NULL,
            prompt_version TEXT    NOT NULL,
            word_set       TEXT,

            -- the exact target words asked for, and the exact prompt used,
            -- so any result can be reproduced later
            target_words   TEXT    NOT NULL,
            prompt         TEXT,

            -- checker output, stored as JSON
            report         TEXT,

            created_at     TEXT    NOT NULL,
            note           TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_drafts_model  ON generation_drafts (model);
        CREATE INDEX IF NOT EXISTS idx_drafts_scheme ON generation_drafts (scheme);
        CREATE INDEX IF NOT EXISTS idx_drafts_set    ON generation_drafts (word_set);
        """,
    ),
]
