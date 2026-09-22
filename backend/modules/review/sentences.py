"""The sentence pool: where review questions come from.

考句池 question pool / 提示池 hint pool / 过量生成 over-generate

**Why a pool and not one sentence.** If a sense is only ever shown in one
sentence, what gets remembered is the sentence. 决定 5 asks for 5–10 per sense,
drawn at random, so that recognising the word in a new context is what is being
tested.

**The corpus cannot supply that.** Measured across 453 articles: the median
sense appears in **3** sentences, and 26.7% appear in exactly one — which is the
very sentence the learner read it in, so it is a hint rather than a question.
Generation is therefore the main source, not a top-up.

**Which pool a sentence belongs to is derived, never stored.** A corpus sentence
from an article the learner has finished is a hint; anything else is a question.
Read another article and its sentences move across on their own — 决定 5 wants
exactly that, and deriving it means no bookkeeping can drift.

**Generated sentences are checked by machine, never by a model.** Lemmatise,
look up, count out-of-syllabus — the same checker the generation module uses,
which is a table lookup with no judgement in it. The model is asked to write,
never to mark its own work; 主文档 §B3 records what happens when it is.
"""

from __future__ import annotations

import json
import random
import re
from typing import Any

from backend.core import runtime_config
from backend.core.db import get_connection
from backend.core.logging import get_logger
from backend.modules.llm import client, jobs
from backend.modules.llm.providers import Provider
from backend.modules.review import repository
from backend.modules.senses import repository as senses
from backend.modules.vocabulary import analyzer, syllabus
from backend.modules.reading.difficulty import WITHIN_CET6

log = get_logger("review.sentences")

KIND = "review_sentences"

CJK = re.compile(r"[一-鿿]")
NUMBERING = re.compile(r"^\s*(?:\d+[.)、]|[-*•])\s*")

#: Sentence length bounds. Short enough to answer at a glance, long enough to
#: carry the context that pins the sense down.
MIN_WORDS, MAX_WORDS = 8, 30


# --------------------------------------------------------------------------- #
# Harvest — free sentences the corpus already has
# --------------------------------------------------------------------------- #

def harvest(item_key: str, sense_id: int, *, limit: int = 20,
            item_type: str = "word") -> int:
    """Copy every corpus occurrence of this sense into the pool.

    Cheap and exact: the sense annotation already says which occurrences carry
    this meaning, so nothing has to be inferred. Tokens flagged ``in_phrase``
    are skipped — their sense annotation describes a word that was never there.

    **For a phrase the flag is read the other way round** (P11): those very
    tokens are where the phrase is, so the phrase path harvests the occurrence
    table instead. The comment above is exactly the reason it cannot share the
    word query — one says "not here", the other says "here".
    """
    if item_type == "phrase":
        return _harvest_phrase(item_key, sense_id, limit=limit)

    conn = get_connection("content")
    rows = conn.execute(
        """
        SELECT t.surface, t.char_start, t.char_end, t.article_id,
               s.id AS sentence_id, s.text, s.char_start AS s_start
          FROM reading_tokens t
          JOIN reading_sentences s ON s.id = t.sentence_id
         WHERE t.sense_id = ? AND t.in_phrase = 0 AND t.headword = ?
         LIMIT ?
        """,
        (sense_id, item_key, limit),
    ).fetchall()

    added = 0
    for row in rows:
        start = row["char_start"] - row["s_start"]
        end = row["char_end"] - row["s_start"]
        text = row["text"]
        if not (0 <= start < end <= len(text)) or text[start:end] != row["surface"]:
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO review_sentences (item_type, item_key, sense_id, text,"
            " blank_start, blank_end, surface, source, article_id, sentence_id, created_at)"
            " VALUES ('word',?,?,?,?,?,?,'corpus',?,?,?)",
            (item_key, sense_id, text, start, end, row["surface"],
             row["article_id"], row["sentence_id"], repository.now_iso()),
        )
        added += cursor.rowcount
    conn.commit()
    return added


# --------------------------------------------------------------------------- #
# Machine validation — no model marks its own work
# --------------------------------------------------------------------------- #

