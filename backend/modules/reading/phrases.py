"""Phrasal verbs and fixed expressions — finding them, and asking about them.

The problem this exists for: you tap ``account`` and are told 账目／账户, while
the sentence says ``renewable sources account for a third of supply``. That
gloss is not merely unhelpful, it is wrong — and wrong input is the one thing
this whole project exists to avoid. About seven times per article the word under
the reader's finger is half of something else. The annotator had already been
reporting it as a symptom for weeks: ``get up``, ``known as`` and ``at least``
were the top entries of the missing-sense report, and no sense of ``get`` or
``least`` was ever going to cover them.

**No inventory of "phrases worth learning".** That was the original plan and it
was wrong: if you do not recognise a phrase you mark it, exactly as with a word,
and whether some syllabus lists it changes nothing. The same rule that says
nothing enters the review queue except by the learner's own signal removes the
whole research problem here — curating a list comparable to the sense sets,
which cost two phases — and leaves three much smaller ones: find them, gloss
them, let them be marked.

**Two stages, because the two questions are different.**

1. *Structural.* A verb followed by a particle or preposition, the pair existing
   in the dictionary. Costs nothing, and it is what keeps ``to be``, ``the
   world`` and ``there is`` out — none of them starts with a verb. Matching the
   dictionary alone finds 43 such "phrases" per article, the top two being
   ``to be`` 69 times and ``have been`` 57; with this filter it is 6.8.

2. *Contextual.* Whether this occurrence is a unit at all, which only the
   sentence can settle::

       He ran into an old friend downtown.   → 遇见, a phrase
       He ran into the room and shut it.     → 跑进, two words

   Asked as a closed question about one named occurrence — the form measured at
   100% recall and 0% false positives on planted collocation errors, against an
   open "find the problems" that gave professionally edited exam prose a mean of
   5.1 imaginary ones — a fast model gets 10/10 on real phrases and 8–9/10 on
   the two negative control groups, and two of those "errors" turned out to be
   mislabelled by the test rather than by the model.

The criterion given to the model is **transparency, not worth**: can the meaning
be read off the parts? ``walk to the station`` can, ``account for`` cannot. That
keeps it answering an objective question, for the same reason the sense work
asks "is this general written English" rather than "is this worth learning".

Term mapping for the design documents:
    phrase      词组        a run of tokens that means something as a unit
    candidate   候选        a structurally plausible run, not yet judged
    verdict     判定        whether this occurrence is a unit
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from backend.core import runtime_config
from backend.core.db import get_connection
from backend.core.logging import get_logger
from backend.modules.llm import client, jobs
from backend.modules.llm.parsing import ItemFailed, json_object
from backend.modules.llm.providers import Provider

log = get_logger("reading.phrases")

KIND = "judge_phrases"

#: Particles and prepositions that form phrasal verbs. A closed set, which is
#: what makes the structural stage cheap and predictable.
PARTICLES = frozenset("""
    up out off on in away back over through down about around along across
    by after for with to into from upon against at of
