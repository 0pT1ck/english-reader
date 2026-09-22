"""M2：抽词组，拿库里的中文跟柯林斯 mdx 原文逐字对拍 — P11 §16。

用法::

    uv run python scripts/m2_phrase_glosses.py            # 抽 20 个
    uv run python scripts/m2_phrase_glosses.py --count 30
    uv run python scripts/m2_phrase_glosses.py --phrase "account for"

**这一项要人来看，不是脚本来判。** 脚本能做的只有一件事：
把「库里存的」和「词典里印的」摆在一起，中间不加任何加工。判断留给眼睛——
P10 的同一项就是这么逮到真问题的（解析器把词性标记漏进了英文定义里，
而所有自动断言都是绿的，因为它们检查的是「有没有值」而不是「值对不对」）。

**抽样是分层的**，不是纯随机：四个档各抽一些。A 档（独立词条）最容易对，
D 档（一块讲好几个变体）最容易错，纯随机抽 20 个大概率全是 A 和 B。
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.db import get_connection  # noqa: E402
from backend.modules.senses import collins  # noqa: E402

MDX = ROOT / "data" / "dictionaries" / "collins-cobuild-2012.mdx"


def load_entries(heads: set[str]) -> dict[str, str]:
    from mdict_utils.reader import MDX as Reader

    out: dict[str, str] = {}
    for key, value in Reader(str(MDX)).items():
        head = key.decode("utf-8", "replace").strip().lower()
        if head not in heads:
            continue
        html = value.decode("utf-8", "replace")
        if html.startswith("@@@LINK=") or collins.is_hollow(html):
            continue
        out.setdefault(head, html)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="M2：词组中文对拍")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--phrase", help="只看这一个")
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args()

    conn = get_connection("content")
    if args.phrase:
        rows = conn.execute(
            "SELECT p.text, p.tier, s.id, s.ordinal, s.gloss_zh, s.source_head,"
            " s.source_block FROM phrase_list p JOIN phrase_senses s ON s.phrase_id = p.id"
            " WHERE p.text = ? ORDER BY s.ordinal", (args.phrase,)).fetchall()
        picked = [args.phrase]
    else:
        # 分层抽样：每档按比例抽，至少一个。
        random.seed(args.seed)
        picked = []
        for tier in ("A", "B", "B*", "C", "D"):
            pool = [r["text"] for r in conn.execute(
                "SELECT text FROM phrase_list WHERE tier = ? ORDER BY text", (tier,))]
            if not pool:
                continue
            take = max(1, round(args.count * len(pool) / max(1, conn.execute(
                "SELECT COUNT(*) FROM phrase_list").fetchone()[0])))
            picked.extend(random.sample(pool, min(take, len(pool))))
        placeholders = ",".join("?" * len(picked))
        rows = conn.execute(
            "SELECT p.text, p.tier, s.id, s.ordinal, s.gloss_zh, s.source_head,"  # noqa: S608
            f" s.source_block FROM phrase_list p JOIN phrase_senses s ON s.phrase_id = p.id"
            f" WHERE p.text IN ({placeholders}) ORDER BY p.text, s.ordinal", picked).fetchall()

    heads = {str(r["source_head"]) for r in rows}
    entries = load_entries(heads)
    blocks: dict[tuple[str, int], collins.CollinsSense] = {}
    for head, html in entries.items():
        try:
            for sense in collins.parse_entry(head, html):
                blocks[(head, sense.block_index)] = sense
        except collins.UnknownMarker:
            continue

    print("=" * 74)
    print(f"M2：{len(picked)} 个词组，库里的中文 vs 柯林斯原文")
    print("=" * 74)
    print("看两件事：① 中文是不是这一块印的那一句；② 这一块讲的是不是这个词组。")
    print("第二件才是重点——第一件解析器很少出错，第二件 C／D 档出过错。\n")

    current = None
    for row in rows:
        if row["text"] != current:
            current = row["text"]
            print(f"\n【{current}】  {row['tier']} 档，出自词条「{row['source_head']}」")
        block = blocks.get((str(row["source_head"]), int(row["source_block"])))
        print(f"  {row['ordinal']}. 库里：{row['gloss_zh']}")
        if block is None:
            print("     词典：**这一块在 mdx 里找不到了** ← 有问题，记下来")
            continue
        print(f"     词典：{block.gloss_zh}")
        print(f"     加粗：{'／'.join(block.bolds) or '（没有加粗）'}")
        if block.concept_en:
            print(f"     定义：{block.concept_en[:96]}")
    print("\n" + "=" * 74)
    print("判法：中文与「词典」那行不一致 → 解析出问题；"
          "「加粗」里根本没有这个词组 → 对号出问题（记下来，那是 ㉑ 判漏的）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
