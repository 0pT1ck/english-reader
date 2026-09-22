"""B3 取样：把新旧标注意见不同的地方挑出来，做成盲判用的题目。

**为什么不能按字符串比「一样不一样」**：两套义项集出自不同来源，
同一个意思的中文写法几乎从不逐字相同（旧的是模型写的短语，
柯林斯是词典编辑写的），所以逐字比会把 100% 都判成「不一致」。
这里用汉字集合的 Jaccard 相似度做**粗筛**，只为把「八成真的换了意思」
的那批捞出来——判定谁对谁错是盲判那一步的事，不是这一步的事。

**题目本身必须不带来源**：A/B 哪个是新的由随机数决定，并且答案单独存。
不这么做的话，判断者（包括我）会不自觉地偏向「新的应该更好」。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD_DB = ROOT / "data/backups/content-20260920-063551-before-senses-v5.db"
NEW_DB = ROOT / "data/content.db"

HAN = re.compile(r"[一-鿿]")


def normalise(gloss: str) -> str:
    """把两边的中文摆成同一种样子。

    **这是盲判成立的前提**：旧义项集把中文存成 JSON 数组
    （["稳定的", "稳固的"]），柯林斯存成一行分号串。不抹平的话，
    判断者根本不用读句子——看方括号就知道哪个是旧的。
    第一版取样就是这么漏的，题目印出来才看见。"""
    text = (gloss or "").strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return "；".join(str(x).strip() for x in parsed if str(x).strip())
        except (TypeError, ValueError):
            pass
    return text.replace(";", "；")


def chars(text: str) -> set[str]:
    return set(HAN.findall(text or ""))


def similarity(a: str, b: str) -> float:
    ca, cb = chars(a), chars(b)
    if not ca or not cb:
        return 0.0
    return len(ca & cb) / len(ca | cb)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--controls", type=int, default=15,
                    help="阳性对照条数：其中一个选项换成同一个词的另一个义项，"
                         "而那个义项在这句里明显不对")
    ap.add_argument("--threshold", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=20260922)
    ap.add_argument("--out", type=Path, default=ROOT / "data/b3-items.json")
    args = ap.parse_args()

    rng = random.Random(args.seed)

    old = sqlite3.connect(OLD_DB); old.row_factory = sqlite3.Row
    new = sqlite3.connect(NEW_DB); new.row_factory = sqlite3.Row

    print("读旧标注 …", flush=True)
    old_rows = {r["id"]: (r["sense_id"], r["gloss_zh"], r["concept_en"])
                for r in old.execute(
                    "SELECT t.id, t.sense_id, s.gloss_zh, s.concept_en"
                    "  FROM reading_tokens t JOIN senses s ON s.id = t.sense_id"
                    " WHERE t.kind = 'content' AND t.sense_id > 0")}
    print(f"  旧库里有标注的 token：{len(old_rows)}", flush=True)

    print("读新标注 …", flush=True)
    new_rows = list(new.execute(
        "SELECT t.id, t.article_id, t.sentence_id, t.surface, t.headword,"
        "       t.sense_id, s.gloss_zh, s.concept_en, s.ordinal"
        "  FROM reading_tokens t JOIN senses s ON s.id = t.sense_id"
        " WHERE t.kind = 'content' AND t.sense_id > 0"))
    print(f"  新库里有标注的 token：{len(new_rows)}", flush=True)

    sentences = {r["id"]: r["text"] for r in new.execute(
        "SELECT id, text FROM reading_sentences")}

    both, candidates = 0, []
    for r in new_rows:
        prior = old_rows.get(r["id"])
        if prior is None:
            continue
        both += 1
        sim = similarity(normalise(prior[1]), normalise(r["gloss_zh"]))
        if sim <= args.threshold:
            candidates.append({
                "token_id": r["id"], "headword": r["headword"],
                "surface": r["surface"],
                "sentence": sentences.get(r["sentence_id"], ""),
                "old_gloss": normalise(prior[1]), "old_en": prior[2],
                "new_gloss": normalise(r["gloss_zh"]), "new_en": r["concept_en"],
                "new_sense_id": r["sense_id"], "similarity": round(sim, 3),
            })

    print(f"新旧都标了的：{both}；中文相似度 ≤ {args.threshold} 的：{len(candidates)}"
          f"（{len(candidates) / max(both, 1):.1%}）")

    # 一个词最多抽两处，免得 100 道题里半数都在问同一个高频词
    rng.shuffle(candidates)
    per_word: dict[str, int] = {}
    picked = []
    for cand in candidates:
        if per_word.get(cand["headword"], 0) >= 2:
            continue
        per_word[cand["headword"]] = per_word.get(cand["headword"], 0) + 1
        picked.append(cand)
        if len(picked) >= args.count:
            break
    print(f"抽出 {len(picked)} 道，覆盖 {len(per_word)} 个词")

    # 阳性对照：把新义项换成同一个词的另一条义项——**挑相似度最低的那条**，
    # 也就是意思差得最远的。判断者要是连它都选，说明这套判法看不出对错。
    controls = []
    pool = [c for c in picked]
    rng.shuffle(pool)
    for cand in pool:
        if len(controls) >= args.controls:
            break
        others = list(new.execute(
            "SELECT id, gloss_zh, concept_en FROM senses"
            " WHERE headword = ? AND id != ?", (cand["headword"], cand["new_sense_id"])))
        if not others:
            continue
        worst = min(others, key=lambda o: similarity(normalise(o["gloss_zh"]),
                                                     cand["new_gloss"]))
        controls.append({**cand, "control": True,
                         "decoy_gloss": normalise(worst["gloss_zh"]),
                         "decoy_en": worst["concept_en"]})
    print(f"阳性对照 {len(controls)} 道")

    items = []
    for index, cand in enumerate(picked):
        # A/B 顺序随机，来源单独存——题面里不许出现「新」「旧」
        flip = rng.random() < 0.5
        items.append({
            "id": f"q{index:03d}", "kind": "real",
            "headword": cand["headword"], "surface": cand["surface"],
            "sentence": cand["sentence"],
            "A": cand["old_gloss"] if flip else cand["new_gloss"],
            "B": cand["new_gloss"] if flip else cand["old_gloss"],
            "A_en": cand["old_en"] if flip else cand["new_en"],
            "B_en": cand["new_en"] if flip else cand["old_en"],
            # **flip 为真时 A 装的是旧释义**，所以 new 是 B。第一版把这两个写反了，
            # 整份结论跟着反过来，而它只在「长度基线算出 17%（柯林斯明明更长）」
            # 这个反常数字上露过一次头——反常的数字要当场追，不能记一句就放过。
            "_answer": {"new": "B" if flip else "A", "old": "A" if flip else "B"},
            "_similarity": cand["similarity"], "_token": cand["token_id"],
        })
    for index, cand in enumerate(controls):
        flip = rng.random() < 0.5
        items.append({
            "id": f"c{index:03d}", "kind": "control",
            "headword": cand["headword"], "surface": cand["surface"],
            "sentence": cand["sentence"],
            "A": cand["new_gloss"] if flip else cand["decoy_gloss"],
            "B": cand["decoy_gloss"] if flip else cand["new_gloss"],
            "A_en": cand["new_en"] if flip else cand["decoy_en"],
            "B_en": cand["decoy_en"] if flip else cand["new_en"],
            "_answer": {"good": "A" if flip else "B", "decoy": "B" if flip else "A"},
            "_token": cand["token_id"],
        })
    rng.shuffle(items)

    args.out.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {args.out}（{len(items)} 道，其中对照 {len(controls)} 道）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
