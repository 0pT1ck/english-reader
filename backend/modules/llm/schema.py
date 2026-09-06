"""Tables for providers and batch jobs.

All three live in ``learning.db`` rather than ``content.db``. They are not
generated content — they are the record of *what was run and what it cost*,
which is operational history worth keeping across a content rebuild. It is also
tiny: a few thousand rows after a year.

``llm_job_items`` is the table that makes resume possible. A job is not a script
that runs to completion; it is a list of units whose individual state is stored,
so a job interrupted at item 1,847 restarts at 1,848 rather than at zero — which
matters when starting over costs real money.
"""

from __future__ import annotations

from backend.core.db import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="llm providers, jobs and job items",
        database="learning",
        apply="""
        CREATE TABLE IF NOT EXISTS llm_providers (
            id          TEXT    PRIMARY KEY,   -- slug, also the secrets.json key
            label       TEXT    NOT NULL,
            kind        TEXT    NOT NULL,      -- 'openai' | 'anthropic'
            base_url    TEXT    NOT NULL,
            model       TEXT    NOT NULL,

            -- Price per million tokens, in whatever currency the user thinks
            -- in. Stored per provider because it changes independently of the
            -- code and differs by an order of magnitude between vendors.
            price_in    REAL    NOT NULL DEFAULT 0,
            price_out   REAL    NOT NULL DEFAULT 0,
            currency    TEXT    NOT NULL DEFAULT 'CNY',

            enabled     INTEGER NOT NULL DEFAULT 1,
            is_default  INTEGER NOT NULL DEFAULT 0,
            note        TEXT,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS llm_jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT    NOT NULL,      -- which worker handles the items
            title       TEXT    NOT NULL,
            provider_id TEXT    NOT NULL,

            -- pending: planned, not started      running: a thread is on it
            -- paused:  stopped, resumable        capped:  hit the spend limit
            -- done:    every item finished       failed:  the job itself broke
            status      TEXT    NOT NULL DEFAULT 'pending',

            params      TEXT,                   -- JSON, passed to the worker
            total       INTEGER NOT NULL DEFAULT 0,
            done        INTEGER NOT NULL DEFAULT 0,
            failed      INTEGER NOT NULL DEFAULT 0,

            tokens_in   INTEGER NOT NULL DEFAULT 0,
            tokens_out  INTEGER NOT NULL DEFAULT 0,
            cost        REAL    NOT NULL DEFAULT 0,

            -- Stop before spending more than this. A job that reaches the cap
            -- pauses rather than failing, so it can be raised and resumed.
            spend_cap   REAL,

            error       TEXT,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS llm_job_items (
            job_id      INTEGER NOT NULL REFERENCES llm_jobs(id) ON DELETE CASCADE,
            seq         INTEGER NOT NULL,
            key         TEXT    NOT NULL,      -- readable id, e.g. the word batch
            status      TEXT    NOT NULL DEFAULT 'pending',  -- pending|done|failed
            payload     TEXT,                   -- JSON input for the worker
            result      TEXT,                   -- raw model reply, kept for audit
            tokens_in   INTEGER NOT NULL DEFAULT 0,
            tokens_out  INTEGER NOT NULL DEFAULT 0,
            cost        REAL    NOT NULL DEFAULT 0,
            attempts    INTEGER NOT NULL DEFAULT 0,
            error       TEXT,
            updated_at  TEXT,
            PRIMARY KEY (job_id, seq)
        );

        CREATE INDEX IF NOT EXISTS idx_job_items_status
            ON llm_job_items (job_id, status);
        CREATE INDEX IF NOT EXISTS idx_jobs_status
            ON llm_jobs (status);
        """,
    ),
]
