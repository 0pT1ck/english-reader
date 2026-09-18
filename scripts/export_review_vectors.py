"""Export the review state machine's transitions as test vectors.

测试向量 test vectors / 镜像 mirror / 漂移 drift

**What this is for.** 架构前提 2 requires the client to finish a day's review
with no network, so the transitions have to exist on the device — and after P9
they exist *only* there: the round is learning state, and learning state moved
to the device with the rest of it.

That is exactly when a mirror stops being checkable by itself. Hand-written
expectations have no authority, and a client checked against its own output is
checked against nothing. So the rules were kept as an independent Python
transcription, `scripts/review_reference.py`, and the expected values below come
from calling it — the same arrangement `scripts/fsrs_reference.py` has.

**2026-09-18 (P9 §12): the source moved, the vectors did not.** Before this the
values came from ``session.answer`` on a real queue row in a real database.
There is no such row any more. What changed in the output is one number —
``max_reveal`` 3 → 1, which P7 changed in 2026-09-13 and nothing re-exported
since. **That is the second thing this file is for**: a constant that drifted
for five days without anything noticing is precisely the silent kind.

**What is covered and what is not.** Only the transitions a client mirrors:
direction, asks, misses, weight, done. Intervals, ratings and due dates belong
to the scheduler and have their own vectors (`export_scheduler_vectors.py`).

**No timestamp in the output.** The point of the file is that re-exporting it
produces no diff; a generation time would make every export differ and the
check would be abandoned within a week.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

OUT = ROOT / "client" / "Tests" / "ERCoreTests" / "Fixtures" / "review-vectors.json"


#: The cases. Random scenarios would cover more ground, but these are the ones
#: whose behaviour is *stated* somewhere — in 决定 7, 决定 10, 决定 11 — and a
#: vector that pins a stated rule is worth more than one that pins whatever the
#: code happened to do. The awkward ones are here on purpose: a round that never
#: ends, a failure on the very last step, a weight already at the floor.
CASES: list[dict[str, Any]] = [
    {
        "name": "一次过：答对第一向解锁第二向，答对第二向就算完",
        "initial": {"step": 1, "asks": 0, "misses": 0, "weight": 1.0},
        "answers": [{"passed": True}, {"passed": True}],
    },
    {
        "name": "第一向答错：退回第一向，权重减半",
        "initial": {"step": 1, "asks": 0, "misses": 0, "weight": 1.0},
        "answers": [{"passed": False}, {"passed": True}, {"passed": True}],
    },
    {
        "name": "第二向答错会重新上锁——决定 7，这条最容易写漏",
        "initial": {"step": 1, "asks": 0, "misses": 0, "weight": 1.0},
        "answers": [{"passed": True}, {"passed": False}, {"passed": True}, {"passed": True}],
    },
    {
        "name": "连错五次：权重一路减半，但永不排除",
        "initial": {"step": 1, "asks": 0, "misses": 0, "weight": 1.0},
        "answers": [{"passed": False}] * 5,
    },
    {
        "name": "权重已经很小，再错还是只减半不归零",
        "initial": {"step": 1, "asks": 0, "misses": 0, "weight": 0.0002},
        "answers": [{"passed": False}, {"passed": False}],
    },
    {
        "name": "开了提示照样算答对，但 revealed 记下来",
        "initial": {"step": 1, "asks": 0, "misses": 0, "weight": 1.0},
        "answers": [{"passed": True, "revealed": 3}, {"passed": True, "revealed": 2}],
    },
    {
        "name": "自称简单：零失误才作数",
        "initial": {"step": 1, "asks": 0, "misses": 0, "weight": 1.0},
        "answers": [{"passed": True, "easy": True}, {"passed": True, "easy": True}],
    },
    {
        "name": "自称简单但中途错过：服务端不认，easy 被撤回",
        "initial": {"step": 1, "asks": 0, "misses": 0, "weight": 1.0},
        "answers": [{"passed": True, "easy": True}, {"passed": False},
                    {"passed": True}, {"passed": True}],
    },
    {
        "name": "从第二向开始（昨天解锁过的半截状态）",
        "initial": {"step": 2, "asks": 3, "misses": 1, "weight": 0.5},
        "answers": [{"passed": True}],
    },
    {
        "name": "今天刚标的词，跟到期的走同一套规则",
        "initial": {"step": 1, "asks": 0, "misses": 0, "weight": 1.0, "bucket": "today"},
        "answers": [{"passed": True}, {"passed": True}],
    },
]


def main() -> int:
    import review_reference as ref
    from backend.core import runtime_config
    import backend.modules.review.module  # noqa: F401  registers review_weight_decay

    decay = float(runtime_config.get("review_weight_decay"))

    vectors: list[dict[str, Any]] = []
    for case in CASES:
        start = case["initial"]
        state = ref.Round(
            direction=int(start["step"]),
            asks=int(start["asks"]),
            misses=int(start["misses"]),
            weight=float(start["weight"]),
        )
        steps = []
        for given in case["answers"]:
            state = ref.answer(
                state,
                passed=bool(given["passed"]),
                revealed=int(given.get("revealed", 0)),
                easy=bool(given.get("easy", False)),
                weight_decay=decay,
            )
            steps.append({
                "direction": state.direction,
                "asks": state.asks,
                "misses": state.misses,
                # Rounded because the decay is repeated multiplication and the
                # last bits of a double are not something two languages need to
                # agree on.
                "weight": round(state.weight, 6),
                "done": state.done,
            })
            if state.done:
                # Answering a finished item raises, and the client is not
                # supposed to try. Stop where the day stops.
                break
        vectors.append({
            "name": case["name"],
            "initial": {
                "direction": int(start["step"]),
                "asks": int(start["asks"]),
                "misses": int(start["misses"]),
                "weight": round(float(start["weight"]), 6),
                "done": False,
            },
            "answers": [
                {"passed": bool(a["passed"]),
                 "revealed": int(a.get("revealed", 0)),
                 "easy": bool(a.get("easy", False))}
                for a in case["answers"][: len(steps)]
            ],
            "expected": steps,
        })

    document = {
        "note": "由 scripts/export_review_vectors.py 从 scripts/review_reference.py 导出，"
                "不要手改。重新导出之后不应有 diff——有 diff 就说明那份参照实现变了，"
                "客户端那份镜像要跟着改。",
        "source": "scripts/review_reference.py::answer",
        "covers": "客户端要镜像的那几条：方向、问过几次、错过几次、权重、今天过没过。"
                  "间隔、评级、到期时间是调度器的，它们有自己那份向量。",
        "constants": {
            "word_to_sense": ref.WORD_TO_SENSE,
            "sense_to_word": ref.SENSE_TO_WORD,
            "max_reveal": ref.MAX_REVEAL,
            "weight_decay": decay,
            "weight_floor": ref.WEIGHT_FLOOR,
        },
        "cases": vectors,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # `newline="\n"`: Python's text mode writes CRLF on Windows while
    # `.gitattributes` asks for LF, so every export would otherwise leave a file
    # that differs only in line endings — and this file exists precisely so that
    # re-exporting it produces no diff.
    OUT.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    print(f"导出 {len(vectors)} 个场景 -> {OUT.relative_to(ROOT)}")
    for v in vectors:
        last = v["expected"][-1]
        print(f"  {v['name']}")
        print(f"    {len(v['answers'])} 次作答 -> 方向 {last['direction']}，"
              f"问过 {last['asks']} 次，错 {last['misses']} 次，"
              f"权重 {last['weight']}，{'过了' if last['done'] else '没过'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
