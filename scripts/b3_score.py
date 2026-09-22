"""B3 记分：把盲判结果读成一句能写进文档的话。

**先看对照，再看结论**：阳性对照答错得多，后面那些数就都不用看了——
那说明这套判法分不出对错，而不是说明标注好或不好（坑 §4.3）。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    items = {i["id"]: i for i in
             json.loads((ROOT / "data/b3-items.json").read_text(encoding="utf-8"))}
    first = json.loads((ROOT / "data/b3-model.json").read_text(encoding="utf-8"))
    swapped = json.loads((ROOT / "data/b3-model-swapped.json").read_text(encoding="utf-8"))

    print("=" * 66)
    print("  ① 阳性对照——判法分不分得出对错")
    print("=" * 66)
    controls = [i for i in items.values() if i["kind"] == "control"]
    tally = Counter()
    for item in controls:
        verdict = first.get(item["id"])
        if verdict == item["_answer"]["good"]:
            tally["选中真标注"] += 1
        elif verdict == item["_answer"]["decoy"]:
            tally["选中假选项"] += 1
        else:
            tally[f"答「{verdict}」"] += 1
    for key, count in tally.most_common():
        print(f"  {key}：{count} / {len(controls)}")

    print()
    print("=" * 66)
    print("  ② 位置偏好——A/B 对调之后还答得一样吗")
    print("=" * 66)
    same = sum(1 for k in items if first.get(k) == swapped.get(k))
    flipped_to_a = sum(1 for k in items
                       if first.get(k) == "B" and swapped.get(k) == "A")
    flipped_to_b = sum(1 for k in items
                       if first.get(k) == "A" and swapped.get(k) == "B")
    print(f"  两次一致：{same} / {len(items)}（{same / len(items):.0%}）")
    print(f"  B→A {flipped_to_a} 道，A→B {flipped_to_b} 道")

    print()
    print("=" * 66)
    print("  ③ 一百道真题：新旧谁更贴合这句")
    print("=" * 66)
    reals = [i for i in items.values() if i["kind"] == "real"]
    score = Counter()
    both_runs = Counter()
    for item in reals:
        verdict = first.get(item["id"])
        if verdict == item["_answer"]["new"]:
            score["新标注更贴合"] += 1
        elif verdict == item["_answer"]["old"]:
            score["旧标注更贴合"] += 1
        else:
            score[{"both": "两个都说得通", "neither": "两个都不对"}.get(verdict, f"答「{verdict}」")] += 1
        # 只认两次（原序 + 对调）都站同一边的那些——**扣掉位置偏好之后剩下的**
        if first.get(item["id"]) == swapped.get(item["id"]):
            key = first.get(item["id"])
            if key == item["_answer"]["new"]:
                both_runs["新"] += 1
            elif key == item["_answer"]["old"]:
                both_runs["旧"] += 1
            else:
                both_runs[str(key)] += 1
    for key, count in score.most_common():
        print(f"  {key}：{count} / {len(reals)}")

    longer_new = sum(1 for i in reals
                     if ("A" if len(i["A"]) >= len(i["B"]) else "B") == i["_answer"]["new"])
    print()
    print(f"  长度基线：新标注是更长那个的有 {longer_new}/{len(reals)}"
          f"（{longer_new / len(reals):.0%}），也就是「一律选更短的」"
          f"能拿到 {100 - longer_new}% 的「同意新标注」")
    print("  → 模型的「新标注更贴合」要明显不同于这个数，才说明它在读句子")

    print()
    print("  扣掉位置偏好（两次答案一致的那些）：")
    total = sum(both_runs.values())
    for key, count in both_runs.most_common():
        print(f"    {key}：{count} / {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