""".split())

#: Parts of speech a particle may carry. Excluding PART is what removes
#: ``need to``, ``want to``, ``have to`` — spaCy tags the infinitive marker PART
#: and a real preposition ADP, and that one distinction cut 741 candidates to
#: 531 on the sample it was measured on.
PARTICLE_POS = frozenset({"ADP", "ADV"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Stage 1 — structural candidates
# --------------------------------------------------------------------------- #


def find_candidates(article_id: int) -> int:
    """Propose phrase spans in one article. Returns how many are new.

    Runs over tokens that are already stored, so it needs no model and no
    re-analysis — which is what makes retrofitting the whole corpus cheap: the
    expensive pass (95,058 contextual sense annotations, 4.6M input tokens)
    is never touched.
    """
    conn = get_connection("content")
    tokens = conn.execute(
        "SELECT id, sentence_id, seq, surface, headword, pos, kind FROM reading_tokens"
        " WHERE article_id = ? AND surface GLOB '[A-Za-z]*' ORDER BY seq",
        (article_id,),
    ).fetchall()

    found = 0
    for index, token in enumerate(tokens):
        if token["pos"] != "VERB" or not token["headword"]:
            continue
        # Longest first: `come up with` should win over `come up`.
        for length in (4, 3, 2):
            if index + length > len(tokens):
                continue
            following = tokens[index + 1: index + length]
            if following[0]["pos"] not in PARTICLE_POS:
                continue
            if following[0]["surface"].lower() not in PARTICLES:
                continue
            # Adjacent in the text, not merely adjacent in the token list — a
            # sentence boundary between them is not a phrase.
            if any(part["sentence_id"] != token["sentence_id"] for part in following):
                continue

            # The lemma first, then the inflected form. Both are "does this
            # combination have a dictionary entry", and the second is not a
            # nicety: `according to` occurs 84 times in the corpus and was
            # missed by all of it, because spaCy lemmatises `according` to
            # `accord` and `accord to` is not an entry. `accord` was then the
            # most frequent word left in the missing-sense report — the symptom
            # pointing straight at the cause.
            tail = [t["surface"].lower() for t in following]
            phrase = None
            for candidate in dict.fromkeys([" ".join([token["headword"].lower()] + tail),
                                            " ".join([token["surface"].lower()] + tail)]):
                if conn.execute("SELECT 1 FROM phrases WHERE phrase = ?",
                                (candidate,)).fetchone():
                    phrase = candidate
                    break
            if phrase is None:
                continue

            surface = " ".join([token["surface"]] + [t["surface"] for t in following])
            cursor = conn.execute(
                "INSERT OR IGNORE INTO reading_phrases (article_id, sentence_id, phrase,"
                " start_seq, end_seq, surface, created_at) VALUES (?,?,?,?,?,?,?)",
                (article_id, token["sentence_id"], phrase, token["seq"],
                 following[-1]["seq"], surface, _now()),
            )
            found += cursor.rowcount
            break

    conn.commit()
    return found


# --------------------------------------------------------------------------- #
# Stage 2 — is it a unit here
# --------------------------------------------------------------------------- #

SYSTEM = "你是一位英语母语者，判断词与词的组合在具体句子里是不是一个整体。"

INSTRUCTION = """\
下面每一条给出一句英文，以及句中的一个词序列。判断：
**在这句话里，这个序列是不是一个固定词组？**

判据只有一条：**整体的意思，能不能从组成词直接看出来？**
  看不出来 → 是词组（true）。account for 是「解释／占…比例」，跟「账户」和「为了」都对不上
  看得出来 → 不是（false）。walk to the station 就是「走到车站」，没有额外意思

同一个序列在不同句子里可以不同：
  ran into an old friend → 遇见，看不出来 → true
  ran into the room      → 跑进去，看得出来 → false

