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

import sqlite3
from typing import Any

from backend.core.db import Migration
from backend.core.logging import get_logger

log = get_logger("senses.schema")


def _stable_sense_keys(conn: sqlite3.Connection) -> None:
    """给义项一个稳定键，把「删了重插」换成「按键更新 ＋ 退休」。

    **P9 §8。** 在这之前义项 id 是自增代理键而 ``store_senses`` 删了重插，
    于是任何一次重建都会让 id 全变——而九万三千条语境标注指着那些 id。
    键的理由与边界见 :mod:`backend.modules.senses.keys`。

    **退休的行搬到另一张表，不是在原表里打标记。**
    有二十处代码在读 ``senses``，要它们全都记得加一句「排除退休的」是不现实的，
    而漏一处的后果是一个已经退休的义项出现在界面上——**静默的**。
    搬出去之后，那二十处自然只看得见活的，一处都不用改；
    只有按 id 反查的那两处（复习卡片、例句）要加一层回落，
    而它们是数得出来的。

    **`sense_key_map` 永不删除。** 13,741 行留着不费什么，而有了它，
    一台离线设备揣着旧 id 回来也翻译得出来——这是多设备逼出来的要求，
    不是可选项。
    """
    from backend.modules.senses import keys as sense_keys

    conn.execute("ALTER TABLE senses ADD COLUMN sense_key TEXT")

    # 退休表：和 senses 同形，外加退休时间。`CREATE TABLE ... AS SELECT` 拿到
    # 列而不拿到约束，正合适——退休行不该受 UNIQUE(headword, ordinal) 约束，
    # 同一个词退休过两条 #1 是完全正常的。
    conn.execute(
        "CREATE TABLE IF NOT EXISTS senses_retired AS"
        " SELECT *, NULL AS retired_at, NULL AS retired_reason FROM senses WHERE 0"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_senses_retired_id ON senses_retired (id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_senses_retired_key ON senses_retired (sense_key)"
    )

    # 键的映射表。**永不删除**，所以它没有 DELETE 的路径，只有 INSERT。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sense_key_map (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            from_key    TEXT    NOT NULL,
            to_key      TEXT,               -- NULL ＝ 这个义项没有对应的新义项
            decided_at  TEXT    NOT NULL,
            reason      TEXT,
            model       TEXT,
            UNIQUE (from_key, to_key)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sense_key_map_from ON sense_key_map (from_key)"
    )

    # 回填。按词分组算，因为撞键的后缀是在一个词的范围内定的。
    rows = conn.execute(
        "SELECT id, headword, ordinal, concept_en FROM senses"
        " ORDER BY headword, ordinal, id"
    ).fetchall()
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(row["headword"], []).append(row)

    collisions = 0
    for headword, senses in grouped.items():
        assigned = sense_keys.assign_keys(
            headword, [row["concept_en"] for row in senses]
        )
        for row, key in zip(senses, assigned):
            if key.count(":") > 1:
                collisions += 1
            conn.execute(
                "UPDATE senses SET sense_key = ? WHERE id = ?", (key, row["id"])
            )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_senses_key ON senses (sense_key)"
    )
    log.info(
        "senses.keys.backfilled",
        f"给 {len(rows)} 条义项算了稳定键，其中 {collisions} 条概念重复、加了后缀",
        senses=len(rows), collisions=collisions,
    )


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
    Migration(
        version=4,
        name="stable sense keys, a retirement table, and a permanent key map",
        database="content",
        apply=_stable_sense_keys,
    ),
]
