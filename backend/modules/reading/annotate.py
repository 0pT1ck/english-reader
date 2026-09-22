"""Contextual sense annotation — layer ① of the three-layer gloss.

For every content word in an article, record **which of our own senses it
carries in this sentence**. The model picks an ordinal from a supplied list; it
never writes prose. That choice is deliberate:

* a chosen ordinal can be checked — it must exist and belong to that headword —
  so a bad batch is detectable and rerunnable, where free text is not;
* it is the same identifier the review state hangs on, so review inherits it;
* it costs a number of output tokens instead of a sentence of them.

**Coverage is the whole article, not just the unknown words.** Measured on
generated articles, 98% of distinct content words have a sense set and over 80%
of them are polysemous. Without annotation, tapping any ordinary-looking word —
``account``, ``measure``, ``subject`` — yields the dictionary's comma pile and
leaves the reader to guess which entry applies. A common word carrying one of
its less obvious senses is exactly what the exam tests, so those are exactly the
words worth annotating.

**Batching is not decoration.** P1c established that a fast model asked to
produce a long structured list drifts in the second half. Batches are cut at
sentence boundaries at roughly ``annotate_batch_words`` decisions each, so every
reply is short, and every reply is validated before it is stored.

Two shortcuts run before the model is called at all, because they are free and
exact: a word with no sense set is recorded as sense 0, and a word with exactly
one sense is recorded as that sense. Only genuinely ambiguous words cost tokens.

**Phrases ride along here rather than in a pass of their own** (P11 决定 ④).
The question is identical in shape — *which of these numbered meanings is this,
in this sentence* — so a phrase occurrence is one more item in the same batch,
and the same shortcuts apply. Measured on the corpus it adds 3.7 decisions per
article against roughly 216 words, and nothing at all for articles ingested
after the list exists.

**For a phrase, 0 means something stronger than it does for a word.** For a word
it means "we have no sense that fits, go and read the dictionary". For a phrase
it means **this is not the phrase here** — ``He ran into the room`` is ``run``
followed by a place, not ``run into`` (P11 §7b). Nothing is highlighted, and the
two words keep the senses they were annotated with individually. So the judgement
P2 paid for separately ("is this run of words a unit") comes out of this one for
free: it is the answer "none of the above".
"""

from __future__ import annotations

import json
from typing import Any

from backend.core import runtime_config
from backend.core.db import get_connection
from backend.core.logging import get_logger
from backend.modules.llm import client, jobs
from backend.modules.llm.parsing import ItemFailed, json_object
from backend.modules.llm.providers import Provider
from backend.modules.reading import ingest, repository
from backend.modules.senses import repository as senses

log = get_logger("reading.annotate")

KIND = "annotate_article"

SYSTEM = (
    "你是一位英语阅读教学的编者，为中国的四六级考生标注文章中每个词的语境义项。"
    "你的判断依据只有一条：这个词在这句话里实际表达的是哪个义项。"
)

INSTRUCTION = """\
下面给出一篇文章中连续的几句原文，以及其中若干个词的候选义项。
请判断每个词**在它所在的那句话里**用的是哪一个义项。

判断的是这一处的用法，不是这个词最常见的用法。
四六级阅读大量考查的正是常见词用在不那么显眼的义项上，
所以不要因为某个义项排在前面就选它，要看句子。

如果候选义项里**没有一条贴合这句的用法，就填 0**。
填 0 是正当答案，不是失败——我们据此补全义项集。
不要为了给出答案而挑一个最接近的：挑错了会让读者看到错的释义，
而填 0 只是让他退回去看词典。

**词组（编号带 p 的那些）判断的是另一件事**：
这两三个词在这句话里**是不是作为一个固定说法出现**。
是的话选它用的那个义项；
如果它们只是碰巧挨在一起（He ran into the room 里的 ran into 是「跑进某处」，
不是词组 run into「偶然碰见」），**填 0**。"""