只输出 JSON：{"verdicts":{"1":true,"2":false,...}}，每一条都要有判断。"""


def pending(article_ids: list[int] | None = None, limit: int = 100000) -> list[dict[str, Any]]:
    conn = get_connection("content")
    where = "p.verdict IS NULL"
    params: list[Any] = []
    if article_ids:
        where += f" AND p.article_id IN ({','.join('?' * len(article_ids))})"
        params.extend(article_ids)
    rows = conn.execute(
        f"SELECT p.id, p.article_id, p.phrase, p.surface, s.text AS sentence"  # noqa: S608
        f" FROM reading_phrases p JOIN reading_sentences s ON s.id = p.sentence_id"
        f" WHERE {where} ORDER BY p.id LIMIT ?",
        [*params, limit],
    ).fetchall()
    return [dict(r) for r in rows]


def _plan(params: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    ids = params.get("article_ids")
    items = pending([int(i) for i in ids] if ids else None)
    size = max(5, int(runtime_config.get("phrase_batch_size")))
    return [
        (f"{items[start]['phrase']}…{items[min(start + size, len(items)) - 1]['phrase']}",
         {"ids": [int(i["id"]) for i in items[start:start + size]]})
        for start in range(0, len(items), size)
    ]


def _run(provider: Provider, payload: dict[str, Any],
         params: dict[str, Any]) -> jobs.ItemOutcome:
    conn = get_connection("content")
    ids = [int(i) for i in payload["ids"]]
    rows = conn.execute(
        f"SELECT p.id, p.phrase, p.surface, s.text AS sentence"  # noqa: S608
        f" FROM reading_phrases p JOIN reading_sentences s ON s.id = p.sentence_id"
        f" WHERE p.id IN ({','.join('?' * len(ids))}) AND p.verdict IS NULL",
        ids,
    ).fetchall()
    if not rows:
        return jobs.ItemOutcome()

    lines = [f'{i + 1}. 句：{r["sentence"].strip()[:220]}\n   序列："{r["surface"]}"'
             for i, r in enumerate(rows)]
    completion = client.complete(
        provider,
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": INSTRUCTION + "\n\n" + "\n".join(lines)}],
        max_tokens=min(2000, 40 * len(rows) + 300),
        temperature=0.1,
        json_mode=True,
    )
    verdicts = json_object(completion.text).get("verdicts") or {}
    if not isinstance(verdicts, dict) or not verdicts:
        raise ItemFailed("这一批没有返回任何判断")

    kept = dropped = 0
    for index, row in enumerate(rows, start=1):
        answer = verdicts.get(str(index), verdicts.get(index))
        if answer is None:
            continue
        is_phrase = bool(answer)
        conn.execute(
            "UPDATE reading_phrases SET verdict = ?, judged_at = ? WHERE id = ?",
            (1 if is_phrase else 0, _now(), int(row["id"])),
        )
        kept += is_phrase
        dropped += not is_phrase
    conn.commit()

    # Flag the tokens straight away rather than at the end of the job: a batch
    # run that is paused, capped or interrupted must still leave the books
    # consistent with the verdicts it did record.
    articles = {int(r["article_id"]) for r in conn.execute(
        f"SELECT DISTINCT article_id FROM reading_phrases"  # noqa: S608
        f" WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()}
    for article_id in articles:
        mark_tokens_in_phrases(article_id)

    return jobs.ItemOutcome(
        result=json.dumps({"kept": kept, "dropped": dropped}, ensure_ascii=False),
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
    )


def _default_provider() -> str | None:
    """Judging is data work, not writing — the same split the annotator follows."""
    value = runtime_config.get("annotate_provider")
    return str(value) if value else None


WORKER = jobs.Worker(
    kind=KIND,
    title="判断词组",
    description="逐处判断动词+小品词的组合在那句话里是不是一个固定词组",
    plan=_plan,
    run_item=_run,
    default_provider=_default_provider,
)


def scan_and_judge(article_ids: list[int], *, title: str | None = None) -> dict[str, Any]:
    """Find candidates in these articles, then queue the judgement."""
    found = sum(find_candidates(article_id) for article_id in article_ids)
    waiting = len(pending(article_ids))
    job_id: int | None = None
    if waiting:
        try:
            job_id = jobs.create(
                KIND, params={"article_ids": [int(i) for i in article_ids]},
                title=title or f"判断词组 {waiting} 处",
            )
            jobs.start(job_id)
        except Exception as exc:  # noqa: BLE001 - nothing to judge is a normal outcome
            log.info("phrases.nothing_to_judge", f"没有需要判断的词组：{exc}")
    log.info(
        "phrases.scanned",
        f"{len(article_ids)} 篇文章里找到 {found} 处新候选，{waiting} 处待判断",
        articles=len(article_ids), found=found, waiting=waiting, job_id=job_id,
    )
    return {"articles": len(article_ids), "found": found,
            "waiting": waiting, "job_id": job_id}


# --------------------------------------------------------------------------- #
# Settling the books
# --------------------------------------------------------------------------- #


def mark_tokens_in_phrases(article_id: int | None = None) -> int:
    """Flag every token that a confirmed phrase covers. Returns rows changed.

    The clean-up the phrase work owes the annotation that came before it. Those
    95,058 sense annotations were made when phrases did not exist, so where the
    text said ``account for`` the annotator could only choose among ``account``'s
    own senses — about 2,181 phrase occurrences over 4,300 tokens, 4.5% of the
    corpus, each one an answer to a question that was never really asked.

    They are flagged rather than re-annotated because re-annotating costs
    thirteen times what the planned dictionary swap costs, and the swap will not
    fix them anyway: §F4 maps old senses onto new ones, which carries the error
    across intact. Waiting for it would have been waiting for nothing.

    Idempotent, and cheap enough to re-run over everything: one UPDATE joined
    against the confirmed spans.
    """
    conn = get_connection("content")
    scope = " AND t.article_id = ?" if article_id else ""
    args: list[Any] = [article_id] if article_id else []
    cursor = conn.execute(
        "UPDATE reading_tokens SET in_phrase = 1 WHERE id IN ("
        "  SELECT t.id FROM reading_tokens t JOIN reading_phrases p"
        "    ON p.article_id = t.article_id AND p.verdict = 1"
        "   AND t.seq BETWEEN p.start_seq AND p.end_seq"
        f"  WHERE t.in_phrase = 0{scope})",  # noqa: S608 - fragment is a literal
        args,
    )
    changed = cursor.rowcount

    # And the other direction: a verdict reversed by a re-run, or a phrase row
    # deleted with its article, must not leave a token flagged for ever.
    cursor = conn.execute(
        "UPDATE reading_tokens SET in_phrase = 0 WHERE id IN ("
        "  SELECT t.id FROM reading_tokens t WHERE t.in_phrase = 1"
        "   AND NOT EXISTS (SELECT 1 FROM reading_phrases p"
        "     WHERE p.article_id = t.article_id AND p.verdict = 1"
        f"      AND t.seq BETWEEN p.start_seq AND p.end_seq){scope})",  # noqa: S608
        args,
    )
    changed += cursor.rowcount
    conn.commit()
    return changed


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def confirmed_for(article_id: int) -> list[dict[str, Any]]:
    """The phrases to show in one article, with their glosses."""
    rows = get_connection("content").execute(
        "SELECT p.phrase, p.start_seq, p.end_seq, p.surface,"
        " d.translation, d.definition"
        " FROM reading_phrases p LEFT JOIN phrases d ON d.phrase = p.phrase"
        " WHERE p.article_id = ? AND p.verdict = 1 ORDER BY p.start_seq",
        (article_id,),
    ).fetchall()
    return [
        {
            "phrase": r["phrase"],
            "surface": r["surface"],
            "start_seq": r["start_seq"],
            "end_seq": r["end_seq"],
            "translation": (r["translation"] or "").split("\n")[0] or None,
            "definition": r["definition"] or None,
        }
        for r in rows
    ]


def stats() -> dict[str, int]:
    conn = get_connection("content")

    def count(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "candidates": count("SELECT COUNT(*) FROM reading_phrases"),
        "confirmed": count("SELECT COUNT(*) FROM reading_phrases WHERE verdict = 1"),
        "rejected": count("SELECT COUNT(*) FROM reading_phrases WHERE verdict = 0"),
        "pending": count("SELECT COUNT(*) FROM reading_phrases WHERE verdict IS NULL"),
        "distinct": count(
            "SELECT COUNT(DISTINCT phrase) FROM reading_phrases WHERE verdict = 1"),
        # How much of the pre-phrase annotation is now known to be describing
        # half of something else.
        "tokens_in_phrase": count("SELECT COUNT(*) FROM reading_tokens WHERE in_phrase = 1"),
        # Sequences that got both verdicts in different sentences. This is the
        # evidence that the sentence is doing the work rather than the string:
        # if every occurrence of a sequence agreed, the model would be matching
        # text and the second stage would be worthless.
        "context_dependent": count(
            "SELECT COUNT(*) FROM (SELECT phrase FROM reading_phrases"
            " WHERE verdict IS NOT NULL GROUP BY phrase"
            " HAVING SUM(verdict = 1) > 0 AND SUM(verdict = 0) > 0)"),
    }
