"""Chinese for the review sentences, and where the word lands in it.

翻译 translation / 词对齐 alignment / 题面 question / 提示 hint

**Why this exists.** 看中文想英文 (P7 决定 15) asks with a Chinese sentence and
expects the English word back. That sentence is the translation of the one the
English side asks with, and the word being tested has to be highlighted in it —
so two things are needed and neither existed: a translation, and the span inside
it that corresponds to the target.

**This does not weaken the ban on Chinese in the question.** The generator's
prompt says "也不要出现中文" and ``sentences.validate()`` rejects any sentence
containing it. That ban is about *the English sentence* — Chinese inside the
question prints the answer on the paper. A translation in its own column is a
different thing, and ``validate()`` is left exactly as it is.

**Alignment is checked, never trusted.** The model is asked to wrap the target
in ``【】``. Three things can go wrong and all three are silent:

* the brackets are missing, or there are more than one pair;
* the text outside the brackets does not match what was translated;
* the span is empty.

Any of those and **the translation is stored without the alignment**. The
sentence still works — it simply is not highlighted. Storing a wrong span would
split 估计 down the middle and nothing would ever report it.

**Batching is small on purpose.** P1c measured a fast model drifting through the
second half of a long structured list. Each sentence here is an independent
judgement, but the output length still accumulates, so the batch is cut at
``review_translate_batch`` sentences.
"""

from __future__ import annotations

import json
import re
from typing import Any

from backend.core import runtime_config
from backend.core.db import get_connection
from backend.core.logging import get_logger
from backend.modules.llm import client, jobs
from backend.modules.llm.parsing import json_object
from backend.modules.llm.providers import Provider
from backend.modules.review import repository

log = get_logger("review.translate")

KIND = "review_translate"

#: The marker the model wraps the target in. Square brackets in their CJK form,
#: because the half-width ones turn up inside real sentences.
MARK_OPEN, MARK_CLOSE = "【", "】"
MARKED = re.compile(r"【([^【】]*)】")

SYSTEM = (
    "你是一位中英翻译，为中国的英语学习者把例句译成自然的中文。"
    "只输出要求的 JSON，不要任何别的文字。"
)

INSTRUCTION = """\
把下面每个英文句子译成中文。

两条要求：
1. **自然的中文**，不要逐词硬译。句子怎么说得顺口就怎么译。
2. 每句都有一个**目标词**。在译文里把**对应那个词的中文**用【】括起来，
   例如：我【估计】这个项目的费用大约是 5000 美元。

括号只能有一对。要是这个词在中文里没有能单独拎出来的对应片段
（比如它被并进了别的结构），**就不要加括号**，照常给译文——
这是正当答案，不是失败。

只输出 JSON。**键就是那个编号本身，带不带 # 都行**（"1" 或 "#1"），
值是译文字符串。
"""


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

