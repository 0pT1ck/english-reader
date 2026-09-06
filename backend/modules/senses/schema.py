"""Sense tables, in ``content.db``.

Every row here was produced by a model and cost money, which is the whole reason
``content.db`` exists as a separate, backed-up database. Nothing in here can be
recovered by re-running the dictionary import.

Keyed by headword rather than by the dictionary's numeric ids: two SQLite files
cannot share a foreign key, and a rebuilt dictionary reassigns its ids — a sense
attached to ``address`` survives that, one attached to ``word_id = 3971`` starts
describing a different word.
"""

from __future__ import annotations

from backend.core.db import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="sense sets, examples and screening",
        database="content",
        apply="""
        -- One sense = one English concept. The Chinese glosses are its
        -- projections into different contexts, which is why concept_en is the
        -- identity column and gloss_zh is a list.
        --
        -- Granularity rule, in one line: merge when knowing one meaning makes
        -- the other understandable on sight; split when it would be
        -- misunderstood. `present` (提出/介绍/呈现) is one sense; `address`
        -- (地址 / 着手解决) is two.
        CREATE TABLE IF NOT EXISTS senses (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            headword       TEXT    NOT NULL,
            ordinal        INTEGER NOT NULL,   -- 1 is the main sense
            concept_en     TEXT    NOT NULL,
            gloss_zh       TEXT    NOT NULL,   -- JSON list, 2-3 entries
            -- Informational only. Part of speech never decides a split: the
            -- noun and the verb `address` (地址 / 写地址) are one concept.
            pos            TEXT,
            -- Rides along with sense generation at almost no extra cost, and
            -- pays for itself twice: topic-grouped target words, and topic
            -- control over generated articles.
            topic          TEXT,
            -- Filled in later from exam-corpus statistics (a phase after this
            -- one). Declared now so the column does not have to be added to a
            -- table with thousands of rows.
            exam_frequency INTEGER NOT NULL DEFAULT 0,
            is_exam_key    INTEGER NOT NULL DEFAULT 0,
            model          TEXT,
            created_at     TEXT,
            UNIQUE (headword, ordinal)
        );
        CREATE INDEX IF NOT EXISTS idx_senses_headword ON senses (headword);
        CREATE INDEX IF NOT EXISTS idx_senses_topic    ON senses (topic);

        -- Example sentences, generated lazily rather than up front: most of
        -- these words will never reach a review card, and paying for 2,766
        -- words' worth of examples in advance would be paying for work that is
        -- mostly never read.
        CREATE TABLE IF NOT EXISTS sense_examples (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sense_id    INTEGER NOT NULL REFERENCES senses(id) ON DELETE CASCADE,
            text_en     TEXT    NOT NULL,
            gloss_zh    TEXT,
            source      TEXT,
            created_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_examples_sense ON sense_examples (sense_id);

        -- The coarse filter's verdict for every target word, kept so the
        -- decision is auditable and a threshold change can be re-run against
        -- what it used to say.
        CREATE TABLE IF NOT EXISTS sense_screening (
            headword     TEXT PRIMARY KEY,
            category     TEXT NOT NULL,   -- multi_pos | dual_pos | many_glosses | simple
            pos_count    INTEGER NOT NULL DEFAULT 0,
            gloss_items  INTEGER NOT NULL DEFAULT 0,
            needs_senses INTEGER NOT NULL DEFAULT 0,
            screened_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_screening_needs
            ON sense_screening (needs_senses, category);
        """,
    ),
    Migration(
        version=2,
        name="wiktionary sense inventory",
        database="content",
        # The external checklist P1c is built on. Imported from the kaikki.org
        # parse of English Wiktionary and kept as-is: this table is *evidence*,
        # not product. Nothing here is shown to the reader — Wiktionary's own
        # wording is largely public-domain 1913 Webster's text ("to superscribe,
        # or to direct and transmit"), which is harder than the words it defines.
        #
        # What it is for is the question P1b could not answer: does our sense
        # set miss anything? A model generating from nothing has no checklist to
        # be measured against; this is that checklist.
        apply="""
        CREATE TABLE IF NOT EXISTS wiktionary_senses (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            headword   TEXT    NOT NULL,
            pos        TEXT,
            -- Wiktionary nests senses; the full path is kept joined so that a
            -- child sense stays readable ("Direction. > A description of the
            -- location of a property").
            gloss      TEXT    NOT NULL,
            -- Space-separated: obsolete, archaic, dialectal, transitive…
            -- The first few are what the free mechanical filter keys on.
            tags       TEXT,
            -- 1 when a tag marks it as no longer current usage. Precomputed
            -- because every query wants it and the rule should live in one place.
            is_dead    INTEGER NOT NULL DEFAULT 0,
            topics     TEXT,
            ordinal    INTEGER NOT NULL DEFAULT 0,
            imported_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_wikt_headword ON wiktionary_senses (headword);
        CREATE INDEX IF NOT EXISTS idx_wikt_live     ON wiktionary_senses (headword, is_dead);
        """,
    ),
    Migration(
        version=3,
        name="archive the P1b sense set and make room for the rebuild",
        database="content",
        # The P1b set was generated with nothing to check it against. It is kept
        # rather than dropped so the two can be compared: "model working alone"
        # versus "model working from a checklist" is a number worth having, and
        # it costs a few megabytes.
        apply="""
        CREATE TABLE IF NOT EXISTS senses_p1b AS SELECT * FROM senses;
        DELETE FROM senses;

        -- Which Wiktionary senses this one covers, as a JSON list of ids.
        -- Traceability is the point: it makes "did we drop a sense on the
        -- floor?" a query rather than a judgement, and the senses nothing
        -- covers are exactly the missing-sense report.
        ALTER TABLE senses ADD COLUMN covers TEXT;
        -- Why the model excluded the senses it left out, in its own words.
        ALTER TABLE senses ADD COLUMN source TEXT;
        """,
    ),
]
