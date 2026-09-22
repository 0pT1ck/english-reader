"""Ask, one candidate at a time, whether a Collins block is about a phrase — 决定 ㉑.

Run::

    uv run python scripts/judge_phrase_candidates.py            # judge, then promote
    uv run python scripts/judge_phrase_candidates.py --controls-only
    uv run python scripts/judge_phrase_candidates.py --promote-only

**Why a model at all.** Tiers A and B of §13 match a phrase to a block by exact
string and are right (14 out of 14 sampled). Tiers C and D need placeholders
dropped or several bold runs joined, and there about a third land on the wrong
block: ``do harm to`` matched a block glossed 「也许值得（做某事）」, ``wait
for`` one glossed 「注意了，听好了」. Taking them wholesale would put wrong
Chinese in front of the reader, which is the one thing this project exists to
avoid; dropping them wholesale loses ``take into account``.

**The question is closed, and that is the whole design.** 踩过的坑 §1.3: asked
to *find the problems*, a model invents them — it gave exam-committee prose a
mean of 5.1 imaginary errors. Asked whether one named thing is the case, the
same model scored 8/8 with no false positives on planted errors. So it is never
asked "which block is this phrase in", only "is this block about this phrase".

**Controls in both directions, or the run does not count** (踩过的坑 §4.3). A
positive control is a tier-B pair, known to be right. A negative control is a
real phrase paired with a block from an unrelated entry, known to be wrong.
Without positives, "it said yes to everything" reads as success; without
negatives, so does "it said no to everything". If either side fails, this script
refuses to promote anything and says so.

**Resumable** (决定 ㉒): the verdict lives on the row, so a run that stops when
the quota runs out picks up exactly where it left off. Nothing is judged twice.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.db import get_connection  # noqa: E402
from backend.core.registry import discover_modules  # noqa: E402
from backend.modules.llm import client, providers  # noqa: E402
from backend.modules.llm.parsing import json_object  # noqa: E402
from backend.modules.phrases import repository  # noqa: E402

#: Small enough that the reply stays short and every item gets attention. P1c
#: measured a fast model drifting in the second half of a long structured list,
#: which is why this is not 100.
BATCH = 20
CONTROLS = 30

SYSTEM = (
    "你是一位英语词典编者，判断柯林斯词典里的某一个义项块讲的是不是某个指定的词组。"
    "你只做这一个判断，不改写、不补充。"
)

INSTRUCTION = """\
下面每一条给出：一个词组，以及柯林斯词典里某一块的加粗搭配、中文释义、英文定义。

请逐条判断：**这一块讲的是不是这个词组？**

判断的依据是这一块讲的内容，不是这个词组本身好不好、常不常用。
- 加粗里写的是同一个说法（允许 sth／sb／one's 这类占位词和单复数、时态的差别）→ true
- 这一块讲的其实是另一个说法，只是碰巧含有相同的词 → false
  例如词组是 on one's account（因为某人的缘故），而这一块讲的是 on account（赊账）→ false

