"""Affix and word-family tables, in ``content.db``.

Not in ``dictionary.db``: the affix glosses are translated by a model and the
irregular derivations are found by one, so this is generated content that costs
money to rebuild — the whole reason ``content.db`` exists. Not in
``learning.db`` either: it is the same for every learner and is not a record of
anything the user did.

Keys are headword strings rather than the numeric ids used inside
``dictionary.db``. Two databases cannot share a foreign key, and a rebuilt
dictionary reassigns its ids — a family keyed by ``nation`` survives that,
whereas one keyed by ``word_id = 41297`` silently points at another word.
"""

from __future__ import annotations

from backend.core.db import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="affixes and word families",
        database="content",
        apply="""
        -- Roots, prefixes and suffixes from ECDICT's wordroot list, plus the
        -- Chinese gloss we generate for each affix.
        CREATE TABLE IF NOT EXISTS affixes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            affix       TEXT    NOT NULL,
            kind        TEXT    NOT NULL,   -- root | prefix | suffix
            meaning_en  TEXT,
            meaning_zh  TEXT,
            -- Part of speech the suffix produces, when it is that kind of
            -- suffix ('noun', 'adjective', 'adverb', 'verb').
            forms_pos   TEXT,
            examples    TEXT,               -- JSON list, from the source data
            created_at  TEXT,
            UNIQUE (affix, kind)
        );

        -- One derivation: member = root + affix.
        --
        -- grade decides how the reader meets it, and is the point of the whole
        -- table:
        --   A  transparent, purely grammatical (carefully <- careful).
        --      Not a new word at all; costs no target-word slot.
        --   B  inferable but specialised (nationality <- nation).
        --      Shown as a breakdown, counts as a third of a slot.
        --   C  meaning has drifted (depart <- part).
        --      An ordinary new word; showing the breakdown would mislead.
        CREATE TABLE IF NOT EXISTS word_families (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            member      TEXT    NOT NULL,
            root        TEXT    NOT NULL,
            affix       TEXT,
            affix_kind  TEXT,               -- prefix | suffix
            grade       TEXT    NOT NULL DEFAULT 'B',
            breakdown_zh TEXT,
            -- rule | wordroot | llm — which pass produced this, so a bad pass
            -- can be re-run without touching what the others found.
            source      TEXT    NOT NULL DEFAULT 'rule',
            confidence  REAL    NOT NULL DEFAULT 1.0,
            created_at  TEXT,
            UNIQUE (member, root, affix)
        );
        CREATE INDEX IF NOT EXISTS idx_family_member ON word_families (member);
        CREATE INDEX IF NOT EXISTS idx_family_root   ON word_families (root);
        CREATE INDEX IF NOT EXISTS idx_family_grade  ON word_families (grade);
        """,
    ),
]
