"""Tables for the phrase inventory — P11.

词组 phrase / 义项子表 sense sub-table / 用法 usage / 发号器 id allocator
/ 溯源 provenance / 归档 archive

**Why a list at all, when P2 deliberately had none.** P2's phrase module opens
with "No inventory of phrases worth learning" — marking was supposed to settle
it, exactly as with words. That held for *which phrases are worth learning*; it
did not hold for *which runs of words are a phrase at all*. The structural
filter plus a model verdict per occurrence recognised about a third of them and
could gloss none, and the verdicts were not even stable: the same article asked
three times gave 37/28/34 phrases, union 50, intersection 19. A dictionary
answers the same question once, for free, and the same way every time — which
is what P10 did to the sense inventory for the same reasons.

**Three tables, and the split is not decorative:**

* ``phrases`` is identity — one row per phrase, holding the number it was
  issued and where it came from. **Marks do not point at it**: a mark keys on
  the phrase's text, exactly as a word's mark keys on its headword.
* ``phrase_senses`` is what a phrase *means*, one row per Collins block. This
  is what marks and review actually hang on, so **these ids are the ones that
  must never move** (架构铁律 5, and 跨 Phase 不变量).
* ``sense_collocations`` is usage — a *word* sense's collocation pattern
  (``contribute`` 的「促成」那条带着 ``contributes to``). It hangs off
  ``senses``, not off a word, because ``contribute``'s four senses each bold
  ``contribute to`` and mean different things by it. P11 stores and serves it;
  **review does not read it** (决定 ⑰).

**The id allocator is the whole of 决定 ⑭.** ``review/session.py`` resolves a
sense by bare id::

    def sense_of(sense_id: int) -> dict | None:   # one argument

Every reader of a review card, a sentence pool and a tapped word goes through
it. A second AUTOINCREMENT table would make phrase sense 42 and word sense 42
two different meanings wearing one number, and the failure is silent: the panel
shows the wrong Chinese and nothing raises. Sharing one number space turns that
into an empty panel instead — **a visible failure rather than a lying one**.
"""

from __future__ import annotations

import sqlite3

from backend.core.db import Migration

#: Where the shared sense-number sequence is kept. Words draw from SQLite's own
#: AUTOINCREMENT counter for ``senses``; phrases draw from here, and every
#: allocation pushes the ``senses`` counter past what it just handed out, so the
#: two can never meet. See :mod:`backend.modules.phrases.ids`.
ALLOCATOR = "sense_id"


def _archive_legacy_verdicts(conn: sqlite3.Connection) -> None:
    """Keep the 3,458 per-occurrence verdicts the model produced in P2.

    They answer a question this phase stops asking ("is this run of words a
    phrase here"), and ``reading_phrases`` is about to be rebuilt from the list,
    so they would otherwise be dropped. **They are the only record of how a
    model reads these phrases** — 567 sequences it always called a phrase, 224
    never, 224 both ways — and P11 §16 M3 uses them as the control group for
    "did the annotator pick the right phrase sense".

    Idempotent by existence check rather than by ``INSERT OR IGNORE``: the
    source table is rebuilt in this phase, so a second run must not copy the
    *new* rows in on top of the archived ones.
    """
    exists = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        " AND name = 'phrase_verdicts_legacy'"
    ).fetchone()[0]
    if exists:
        return
    conn.execute(
        """
        CREATE TABLE phrase_verdicts_legacy AS
        SELECT id, article_id, sentence_id, phrase, start_seq, end_seq, surface,
               verdict, judged_at, created_at
        FROM reading_phrases
        """
    )
    conn.execute(
        "CREATE INDEX idx_verdicts_legacy_phrase"
        " ON phrase_verdicts_legacy (phrase)"
    )


