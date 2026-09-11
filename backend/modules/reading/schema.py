"""Tables for the reading loop.

All of it lives in ``learning.db``, alongside the study record, rather than in
``content.db`` where generated content otherwise goes. The reason is restore
consistency: if articles lived in one file and the marks referring to them in
another, restoring only one of the two would leave dangling article ids. Backup
size (a few MB becoming a few tens of MB) is the price, and it is cheap.

Term mapping for the design documents:
    article       文章
    sentence      句子
    token         词的一次出现
    target word   目标词        the words this article was written to teach
    incidental    附带词        met in passing, glossed but never assigned
    sense state   义项掌握状态   what the learner knows, per sense

Three layers store *what happened*: articles, sentences, tokens and phrases are
the text as analysed once at ingest, and never change afterwards. ``word_marks`` is what the
learner did. ``sense_states`` is the third thing the design needed and did not
have — *where each sense stands now*, which is what both review scheduling and
"which words have not been taught yet" have to read.
"""

from __future__ import annotations

import sqlite3

from backend.core.db import Migration


def _merge_study_tables(conn: sqlite3.Connection) -> None:
    """Rename the two study tables and give them a type column.

    ``word_marks`` becomes ``study_marks`` and ``sense_states`` becomes
    ``study_states``, each gaining ``item_type`` (word / phrase) and
    ``item_key`` (``account`` / ``account for``).

    **Why one table per concern rather than one per type.** Review scheduling
    has to answer "what comes back today", and the answer is one list with words
    and phrases mixed into it. Split across two tables, the review scheduler reads both
    and merges — and that scheduler is the core algorithm of this project, so
    writing it twice means two copies that drift apart.

    **Why now.** A table called 单词标记 holding phrases is a name that lies. The
    two tables hold six rows each today, so the migration is free; after half a
    year of reading, with review's columns grown onto them, it is not. This is the
    only free moment there will be.

    ``sense_id`` stays, and is always 0 for a phrase. That is a column only some
    types use, which is an ordinary shape; splitting further to avoid it would
    be the worse trade.

    Row counts are asserted rather than trusted. A silent loss here loses the
    learner's own record, the one thing in this project that cannot be
    regenerated, so a mismatch aborts the migration — and the automatic
    pre-migration backup is what it is aborting in favour of.
    """
    def count(table: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row[0] == 0:
            return -1
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608

    before = {"marks": count("word_marks"), "states": count("sense_states")}

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS study_marks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            learner_id  INTEGER NOT NULL DEFAULT 1,

            -- Constrained in the database rather than by convention: a typo at
            -- one call site would otherwise create a third kind of item that
            -- every query silently ignores.
            item_type   TEXT    NOT NULL DEFAULT 'word'
                        CHECK (item_type IN ('word', 'phrase')),
            -- The headword for a word, the dictionary form for a phrase.
            item_key    TEXT    NOT NULL,

            -- 0 when the item has no sense set at all (about 10% of exam-paper
            -- vocabulary), and always 0 for a phrase.
            sense_id    INTEGER NOT NULL DEFAULT 0,
            -- unknown | fuzzy
            kind        TEXT    NOT NULL,
            article_id  INTEGER,
            sentence_id INTEGER,
            token_id    INTEGER,
            created_at  TEXT    NOT NULL,
            -- `kind` is deliberately *not* part of the key: an item is unknown
            -- or fuzzy, never both, and the database should say so rather than
            -- trusting every writer to clear the other one first.
            UNIQUE (learner_id, item_type, item_key, sense_id)
        );
        INSERT INTO study_marks (id, learner_id, item_type, item_key, sense_id, kind,
                                 article_id, sentence_id, token_id, created_at)
            SELECT id, learner_id, 'word', headword, sense_id, kind,
                   article_id, sentence_id, token_id, created_at
            FROM word_marks;
        DROP TABLE word_marks;
        CREATE INDEX IF NOT EXISTS idx_marks_item
            ON study_marks (learner_id, item_type, item_key);

        CREATE TABLE IF NOT EXISTS study_states (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            learner_id            INTEGER NOT NULL DEFAULT 1,
            item_type             TEXT    NOT NULL DEFAULT 'word'
                                  CHECK (item_type IN ('word', 'phrase')),
            item_key              TEXT    NOT NULL,
            sense_id              INTEGER NOT NULL DEFAULT 0,

            -- new | reviewing | graduated
            pool                  TEXT    NOT NULL DEFAULT 'new',

            introduced_at         TEXT,
            introduced_article_id INTEGER,
            introduced_sentence_id INTEGER,
            encounters            INTEGER NOT NULL DEFAULT 0,
            updated_at            TEXT    NOT NULL,
            UNIQUE (learner_id, item_type, item_key, sense_id)
        );
        INSERT INTO study_states (id, learner_id, item_type, item_key, sense_id, pool,
                                  introduced_at, introduced_article_id,
                                  introduced_sentence_id, encounters, updated_at)
            SELECT id, learner_id, 'word', headword, sense_id, pool,
                   introduced_at, introduced_article_id,
                   introduced_sentence_id, encounters, updated_at
            FROM sense_states;
        DROP TABLE sense_states;
        CREATE INDEX IF NOT EXISTS idx_states_pool ON study_states (learner_id, pool);
        CREATE INDEX IF NOT EXISTS idx_states_item
            ON study_states (learner_id, item_type, item_key);
    """)

    after = {"marks": count("study_marks"), "states": count("study_states")}
    if before["marks"] >= 0 and after != before:
        raise RuntimeError(
            f"学习记录迁移前后行数对不上：迁移前 {before}，迁移后 {after}。"
            "已回滚，迁移前的自动备份在 data/backups/ 下。"
        )

MIGRATIONS = [
    Migration(
        version=1,
        name="articles, sentences, tokens",
        database="learning",
        apply="""
        CREATE TABLE IF NOT EXISTS reading_articles (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,

            -- generated | cet4 | cet6 | kaoyan
            source         TEXT    NOT NULL,
            -- exam paper filename, or the generation_drafts id it came from.
            -- Unique with source so ingesting the same paper twice is refused
            -- rather than silently duplicating an article the learner may
            -- already have marks against.
            source_ref     TEXT    NOT NULL,
            title          TEXT,
            body           TEXT    NOT NULL,

            word_count     INTEGER NOT NULL DEFAULT 0,
            sentence_count INTEGER NOT NULL DEFAULT 0,

            -- pending | analysing | annotating | ready | failed
            status         TEXT    NOT NULL DEFAULT 'pending',
            status_detail  TEXT,

            -- When it was put on the shelf. The client's "fresh" list shows the
            -- last `reading_fresh_days` days; everything older moves to the
            -- back catalogue rather than being deleted, because an unread
            -- article is not waste — it is one whose turn has not come.
            prepared_at    TEXT,
            read_at        TEXT,

            -- JSON: every indicator (§8 of the phase document), kept separately
            -- from the composite so a wrong weighting never hides the raw data.
            difficulty     TEXT,
            difficulty_score REAL,

            created_at     TEXT    NOT NULL,
            UNIQUE (source, source_ref)
        );
        CREATE INDEX IF NOT EXISTS idx_articles_shelf
            ON reading_articles (status, prepared_at DESC);
        CREATE INDEX IF NOT EXISTS idx_articles_source ON reading_articles (source);

        CREATE TABLE IF NOT EXISTS reading_sentences (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL REFERENCES reading_articles(id) ON DELETE CASCADE,
            seq        INTEGER NOT NULL,
            text       TEXT    NOT NULL,
            char_start INTEGER NOT NULL,
            char_end   INTEGER NOT NULL,
            UNIQUE (article_id, seq)
        );

        -- One row per word occurrence. This *is* the encounter record: F2 wants
        -- "which sense of which word, in which sentence of which article", and
        -- that is exactly a row here. A separate encounters table would copy it.
        CREATE TABLE IF NOT EXISTS reading_tokens (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id    INTEGER NOT NULL REFERENCES reading_articles(id) ON DELETE CASCADE,
            sentence_id   INTEGER NOT NULL REFERENCES reading_sentences(id) ON DELETE CASCADE,
            seq           INTEGER NOT NULL,
            surface       TEXT    NOT NULL,
            lemma         TEXT,
            headword      TEXT,
            pos           TEXT,
            char_start    INTEGER NOT NULL,
            char_end      INTEGER NOT NULL,

            -- content | proper | function | nonword.
            -- Decides which branch the tap panel shows.
            kind          TEXT    NOT NULL,

            -- Was this article written to teach this word. Only generated
            -- articles have target words; exam papers were not written to
            -- teach anything, so nothing there is ever a target.
            is_target     INTEGER NOT NULL DEFAULT 0,

            -- Outside the CET-6 syllabus, judged on cumulative tags. Kept
            -- separate from `kind` rather than folded into it because the two
            -- answer different questions, and a word can be both content and
            -- beyond. Whether the reader is *told* about it depends on the
            -- article: C10 marks these in generated text ("outside your range,
            -- no need to learn it") but deliberately not in exam papers, where
            -- meeting an unknown word is itself the skill being practised.
            beyond        INTEGER NOT NULL DEFAULT 0,

            -- Word-formation breakdown, shown instead of a plain gloss when a
            -- derived word is met (nationality <- nation + -ity). P2 shows it
            -- unconditionally: the original design gates this on the root
            -- already being known, and that needs the P3 ability estimate.
            derived_root  TEXT,
            derived_affix TEXT,

            -- Contextual sense annotation: which sense this occurrence carries.
            -- 0 means "no sense set exists for this word", which is different
            -- from NULL meaning "not annotated yet".
            sense_id      INTEGER,
            sense_ordinal INTEGER,
            UNIQUE (article_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_tokens_sentence ON reading_tokens (sentence_id);
        CREATE INDEX IF NOT EXISTS idx_tokens_headword ON reading_tokens (headword);
        CREATE INDEX IF NOT EXISTS idx_tokens_pending
            ON reading_tokens (article_id, kind, sense_id);
        """,
    ),
    Migration(
        version=2,
        name="marks, sense states, progress, client events",
        database="learning",
        apply="""
        -- What the learner said about a word. Three levels, counting the
        -- absence of a mark: unknown (into the review queue), fuzzy (understood
        -- now, may well not be later), and nothing at all (assumed known).
        --
        -- "fuzzy" is deliberately a manual button rather than "you tapped it":
        -- taps are already recorded, so a fuzzy flag that merely meant "tapped"
        -- would carry no information the tap events do not.
        CREATE TABLE IF NOT EXISTS word_marks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            learner_id  INTEGER NOT NULL DEFAULT 1,
            headword    TEXT    NOT NULL,
            -- 0 when the word has no sense set at all (about 10% of the words
            -- in exam papers). Those fall back to headword-level tracking,
            -- which loses nothing: they are the words C10 says not to learn.
            sense_id    INTEGER NOT NULL DEFAULT 0,
            -- unknown | fuzzy
            kind        TEXT    NOT NULL,
            article_id  INTEGER,
            sentence_id INTEGER,
            token_id    INTEGER,
            created_at  TEXT    NOT NULL,
            UNIQUE (learner_id, headword, sense_id, kind)
        );
        CREATE INDEX IF NOT EXISTS idx_marks_headword ON word_marks (learner_id, headword);

        -- Where each sense stands now. Written when an article is *finished*,
        -- never when it is ingested — that single rule is what makes "words
        -- used in articles I never read stay available" true.
        --
        -- Review owns scheduling and will add its own columns here (strength,
        -- difficulty, due date). None of them exist yet, on purpose: P2 records
        -- state, it does not decide when anything comes back.
        CREATE TABLE IF NOT EXISTS sense_states (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            learner_id            INTEGER NOT NULL DEFAULT 1,
            headword              TEXT    NOT NULL,
            sense_id              INTEGER NOT NULL DEFAULT 0,

            -- new | reviewing | graduated
            -- "graduated" is never set in P2; the condition for it needs real
            -- reading data and belongs to review. The value exists so the column
            -- does not have to change meaning later.
            pool                  TEXT    NOT NULL DEFAULT 'new',

            introduced_at         TEXT,
            introduced_article_id INTEGER,
            introduced_sentence_id INTEGER,
            encounters            INTEGER NOT NULL DEFAULT 0,
            updated_at            TEXT    NOT NULL,
            UNIQUE (learner_id, headword, sense_id)
        );
        CREATE INDEX IF NOT EXISTS idx_states_pool ON sense_states (learner_id, pool);

        CREATE TABLE IF NOT EXISTS reading_progress (
            learner_id   INTEGER NOT NULL DEFAULT 1,
            article_id   INTEGER NOT NULL REFERENCES reading_articles(id) ON DELETE CASCADE,
            sentence_seq INTEGER NOT NULL DEFAULT 0,
            percent      REAL    NOT NULL DEFAULT 0,
            finished_at  TEXT,
            updated_at   TEXT    NOT NULL,
            PRIMARY KEY (learner_id, article_id)
        );

        -- Reported events, stored verbatim before anything is derived from
        -- them. Two reasons for the extra layer: the idempotency key needs
        -- somewhere to be unique, and later phases will add many more event types —
        -- when the code that turns events into state changes, history can be
        -- replayed instead of lost.
        CREATE TABLE IF NOT EXISTS client_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            idem_key     TEXT    NOT NULL UNIQUE,
            device_id    INTEGER,
            learner_id   INTEGER NOT NULL DEFAULT 1,
            type         TEXT    NOT NULL,
            payload      TEXT,
            -- Client clock. An offline device may be wrong; both are kept so a
            -- disagreement is visible rather than silently resolved.
            occurred_at  TEXT,
            received_at  TEXT    NOT NULL,
            processed_at TEXT,
            error        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_type ON client_events (type, received_at);
        """,
    ),
    Migration(
        version=3,
        name="one study record for words and phrases",
        database="learning",
        apply=_merge_study_tables,
    ),
    Migration(
        version=4,
        name="phrase occurrences",
        database="learning",
        # Where a run of tokens is a phrase rather than words that merely stand
        # next to each other. Two stages produce a row here: a structural filter
        # (a verb followed by a particle, the pair having a dictionary entry)
        # proposes, and the model decides per occurrence.
        #
        # Rejected candidates are kept, not deleted. `verdict = 0` records that
        # this one was already asked about; without it every re-run pays again
        # for the same question and the same negative answer.
        apply="""
        CREATE TABLE IF NOT EXISTS reading_phrases (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id  INTEGER NOT NULL REFERENCES reading_articles(id) ON DELETE CASCADE,
            sentence_id INTEGER NOT NULL REFERENCES reading_sentences(id) ON DELETE CASCADE,

            -- The dictionary form, e.g. "account for". This is what marks and
            -- study state key on: a phrase is an item in its own right, so
            -- marking `account for` never touches what is recorded about
            -- `account` — that separation is the whole point.
            phrase      TEXT    NOT NULL,

            -- Token span, in article-level token positions, so a client can
            -- join the words visually and either half opens the phrase.
            start_seq   INTEGER NOT NULL,
            end_seq     INTEGER NOT NULL,
            surface     TEXT    NOT NULL,

            -- NULL not judged yet, 1 a phrase here, 0 adjacent words only.
            verdict     INTEGER,
            judged_at   TEXT,
            created_at  TEXT    NOT NULL,
            UNIQUE (article_id, start_seq, phrase)
        );
        CREATE INDEX IF NOT EXISTS idx_phrases_article
            ON reading_phrases (article_id, verdict);
        CREATE INDEX IF NOT EXISTS idx_phrases_pending
            ON reading_phrases (verdict, article_id);
        CREATE INDEX IF NOT EXISTS idx_phrases_phrase ON reading_phrases (phrase);

        -- This token is part of a confirmed phrase, so its own sense annotation
        -- describes something that was never there. The annotator swept the
        -- whole corpus before phrases existed: meeting `account for` it could
        -- only pick one of `account`'s four senses, and about 4.5% of the
        -- 95,058 annotations are wrong in that way.
        --
        -- Flagged rather than re-annotated. Re-annotating costs thirteen times
        -- what a sense-level remap costs, and the planned dictionary swap will
        -- not clean it up either — §F4 maps old senses to new, which carries
        -- the error across intact. A flag lets every consumer exclude these for
        -- the price of one column.
        ALTER TABLE reading_tokens ADD COLUMN in_phrase INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_tokens_in_phrase ON reading_tokens (in_phrase);
        """,
    ),
    Migration(
        version=5,
        name="memory state on study_states, for P3 review",
        database="learning",
        apply="""
        -- P3 needs somewhere to keep each item's memory state, and this is the
        -- table that already answers "where does this sense stand now".
        --
        -- **Added here rather than by the review module on purpose.** The rule
        -- established in P2 is that whoever owns a table adds the column —
        -- ``devices.learner_id`` was added by core auth for the same reason.
        -- The alternative, a parallel ``review_states`` keyed the same way,
        -- would split one answer across two tables and make every consumer
        -- join to find out where a sense stands.
        --
        -- The columns are FSRS's state, but named for what they mean rather
        -- than for the algorithm: swapping schedulers must not require a
        -- migration. ``fsrs_state``/``fsrs_step`` are the exception — they are
        -- opaque scheduler bookkeeping, stored and handed back untouched, and
        -- a different scheduler would simply leave them NULL.
        ALTER TABLE study_states ADD COLUMN stability      REAL;
        ALTER TABLE study_states ADD COLUMN difficulty     REAL;
        ALTER TABLE study_states ADD COLUMN due_at         TEXT;
        ALTER TABLE study_states ADD COLUMN last_review_at TEXT;
        ALTER TABLE study_states ADD COLUMN reps           INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE study_states ADD COLUMN lapses         INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE study_states ADD COLUMN fsrs_state     INTEGER;
        ALTER TABLE study_states ADD COLUMN fsrs_step      INTEGER;

        -- The review queue reads this every day: "what is due, oldest first".
        CREATE INDEX IF NOT EXISTS idx_states_due
            ON study_states (learner_id, due_at) WHERE due_at IS NOT NULL;
        """,
    ),
]
