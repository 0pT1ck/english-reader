"""Dictionary schema.

All of this lives in ``dictionary.db``: reference data, rebuildable from the
import script, deliberately kept out of the file that gets backed up.

Two tables here are **created empty on purpose**. ``senses`` and
``word_families`` need an LLM to populate — sense sets have to be generated at
the right granularity, and morphological breakdowns have to be written. That is
P1 work. Creating the structure now is "leaving room"; filling it now would be
building P1 during P0.

Term mapping for the design documents:
    headword    词条        the lemma a surface form resolves to
    sense       义项        one *English concept*, not one Chinese translation
    word family 词族        root plus its derivations
"""

from __future__ import annotations

from backend.core.db import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="dictionary entries",
        database="dictionary",
        apply="""
        -- One row per headword, imported from ECDICT.
        CREATE TABLE IF NOT EXISTS words (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            headword    TEXT    NOT NULL UNIQUE,
            phonetic    TEXT,
            -- ECDICT's English definition. Not the same thing as a sense's
            -- concept definition: this is dictionary prose, senses are ours.
            definition  TEXT,
            -- Chinese, comma-piled. Layer 2 of the three-layer gloss.
            translation TEXT,
            pos_hint    TEXT,
            collins     INTEGER,
            oxford      INTEGER,
            -- Syllabus tags: zk / gk / cet4 / cet6 / ky / toefl / ielts / gre
            tags        TEXT,
            -- Frequency ranks. Lower is more common; these are the difficulty
            -- prior the ability model will build on in P3.
            bnc         INTEGER,
            frq         INTEGER,
            -- ECDICT inflection field, kept verbatim for cross-checking lemmas.
            exchange    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_words_headword ON words (headword);
        CREATE INDEX IF NOT EXISTS idx_words_bnc      ON words (bnc);
        CREATE INDEX IF NOT EXISTS idx_words_frq      ON words (frq);
        CREATE INDEX IF NOT EXISTS idx_words_tags     ON words (tags);

        -- Surface form -> headword, expanded from the exchange field at import.
        -- Used to cross-check the NLP lemmatiser and to catch forms it misses;
        -- it cannot disambiguate on its own (left -> leave or left?), which is
        -- exactly why part-of-speech tagging carries the main load.
        CREATE TABLE IF NOT EXISTS word_forms (
            surface   TEXT NOT NULL,
            headword  TEXT NOT NULL,
            kind      TEXT,
            PRIMARY KEY (surface, headword)
        );
        CREATE INDEX IF NOT EXISTS idx_word_forms_surface ON word_forms (surface);
        """,
    ),
    Migration(
        version=2,
        name="sense and word family structure (empty until P1)",
        database="dictionary",
        apply="""
        -- A sense is one English concept. Chinese glosses are its projections,
        -- which is why concept_en is the identity column and gloss_zh is not.
        -- Granularity: medium, split on concept boundaries, never on
        -- differences that exist only in Chinese translation.
        CREATE TABLE IF NOT EXISTS senses (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id        INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
            ordinal        INTEGER NOT NULL,
            concept_en     TEXT    NOT NULL,
            gloss_zh       TEXT    NOT NULL,
            -- Written with words the learner already knows, so reading a
            -- definition never introduces new unknowns. Regenerated as their
            -- level rises.
            simple_def_en  TEXT,
            -- Populated from exam corpus statistics in P1: how often this sense
            -- actually appears in past papers, which outranks any word list.
            exam_frequency INTEGER NOT NULL DEFAULT 0,
            is_exam_key    INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT,
            UNIQUE (word_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS idx_senses_word ON senses (word_id);

        -- Root -> derivation, with the breakdown shown when a derived word is
        -- met and its root is already known (nationality <- nation + -ity).
        CREATE TABLE IF NOT EXISTS word_families (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            root_word_id   INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
            member_word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
            affix          TEXT,
            breakdown_zh   TEXT,
            UNIQUE (root_word_id, member_word_id)
        );
        CREATE INDEX IF NOT EXISTS idx_family_member ON word_families (member_word_id);
        """,
    ),
]