def _prefill(article_id: int) -> tuple[int, int]:
    """Settle everything that does not need a model. Returns (no-sense, single).

    Covers phrase occurrences too: a phrase with one sense is that sense, and a
    phrase with none — which 验收 7 says cannot happen, but a guard that only
    holds when the data is right is not a guard — is recorded as 0.
    """
    pending = repository.unannotated_tokens(article_id)
    no_sense = single = 0
    for token in pending:
        headword = token.get("headword")
        options = senses.senses_of(headword) if headword else []
        if not options:
            # 0 is "annotated, and there is no sense set for this word" — about
            # 10% of exam-paper vocabulary. Distinct from NULL, which means the
            # annotator has not reached it yet.
            repository.set_token_sense(int(token["id"]), 0, None)
            no_sense += 1
        elif len(options) == 1:
            repository.set_token_sense(int(token["id"]), int(options[0]["id"]),
                                       int(options[0]["ordinal"]))
            single += 1

    # **词组只用得上第一条捷径，用不了第二条。**
    # 单词那条「只有一个义项就直接填」成立，是因为这个词确实在这句里；
    # 而词组的第一个问题是**它在这儿到底算不算一个词组**，一个义项的词组
    # 照样可能只是两个碰巧挨着的词。实测：`talk about` 在柯林斯里只有
    # 「这才叫…；真是」（Talk about lucky!）一条，于是预填把语料里每一句
    # 平常的 `talked about` 都标成了那个意思——**不报错，只是全错**。
    for row in repository.unannotated_phrases(article_id):
        options = repository.phrase_senses_of(int(row["phrase_id"] or 0))
        if not options:
            repository.set_phrase_sense(int(row["id"]), NO_SENSE_FITS)
            no_sense += 1

    repository.commit()
    return no_sense, single


