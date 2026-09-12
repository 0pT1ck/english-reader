"""Export the review state machine's transitions as test vectors.

测试向量 test vectors / 镜像 mirror / 漂移 drift

**What this is for.** 架构前提 2 requires the client to finish a day's review
with no network, so the transitions have to exist on the device as well as
here — one machine, written twice, in two languages. 架构前提 1 warns about
exactly that: two implementations drift, and when they drift nothing errors.

So the client's copy is checked against this one. Not against hand-written
expectations — those have no authority, and two sides both "passing" may only
mean both are wrong. The expected values below come from calling
``session.answer`` itself, on a real queue row, in this process.

**What is covered and what is not.** Only the transitions a client mirrors:
direction, asks, misses, weight, done. Intervals, ratings and due dates are the
scheduler's, the client never computes them, and including them would tie these
vectors to FSRS parameters that are free to change.

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

OUT = ROOT / "client" / "Tests" / "ERCoreTests" / "Fixtures" / "review-vectors.json"

#: Item key used for the probe rows. Deliberately not a real word: if one of
#: these ever survives a crash mid-export, it should be obvious in the database
#: what it was and where it came from.
PROBE = "__vector_probe__"


def _seed(conn, session_id: int, case_no: int, initial: dict[str, Any]) -> int:
    """One queue row in a known starting state. Returns its id."""
    cursor = conn.execute(
        "INSERT INTO review_queue (session_id, item_type, item_key, sense_id, bucket,"
        " step, asks, misses, weight, easy, done_at)"
        " VALUES (?,'word',?,?,?,?,?,?,?,0,NULL)",
        (session_id, PROBE, case_no, initial.get("bucket", "due"),
         initial["step"], initial["asks"], initial["misses"], initial["weight"]),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _snapshot(conn, queue_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT step, asks, misses, weight, done_at FROM review_queue WHERE id = ?",
        (queue_id,),
    ).fetchone()
    return {
        "direction": int(row["step"]),
        "asks": int(row["asks"]),
        "misses": int(row["misses"] or 0),
        # Rounded because the decay is repeated multiplication and the last bits
        # of a double are not something two languages need to agree on.
        "weight": round(float(row["weight"]), 6),
        "done": bool(row["done_at"]),
    }


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
    from backend.core import notifications, runtime_config
    from backend.core.db import get_connection
    from backend.main import create_app
    from backend.modules.review import repository, session

    create_app()
    conn = get_connection("learning")
    decay = float(runtime_config.get("review_weight_decay"))

    def clean() -> None:
        conn.execute("DELETE FROM review_queue WHERE item_key = ?", (PROBE,))
        conn.execute("DELETE FROM review_history WHERE item_key = ?", (PROBE,))
        conn.execute("DELETE FROM study_states WHERE item_key = ?", (PROBE,))
        conn.commit()

    # 前后各清一次 (坑 §4.1): an export interrupted halfway must not poison the
    # next run, and must not leave rows behind either.
    clean()

    state = repository.session_for(1, repository.today()) or repository.open_session(
        1, repository.today()
    )
    session_id = int(state["id"])

    vectors: list[dict[str, Any]] = []
    try:
        # Errors are seeded on purpose in some cases; alerting on them would be
        # training the user to ignore the channel (坑 §4.4).
        with notifications.muted():
            for case_no, case in enumerate(CASES):
                queue_id = _seed(conn, session_id, case_no, case["initial"])
                steps = []
                for answer in case["answers"]:
                    session.answer(
                        1, queue_id,
                        passed=bool(answer["passed"]),
                        revealed=int(answer.get("revealed", 0)),
                        easy=bool(answer.get("easy", False)),
                    )
                    steps.append(_snapshot(conn, queue_id))
                    if steps[-1]["done"]:
                        # Answering a finished item raises, and the client is
                        # not supposed to try. Stop where the day stops.
                        break
                vectors.append({
                    "name": case["name"],
                    "initial": {
                        "direction": case["initial"]["step"],
                        "asks": case["initial"]["asks"],
                        "misses": case["initial"]["misses"],
                        "weight": round(float(case["initial"]["weight"]), 6),
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
    finally:
        clean()

    document = {
        "note": "由 scripts/export_review_vectors.py 从服务端真实代码导出，不要手改。"
                "重新导出之后不应有 diff——有 diff 就说明服务端的规则变了，"
                "客户端那份镜像要跟着改。",
        "source": "backend/modules/review/session.py::answer",
        "covers": "客户端要镜像的那几条：方向、问过几次、错过几次、权重、今天过没过。"
                  "间隔、评级、到期时间是调度器的，客户端从不计算，故意不在这里。",
        "constants": {
            "word_to_sense": session.WORD_TO_SENSE,
            "sense_to_word": session.SENSE_TO_WORD,
            "max_reveal": session.MAX_REVEAL,
            "weight_decay": decay,
            "weight_floor": 0.0001,
        },
        "cases": vectors,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
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
