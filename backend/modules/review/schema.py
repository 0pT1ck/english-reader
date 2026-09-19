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
        database="events",
        # **`review_sentences` 明写成 `content.`，其余几张不写**（P9 §10）。
        #
        # 一次迁移只属于一个文件——它的记录记在那个文件的 `schema_migrations` 里，
        # 未限定的 `CREATE` 也落在那里。而这一条建的五张表分属两边:句子是**内容**
        # （模型写的，花了钱，每个学习者都一样），会话／队列／历史／拼写是**记录**。
        # 拆迁移会动版本号，而版本号已经记在库里了，所以改的是这一处限定名。
        #
        # 顺带一个不写限定名就会静默出错的地方:`review_sentences` 上那两个外键指向
        # `reading_articles` 与 `reading_sentences`，而 **SQLite 的外键不跨文件**。
        # 三张表同在 content.db 里，那两个外键才真的在管事。
        apply="""
        CREATE TABLE IF NOT EXISTS content.review_sentences (
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
        CREATE INDEX IF NOT EXISTS content.idx_rsent_item
            ON review_sentences (item_type, item_key, sense_id);
        CREATE INDEX IF NOT EXISTS content.idx_rsent_article
            ON review_sentences (article_id);

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
        database="events",
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
    Migration(
        version=3,
        name="Chinese for every review sentence, plus where the word lands in it",
        database="content",
        apply="""
        -- 看中文想英文那个方向，题面就是这一列（P7 决定 15）。
        --
        -- **这跟「句子里不许出现中文」那条禁令不冲突。**生成提示词写着
        -- 「也不要出现中文」，`sentences.validate()` 里 `含中文 → 不合格`，
        -- 禁的是**题面英文句里混中文**——那等于把答案印在题目上。
        -- 翻译存在自己的列里，不经过那道校验，禁令原样留着。
        ALTER TABLE review_sentences ADD COLUMN text_zh TEXT;

        -- 目标词在中文里对应哪一段，给高亮用（「我【估计】这个项目…」）。
        --
        -- **对不齐就两列都是 NULL，只留 text_zh。**有些词在中文里没有干净的
        -- 对应片段，那时不高亮、整句照显——**高亮错位置比不高亮糟**，
        -- 它会把「估」和「计」中间劈开，而没有任何东西会报错。
        ALTER TABLE review_sentences ADD COLUMN zh_start INTEGER;
        ALTER TABLE review_sentences ADD COLUMN zh_end   INTEGER;

        -- 哪些还没翻译，是要反复查的（补翻批次每次都要问一遍）。
        CREATE INDEX IF NOT EXISTS idx_rsent_untranslated
            ON review_sentences (id) WHERE text_zh IS NULL;
        """,
    ),
    Migration(
        version=4,
        name="two ceilings on the grade: hints taken, and fuzzy-marked today",
        database="events",
        apply="""
        -- 今天在这一条上开过几级提示。**以前只记进历史表，不进调度**——
        -- 于是提示是免费的：把语境和首字母都看了、再点「认识」，
        -- 调度器看到 misses=0 就给 Good，间隔照常拉长。
        -- FSRS 的 Hard 本来就是「想起来了，但费劲」，那正是开了提示的意思。
        ALTER TABLE review_queue ADD COLUMN revealed INTEGER NOT NULL DEFAULT 0;

        -- 这一条是今天标成「模糊」才进来的（P7 推翻 P3 决定 18）。
        -- 决定 18 原来的理由仍然成立：**你几分钟前刚读过它，答对是必然的**，
        -- 当天那一次没有信息量。所以它照常复习、照常结算，但**评级封顶到 Hard**，
        -- 不让首个间隔被一次必然的「答对」吹起来。而「模糊」是最常标的那一档。
        ALTER TABLE review_queue ADD COLUMN capped INTEGER NOT NULL DEFAULT 0;
        """,
    ),
]
