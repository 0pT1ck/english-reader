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
        database="content",
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
    Migration(
        version=4,
        name="topic and one-line summary on drafts, carried into the article",
        database="content",
        apply="""
        -- 话题以前是写进 ``word_set`` 的——那一列说的是「A 组还是 B 组目标词」，
        -- 两件事共用一列，谁都说不清读出来的是哪一个。给它一列自己的地方。
        --
        -- ``summary_zh`` 是新的：手机端列表卡片上标题下面那一行。
        -- 写的时候顺手生成，入库时原样搬进 ``reading_articles``——
        -- 入库时再算一遍等于同一篇文章在两个地方说两句不同的话。
        ALTER TABLE generation_drafts ADD COLUMN topic      TEXT;
        ALTER TABLE generation_drafts ADD COLUMN summary_zh TEXT;

        -- 已有的行：话题从它当初被塞进去的地方搬过来，概括补不了，留空。
        UPDATE generation_drafts SET topic = word_set
            WHERE topic IS NULL AND word_set IS NOT NULL AND word_set <> '';
        """,
    ),
]