def locate(text: str, item_key: str) -> tuple[int, int, str] | None:
    """Where the target word sits in this sentence, and the form it takes.

    Returns ``None`` when the word is not actually there — which is the single
    most common way a generated sentence fails.

    **A phrase is located as a run of tokens**, and the whole run is what gets
    blanked: 跨 Phase 不变量 says a phrase is selected and marked as one thing,
    and a card that blanked only ``account`` of ``account for`` would be asking
    a different question from the one the learner marked.
    """
    parsed = analyzer.analyze(text)
    wanted = item_key.lower().split()
    if len(wanted) > 1:
        for sentence in parsed:
            tokens = [t for t in sentence.tokens if t.is_word]
            for start in range(len(tokens) - len(wanted) + 1):
                span = tokens[start:start + len(wanted)]
                surfaces = [t.text.lower() for t in span]
                lemmas = [(t.headword or t.text).lower() for t in span]
                if wanted in (surfaces, lemmas):
                    return (span[0].char_start, span[-1].char_end,
                            text[span[0].char_start:span[-1].char_end])
        return None
    for sentence in parsed:
        for token in sentence.tokens:
            if (token.headword or "").lower() == item_key.lower():
                return token.char_start, token.char_end, token.text
    return None


def validate(text: str, item_key: str, concept: str = "") -> tuple[bool, str]:
    """Whether a generated sentence may enter the pool. ``(ok, why_not)``."""
    if CJK.search(text):
        return False, "含中文"
    if concept:
        words = re.findall(r"[a-z]+", concept.lower())
        norm = " ".join(re.findall(r"[a-z]+", text.lower()))
        if any(" ".join(words[i:i + 5]) in norm for i in range(max(0, len(words) - 4))):
            return False, "把释义写进了句子"

    parsed = analyzer.analyze(text)
    tokens = [t for s in parsed for t in s.tokens if t.is_word]
    if not (MIN_WORDS <= len(tokens) <= MAX_WORDS):
        return False, f"句长 {len(tokens)}"

    found = None
    for token in tokens:
        if (token.headword or "").lower() == item_key.lower():
            found = token
            break
    if found is None:
        return False, "没用上目标词"

    for token in tokens:
        if not token.is_content or token.is_proper_noun:
            continue
        if (token.headword or "").lower() == item_key.lower():
            continue
        if not syllabus.known(token.headword or "", token.text, WITHIN_CET6):
            return False, f"超纲词 {token.text}"
    return True, ""


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #

SYSTEM = "你在为中国的英语学习者制作例句。只输出句子，一行一句。"


def prompt_for(word: str, pos: str, concept: str, gloss: list[str], count: int,
               *, item_type: str = "word") -> str:
    """The prompt measured at 96% machine-valid on 2026-09-09.

    Kept close to what was measured. Note what it does *not* do: it never asks
    the model to check itself, and it asks for more sentences than are needed
    rather than for "good" ones — this project has three times shown that models
    are deaf to quantity-and-quality instructions but fine with plain volume.

    **A phrase changes one word of it** (P11 决定 ⑫): the label. Everything the
    prompt actually constrains — length, vocabulary, one scene per sentence, no
    Chinese, no explaining the item — applies unchanged, and rewriting a
    measured prompt to say the same things differently would throw away the
    measurement.
    """
    label = "词组" if item_type == "phrase" else "词"
    return (
        f"{label}：{word}（{pos or '不限'}）\n"
        f"这个义项的英文定义：{concept}\n"
        f"中文对应：{' / '.join(gloss) if gloss else '（无）'}\n\n"
        f"请写 {count} 个英文句子，每一句都用 {word} 的这个义项（不是它的其他意思）。\n"
        + (f"{word} 必须整体出现，中间不要插入别的词。\n\n" if item_type == "phrase" else "\n")
        + "要求：\n"
        f"- 每句 {MIN_WORDS} 到 {MAX_WORDS} 个词\n"
        f"- 除了 {word} 本身，句子里其他所有词都必须是常见词——中考、高考、大学英语四级或六级"
        "词表范围之内。不要用专业术语、生僻词，不要用少见的人名地名\n"
        "- 句子要有具体的场景和对象，不要空洞的泛论\n"
        f"- 不要在句子里解释 {word} 的意思，也不要出现中文\n"
        "- 每句的场景各不相同\n"
        "- 只输出句子本身，一行一句，不要编号，不要任何别的文字"
    )


