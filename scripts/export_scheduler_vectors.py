"""Export the scheduler's grade mapping and schedule as test vectors.

调度向量 scheduler vectors / 评级映射 grade mapping / 容差 tolerance

**Why this file exists, and why it did not before.**
``export_review_vectors.py`` says out loud what it leaves out: "Intervals,
ratings and due dates are the scheduler's, **the client never computes them**,
and including them would tie these vectors to FSRS parameters that are free to
change." Phase 9 moves scheduling onto the device (phase-9.html §4), so that
sentence stops being true and this second set of vectors becomes the net that
lets ``py-fsrs`` be deleted from the server.

**The old objection is answered by recording the settings.** The vectors below
carry the exact scheduler configuration they were generated with, so changing
``fsrs_desired_retention`` in the console does not invalidate them: they state
"given these settings, these inputs produce these outputs", which stays true.
The Swift side must feed the same settings in, not its own defaults.

**No app, no database, no scheduled tasks.** ``rating_for`` and ``review`` are
pure — the module docstring calls itself "A pure function: no database, no
network, no clock of its own" — so this script imports the review module only
to register the config specs and never calls ``create_app()``. That matters:
坑 §4.6 is exactly this script's shape doing it wrong, where creating the app
starts the task loop, the nightly batch fires for real, and the process then
exits and kills it halfway.

**Fuzzing is off.** It scatters intervals by a few percent from a seeded PRNG,
and two languages seeding differently is not a disagreement worth reporting.
The acceptance suite already turns it off for the same reason.

**Tolerance.** ``rating``, ``fsrs_state`` and ``lapses`` must match exactly —
they are integers and a difference is a real difference. ``stability``,
``difficulty`` and ``interval_days`` are doubles arrived at by repeated
floating-point work, so the mirror should compare them within 1e-6, the same
judgement ``export_review_vectors.py`` makes about the decayed weight.

**No timestamp in the output**, same as the review vectors: re-exporting must
produce no diff, or the check gets abandoned within a week.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "client" / "Tests" / "ERCoreTests" / "Fixtures" / "scheduler-vectors.json"

#: A fixed instant, so the due dates in the file are stable across exports.
#: Midnight UTC on a date with nothing special about it.
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: How many decimals the doubles are written with. Six, matching the review
#: vectors' treatment of the decayed weight.
PLACES = 6


def _round(value: Any) -> Any:
    return round(float(value), PLACES) if isinstance(value, (int, float)) else value


# --------------------------------------------------------------------------
# Part A — the grade mapping
# --------------------------------------------------------------------------
#
# Enumerated rather than sampled: the input space is four small dimensions, so
# the whole of it fits, and a complete table cannot have a gap in the middle.
# This half is **our own rule**, not FSRS's — 决定 11 as revised, plus the two
# ceilings P7 added — which makes it the half most likely to be mis-ported.

MISSES = (0, 1, 2, 3)
FLAGS = (False, True)
REVEALED = (0, 1)


def grade_cases() -> list[dict[str, Any]]:
    from backend.modules.review import scheduler as sched

    cases = []
    for misses in MISSES:
        for easy in FLAGS:
            for revealed in REVEALED:
                for capped in FLAGS:
                    rating = sched.rating_for(misses, easy=easy,
                                              revealed=revealed, capped=capped)
                    cases.append({
                        "misses": misses,
                        "easy": easy,
                        "revealed": revealed,
                        "capped": capped,
                        "rating": int(rating.value),
                        "rating_name": rating.name,
                    })
    return cases


# --------------------------------------------------------------------------
# Part B — the schedule
# --------------------------------------------------------------------------
#
# Every case pins something that is **stated** somewhere, following the review
# vectors' rule that a vector pinning a stated rule is worth more than one
# pinning whatever the code happened to do.

SCHEDULE_CASES: list[dict[str, Any]] = [
    {
        "name": "全新卡一次过：关掉 learning steps 之后落在两天外，不是十分钟外",
        "pins": "scheduler.py 模块注释：with the steps off, a card leaves Learning "
                "on its first review and lands two days out rather than ten minutes out",
        "initial": None,
        "reviews": [{"misses": 0}],
    },
    {
        "name": "全新卡失误一次 → Hard",
        "pins": "决定 11（2026-09-09 修订）：失误一次 → Hard",
        "initial": None,
        "reviews": [{"misses": 1}],
    },
    {
        "name": "全新卡失误两次 → Again，并记一次 lapse",
        "pins": "决定 11：失误两次及以上 → Again",
        "initial": None,
        "reviews": [{"misses": 2}],
    },
    {
        "name": "自称简单 → Easy，只有零失误才作数",
        "pins": "EASY_RATING 的注释：Easy is claimed by the learner, never inferred",
        "initial": None,
        "reviews": [{"misses": 0, "easy": True}],
    },
    {
        "name": "自称简单但失误过 → 不认，按失误算",
        "pins": "rating_for：claiming a word was trivial after failing it twice "
                "is not a claim about anything",
        "initial": None,
        "reviews": [{"misses": 1, "easy": True}],
    },
    {
        "name": "开过提示 → 降一级（Good → Hard）",
        "pins": "P7 决定（2026-09-13）：taking a hint costs a grade",
        "initial": None,
        "reviews": [{"misses": 0, "revealed": 1}],
    },
    {
        "name": "当天标记封顶 → 同样降一级",
        "pins": "P7 推翻 P3 决定 18：模糊的词当天也进，但当天这一次封顶为 Hard",
        "initial": None,
        "reviews": [{"misses": 0, "capped": True}],
    },
    {
        "name": "自称简单 ＋ 开过提示 → 降一档到 Good，不是 Hard，也不是白拿 Easy",
        "pins": "用户 2026-09-17 定：「太简单了」是学习者的判断、提示不否决它，"
                "但看了提示就降一档。这条向量正是那个决定的落点——"
                "在它之前这一组会给 Easy（8 天），因为 easy 的分支提前 return 了。",
        "initial": None,
        "reviews": [{"misses": 0, "easy": True, "revealed": 1}],
    },
    {
        "name": "自称简单 ＋ 当天刚标 → 封顶为 Hard，因为封顶不是降档",
        "pins": "P7 §3 的原话是「当天这一次封顶为 Hard」。"
                "两个上限语义不同，在 Good 上结果相同所以一直没露出来；"
                "在 Easy 上一个给 Good、一个给 Hard。",
        "initial": None,
        "reviews": [{"misses": 0, "easy": True, "capped": True}],
    },
    {
        "name": "连续五次一次过：间隔一路拉长，并被 maximum_interval 封在 180 天",
        "pins": "坑 §4.5：正确的间隔序列是 2→11→46→163→180，"
                "而 8→66→180 是接线错的那一版。这条同时钉住封顶。",
        "initial": None,
        "reviews": [{"misses": 0}, {"misses": 0}, {"misses": 0},
                    {"misses": 0}, {"misses": 0}],
    },
    {
        "name": "三次过之后失手一次：lapse 记上，间隔塌回来",
        "pins": "lapses 只在 Again 时加一（review() 里那一行）",
        "initial": None,
        "reviews": [{"misses": 0}, {"misses": 0}, {"misses": 0}, {"misses": 2}],
    },
    {
        "name": "P3 之前标的词：没有记忆状态，当成没复习过的新卡",
        "pins": "to_card：a row with no memory state yet — every item marked "
                "before P3 — becomes a fresh card, which is the right answer",
        "initial": {"stability": None, "difficulty": None, "reps": 0, "lapses": 0},
        "reviews": [{"misses": 0}],
    },
    {
        "name": "带着已有记忆状态的卡，隔 30 天再复习",
        "pins": "to_card / from_card 的往返：两边存的是同一组列",
        "initial": {
            "stability": 12.5, "difficulty": 6.0,
            "fsrs_state": 2, "fsrs_step": None,
            "due_at": "2026-01-01T00:00:00+00:00",
            "last_review_at": "2025-12-02T00:00:00+00:00",
            "reps": 3, "lapses": 1,
        },
        "reviews": [{"misses": 0, "after_days": 30}],
    },
]


def schedule_cases(settings: dict[str, Any]) -> list[dict[str, Any]]:
    from backend.modules.review import scheduler as sched

    out = []
    for case in SCHEDULE_CASES:
        state = dict(case["initial"]) if case["initial"] else None
        now = NOW
        steps = []
        for review in case["reviews"]:
            now = now + timedelta(days=float(review.get("after_days", 0)))
            outcome = sched.review(
                state,
                int(review["misses"]),
                now,
                easy=bool(review.get("easy", False)),
                revealed=int(review.get("revealed", 0)),
                capped=bool(review.get("capped", False)),
                # Never the configured value: see the module docstring.
                fuzz=False,
            )
            state = outcome.state
            steps.append({
                "at": now.isoformat(timespec="seconds"),
                "rating": int(outcome.rating.value),
                "rating_name": outcome.rating.name,
                "interval_days": _round(outcome.interval_days),
                "due_at": outcome.due_at.isoformat(timespec="seconds"),
                "stability": _round(state["stability"]),
                "difficulty": _round(state["difficulty"]),
                "fsrs_state": state["fsrs_state"],
                "fsrs_step": state["fsrs_step"],
                "reps": state["reps"],
                "lapses": state["lapses"],
            })
            # The next review is a day after this one falls due, unless the case
            # says otherwise — that is what makes the interval sequence in the
            # 坑 §4.5 case a sequence rather than five first reviews.
            now = outcome.due_at
        out.append({
            "name": case["name"],
            "pins": case["pins"],
            "initial": case["initial"],
            "reviews": case["reviews"],
            "expected": steps,
        })
    return out


def main() -> int:
    from importlib.metadata import version

    from backend.core import runtime_config
    import backend.modules.review.module  # noqa: F401  registers the fsrs_* specs

    from backend.modules.review import scheduler as sched

    # Built once here only to read the effective parameters back out of it —
    # the cases below each build their own through `review()`, which is what
    # keeps them exercising the real path rather than a copy of it.
    built = sched.scheduler(fuzz=False)

    # Recorded, not assumed. These are the settings the vectors were made with;
    # the mirror has to be handed the same ones.
    settings = {
        "fsrs_package": f"fsrs {version('fsrs')}",
        "learning_steps": [],
        "relearning_steps": [],
        "enable_fuzzing": False,
        "desired_retention": float(runtime_config.get("fsrs_desired_retention")),
        "maximum_interval": int(runtime_config.get("fsrs_maximum_interval")),
        # **The effective array, never "whatever the package ships with".**
        # 2026-09-17: the two packages do not agree on what they ship with.
        # `py-fsrs` 6.3.2 defaults to FSRS-6 (21 numbers); `swift-fsrs` keeps
        # FSRS-6's vector in a *separate* constant and still defaults `w` to
        # FSRS-5's 19. Two sides each reaching for "the default" would therefore
        # run different algorithms and produce different intervals — with
        # nothing to report, because both are behaving as documented.
        #
        # So the array is written out in full and the mirror is fed exactly it.
        # `configured` records whether it came from the console or from the
        # package, because that is the interesting part for a human reading the
        # file; the numbers are authoritative either way.
        "parameters": list(built.parameters),
        "parameters_configured": sched._parameters() is not None,
    }

    grades = grade_cases()
    schedules = schedule_cases(settings)

    document = {
        "note": "由 scripts/export_scheduler_vectors.py 从服务端真实代码导出，不要手改。"
                "重新导出之后不应有 diff——有 diff 就说明调度规则或 FSRS 版本变了。",
        "source": "backend/modules/review/scheduler.py::rating_for / review",
        "covers": "两半：评级映射（我们自己的规则，全枚举）与排期结果（FSRS 的，"
                  "钉住有明文依据的那些场景）。P9 把调度搬进客户端之后，"
                  "服务端不再需要 py-fsrs，这个文件是那一步的网。",
        "tolerance": {
            "exact": ["rating", "fsrs_state", "fsrs_step", "reps", "lapses"],
            "within_1e-6": ["stability", "difficulty", "interval_days"],
            "why": "整数不一致就是真不一致；双精度是反复浮点运算的结果，"
                   "两种语言的末几位不值得当成分歧（同 review-vectors 对权重的处理）。",
        },
        "settings": settings,
        "ratings": {
            "note": "全枚举：misses 0-3 × easy × revealed 0-1 × capped。"
                    "两个上限都只降级，绝不把失误变成通过。",
            "cases": grades,
        },
        "schedules": {
            "note": f"起点固定在 {NOW.isoformat(timespec='seconds')}，"
                    "每次复习默认发生在上一次的到期时刻，除非 after_days 另说。",
            "now": NOW.isoformat(timespec="seconds"),
            "cases": schedules,
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" for the same reason as the review vectors: text mode writes
    # CRLF on Windows while .gitattributes asks for LF, and this file exists
    # precisely so that re-exporting produces no diff.
    OUT.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")

    print(f"导出 {len(grades)} 条评级映射 + {len(schedules)} 个排期场景 "
          f"-> {OUT.relative_to(ROOT)}")
    print(f"  设置：{settings['fsrs_package']}，"
          f"retention {settings['desired_retention']}，"
          f"上限 {settings['maximum_interval']} 天，"
          f"{len(settings['parameters'])} 个参数"
          f"（FSRS-{'6' if len(settings['parameters']) == 21 else '5'}，"
          f"{'控制台配的' if settings['parameters_configured'] else '包自带的'}）")
    print()
    print("评级映射（misses/easy/revealed/capped -> 评级）：")
    for c in grades:
        flags = "".join([
            "E" if c["easy"] else "-",
            "R" if c["revealed"] else "-",
            "C" if c["capped"] else "-",
        ])
        print(f"  misses={c['misses']} {flags}  ->  {c['rating_name']}")
    print()
    print("排期：")
    for c in schedules:
        seq = " → ".join(f"{s['interval_days']:g}d" for s in c["expected"])
        print(f"  {c['name']}")
        print(f"    {seq}   （末次 {c['expected'][-1]['rating_name']}，"
              f"S={c['expected'][-1]['stability']}，"
              f"D={c['expected'][-1]['difficulty']}，"
              f"lapses={c['expected'][-1]['lapses']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