def _pending(limit: int | None = None) -> list[dict[str, Any]]:
    """Sentences with no Chinese yet, oldest first."""
    sql = (
        "SELECT id, text, blank_start, blank_end, surface, item_key"
        "  FROM review_sentences WHERE text_zh IS NULL ORDER BY id"
    )
    params: tuple[Any, ...] = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    rows = get_connection("learning").execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _plan(params: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    limit = params.get("limit")
    rows = _pending(int(limit) if limit else None)
    size = max(1, int(runtime_config.get("review_translate_batch")))
    units: list[tuple[str, dict[str, Any]]] = []
    for start in range(0, len(rows), size):
        chunk = rows[start:start + size]
        units.append((
            f"{len(chunk)} 句（从 #{chunk[0]['id']} 起）",
            {"ids": [r["id"] for r in chunk]},
        ))
    return units


# --------------------------------------------------------------------------- #
# Running one batch
# --------------------------------------------------------------------------- #

def _build_prompt(rows: list[dict[str, Any]]) -> str:
    lines = []
    for seq, row in enumerate(rows, start=1):
        lines.append(f"#{seq}")
        lines.append(f"句子：{row['text']}")
        # The surface form, not the lemma: the sentence says `estimated`, and
        # telling the model to align on `estimate` invites it to mark the wrong
        # thing when both appear.
        lines.append(f"目标词：{row['surface']}")
        lines.append("")
    return "\n".join(lines)


def _split_marks(reply: str) -> tuple[str, int | None, int | None]:
    """``(text, start, end)`` — the span is ``None`` whenever it cannot be trusted.

    Everything here is a reason to drop the alignment and keep the translation.
    """
    text = " ".join(str(reply).split()).strip()
    if not text:
        return "", None, None

    found = MARKED.findall(text)
    if len(found) != 1:
        # None means the model declined to align (a legitimate answer), more
        # than one means it marked several places and we cannot tell which.
        return text.replace(MARK_OPEN, "").replace(MARK_CLOSE, ""), None, None

    inner = found[0].strip()
    stripped = MARKED.sub(lambda m: m.group(1), text)
    if not inner:
        return stripped, None, None

    start = stripped.find(inner)
    if start < 0 or stripped.count(inner) != 1:
        # Not locatable, or ambiguous — a span pointing at the wrong occurrence
        # highlights the wrong characters, which is worse than no highlight.
        return stripped, None, None
    return stripped, start, start + len(inner)


def _run(provider: Provider, payload: dict[str, Any], params: dict[str, Any]) -> jobs.ItemOutcome:
    ids = [int(i) for i in payload.get("ids", [])]
    conn = get_connection("learning")
    placeholders = ",".join("?" * len(ids))
    rows = [dict(r) for r in conn.execute(  # noqa: S608 - count-built placeholders
        f"SELECT id, text, surface FROM review_sentences"
        f" WHERE id IN ({placeholders}) AND text_zh IS NULL ORDER BY id",
        ids,
    ).fetchall()]
    if not rows:
        return jobs.ItemOutcome(result="这一批已经翻过了")

    completion = client.complete(
        provider,
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": f"{INSTRUCTION}\n\n{_build_prompt(rows)}"}],
        max_tokens=200 * len(rows) + 200,
        temperature=0.2,
    )
    answer = json_object(completion.text)

    # **The model writes "#1", not "1".** It was told the key is "the number
    # after the #" and it kept the # — a reasonable reading. 236 of 366
    # sentences went untranslated over it, and every batch reported success,
    # because a key that matches nothing is not an error anywhere.
    # Normalised here rather than insisted on in the prompt: the prompt has been
    # made permissive too, but a parser that only accepts one spelling is a
    # parser that will meet the other one again.
    normalised = {str(k).lstrip("#").strip(): v for k, v in answer.items()}

    stored = aligned = 0
    for seq, row in enumerate(rows, start=1):
        reply = normalised.get(str(seq))
        if not isinstance(reply, str) or not reply.strip():
            continue
        text_zh, start, end = _split_marks(reply)
        if not text_zh:
            continue
        conn.execute(
            "UPDATE review_sentences SET text_zh = ?, zh_start = ?, zh_end = ? WHERE id = ?",
            (text_zh, start, end, row["id"]),
        )
        stored += 1
        if start is not None:
            aligned += 1
    conn.commit()

    missed = len(rows) - stored
    if missed:
        # Not an error: the batch is re-plannable, and whatever stayed NULL will
        # be picked up next time. Worth a line because a model that keeps
        # skipping items is a prompt problem, not a transient one.
        log.warning(
            "translate.partial",
            f"这一批 {len(rows)} 句里有 {missed} 句没拿到译文",
            missing=missed, total=len(rows),
        )

    # **The raw reply, not a summary of it.** `ItemOutcome.result` exists so
    # that a batch which produced nonsense can be read back — it is the only
    # way to tell a bad prompt from a bad parser. The first run of this job
    # stored a tidy `{"stored": 0, "aligned": 0}` instead and left 236 of 366
    # sentences untranslated with nothing to look at.
    return jobs.ItemOutcome(
        result=json.dumps(
            {"stored": stored, "aligned": aligned, "of": len(rows),
             "reply": completion.text},
            ensure_ascii=False),
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
    )


def _default_provider() -> str | None:
    """Translation is data work, not writing — the fast model, per the split."""
    value = runtime_config.get("review_translate_provider")
    return str(value) if value else None


WORKER = jobs.Worker(
    kind=KIND,
    title="给复习句子配中文",
    description="把句子池里的英文句译成中文，并标出目标词对应的中文片段（看中文想英文那个方向的题面）",
    plan=_plan,
    run_item=_run,
    default_provider=_default_provider,
)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def pending_count() -> int:
    return int(get_connection("learning").execute(
        "SELECT COUNT(*) FROM review_sentences WHERE text_zh IS NULL"
    ).fetchone()[0])


def start(*, limit: int | None = None, provider_id: str | None = None) -> int | None:
    """Queue and start a translation batch. ``None`` when there is nothing left."""
    if not _pending(limit):
        return None
    job_id = jobs.create(
        KIND,
        params={"limit": limit} if limit else {},
        provider_id=provider_id,
        title="给复习句子配中文",
    )
    jobs.start(job_id)
    return job_id


def translate_one(sentence_id: int, *, provider_id: str | None = None) -> dict[str, Any]:
    """Translate a single sentence synchronously — for spot-checking quality."""
    from backend.modules.llm import providers

    row = get_connection("learning").execute(
        "SELECT id, text, surface FROM review_sentences WHERE id = ?", (sentence_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"没有这条句子：{sentence_id}")
    provider = providers.get(provider_id) if provider_id else providers.default_provider()
    outcome = _run(provider, {"ids": [sentence_id]}, {})
    return {"sentence_id": sentence_id, "result": outcome.result}


__all__ = ["KIND", "WORKER", "pending_count", "start", "translate_one"]
