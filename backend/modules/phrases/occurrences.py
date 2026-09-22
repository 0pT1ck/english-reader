"""Where the phrases on the list actually appear in the corpus — P11 步骤 2.1.

出现位置 occurrence / 表层形 surface form / 词形还原 lemma

**This replaces a filter and a model with a lookup.** P2 proposed candidates
structurally (a verb followed by a particle, the pair existing in ECDICT) and
then paid a model to decide, per occurrence, whether the run was a unit at all.
That recognised about a third of them and the verdicts were not stable. The list
built in 步骤 1 answers "is this a phrase" once, for the whole corpus; all that
is left here is finding the strings, which is exact and free.

**Two forms are looked up at every position, not one.** 踩过的坑 §5.4: all 84
occurrences of ``according to`` were missed because spaCy lemmatises
``according`` to ``accord``, and ``accord to`` is not a phrase anybody lists.
Surface form and headword are therefore both tried, in that order — surface
first because ``accounts for`` should match ``account for`` through the lemma,
while ``leaves`` in ``fallen leaves`` must not become ``leave``.

**Longest match wins, and phrases do not cross punctuation.** ``in spite of``
and ``in spite`` are both on the list; matching the shorter one first would
leave ``of`` dangling and change what the reader is shown. A comma between two
words means they are not a unit no matter what the list says.

**What a row does not say.** Whether *this* occurrence is really the phrase is
not decided here — that is the annotator's job (决定 ④), and its answer of 0
means "these are two ordinary words standing next to each other" (§7b:
``He ran into the room``). A row with ``sense_id IS NULL`` has not been asked
about yet; the two states are different and must stay so.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from backend.core.db import get_connection
from backend.core.logging import get_logger
from backend.modules.phrases.matching import normalise

log = get_logger("phrases.occurrences")

#: Token kinds a phrase may be made of. **Everything else breaks the span** —
#: and the important one is `nonword`, which is what punctuation is classified
#: as (`ingest._classify`). The first version listed `punct`, a kind this
#: project does not have, so nothing ever broke a span and the corpus came back
#: with `at - home` and `talked about .` — matches that read across a comma or
#: a dash. **A name that is never equal to anything fails silently**: no error,
#: just spans that quietly include the punctuation between them.
PHRASE_KINDS = frozenset({"content", "function", "proper"})


def load_list(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Phrase text -> phrase id, for every phrase on the list."""
    conn = conn or get_connection("content")
    return {str(r["text"]): int(r["id"])
            for r in conn.execute("SELECT id, text FROM phrase_list")}


def _variants(tokens: list[sqlite3.Row], start: int, length: int) -> list[str]:
    """The two readings of one span: as written, and lemmatised.

    Returns at most two strings, deduplicated — most spans read the same both
    ways and there is no point looking twice.
    """
    surface = " ".join(str(t["surface"] or "") for t in tokens[start:start + length])
    lemma = " ".join(str(t["headword"] or t["surface"] or "")
                     for t in tokens[start:start + length])
    out = [normalise(surface)]
    normalised_lemma = normalise(lemma)
    if normalised_lemma != out[0]:
        out.append(normalised_lemma)
    return out


def scan_article(article_id: int, phrases: dict[str, int], max_length: int,
                 conn: sqlite3.Connection | None = None) -> list[dict]:
    """Every occurrence in one article, left to right, longest match first."""
    conn = conn or get_connection("content")
    tokens = conn.execute(
        "SELECT id, seq, surface, headword, kind, sentence_id"
        " FROM reading_tokens WHERE article_id = ? ORDER BY seq",
        (article_id,),
    ).fetchall()

    found: list[dict] = []
    index = 0
    while index < len(tokens):
        if str(tokens[index]["kind"]) not in PHRASE_KINDS:
            index += 1
            continue
        hit = None
        longest = min(max_length, len(tokens) - index)
        for length in range(longest, 1, -1):
            span = tokens[index:index + length]
            if any(str(t["kind"]) not in PHRASE_KINDS for t in span):
                continue
            for text in _variants(tokens, index, length):
                if text in phrases:
                    hit = (length, text, span)
                    break
            if hit:
                break
        if not hit:
            index += 1
            continue
        length, text, span = hit
        found.append({
            "phrase": text,
            "phrase_id": phrases[text],
            "start_seq": int(span[0]["seq"]),
            "end_seq": int(span[-1]["seq"]),
            "sentence_id": int(span[0]["sentence_id"]),
            "surface": " ".join(str(t["surface"] or "") for t in span),
        })
        index += length
    return found