MIGRATIONS = [
    Migration(
        version=1,
        name="phrase inventory",
        database="content",
        apply="""
        -- One row per phrase on the list. **Identity, not meaning.**
        --
        -- **Not called `phrases`**, although that is what it is: ECDICT's own
        -- phrase table in `dictionary.db` already has that name, and every
        -- database is attached to every connection (`core/db.py`), so an
        -- unqualified `phrases` would resolve to whichever came first. That is
        -- the quietest kind of bug this project knows — the query runs, the
        -- rows come back, and they are from the wrong table.
        --
        -- `id` is issued at import and is part of the contract, but it is not
        -- what a mark points at: a mark keys on `text`, the way a word's mark
        -- keys on its headword (study_marks.item_key). That is why rebuilding
        -- occurrences is safe and rebuilding this table is not.
        CREATE TABLE IF NOT EXISTS phrase_list (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            text        TEXT    NOT NULL UNIQUE,

            -- Provenance, not a hash of the content (P10 §5). A re-import
            -- recognises a phrase by where it came from, so nothing churns.
            source      TEXT    NOT NULL,            -- collins
            source_key  TEXT    NOT NULL,            -- entry headword it was found under
            -- Which of §13's four tiers matched it: A own entry, B bold string
            -- equal, C equal after placeholders, D the block's bolds joined.
            tier        TEXT    NOT NULL,

            -- The syllabus list it appears on, free from the phrase list:
            -- junior / senior / cet4 / cet6. Informational — 不在大纲里 never
            -- means 不许学 (跨 Phase 不变量).
            level       TEXT,
            created_at  TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_phrase_list_text ON phrase_list (text);

        -- One row per Collins block. **These ids are the permanent ones.**
        --
        -- `id` is NOT autoincrement: it comes from the shared allocator so that
        -- it can never collide with a word sense id — see the module docstring.
        CREATE TABLE IF NOT EXISTS phrase_senses (
            id             INTEGER PRIMARY KEY,
            phrase_id      INTEGER NOT NULL REFERENCES phrase_list(id) ON DELETE CASCADE,
            ordinal        INTEGER NOT NULL,
            gloss_zh       TEXT    NOT NULL,
            concept_en     TEXT,
            pos            TEXT,
            pos_zh         TEXT,
            register       TEXT,
            pattern        TEXT,

            -- The provenance triple, same shape as `senses` (P10 §5): which
            -- dictionary, which entry, which block in document order. A
            -- re-import matches on this and keeps the id.
            source_dict    TEXT    NOT NULL,
            source_head    TEXT    NOT NULL,
            source_block   INTEGER NOT NULL,
            source_ordinal INTEGER,

            -- Filled by the exam-frequency recount (决定 ⑳), from the phrase
            -- occurrences rather than from tokens.
            exam_frequency INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT    NOT NULL,
            UNIQUE (phrase_id, ordinal),
            -- One block can serve two phrases (`take into account` and
            -- `take account of` share one), so the phrase is part of the key.
            UNIQUE (phrase_id, source_dict, source_head, source_block)
        );
        CREATE INDEX IF NOT EXISTS idx_phrase_senses_phrase
            ON phrase_senses (phrase_id);

        -- 用法: a *word* sense's collocation pattern, from the bold runs of a
        -- WORD block. Stored as Collins prints it — **no lemmatising** (决定
        -- ⑨): only 8.1% is inflectional noise, while `is absorbed into` and
        -- `the allies` carry voice and nominalisation, which a blanket
        -- un-inflection would destroy.
        CREATE TABLE IF NOT EXISTS sense_collocations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sense_id    INTEGER NOT NULL REFERENCES senses(id) ON DELETE CASCADE,
            ordinal     INTEGER NOT NULL,
            text        TEXT    NOT NULL,
            created_at  TEXT    NOT NULL,
            UNIQUE (sense_id, text)
        );
        CREATE INDEX IF NOT EXISTS idx_collocations_sense
            ON sense_collocations (sense_id);

        -- Tier C and D matches, which are ~30% wrong and so are asked about one
        -- at a time (决定 ㉑). Rows carry their own verdict, which is what makes
        -- the run resumable (决定 ㉒): a restart skips what is already judged.
        CREATE TABLE IF NOT EXISTS phrase_candidates (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            text         TEXT    NOT NULL,
            tier         TEXT    NOT NULL,
            source_head  TEXT    NOT NULL,
            source_block INTEGER NOT NULL,
            bold         TEXT    NOT NULL,
            gloss_zh     TEXT    NOT NULL,
            level        TEXT,
            -- NULL not asked yet / 1 the block is about this phrase / 0 it is not
            verdict      INTEGER,
            judged_at    TEXT,
            -- 0 a real candidate / 1 a control whose answer is known to be
            -- yes / 2 a control whose answer is known to be no. **Both kinds
            -- are needed**: positives alone cannot tell "the judge is right"
            -- from "the judge says yes to everything", and negatives alone
            -- cannot tell it from "says no to everything" (踩过的坑 §4.3).
            -- Controls are never promoted onto the list.
            control      INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT    NOT NULL,
            UNIQUE (text, source_head, source_block)
        );
        CREATE INDEX IF NOT EXISTS idx_phrase_candidates_pending
            ON phrase_candidates (verdict);

        -- What each side of the intersection收 alone (决定 ①). Kept rather than
        -- deleted so that "why is `manage to do sth` not on the list" has an
        -- answer. **Senses are not archived** — the mdx does not change, so
        -- re-parsing is one command, while a copy is one more thing to drift.
        CREATE TABLE IF NOT EXISTS phrase_excluded (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            text       TEXT    NOT NULL,
            source     TEXT    NOT NULL,   -- syllabus_only | collins_only
            reason     TEXT    NOT NULL,
            level      TEXT,
            created_at TEXT    NOT NULL,
            UNIQUE (text, source)
        );

        -- The shared sense-number sequence. One row, one name, so that adding a
        -- second shared counter later needs no schema change.
        CREATE TABLE IF NOT EXISTS id_allocator (
            name TEXT PRIMARY KEY,
            next INTEGER NOT NULL
        );
        """,
    ),
    Migration(
        version=2,
        name="archive the P2 phrase verdicts",
        database="content",
        apply=_archive_legacy_verdicts,
    ),
    Migration(
        version=3,
        name="rename phrases to phrase_list, away from ECDICT's table",
        database="content",
        apply="""
        -- 撞名：`dictionary.db` 里 ECDICT 自己就有一张 `phrases`（31.8 万行），
        -- 而所有库都挂在同一个连接上，不带库名的 `phrases` 解析到哪一张
        -- 取决于连接是从哪个库开的。**查询照样跑、照样返回行，只是来自另一张表。**
        ALTER TABLE phrases RENAME TO phrase_list;
        """,
    ),
]
