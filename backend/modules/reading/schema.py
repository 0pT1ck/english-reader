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

Three layers store *what happened*: articles, sentences, tokens are the text as
analysed once at ingest, and never change afterwards. ``word_marks`` is what the
learner did. ``sense_states`` is the third thing the design needed and did not
have — *where each sense stands now*, which is what both review scheduling and
"which words have not been taught yet" have to read.
"""

from __future__ import annotations

from backend.core.db import Migration

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
        -- P5 owns scheduling and will add its own columns here (strength,
        -- difficulty, due date). None of them exist yet, on purpose: P2 records
        -- state, it does not decide when anything comes back.
        CREATE TABLE IF NOT EXISTS sense_states (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            learner_id            INTEGER NOT NULL DEFAULT 1,
            headword              TEXT    NOT NULL,
            sense_id              INTEGER NOT NULL DEFAULT 0,

            -- new | reviewing | graduated
            -- "graduated" is never set in P2; the condition for it needs real
            -- reading data and belongs to P5. The value exists so the column
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
        -- somewhere to be unique, and P4/P5 will add many more event types —
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
]