def rebuild(article_ids: list[int] | None = None,
            conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Rebuild the occurrence table from the list.

    **Safe to repeat.** Occurrence rows carry no identity anybody outside this
    database has seen — the contract sends a phrase's text and its token span,
    never this row's id — so rebuilding them is not what 架构铁律 5 is about.
    What must survive is the *phrase sense* id, and that lives in another table.
    """
    conn = conn or get_connection("content")
    phrases = load_list(conn)
    if not phrases:
        raise RuntimeError("清单是空的，先跑 import_phrases.py")
    max_length = max(len(text.split()) for text in phrases)

    if article_ids is None:
        article_ids = [int(r["id"]) for r in
                       conn.execute("SELECT id FROM reading_articles ORDER BY id")]

    now = datetime.now(timezone.utc).isoformat()
    total = 0
    conn.execute("DELETE FROM reading_phrases")
    for article_id in article_ids:
        rows = scan_article(article_id, phrases, max_length, conn)
        conn.executemany(
            "INSERT INTO reading_phrases (article_id, sentence_id, phrase, phrase_id,"
            " start_seq, end_seq, surface, created_at) VALUES (?,?,?,?,?,?,?,?)",
            [(article_id, r["sentence_id"], r["phrase"], r["phrase_id"],
              r["start_seq"], r["end_seq"], r["surface"], now) for r in rows],
        )
        total += len(rows)
    conn.commit()
    log.info("phrases.rebuilt", f"重扫 {len(article_ids)} 篇，命中 {total} 处",
             articles=len(article_ids), occurrences=total)
    return {"articles": len(article_ids), "occurrences": total}


def mark_tokens(article_id: int | None = None,
                conn: sqlite3.Connection | None = None) -> int:
    """Flag the tokens a confirmed occurrence covers. Returns rows changed.

    **The criterion changed with P11, and the direction matters.** It used to be
    ``verdict = 1`` — a model had said "yes, a phrase here". It is now
    ``sense_id > 0`` — the annotator picked one of the phrase's meanings. A 0
    means the annotator looked at the sentence and said these are two ordinary
    words (§7b), so those tokens must stay unflagged and keep showing their own
    senses.

    Both directions are done, because a rebuilt occurrence table can leave a
    token flagged for a phrase that is no longer there — and a token wrongly
    flagged shows the reader nothing at all where its gloss used to be, which is
    a silent failure rather than a visible one.

    Idempotent, and cheap enough to run over the whole corpus.
    """
    conn = conn or get_connection("content")
    scope = " AND t.article_id = ?" if article_id else ""
    args: list[int] = [article_id] if article_id else []
    changed = conn.execute(
        "UPDATE reading_tokens SET in_phrase = 1 WHERE id IN ("
        "  SELECT t.id FROM reading_tokens t JOIN reading_phrases p"
        "    ON p.article_id = t.article_id AND p.sense_id > 0"
        "   AND t.seq BETWEEN p.start_seq AND p.end_seq"
        f"  WHERE t.in_phrase = 0{scope})",  # noqa: S608 - fragment is a literal
        args,
    ).rowcount
    changed += conn.execute(
        "UPDATE reading_tokens SET in_phrase = 0 WHERE id IN ("
        "  SELECT t.id FROM reading_tokens t WHERE t.in_phrase = 1"
        "   AND NOT EXISTS (SELECT 1 FROM reading_phrases p"
        "     WHERE p.article_id = t.article_id AND p.sense_id > 0"
        f"      AND t.seq BETWEEN p.start_seq AND p.end_seq){scope})",  # noqa: S608
        args,
    ).rowcount
    conn.commit()
    log.info("phrases.tokens_marked", f"{changed} 个 token 的「在词组里」标记有变",
             changed=changed, article_id=article_id)
    return changed