def split_lines(raw: str) -> list[str]:
    out = []
    for line in (raw or "").splitlines():
        line = NUMBERING.sub("", line).strip().strip('"').strip()
        if len(line) >= 15 and re.search(r"[A-Za-z]", line):
            out.append(line)
    return out


def _plan(params: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """One unit per sense that is short of sentences."""
    target = int(runtime_config.get("review_pool_target"))
    wanted = params.get("senses")
    if wanted:
        pairs = [(str(w.get("item_type") or "word"), str(w["item_key"]),
                  int(w["sense_id"])) for w in wanted]
    else:
        # **照上报的词池快照挑，不照服务端那份存档**（P9 §7）。
        #
        # 造句子是工厂的活，而「哪些词在学」是学习记录——服务端不推它，用设备
        # 报上来的那个值。存档那一份（`study_states.pool`）现在只由标记事件维护，
        # 拿它来排生成会给已经毕业的词继续造句，而给真正在学的词漏掉。
        # **词组也在里面**（P11 决定 ⑤c／⑫）：标了才造，跟单词同一条规矩。
        rows = get_connection("events").execute(
            """
            SELECT p.item_type, p.item_key, p.sense_id FROM learner_pool p
             WHERE p.learner_id = ? AND p.pool = 'reviewing'
               AND p.sense_id > 0
               AND (SELECT COUNT(*) FROM review_sentences r
                     WHERE r.item_type = p.item_type AND r.item_key = p.item_key
                       AND r.sense_id = p.sense_id) < ?
             LIMIT ?
            """,
            (int(params.get("learner_id", 1)), target, int(params.get("limit", 200))),
        ).fetchall()
        pairs = [(str(r["item_type"] or "word"), r["item_key"], int(r["sense_id"]))
                 for r in rows]

    units = []
    for item_type, item_key, sense_id in pairs:
        # free sentences first, then ask for the rest
        harvest(item_key, sense_id, item_type=item_type)
        have = get_connection("content").execute(
            "SELECT COUNT(*) FROM review_sentences WHERE item_type=? AND item_key=?"
            " AND sense_id=? AND source='generated'",
            (item_type, item_key, sense_id),
        ).fetchone()[0]
        if have >= target:
            continue
        units.append((f"{item_key}#{sense_id}",
                      {"item_type": item_type, "item_key": item_key,
                       "sense_id": sense_id}))
    return units


def _run(provider: Provider, payload: dict[str, Any], params: dict[str, Any]) -> jobs.ItemOutcome:
    item_key, sense_id = payload["item_key"], int(payload["sense_id"])
    item_type = str(payload.get("item_type") or "word")
    if item_type == "phrase":
        from backend.modules.phrases import repository as phrase_repo
        sense = phrase_repo.sense_by_id(sense_id)
    else:
        # `sense_by_id` 2026-09-17 真的做出来了（P9 §8），所以那个 `hasattr` 的
        # 预留可以撤了。它**退休的义项也找得到**——造例句这一路正是会遇到旧义项
        # id 的地方:词标记在两个月前，而义项集后来重建过。
        sense = senses.sense_by_id(sense_id)
    if not sense:
        return jobs.ItemOutcome(result="义项不存在，跳过")

    try:
        gloss = json.loads(sense["gloss_zh"] or "[]")
    except (TypeError, ValueError):
        gloss = [str(sense["gloss_zh"] or "")]

    count = int(runtime_config.get("review_generate_batch"))
    completion = client.complete(
        provider,
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": prompt_for(
             item_key, sense.get("pos_zh") or sense.get("pos") or "",
             sense.get("concept_en") or "", gloss, count, item_type=item_type)}],
        max_tokens=3000,
        temperature=0.8,
    )

    conn = get_connection("content")
    kept = rejected = 0
    reasons: list[str] = []
    for line in split_lines(completion.text):
        ok, why = validate(line, item_key, sense.get("concept_en") or "")
        if not ok:
            rejected += 1
            reasons.append(why)
            continue
        where = locate(line, item_key)
        if where is None:
            rejected += 1
            continue
        start, end, surface = where
        cursor = conn.execute(
            "INSERT OR IGNORE INTO review_sentences (item_type, item_key, sense_id, text,"
            " blank_start, blank_end, surface, source, model, created_at)"
            " VALUES (?,?,?,?,?,?,?,'generated',?,?)",
            (item_type, item_key, sense_id, line, start, end, surface,
             completion.model, repository.now_iso()),
        )
        kept += cursor.rowcount
    conn.commit()

    return jobs.ItemOutcome(
        result=f"{item_key}#{sense_id} 留下 {kept} 条，丢弃 {rejected} 条"
               + (f"（{'、'.join(reasons[:4])}）" if reasons else ""),
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
    )


