"""把验收夹具留在会合点的事件清掉，而不碰真的标记。

会合点 meeting point / 夹具 fixture / 派生 derived

**为什么需要这个东西。** 验收脚本会真的往 `/v1/client/events` 上报
`word.marked` / `word.unmarked`——那是它该做的，那条路正是它要验的。
P9 之前后果只是不整洁:词池里多几个探针词，控制台看得见。
**P9 §6 做了双向同步之后，它变成了一条污染通道**——每台设备都会把会合点里的
事件拉下来重放进自己的投影，于是手机上出现一批你从没标过的词，
而两边都不报错。2026-09-18 真机上实测:776 条事件里 240 条是夹具。

**这里最要紧的一条判断:派生出来的标记不能跟着无条件删。**
`verify_phase2` 标的是**真文章里的一个真词**（`sample["headword"]`），
而不是一个造出来的探针词。所以「删掉这个词的 `study_marks`」有可能删掉的是
用户自己标的那一笔。规则因此是:**只有当没有任何真事件支持它时，才删派生行。**

**真正的解法是验收用自己的 learner**，那样它产生的事实根本流不到本人的设备上
（`/events` 按 learner 过滤）。那要先有多用户，记进主文档 §M。
在那之前这个模块是唯一的保护。

**一份规则，两个调用方**:`verify_phase2` 每次跑前跑后各清一次，
`scripts/purge_fixture_events.py` 给已经积下来的存量用。写两遍早晚不一样。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

#: 夹具形状的幂等键。**列的是形状不是具体的键**——一次性探针没有主人，
#: 而它们照样会被每台设备收下。
FIXTURE_SHAPES: tuple[str, ...] = (
    "v2-open-%", "v2-mark-%", "v2-prog-%",      # verify_phase2 §6
    "v2-ph-mark-%", "v2-ph-unmark-%",           # verify_phase2 词组那一节
    "k-%", "repro%", "rv-%",                    # 早年一次性探针
    "%probe%", "verify%",                       # 各 Phase 的验收探针
)

#: 客户端会收下并重放的那五种。**别的类型留在库里不伤人**——
#: 遥测（打开文章、读到哪、点了哪个词）不进设备的日志。
PULLED_TYPES: tuple[str, ...] = (
    "word.marked", "word.unmarked", "article.finished",
    "review.answered", "review.spelled",
)


def _like_clause(column: str, shapes: tuple[str, ...]) -> str:
    return " OR ".join([f"{column} LIKE ?"] * len(shapes))


def _identity(payload: str | None) -> tuple[str, str, int] | None:
    """事件指着哪个条目。认不出来就回 None——认不出的不动。"""
    try:
        data = json.loads(payload or "{}")
    except (TypeError, ValueError):
        return None
    key = str(data.get("item_key") or data.get("headword") or "").strip().lower()
    if not key:
        return None
    item_type = str(data.get("item_type") or "word")
    try:
        sense_id = int(data.get("sense_id") or 0)
    except (TypeError, ValueError):
        sense_id = 0
    return item_type, key, sense_id


def survey(conn: sqlite3.Connection) -> dict[str, Any]:
    """会合点里有多少夹具，其中多少条会被设备收下。只读。"""
    total = conn.execute("SELECT COUNT(*) FROM client_events").fetchone()[0]
    fixtures = conn.execute(
        f"SELECT COUNT(*) FROM client_events WHERE {_like_clause('idem_key', FIXTURE_SHAPES)}",
        FIXTURE_SHAPES).fetchone()[0]
    pullable = conn.execute(
        f"SELECT COUNT(*) FROM client_events"
        f" WHERE type IN ({','.join('?' * len(PULLED_TYPES))})"
        f"   AND ({_like_clause('idem_key', FIXTURE_SHAPES)})",
        (*PULLED_TYPES, *FIXTURE_SHAPES)).fetchone()[0]
    return {"total": total, "fixtures": fixtures, "pullable": pullable}


def purge(conn: sqlite3.Connection) -> dict[str, Any]:
    """删掉夹具事件，以及**没有真事件支持的**那些派生行。

    两步分开，顺序有意义:先把事件删掉（那是止住污染的那一步，也是唯一非做不可
    的一步），再逐个条目问「还有没有真事件支持它」。反过来的话，判断依据里
    还混着待删的那些。
    """
    rows = conn.execute(
        f"SELECT idem_key, type, payload FROM client_events"
        f" WHERE {_like_clause('idem_key', FIXTURE_SHAPES)}",
        FIXTURE_SHAPES).fetchall()
    touched: set[tuple[str, str, int]] = set()
    for row in rows:
        identity = _identity(row["payload"] if isinstance(row, sqlite3.Row) else row[2])
        if identity:
            touched.add(identity)

    removed = conn.execute(
        f"DELETE FROM client_events WHERE {_like_clause('idem_key', FIXTURE_SHAPES)}",
        FIXTURE_SHAPES).rowcount

    kept: list[str] = []
    dropped: list[str] = []
    for item_type, key, sense_id in sorted(touched):
        # **还有没有真事件说过这个条目？** 有就说明用户自己标过，派生行不许动。
        # 这里要连 payload 一起看，因为两种字段名都用过（`item_key` / `headword`）。
        real = conn.execute(
            f"SELECT COUNT(*) FROM client_events"
            f" WHERE type IN ('word.marked', 'word.unmarked')"
            f"   AND (payload LIKE ? OR payload LIKE ?)",
            (f'%"item_key": "{key}"%', f'%"headword": "{key}"%')).fetchone()[0]
        if real:
            kept.append(f"{key}#{sense_id}")
            continue
        conn.execute(
            "DELETE FROM study_marks WHERE item_type = ? AND item_key = ? AND sense_id = ?",
            (item_type, key, sense_id))
        conn.execute(
            "DELETE FROM study_states WHERE item_type = ? AND item_key = ? AND sense_id = ?",
            (item_type, key, sense_id))
        dropped.append(f"{key}#{sense_id}")
    conn.commit()

    leftover = conn.execute(
        f"SELECT COUNT(*) FROM client_events WHERE {_like_clause('idem_key', FIXTURE_SHAPES)}",
        FIXTURE_SHAPES).fetchone()[0]
    return {"removed": removed, "leftover": leftover,
            "marks_dropped": dropped, "marks_kept": kept}
