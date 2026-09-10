"""Tables for the review loop.

句子池 sentence pool / 复习会话 review session / 复习历史 review history

All in ``learning.db``, next to the study record, for the same reason the
reading tables are: a backup that restored the marks but not the sentences they
point at would be worse than no backup.

**The memory state itself is not here.** It lives on ``study_states``, added by
``reading``'s migration 5, because reading owns that table — the same rule that
put ``devices.learner_id`` in core auth. Splitting "where does this sense stand"
across two tables would make every consumer join to find out.

Four tables:

* ``review_sentences`` — the pool. Which sentence pool a row belongs to is
  **derived, not stored**: a corpus sentence from an article you have finished
  is a hint, anything else is a question. 决定 5 wants sentences to move from
  one pool to the other as you read more, and deriving it makes that happen with
  no bookkeeping at all.
* ``review_sessions`` — one per learner per day.
* ``review_queue`` — what is in play in one session, and how it is going. This
  is where "解锁到第几步" lives, which the plan had put on ``study_states``:
  it turned out to be session state, not durable state — 决定 7's unlock is
  re-earned every round, and a column on ``study_states`` would have implied it
  survives the day.
* ``spelling_attempts`` — **written and never read**, on purpose. 决定 13 keeps
  spelling out of scheduling; this is the record a future spelling-focused
  feature will start from, and recording ``typed`` rather than just a boolean is
  the whole point: "wrong" says nothing, ``existance`` for ``existence`` says
  which trap you keep falling into.
"""

from __future__ import annotations

from backend.core.db import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="sentence pool, sessions, queue, review history, spelling",
        database="learning",
        apply="""
        CREATE TABLE IF NOT EXISTS review_sentences (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type   TEXT    NOT NULL DEFAULT 'word'
                        CHECK (item_type IN ('word', 'phrase')),
            item_key    TEXT    NOT NULL,
            sense_id    INTEGER NOT NULL DEFAULT 0,

            text        TEXT    NOT NULL,

            -- Character offsets of the target inside `text`, and the form it
            -- actually takes there. 决定 21: the blank is filled with the form
            -- in the sentence (`addressed`), not the lemma — the sentence has
            -- to stay grammatical.
            blank_start INTEGER NOT NULL,
            blank_end   INTEGER NOT NULL,
            surface     TEXT    NOT NULL,

            -- 'corpus' — lifted from an ingested article; 'generated' — written
            -- by a model and passed through the machine check.
            source      TEXT    NOT NULL CHECK (source IN ('corpus', 'generated')),
            article_id  INTEGER REFERENCES reading_articles(id) ON DELETE SET NULL,
            sentence_id INTEGER REFERENCES reading_sentences(id) ON DELETE SET NULL,
            model       TEXT,
            created_at  TEXT    NOT NULL,
            UNIQUE (item_type, item_key, sense_id, text)
        );
        CREATE INDEX IF NOT EXISTS idx_rsent_item
            ON review_sentences (item_type, item_key, sense_id);
        CREATE INDEX IF NOT EXISTS idx_rsent_article ON review_sentences (article_id);

        CREATE TABLE IF NOT EXISTS review_sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            learner_id  INTEGER NOT NULL DEFAULT 1,
            day         TEXT    NOT NULL,
            started_at  TEXT    NOT NULL,
            finished_at TEXT,
            -- Spelling is offered once the day's review is done (决定 13).
            spelling_at TEXT,
            UNIQUE (learner_id, day)
        );

        CREATE TABLE IF NOT EXISTS review_queue (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
            item_type  TEXT    NOT NULL DEFAULT 'word',
            item_key   TEXT    NOT NULL,
            sense_id   INTEGER NOT NULL DEFAULT 0,

            -- 'today'  newly marked while reading today  (决定 18, 19)
            -- 'due'    the scheduler says it is due
            bucket     TEXT    NOT NULL CHECK (bucket IN ('today', 'due')),

            -- 1 看词想义, 2 看义想词. Starts at 1 every round; passing 1 unlocks
            -- 2, failing 2 locks it again and sends the item back to 1 (决定 7).
            step       INTEGER NOT NULL DEFAULT 1,

            -- How many times this item has been asked today, both directions
            -- together. This is what becomes the grade (决定 11).
            asks       INTEGER NOT NULL DEFAULT 0,

            -- Halved on every failure so a stuck item stops blocking the rest
            -- without ever being excluded (决定 10). The day ends when the pool
            -- is empty — and only then (决定 15).
            weight     REAL    NOT NULL DEFAULT 1.0,
            done_at    TEXT,
            UNIQUE (session_id, item_type, item_key, sense_id)
        );
        CREATE INDEX IF NOT EXISTS idx_rqueue_open
            ON review_queue (session_id, done_at);

        CREATE TABLE IF NOT EXISTS review_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            learner_id  INTEGER NOT NULL DEFAULT 1,
            session_id  INTEGER REFERENCES review_sessions(id) ON DELETE SET NULL,
            item_type   TEXT    NOT NULL DEFAULT 'word',
            item_key    TEXT    NOT NULL,
            sense_id    INTEGER NOT NULL DEFAULT 0,
            direction   INTEGER NOT NULL,
            sentence_id INTEGER REFERENCES review_sentences(id) ON DELETE SET NULL,

            -- How far the learner had to open the hints, 0-3. This *is* the
            -- score (决定 4 of the original design): behaviour rather than
            -- self-rated difficulty.
            revealed    INTEGER NOT NULL DEFAULT 0,
            result      TEXT    NOT NULL CHECK (result IN ('pass', 'fail')),
            asked_at    TEXT    NOT NULL,

            -- What the scheduler decided at the end of the round, when it did.
            -- Kept so an algorithm change can be replayed against real history
            -- instead of re-derived from assumptions (§5 of the plan).
            rating      INTEGER,
            interval_d  REAL
        );
        CREATE INDEX IF NOT EXISTS idx_rhist_item
            ON review_history (learner_id, item_type, item_key, sense_id);
        CREATE INDEX IF NOT EXISTS idx_rhist_session ON review_history (session_id);

        CREATE TABLE IF NOT EXISTS spelling_attempts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            learner_id INTEGER NOT NULL DEFAULT 1,
            session_id INTEGER REFERENCES review_sessions(id) ON DELETE SET NULL,
            item_key   TEXT    NOT NULL,
            expected   TEXT    NOT NULL,
            typed      TEXT    NOT NULL,
            correct    INTEGER NOT NULL,
            created_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_spell_item ON spelling_attempts (item_key);
        """,
    ),
    Migration(
        version=2,
        name="count misses, not questions",
        database="learning",
        apply="""
        -- 决定 11 grades a round by "how many times it took". That was
        -- implemented as a count of *questions asked*, and a round is two
        -- questions by construction — so a flawless round counted as 2 and one
        -- stumble counted as 4. Under the mapping in force that made a perfect
        -- round Good (never Easy, which was unreachable) and a single stumble
        -- Again, the worst grade there is.
        --
        -- What the decision always meant is **how many times you missed**, so
        -- that is what is counted now. `asks` stays: it is still the honest
        -- record of how many questions the day put in front of you.
        ALTER TABLE review_queue ADD COLUMN misses INTEGER NOT NULL DEFAULT 0;

        -- Set by the learner, never inferred. We measure whether you got it,
        -- not how hard it felt, so "trivial" is not ours to guess — 2026-09-09.
        ALTER TABLE review_queue ADD COLUMN easy INTEGER NOT NULL DEFAULT 0;
        """,
    ),
]