def _plan(params: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Batches for one article, or for many.

    Taking a list matters for the corpus backfill: 376 papers as 376 separate
    jobs means 376 rounds of start-and-wait, and none of the batch machinery —
    pause, resume, spend cap, recovery after an interrupted run — applies across
    them. One job over the lot gets all of that, and the rate limiter schedules
    the calls rather than a script's sleep loop.
    """
    ids = params.get("article_ids")
    article_ids = [int(i) for i in ids] if ids else [int(params["article_id"])]

    size = max(5, int(runtime_config.get("annotate_batch_words")))
    batches: list[tuple[str, dict[str, Any]]] = []

    for article_id in article_ids:
        no_sense, single = _prefill(article_id)
        if no_sense or single:
            log.debug(
                "annotate.prefilled",
                f"文章 {article_id}：{no_sense} 个词没有义项集，{single} 个词只有一个义项，"
                "都已直接落库，不用调模型",
                article_id=article_id, no_sense=no_sense, single=single,
            )

        remaining = repository.unannotated_tokens(article_id)
        phrases = repository.unannotated_phrases(article_id)
        if not remaining and not phrases:
            ingest.finalise_if_annotated(article_id)
            continue

        # Phrases are attached to the batch that carries their sentence, so the
        # model sees the sentence it is judging. Any left over (their sentence
        # had no ambiguous word in it) go in a batch of their own.
        by_sentence: dict[int, list[dict[str, Any]]] = {}
        for row in phrases:
            by_sentence.setdefault(int(row["sentence_id"]), []).append(row)

        current: list[dict[str, Any]] = []
        for position, token in enumerate(remaining):
            current.append(token)
            # Cut only between sentences: a word's sense is decided from its
            # sentence, so a batch ending mid-sentence would hide the context
            # from the model for the tokens that follow.
            last = position == len(remaining) - 1
            at_boundary = last or token["sentence_id"] != remaining[position + 1]["sentence_id"]
            if len(current) >= size and at_boundary:
                batches.append(_batch(article_id, current, by_sentence))
                current = []
        if current:
            batches.append(_batch(article_id, current, by_sentence))

        leftover = [row for rows in by_sentence.values() for row in rows]
        if leftover:
            batches.append(_batch(article_id, [], {-1: leftover}))

    return batches


def _batch(article_id: int, tokens: list[dict[str, Any]],
           by_sentence: dict[int, list[dict[str, Any]]]) -> tuple[str, dict[str, Any]]:
    """One call's worth of work. Consumes the phrase rows it takes."""
    sentences = {int(t["sentence_id"]) for t in tokens} or set(by_sentence)
    phrases: list[dict[str, Any]] = []
    for sentence_id in sentences:
        phrases.extend(by_sentence.pop(sentence_id, []))

    if tokens:
        first, last = tokens[0], tokens[-1]
        key = f"文章 {article_id} 句 {first['sentence_seq'] + 1}–{last['sentence_seq'] + 1}"
    else:
        key = f"文章 {article_id} 词组 {len(phrases)} 处"
    return key, {
        "article_id": article_id,
        "token_ids": [int(t["id"]) for t in tokens],
        "phrase_ids": [int(p["id"]) for p in phrases],
    }


def start_for_many(article_ids: list[int], *, title: str | None = None,
                   provider_id: str | None = None) -> int | None:
    """One job covering many articles. Returns ``None`` if none need work."""
    if not article_ids:
        return None
    try:
        job_id = jobs.create(
            KIND,
            params={"article_ids": [int(i) for i in article_ids]},
            provider_id=provider_id,
            title=title or f"批量标注 {len(article_ids)} 篇",
        )
    except Exception as exc:  # noqa: BLE001 - "nothing to annotate" is normal
        log.info("annotate.nothing_to_do", f"没有需要标注的内容：{exc}")
        return None
    jobs.start(job_id)
    return job_id


def _sentences_for(token_ids: list[int]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" * len(token_ids))
    rows = get_connection("content").execute(
        "SELECT DISTINCT s.id, s.seq, s.text FROM reading_sentences s"  # noqa: S608
        f" JOIN reading_tokens t ON t.sentence_id = s.id WHERE t.id IN ({placeholders})"
        " ORDER BY s.seq",
        token_ids,
    ).fetchall()
    return [dict(r) for r in rows]


def _load_tokens(token_ids: list[int]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" * len(token_ids))
    rows = get_connection("content").execute(
        "SELECT t.*, s.seq AS sentence_seq FROM reading_tokens t"  # noqa: S608
        f" JOIN reading_sentences s ON s.id = t.sentence_id WHERE t.id IN ({placeholders})"
        " ORDER BY t.seq",
        token_ids,
    ).fetchall()
    return [dict(r) for r in rows]


def _load_phrases(row_ids: list[int]) -> list[dict[str, Any]]:
    if not row_ids:
        return []
    placeholders = ",".join("?" * len(row_ids))
    rows = get_connection("content").execute(
        "SELECT p.*, s.seq AS sentence_seq, s.text AS sentence_text"  # noqa: S608
        f" FROM reading_phrases p JOIN reading_sentences s ON s.id = p.sentence_id"
        f" WHERE p.id IN ({placeholders}) ORDER BY p.start_seq",
        row_ids,
    ).fetchall()
    return [dict(r) for r in rows]


def _build_prompt(tokens: list[dict[str, Any]], phrases: list[dict[str, Any]],
                  ) -> tuple[str, dict[int, list[dict[str, Any]]],
                             dict[int, list[dict[str, Any]]]]:
    sentences = _sentences_for([int(t["id"]) for t in tokens])
    seen = {int(s["id"]) for s in sentences}
    for row in phrases:
        # A phrase can be the only thing left to ask about in its sentence, and
        # a judgement about a phrase without its sentence is a judgement about
        # nothing.
        if int(row["sentence_id"]) not in seen:
            seen.add(int(row["sentence_id"]))
            sentences.append({"id": int(row["sentence_id"]),
                              "seq": int(row["sentence_seq"]),
                              "text": str(row["sentence_text"])})
    sentences.sort(key=lambda s: int(s["seq"]))

    lines = ["原文："]
    for sentence in sentences:
        lines.append(f"[{sentence['seq'] + 1}] {sentence['text'].strip()}")

    lines.append("")
    lines.append("需要判断的词：")

    options_by_token: dict[int, list[dict[str, Any]]] = {}
    for token in tokens:
        headword = token["headword"]
        options = senses.senses_of(headword)
        options_by_token[int(token["id"])] = options
        lines.append(
            f"#{token['id']} {token['surface']}"
            f"（第 {token['sentence_seq'] + 1} 句，词条 {headword}）"
        )
        for option in options:
            gloss = "／".join(option["gloss_zh"]) if isinstance(option["gloss_zh"], list) \
                else str(option["gloss_zh"])
            # **Order was measured, not assumed** (P10). Collins definitions
            # are COBUILD full sentences averaging 101 characters against the
            # old model-written 51, and many open with the same four words
            # ("You use X when…"), so putting the Chinese first looked like an
            # obvious win. Measured on one article it changed nothing —
            # 14 "none of these fits" against 13 — so the English stays in
            # front, where it has always been. The 4.6% is the model's own
            # behaviour, not a layout problem: the same article annotated
            # against the *old* inventory with this model gives the same 4.6%.
            lines.append(f"   {option['ordinal']}. {option['concept_en']} — {gloss}")

    options_by_phrase: dict[int, list[dict[str, Any]]] = {}
    if phrases:
        lines.append("")
        lines.append("需要判断的词组：")
        for row in phrases:
            options = repository.phrase_senses_of(int(row["phrase_id"] or 0))
            options_by_phrase[int(row["id"])] = options
            lines.append(
                f"#p{row['id']} {row['surface']}"
                f"（第 {int(row['sentence_seq']) + 1} 句，词组 {row['phrase']}）"
            )
            for option in options:
                lines.append(f"   {option['ordinal']}. {option['gloss_zh']}"
                             + (f" — {option['concept_en']}" if option["concept_en"] else ""))

    example = f'"{tokens[0]["id"]}": 1' if tokens else f'"p{phrases[0]["id"]}": 1'
    lines.append("")
    lines.append(
        '只输出 JSON，键是 # 后面的编号（字符串，词组的带 p），'
        '值是义项序号（整数，没有贴合的填 0）：{' + example + ', ...}'
    )
    lines.append("每一个词和词组都必须出现在结果里。不要输出任何解释。")
    return "\n".join(lines), options_by_token, options_by_phrase


def _run(provider: Provider, payload: dict[str, Any], params: dict[str, Any]) -> jobs.ItemOutcome:
    article_id = int(payload["article_id"])

    # Re-check what is still missing rather than redoing the planned batch: a
    # retry exists because part of a batch came back and part did not, and
    # re-asking for what already landed pays twice for the same work.
    wanted = set(payload["token_ids"])
    tokens = [t for t in _load_tokens(payload["token_ids"])
              if int(t["id"]) in wanted and t["sense_id"] is None]
    phrase_ids = [int(i) for i in payload.get("phrase_ids") or []]
    phrases = [p for p in _load_phrases(phrase_ids) if p["sense_id"] is None]
    if not tokens and not phrases:
        ingest.finalise_if_annotated(article_id)
        return jobs.ItemOutcome()

    prompt, options_by_token, options_by_phrase = _build_prompt(tokens, phrases)
    completion = client.complete(
        provider,
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": f"{INSTRUCTION}\n\n{prompt}"}],
        # **上限跟着批次走，不再钉死在 4000**（P10）。
        # 回复是 `{"12345": 7, …}`，一条约 11 个字符、不到 10 个 token；
        # 60/条是宽裕的估计。而 P10 把批次从 35 加到了整篇一次
        # （最长的文章 328 个实词），4000 的硬顶正好擦着这个量——
        # **截断的后果是后半截词悄悄标不上**，只会表现为「没标完」，
        # 不会报错。给足余量比省那点输出预算重要。
        max_tokens=60 * (len(tokens) + len(phrases)) + 400,
        temperature=0.1,
        json_mode=True,
    )

    data = json_object(completion.text)

    stored = rejected = 0
    declined: list[int] = []
    for raw_key, raw_value in data.items():
        key = str(raw_key).lstrip("#")
        try:
            ordinal = int(raw_value)
            is_phrase = key[:1] in ("p", "P")
            item_id = int(key[1:] if is_phrase else key)
        except (TypeError, ValueError):
            rejected += 1
            continue

        if is_phrase:
            options = options_by_phrase.get(item_id)
            if not options:
                rejected += 1
                continue
            if ordinal == 0:
                # **Not a failure: the strongest answer this prompt can give.**
                # These two words are not the phrase here, so nothing is joined
                # and each keeps its own sense (P11 §7b).
                repository.set_phrase_sense(item_id, NO_SENSE_FITS)
                stored += 1
                continue
            match = next((o for o in options if int(o["ordinal"]) == ordinal), None)
            if match is None:
                rejected += 1
                continue
            repository.set_phrase_sense(item_id, int(match["id"]))
            stored += 1
            continue

        token_id = item_id
        options = options_by_token.get(token_id)
        if not options:
            rejected += 1
            continue

        # 0 is the escape hatch: none of the candidate senses covers this use.
        # It is a real answer, not a failure — see NO_SENSE_FITS.
        if ordinal == 0:
            repository.set_token_sense(token_id, NO_SENSE_FITS, None)
            declined.append(token_id)
            continue

        # The whole point of asking for an ordinal instead of prose: this check
        # is possible. An ordinal that does not belong to this headword is
        # dropped, and the token stays NULL so a retry picks it up.
        match = next((o for o in options if int(o["ordinal"]) == ordinal), None)
        if match is None:
            rejected += 1
            continue
        repository.set_token_sense(token_id, int(match["id"]), ordinal)
        stored += 1

    repository.commit()

    if rejected:
        log.warning(
            "annotate.rejected",
            f"文章 {article_id} 有 {rejected} 条标注不合法，已丢弃等待重试",
            article_id=article_id, rejected=rejected, stored=stored,
        )

    by_id = {int(t["id"]): t for t in tokens}
    for token_id in declined:
        token = by_id.get(token_id)
        if token is None:
            continue
        log.warning(
            "annotate.no_sense_fits",
            f"「{token['surface']}」在这句里没有贴合的义项，可能是义项集漏了一条",
            article_id=article_id, headword=token["headword"],
            surface=token["surface"], sentence=token.get("sentence_seq"),
        )

    if not stored and not declined:
        raise ItemFailed(f"这一批没有任何合法标注（收到 {len(data)} 条）")

    _settle_declined(article_id, tokens)
    ingest.finalise_if_annotated(article_id)
    return jobs.ItemOutcome(
        result=json.dumps({"stored": stored, "declined": len(declined),
                           "rejected": rejected}, ensure_ascii=False),
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
    )


#: "Asked, and none of the candidate senses fitted." Distinct from 0 ("this word
#: has no sense set at all") and from NULL ("not asked yet"), because it means
#: something quite specific and useful: the sense set is probably missing an
#: entry. Real examples from the first corpus run — ``around`` in "around 3:30
#: a.m." (the 大约 sense is absent) and ``note`` in "a heavy note of hypocrisy"
#: (the 口气 sense is absent). The model declining is the right behaviour; it
#: was told to pick the closest, and picking a wrong one would be worse.
NO_SENSE_FITS = -1


def _settle_declined(article_id: int, asked: list[dict[str, Any]]) -> None:
    """Close out the tokens the model was asked about but did not answer.

    Without this, one word the model declines leaves an entire article stuck in
    ``annotating`` for ever — three articles out of seventy-seven, over three
    words, in the first corpus run. The word falls back to the dictionary gloss
    and the article becomes readable.

    Each one is logged with its sentence, because the pattern is exactly the
    "漏义项报告" the sense design calls for: a word whose senses do not cover a
    use that actually occurs in the exam corpus. Re-running annotation from the
    console resets these to NULL so they get another chance.
    """
    still_null = [t for t in _load_tokens([int(t["id"]) for t in asked])
                  if t["sense_id"] is None]
    for token in still_null:
        repository.set_token_sense(int(token["id"]), NO_SENSE_FITS, None)
        log.warning(
            "annotate.no_sense_fits",
            f"「{token['surface']}」在这句里没有贴合的义项，可能是义项集漏了一条",
            article_id=article_id, headword=token["headword"],
            surface=token["surface"], sentence=token.get("sentence_seq"),
        )
    if still_null:
        repository.commit()


def _default_provider() -> str | None:
    """Annotation is data work, not writing.

    The measured split from P1 applies: sorting data out wants a fast
    non-reasoning model, and writing prose wants the strong one. This is the
    former, so it follows the same setting the sense-set jobs use rather than
    the article writer's.
    """
    value = runtime_config.get("annotate_provider")
    return str(value) if value else None


WORKER = jobs.Worker(
    kind=KIND,
    title="标注语境义项",
    description="为一篇文章的每个实词标出它在句子里用的是哪个义项（三层释义的第一层）",
    plan=_plan,
    run_item=_run,
    default_provider=_default_provider,
)


def start_for(article_id: int, *, provider_id: str | None = None,
              retry_declined: bool = False) -> int | None:
    """Queue and start annotation for one article. ``None`` when nothing to do.

    ``retry_declined`` puts the "no sense fitted" tokens back in the queue,
    which is what the console's 重跑标注 does — usually after a missing sense
    has been added to the sense set.
    """
    if retry_declined:
        repository.reset_declined(article_id)
    try:
        job_id = jobs.create(
            KIND,
            params={"article_id": article_id},
            provider_id=provider_id,
            title=f"标注文章 {article_id}",
        )
    except Exception as exc:  # noqa: BLE001 - "nothing to annotate" is a normal outcome
        if ingest.finalise_if_annotated(article_id):
            return None
        log.warning(
            "annotate.queue.failed",
            f"文章 {article_id} 的标注任务没能建立：{exc}",
            article_id=article_id,
        )
        return None
    jobs.start(job_id)
    return job_id
