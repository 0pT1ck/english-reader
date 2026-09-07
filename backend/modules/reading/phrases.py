"""Phrasal verbs and fixed expressions — finding them, and asking about them.

The problem this exists for: you tap ``account`` and are told 账目／账户, when
the sentence says ``renewable sources account for a third of supply``. About
seven times per article the word under your finger is half of something else.
Before this, the annotator simply declined those — ``get up``, ``known as``,
``at least`` all turned up in the missing-sense report — so they were visible as
a symptom long before they were handled.

**No inventory of "phrases worth learning".** That was the original plan and it
was wrong: if you do not recognise a phrase you mark it, exactly as with a word,
and whether some syllabus lists it changes nothing. That removed the whole
research problem — a curated list comparable to the sense sets, which cost two
phases — and left three much smaller ones: find them, gloss them, let them be
marked.

**Two stages, because the two questions are different.**

1. *Structural.* A verb followed by a particle or preposition, the pair existing
   in the dictionary. Cheap, no model, and it is what keeps ``to be``,
   ``the world`` and ``there is`` out — none of them starts with a verb. Matching
   the dictionary alone finds 43 such "phrases" per article, of which the top
   are ``to be`` 69 times and ``have been`` 57; with this filter it is 6.9.

2. *Contextual.* Whether this occurrence is a unit at all, which only the
   sentence can settle::

       He ran into an old friend downtown.   → 遇见, a phrase
       He ran into the room and shut it.     → 跑进, two words

   Asked as a closed question about one occurrence — the form measured at 100%
   recall and 0% false positives on planted collocation errors — a fast model
   gets 10/10 on real phrases and 8–9/10 on the two negative groups, and two of
   those "errors" were mislabelled by the test rather than by the model.

The criterion given to the model is transparency, not worth: *can the meaning be
read off the parts?* ``walk to the station`` can, ``account for`` cannot. That
keeps it answering an objective question, which is the same reason the sense
work asks "is this general written English" rather than "is this worth learning".
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
#: `need to`, `want to`, `have to` — spaCy tags the infinitive marker PART and
#: a real preposition ADP, and that one distinction cut 741 candidates to 531.
PARTICLE_POS = frozenset({"ADP", "ADV"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Stage 1 — structural candidates
# --------------------------------------------------------------------------- #


def find_candidates(article_id: int) -> int:
    """Propose phrase spans in one article. Returns how many are new.

    Runs over tokens already stored, so it needs no model and no re-analysis —
    which is what makes retrofitting the whole corpus cheap: the expensive pass
    (95,058 contextual sense annotations, 4.6M input tokens) stays untouched.
    """
    conn = get_connection("learning")
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
            # Adjacent in the text, not merely adjacent in the token list —
            # a sentence boundary between them is not a phrase.
            if any(following[k]["sentence_id"] != token["sentence_id"]
                   for k in range(len(following))):
                continue

            phrase = " ".join([token["headword"].lower()]
                              + [t["surface"].lower() for t in following])
            entry = conn.execute(
                "SELECT phrase FROM dict.phrases WHERE phrase = ?", (phrase,)
            ).fetchone()
            if entry is None:
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
    conn = get_connection("learning")
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
    conn = get_connection("learning")
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

    return jobs.ItemOutcome(
        result=json.dumps({"kept": kept, "dropped": dropped}, ensure_ascii=False),
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
    )


def _default_provider() -> str | None:
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
        except Exception as exc:  # noqa: BLE001 - nothing to judge is normal
            log.info("phrases.nothing_to_judge", f"没有需要判断的词组：{exc}")
    log.info(
        "phrases.scanned",
        f"{len(article_ids)} 篇文章里找到 {found} 处新候选，{waiting} 处待判断",
        articles=len(article_ids), found=found, waiting=waiting, job_id=job_id,
    )
    return {"articles": len(article_ids), "found": found,
            "waiting": waiting, "job_id": job_id}


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def confirmed_for(article_id: int) -> list[dict[str, Any]]:
    """The phrases to show in one article, with their glosses."""
    rows = get_connection("learning").execute(
        "SELECT p.phrase, p.start_seq, p.end_seq, p.surface,"
        " d.translation, d.definition"
        " FROM reading_phrases p LEFT JOIN dict.phrases d ON d.phrase = p.phrase"
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
    conn = get_connection("learning")

    def count(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "candidates": count("SELECT COUNT(*) FROM reading_phrases"),
        "confirmed": count("SELECT COUNT(*) FROM reading_phrases WHERE verdict = 1"),
        "rejected": count("SELECT COUNT(*) FROM reading_phrases WHERE verdict = 0"),
        "pending": count("SELECT COUNT(*) FROM reading_phrases WHERE verdict IS NULL"),
        "distinct": count(
            "SELECT COUNT(DISTINCT phrase) FROM reading_phrases WHERE verdict = 1"),
    }