WORKER = jobs.Worker(
    kind=KIND,
    title="生成复习例句",
    description="给进了复习队列的义项凑够句子池：过量生成，逐句机器校验，留够目标条数",
    plan=_plan,
    run_item=_run,
    default_provider=lambda: str(runtime_config.get("review_gen_provider")) or None,
)


def start_for(senses_wanted: list[dict[str, Any]] | None = None, *,
              learner_id: int = 1, provider_id: str | None = None) -> int | None:
    """Queue and start generation. ``None`` when every pool is already full."""
    params: dict[str, Any] = {"learner_id": learner_id}
    if senses_wanted:
        params["senses"] = senses_wanted
    try:
        job_id = jobs.create(KIND, params=params, provider_id=provider_id)
    except Exception as exc:  # noqa: BLE001 - "nothing to generate" is a normal outcome
        log.info("review.generate.nothing", f"没有需要生成句子的义项：{exc}")
        return None
    jobs.start(job_id)
    return job_id


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

def as_card(row: dict[str, Any]) -> dict[str, Any]:
    """One sentence in the shape a client renders.

    ``first_letter`` is level 1 of the 看义想词 hints (决定 22) and is computed
    here rather than on the client: what counts as the first letter is a rule,
    and architecture rule 1 keeps rules on this side.
    """
    return {
        "id": row["id"],
        "text": row["text"],
        "blank_start": row["blank_start"],
        "blank_end": row["blank_end"],
        "surface": row["surface"],
        "first_letter": (row["surface"] or "?")[:1],
        "source": row["source"],
        # **P9 加的**：考句／提示的划分搬到客户端之后，客户端要判「这一句你见过吗」，
        # 而它那个「见过」的集合是按文章里的句子 id 记的（标记事件带着它）。
        # 和上面那个 `id` 不是一回事——那个是句子池自己的行号。
        "sentence_id": row["sentence_id"],
        # Lets a hint say where it came from ("——文章a"), and lets a future
        # version jump back into the article. Null for generated sentences,
        # which are never hints anyway.
        "article_id": row["article_id"],
        "article_title": row.get("article_title"),
        # 看中文想英文 asks with this (P7 决定 15). Null until the translation
        # batch has been through — the client shows the English side only.
        "text_zh": row.get("text_zh"),
        # Where the target lands in the Chinese, for the highlight. **Null is a
        # normal answer**: some words have no clean Chinese span, and a wrong
        # span would split 估计 down the middle with nothing reporting it.
        "zh_start": row.get("zh_start"),
        "zh_end": row.get("zh_end"),
    }


def _rows(item_key: str, sense_id: int, *,
          item_type: str = "word") -> list[dict[str, Any]]:
    # The title rides along so a hint can say where it came from. It is a join
    # rather than a column: the article may be renamed, and a copy would drift.
    #
    # **`item_type` is part of the key, not decoration** (P11): a phrase sense id
    # and a word sense id are drawn from one number space and so can never
    # collide, but `item_key` alone can — nothing stops a phrase being spelled
    # like a word once phrases with one word exist. Keying on all three is what
    # the table's own UNIQUE constraint does.
    rows = get_connection("content").execute(
        "SELECT r.*, a.title AS article_title FROM review_sentences r"
        "  LEFT JOIN reading_articles a ON a.id = r.article_id"
        " WHERE r.item_type = ? AND r.item_key = ? AND r.sense_id = ? ORDER BY r.id",
        (item_type, item_key, sense_id),
    ).fetchall()
    return [dict(r) for r in rows]


