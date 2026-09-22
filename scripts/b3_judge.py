"""B3 盲判：拿模型判一遍题，并量出「不读句子也能猜对」的基线。

**三方独立判断这次做不到，如实记下**：P3 那次用了三个不同的模型，
而现在只有 `gemini-3.8-flash-high` 一条路调得通（CLAUDE.md「模型与中转站」）。
同一个模型跑三遍不是三个判断者，共有盲区一个不少。所以这里只有：
① 模型判一遍；② 把 A/B 对调再判一遍，量它的位置偏好；③ 人（我）判一遍。
结论必须写成「在能验的范围内」。

**长度基线**：柯林斯的中文普遍更长、常带括注，旧的是短语列表。
如果「总选更长的那个」就能达到跟模型差不多的命中率，那模型可能根本没在读句子。
这条基线不花一次调用，却是唯一能戳破它的东西。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core import runtime_config  # noqa: E402
from backend.core.registry import run_core_migrations  # noqa: E402
from backend.modules.llm import client as llm  # noqa: E402
from backend.modules.llm import providers  # noqa: E402
from backend.modules.llm import module as _llm_module  # noqa: E402,F401

PROMPT = """你在判断词义标注。下面每一题给出一个英文句子、句中的一个词，
以及两个候选中文释义。请判断**在这句话里**，这个词是哪个意思。

只看句子和词义是否贴合，不要考虑释义写得长短、详略或风格。

每题回答一个：
  A —— A 更贴合
  B —— B 更贴合
  both —— 两个都说得通（意思基本相同，或都对）
  neither —— 两个都不对

只输出 JSON 数组，每项形如 {{"id": "q001", "verdict": "A"}}，不要别的话。

题目：
{questions}"""


def render(item: dict) -> str:
    return (f'[{item["id"]}] 句子：{item["sentence"]}\n'
            f'　　词：{item["surface"]}\n'
            f'　　A：{item["A"]}\n'
            f'　　B：{item["B"]}')


def ask(batch: list[dict], swap: bool) -> dict[str, str]:
    shown = []
    for item in batch:
        copy = dict(item)
        if swap:
            copy["A"], copy["B"] = item["B"], item["A"]
        shown.append(copy)
    completion = llm.complete(
        providers.default_provider(),
        [{"role": "user",
          "content": PROMPT.format(
              questions="\n\n".join(render(i) for i in shown))}],
        max_tokens=80 * len(batch) + 400, temperature=0.1, json_mode=True,
    )
    body = completion.text if hasattr(completion, "text") else str(completion)
    # **逐个对象抽，不要整段 `[.*]`**：贪婪匹配会从第一个方括号吃到最后一个，
    # 而题面里出现一次方括号就切歪；第一版就是这么在第 9 行崩的。
    rows = re.findall(r'\{[^{}]*"id"\s*:\s*"([^"]+)"[^{}]*"verdict"\s*:\s*"([^"]*)"[^{}]*\}',
                      body)
    if not rows:
        raise RuntimeError(f"读不懂模型的回答：{body[:300]}")
    out = {}
    for row_id, raw_verdict in rows:
        row = {"id": row_id, "verdict": raw_verdict}
        verdict = str(row.get("verdict", "")).strip()
        if swap and verdict in ("A", "B"):        # 换回原来的编号
            verdict = "B" if verdict == "A" else "A"
        out[str(row.get("id"))] = verdict
    return out


def longer_side(item: dict) -> str:
    return "A" if len(item["A"]) >= len(item["B"]) else "B"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--items", type=Path, default=ROOT / "data/b3-items.json")
    ap.add_argument("--out", type=Path, default=ROOT / "data/b3-model.json")
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--swap", action="store_true", help="把 A/B 对调再判一遍，量位置偏好")
    args = ap.parse_args()

    run_core_migrations()
    items = json.loads(args.items.read_text(encoding="utf-8"))

    verdicts: dict[str, str] = {}
    for start in range(0, len(items), args.batch):
        batch = items[start:start + args.batch]
        got = ask(batch, swap=args.swap)
        verdicts.update(got)
        print(f"  {start + len(batch)}/{len(items)}", flush=True)

    args.out.write_text(json.dumps(verdicts, ensure_ascii=False, indent=1), encoding="utf-8")

    # 长度基线：不读句子，一律选更长的那个，能有多准
    controls = [i for i in items if i["kind"] == "control"]
    reals = [i for i in items if i["kind"] == "real"]
    hit = sum(1 for i in reals if longer_side(i) == i["_answer"]["new"])
    print(f"\n长度基线：在 {len(reals)} 道真题里，「选更长的那个」命中新标注 "
          f"{hit} 次（{hit / len(reals):.0%}）——**模型的成绩要跟这个比**")
    print(f"写入 {args.out}（{len(verdicts)} 条判决，对照 {len(controls)} 道）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