只输出 JSON：{"verdicts": {"1": true, "2": false, ...}}，每一条都要有判断。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_controls(conn) -> int:
    """Mix in pairs whose answer is already known, both ways round."""
    if conn.execute("SELECT COUNT(*) FROM phrase_candidates WHERE control > 0").fetchone()[0]:
        return 0
    rows = conn.execute(
        "SELECT p.text, s.source_head, s.source_block, s.gloss_zh"
        " FROM phrase_list p JOIN phrase_senses s ON s.phrase_id = p.id"
        " WHERE p.tier = 'B' ORDER BY p.text"
    ).fetchall()
    if len(rows) < CONTROLS * 2:
        raise SystemExit("tier B 的行数不够种对照，先跑 import_phrases.py")

    random.seed(11)
    sample = random.sample(list(rows), CONTROLS * 2)
    added = 0
    for row in sample[:CONTROLS]:
        repository.store_candidate(
            str(row["text"]), tier="control+", head=str(row["source_head"]),
            block=int(row["source_block"]), bold=str(row["text"]),
            gloss_zh=str(row["gloss_zh"]), level=None, control=True, conn=conn)
        conn.execute(
            "UPDATE phrase_candidates SET control = 1 WHERE text = ?"
            " AND source_head = ? AND source_block = ?",
            (str(row["text"]), str(row["source_head"]), int(row["source_block"])))
        added += 1
    # A phrase paired with somebody else's block: known to be wrong.
    others = sample[CONTROLS:]
    for offset, row in enumerate(others):
        wrong = others[(offset + 7) % len(others)]
        if str(wrong["source_head"]) == str(row["source_head"]):
            continue
        repository.store_candidate(
            str(row["text"]), tier="control-", head=str(wrong["source_head"]),
            block=int(wrong["source_block"]), bold=str(wrong["text"]),
            gloss_zh=str(wrong["gloss_zh"]), level=None, control=True, conn=conn)
        conn.execute(
            "UPDATE phrase_candidates SET control = 2 WHERE text = ?"
            " AND source_head = ? AND source_block = ?",
            (str(row["text"]), str(wrong["source_head"]), int(wrong["source_block"])))
        added += 1
    conn.commit()
    return added


def _prompt(items) -> str:
    lines = []
    for number, row in enumerate(items, start=1):
        lines.append(
            f"{number}. 词组：{row['text']}\n"
            f"   加粗搭配：{row['bold']}\n"
            f"   中文释义：{row['gloss_zh']}\n"
            f"   出自词条：{row['source_head']}"
        )
    return INSTRUCTION + "\n\n" + "\n\n".join(lines)


def judge(conn, provider, *, limit: int | None) -> dict[str, int]:
    pending = conn.execute(
        "SELECT id, text, bold, gloss_zh, source_head FROM phrase_candidates"
        " WHERE verdict IS NULL ORDER BY control DESC, id"
    ).fetchall()
    if limit:
        pending = pending[:limit]
    done = failed = 0
    for start in range(0, len(pending), BATCH):
        batch = pending[start:start + BATCH]
        try:
            completion = client.complete(
                provider,
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": _prompt(batch)}],
                max_tokens=1200, temperature=0.1, json_mode=True)
            verdicts = json_object(completion.text).get("verdicts") or {}
        except Exception as exc:  # noqa: BLE001 - the run must survive one bad batch
            print(f"  批次 {start // BATCH + 1} 失败：{exc}")
            failed += len(batch)
            continue
        for number, row in enumerate(batch, start=1):
            answer = verdicts.get(str(number), verdicts.get(number))
            if answer is None:
                failed += 1
                continue
            conn.execute(
                "UPDATE phrase_candidates SET verdict = ?, judged_at = ? WHERE id = ?",
                (1 if answer else 0, _now(), int(row["id"])))
            done += 1
        conn.commit()
        print(f"  已判 {done}/{len(pending)}", end="\r", flush=True)
    print()
    return {"judged": done, "failed": failed}


def controls_report(conn) -> dict[str, float]:
    rows = conn.execute(
        "SELECT control, verdict, COUNT(*) AS n FROM phrase_candidates"
        " WHERE control > 0 AND verdict IS NOT NULL GROUP BY control, verdict"
    ).fetchall()
    tally = {(int(r["control"]), int(r["verdict"])): int(r["n"]) for r in rows}
    positives = tally.get((1, 1), 0) + tally.get((1, 0), 0)
    negatives = tally.get((2, 1), 0) + tally.get((2, 0), 0)
    return {
        "positive_total": positives,
        "positive_right": tally.get((1, 1), 0),
        "negative_total": negatives,
        "negative_right": tally.get((2, 0), 0),
    }