def _harvest_phrase(item_key: str, sense_id: int, *, limit: int = 20) -> int:
    """Corpus sentences where this phrase carries this sense.

    The blank covers the whole phrase, from the first token's character to the
    last token's — the invariant again: one unit, one blank.
    """
    conn = get_connection("content")
    rows = conn.execute(
        """
        SELECT p.surface, p.article_id, s.id AS sentence_id, s.text,
               s.char_start AS s_start,
               (SELECT t.char_start FROM reading_tokens t
                 WHERE t.article_id = p.article_id AND t.seq = p.start_seq) AS c_start,
               (SELECT t.char_end FROM reading_tokens t
                 WHERE t.article_id = p.article_id AND t.seq = p.end_seq) AS c_end
          FROM reading_phrases p
          JOIN reading_sentences s ON s.id = p.sentence_id
         WHERE p.phrase = ? AND p.sense_id = ?
         ORDER BY p.id LIMIT ?
        """,
        (item_key, sense_id, limit),
    ).fetchall()

    kept = 0
    for row in rows:
        if row["c_start"] is None or row["c_end"] is None:
            continue
        start = int(row["c_start"]) - int(row["s_start"])
        end = int(row["c_end"]) - int(row["s_start"])
        text = str(row["text"])
        if start < 0 or end > len(text) or start >= end:
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO review_sentences (item_type, item_key, sense_id,"
            " text, blank_start, blank_end, surface, source, article_id, sentence_id,"
            " created_at) VALUES ('phrase',?,?,?,?,?,?,'corpus',?,?,?)",
            (item_key, sense_id, text, start, end, text[start:end],
             int(row["article_id"]), int(row["sentence_id"]), repository.now_iso()),
        )
        kept += cursor.rowcount
    conn.commit()
    return kept


def split_pools(item_key: str, sense_id: int, finished: set[int],
                seen_sentence_ids: set[int] | None = None) -> tuple[list, list]:
    """``(questions, hints)`` — derived, see the module docstring.

    **The criterion is "have you seen this sentence", not "did you finish the
    article".** Those came apart in P7: you mark a word *while reading*, so the
    sentence you marked it in has certainly been seen — but the article may not
    be finished for hours. Under the old test that sentence counted as unseen
    and could be drawn as a question, which asks you with the very sentence you
    read it in five minutes ago. It reads as an easy question and lands as an
    inflated grade, and nothing reports it.

    ``finished`` still carries most of the work: every sentence in an article
    you read is seen. ``seen_sentence_ids`` adds the ones the ledger knows about
    individually — where the mark happened.
    """
    seen = seen_sentence_ids or set()
    questions, hints = [], []
    for row in _rows(item_key, sense_id):
        if row["source"] != "corpus":
            questions.append(row)          # generated sentences are never hints
            continue
        is_hint = row["article_id"] in finished or row["sentence_id"] in seen
        (hints if is_hint else questions).append(row)
    return questions, hints


def pick_question(item_key: str, sense_id: int, *, finished: set[int],
                  exclude: set[int] | None = None,
                  seen_sentence_ids: set[int] | None = None,
                  rng: random.Random | None = None) -> dict[str, Any] | None:
    """A sentence to ask with, preferring one not yet used today.

    决定 16: not repeating is a preference, not a rule. When the pool has been
    exhausted, reusing a sentence beats having nothing to ask — and by 决定 15
    there is no option to let the item go.
    """
    rng = rng or random
    questions, hints = split_pools(item_key, sense_id, finished, seen_sentence_ids)
    pool = questions or hints
    if not pool:
        return None
    fresh = [s for s in pool if s["id"] not in (exclude or set())]
    return rng.choice(fresh or pool)


def pick_hint(item_key: str, sense_id: int, *, finished: set[int],
              seen_sentence_ids: set[int] | None = None,
              rng: random.Random | None = None) -> dict[str, Any] | None:
    """The sentence the learner originally met this sense in, if there is one."""
    rng = rng or random
    _questions, hints = split_pools(item_key, sense_id, finished, seen_sentence_ids)
    return rng.choice(hints) if hints else None


def pool_size(item_key: str, sense_id: int) -> int:
    return get_connection("content").execute(
        "SELECT COUNT(*) FROM review_sentences WHERE item_key = ? AND sense_id = ?",
        (item_key, sense_id),
    ).fetchone()[0]
