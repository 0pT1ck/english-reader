"""Annotate one article twice under different settings and diff it — P10.

Usage::

    uv run python scripts/compare_annotation.py 491 35 100

**Why this exists.** The obvious quality number for annotation is "how often
did the model say none of these senses fit", and P10 spent most of a day
steering by it before noticing it measures the wrong thing: **it counts
whether an answer was given, not whether the answer is right**. A setting that
makes the model guess more scores better on it while getting more tokens wrong.
坑 §4.3 in one line — a metric that cannot separate "correct" from "guessed"
is not a metric.

What this does instead is annotate the same article twice and compare token by
token. Three numbers come out, and each means something different:

* **agreement** — both settings picked the same sense. Not proof either is
  right, but it does say the answer does not depend on how the batch was cut.
* **only-A / only-B** — one gave a sense where the other said "none fits".
  **This is where a guessier setting shows up**, and it is invisible to the
  headline number.
* **disagreements** — both answered, differently. At least one is wrong, and
  these are listed in full because judging them needs a human (or at least a
  second model, with the caveat P3's acceptance E recorded: the judges share
  blind spots).

Measured on article 491, batch 35 against batch 400: 93.5% agreement, and
**every one of the 22 one-sided answers came from batch 400** — of which 11
were right, 4 wrong (`crystal clear`, `in addition`, `went away` — all phrases,
where "none fits" was the correct answer) and 5 arguable.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core import runtime_config  # noqa: E402
from backend.core.db import get_connection  # noqa: E402
from backend.core.registry import run_core_migrations  # noqa: E402
from backend.modules.llm import jobs, providers  # noqa: E402
from backend.modules.llm import module as _llm_module  # noqa: E402,F401
from backend.modules.reading import annotate, phrases  # noqa: E402
from backend.modules.reading import module as _reading_module  # noqa: E402,F401
from backend.modules.senses import module as _senses_module  # noqa: E402,F401

NO_SENSE_FITS = annotate.NO_SENSE_FITS


def annotate_once(article_id: int, batch: int, provider) -> tuple[dict[int, int], int]:
    """Wipe this article's annotations and redo them at one batch size."""
    conn = get_connection("content")
    conn.execute(
        "UPDATE reading_tokens SET sense_id = NULL, sense_ordinal = NULL"
        " WHERE article_id = ?", (article_id,))
    conn.commit()
    runtime_config.set("annotate_batch_words", batch)

    plan = annotate._plan({"article_id": article_id})
    for _key, payload in plan:
        annotate._run(provider, payload, {})

    return {
        row["id"]: (row["sense_id"], row["sense_ordinal"])
        for row in conn.execute(
            "SELECT id, sense_id, sense_ordinal FROM reading_tokens"
            " WHERE article_id = ? AND kind = 'content'", (article_id,))
    }, len(plan)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("article", type=int)
    parser.add_argument("batch_a", type=int)
    parser.add_argument("batch_b", type=int)
    parser.add_argument("--keep", type=int,
                        help="跑完把批次设回这个值（默认设回 batch_b）")
    args = parser.parse_args()

    run_core_migrations()
    jobs.register_worker(annotate.WORKER)
    jobs.register_worker(phrases.WORKER)

    conn = get_connection("content")
    provider_id = runtime_config.get(jobs.provider_key(annotate.KIND))
    provider = (providers.get(provider_id) if provider_id
                else providers.default_provider())

    started = time.time()
    first, calls_a = annotate_once(args.article, args.batch_a, provider)
    second, calls_b = annotate_once(args.article, args.batch_b, provider)
    runtime_config.set("annotate_batch_words", args.keep or args.batch_b)

    answered = lambda v: v[0] is not None and v[0] > 0  # noqa: E731
    both = [k for k in first if answered(first[k]) and answered(second[k])]
    same = [k for k in both if first[k][1] == second[k][1]]
    diff = [k for k in both if first[k][1] != second[k][1]]
    only_a = [k for k in first if answered(first[k]) and not answered(second[k])]
    only_b = [k for k in first if not answered(first[k]) and answered(second[k])]

    print(f"\n文章 {args.article}，用时 {time.time() - started:.0f}s")
    print(f"  批次 {args.batch_a}：{calls_a} 次调用   "
          f"批次 {args.batch_b}：{calls_b} 次调用")
    print(f"  两边都给了义项 {len(both)} → 一致 {len(same)}"
          f"（{len(same) / max(1, len(both)) * 100:.1f}%）、不一致 {len(diff)}")
    print(f"  只有批次 {args.batch_a} 给了义项：{len(only_a)}")
    print(f"  只有批次 {args.batch_b} 给了义项：{len(only_b)}")
    print("  ↑ **这两行是重点**：单边作答就是「更爱猜」的地方，"
          "而它在「没贴合率」里看不见")

    def gloss(headword: str, ordinal: int | None) -> str:
        if ordinal is None:
            return ""
        row = conn.execute(
            "SELECT gloss_zh FROM senses WHERE headword = ? AND ordinal = ?",
            (headword, ordinal)).fetchone()
        return row["gloss_zh"][:24] if row else ""

    for label, keys in (("不一致", diff),
                        (f"只有批次 {args.batch_b} 作答", only_b),
                        (f"只有批次 {args.batch_a} 作答", only_a)):
        if not keys:
            continue
        print(f"\n{label}（{len(keys)} 个，最多列 14）：")
        for key in keys[:14]:
            row = conn.execute(
                "SELECT t.surface, t.headword, q.text FROM reading_tokens t"
                " JOIN reading_sentences q ON q.id = t.sentence_id"
                " WHERE t.id = ?", (key,)).fetchone()
            a_text = gloss(row["headword"], first[key][1])
            b_text = gloss(row["headword"], second[key][1])
            print(f"  「{row['surface']}」 {args.batch_a}→{a_text or '没贴合'}"
                  f"   {args.batch_b}→{b_text or '没贴合'}")
            print(f"      {row['text'][:116]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