def promote(conn) -> dict[str, int]:
    """Accepted candidates join the list; rejected ones are archived with a reason."""
    from backend.modules.senses import collins
    from mdict_utils.reader import MDX

    accepted = conn.execute(
        "SELECT * FROM phrase_candidates WHERE verdict = 1 AND control = 0"
        " ORDER BY text, source_head, source_block"
    ).fetchall()
    if not accepted:
        return {"promoted": 0, "rejected": 0}

    wanted = {str(r["source_head"]) for r in accepted}
    parsed: dict[str, list] = {}
    for key, value in MDX("data/dictionaries/collins-cobuild-2012.mdx").items():
        head = key.decode("utf-8", "replace").strip().lower()
        if head not in wanted:
            continue
        html = value.decode("utf-8", "replace")
        if html.startswith("@@@LINK=") or collins.is_hollow(html):
            continue
        parsed.setdefault(head, collins.parse_entry(head, html))

    by_phrase: dict[str, list] = {}
    for row in accepted:
        by_phrase.setdefault(str(row["text"]), []).append(row)

    promoted = 0
    for text, rows in by_phrase.items():
        senses = []
        for row in rows:
            head, block = str(row["source_head"]), int(row["source_block"])
            for sense in parsed.get(head, []):
                if sense.block_index != block or not sense.gloss_zh:
                    continue
                senses.append({
                    "gloss_zh": sense.gloss_zh, "concept_en": sense.concept_en,
                    "pos": sense.pos, "pos_zh": sense.pos_zh,
                    "register": sense.register, "pattern": sense.pattern,
                    "source_head": head, "source_block": block,
                    "source_ordinal": sense.ordinal,
                })
        if not senses:
            continue
        phrase_id = repository.upsert_phrase(
            text, source_key=str(rows[0]["source_head"]),
            tier=str(rows[0]["tier"]), level=rows[0]["level"], conn=conn)
        repository.store_senses(phrase_id, senses, conn=conn)
        promoted += 1

    rejected = conn.execute(
        "SELECT text, level FROM phrase_candidates WHERE verdict = 0 AND control = 0"
    ).fetchall()
    repository.store_excluded(
        ((str(r["text"]), "judged_out", "柯林斯里找得到相近的块，但判定说那一块讲的不是它",
          r["level"]) for r in rejected), conn=conn)
    conn.commit()
    return {"promoted": promoted, "rejected": len(rejected)}


def main() -> int:
    parser = argparse.ArgumentParser(description="判定词组候选（㉑）")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--limit", type=int, default=None, help="这一趟最多判几条")
    parser.add_argument("--controls-only", action="store_true")
    parser.add_argument("--promote-only", action="store_true")
    args = parser.parse_args()

    discover_modules()
    conn = get_connection("content")
    provider = providers.get(args.provider) if args.provider else providers.default_provider()

    if not args.promote_only:
        seeded = seed_controls(conn)
        if seeded:
            print(f"种了 {seeded} 条对照（阳性 ＋ 阴性）")
        started = time.time()
        limit = CONTROLS * 2 if args.controls_only else args.limit
        print(f"用 {provider.model} 判定，每批 {BATCH} 条 …")
        print(" ", judge(conn, provider, limit=limit), f"{time.time() - started:.0f}s")

    report = controls_report(conn)
    print("对照：", report)
    ok = (report["positive_total"] and report["negative_total"]
          and report["positive_right"] / report["positive_total"] >= 0.9
          and report["negative_right"] / report["negative_total"] >= 0.9)
    if not ok:
        print("⚠️ 对照没过关，**不提升任何候选**——这一趟的判定不能用。")
        return 1
    pending = conn.execute(
        "SELECT COUNT(*) FROM phrase_candidates WHERE verdict IS NULL").fetchone()[0]
    if pending and not args.promote_only:
        print(f"还剩 {pending} 条没判（额度用完就再跑一次），先不提升。")
        return 0
    print("提升：", promote(conn))
    print("库里现在：", repository.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
