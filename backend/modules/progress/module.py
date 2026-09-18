"""词池快照：收下来、存着、给生成用。

词池 word pool / 快照 snapshot / 生词 new word

**快照不是日志。** 事件日志只增不减，而这一份是「现在的样子」——每次上报整份替换。
两者的区别不是实现选择:日志是事实的序列，快照是那个序列的一个函数值，
而服务端不想去算那个函数（见包注释）。

**多设备时最后收到的那份算。** 两台设备各自重放同一份日志会得出同一个词池，
所以正常情况下两份快照一样；不一样只发生在一台设备还没同步完的时候，
而它下一次上报就自己纠正了。**所以不需要仲裁**——需要仲裁的是事实，
而这不是事实，是一个算得出来的值。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.core import auth, events
from backend.core.db import Migration, get_connection
from backend.core.logging import get_logger
from backend.core.registry import Module

log = get_logger("progress")

client_router = APIRouter()
admin_router = APIRouter(dependencies=[Depends(auth.require_admin)])

DeviceId = Annotated[int, Depends(auth.require_device)]

#: 词池的三档，和客户端那边一字不差。拼错了服务端就选不对生词，
#: 而那种错是静默的:它只会让每篇文章的生词数悄悄不对。
POOLS = ("new", "reviewing", "graduated")


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

MIGRATIONS = [
    Migration(
        version=1,
        name="learner pool snapshot",
        database="learning",
        apply="""
        CREATE TABLE IF NOT EXISTS learner_pool (
            learner_id  INTEGER NOT NULL DEFAULT 1,
            item_type   TEXT    NOT NULL DEFAULT 'word',
            item_key    TEXT    NOT NULL,
            sense_id    INTEGER NOT NULL DEFAULT 0,
            -- new | reviewing | graduated
            pool        TEXT    NOT NULL,
            PRIMARY KEY (learner_id, item_type, item_key, sense_id)
        );
        CREATE INDEX IF NOT EXISTS idx_learner_pool_key
            ON learner_pool (learner_id, item_key);

        -- 这份报告是什么时候的、哪台设备报的、有多少条。
        -- **「什么时候的」这个字段是 §7 特意留的**：长期不上线的退化策略推迟到
        -- 将来做，但那时要有东西可依据，不能到时候再改契约。
        CREATE TABLE IF NOT EXISTS learner_pool_reports (
            learner_id   INTEGER NOT NULL DEFAULT 1,
            device_id    INTEGER,
            reported_at  TEXT,
            received_at  TEXT    NOT NULL,
            entries      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (learner_id)
        );
        """,
    ),
]


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #


class PoolEntryIn(BaseModel):
    item_type: str = Field(default="word", description="word 或 phrase")
    item_key: str
    sense_id: int = Field(default=0, description="词组带 0——它是一个整体")
    pool: str = Field(description="new | reviewing | graduated")


class PoolSnapshotIn(BaseModel):
    """整份词池。**只带非 `new` 的那些就够了**，但带全了也接受。

    服务端要的是「哪些词你已经在学或学过」，`new` 是补集。客户端只报非 new 的，
    省掉绝大部分条目——实测非 new 的有 30 条，而词典里有 318,207 条。
    """

    reported_at: str | None = Field(
        default=None,
        description="设备上算出这份快照的时刻。**服务端不用它做判断**，"
        "只是存着——将来做「长期不上线」的退化策略时要有东西可依据",
    )
    entries: list[PoolEntryIn]


class PoolSnapshotResponse(BaseModel):
    stored: int = Field(description="收下了多少条")
    reviewing: int
    graduated: int
    received_at: str


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def replace_snapshot(learner_id: int, device_id: int | None,
                     snapshot: PoolSnapshotIn) -> dict[str, Any]:
    """整份替换。

    **删了重插，而且在一个事务里。** 逐条 upsert 会留下上一份里已经撤销掉的词——
    撤销标记让条目退回 `new`，而 `new` 不在上报范围内，所以它在新快照里
    「不出现」，逐条 upsert 读不出这个意思。
    """
    conn = get_connection("learning")
    rows = [
        (learner_id, e.item_type, e.item_key, int(e.sense_id), e.pool)
        for e in snapshot.entries
        if e.item_key and e.pool in POOLS
    ]
    received = _now()
    try:
        conn.execute("DELETE FROM learner_pool WHERE learner_id = ?", (learner_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO learner_pool"
            " (learner_id, item_type, item_key, sense_id, pool) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.execute(
            "INSERT OR REPLACE INTO learner_pool_reports"
            " (learner_id, device_id, reported_at, received_at, entries)"
            " VALUES (?,?,?,?,?)",
            (learner_id, device_id, snapshot.reported_at, received, len(rows)),
        )
        conn.commit()
    except Exception:
        # 半份快照比旧快照糟得多:它会让生成以为某些词没在学。
        conn.rollback()
        raise

    counts = {p: sum(1 for r in rows if r[4] == p) for p in POOLS}
    # **「在学的词变了」是补句子池的信号**（架构铁律 6:订阅，不改别人的流程）。
    #
    # 这一条是 P9 逼出来的:决定 23 说句子要在读完那一刻就补上，而不是等夜里——
    # 当天标的词当天就要复习，那一轮需要一句你没见过的。读完那一刻补，靠的是
    # `article.finished`;但从 P9 起「哪些词在学」是设备报上来的，而客户端的顺序是
    # **先推事件、再报快照**。读完事件到的时候快照里还没有今天新标的那些词，
    # 补句子就会漏掉它们——而那是静默的:第二天你会发现那个词没题可出。
    #
    # 所以真正的信号是这一条，不是读完。读完那条订阅留着不删:两条都便宜
    # （池子满了 `start_for` 直接回 None），而留着它，先报快照后推事件的客户端
    # 也照样对。
    events.emit("progress.pool.reported", learner_id=learner_id,
                device_id=device_id, entries=len(rows),
                reviewing=counts["reviewing"])
    log.info(
        "progress.pool.reported",
        f"收到词池快照：{len(rows)} 条（在学 {counts['reviewing']}、"
        f"学过 {counts['graduated']}）",
        learner_id=learner_id, device_id=device_id, entries=len(rows),
        reported_at=snapshot.reported_at,
    )
    return {
        "stored": len(rows),
        "reviewing": counts["reviewing"],
        "graduated": counts["graduated"],
        "received_at": received,
    }


def words_in_progress(learner_id: int = 1) -> set[str] | None:
    """已经在学或学过的词，小写。**`None` ＝ 还没有人报过快照。**

    返回可选值而不是空集合，因为这两件事差别很大:空集合是「一个词都还没学」，
    而没报过是「不知道」。把「不知道」当成「一个都没有」，生成就会把你正在学的词
    当成生词再教一遍——**而那是静默的**，只表现为「这篇怎么全是我标过的词」。
    调用方据此决定回落还是报警。
    """
    row = get_connection("learning").execute(
        "SELECT COUNT(*) AS n FROM learner_pool_reports WHERE learner_id = ?",
        (learner_id,),
    ).fetchone()
    if not row or not int(row["n"] or 0):
        return None
    rows = get_connection("learning").execute(
        "SELECT DISTINCT item_key FROM learner_pool"
        " WHERE learner_id = ? AND item_type = 'word' AND pool != 'new'",
        (learner_id,),
    ).fetchall()
    return {str(r["item_key"]).lower() for r in rows if r["item_key"]}


def snapshot_status(learner_id: int = 1) -> dict[str, Any]:
    """管理台那一行:报过没有、什么时候、多少条。"""
    conn = get_connection("learning")
    report = conn.execute(
        "SELECT * FROM learner_pool_reports WHERE learner_id = ?", (learner_id,)
    ).fetchone()
    counts = {
        str(r["pool"]): int(r["n"])
        for r in conn.execute(
            "SELECT pool, COUNT(*) AS n FROM learner_pool WHERE learner_id = ?"
            " GROUP BY pool", (learner_id,)
        ).fetchall()
    }
    return {
        "reported": report is not None,
        "reported_at": report["reported_at"] if report else None,
        "received_at": report["received_at"] if report else None,
        "device_id": report["device_id"] if report else None,
        "entries": int(report["entries"]) if report else 0,
        "by_pool": counts,
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@client_router.put("/progress/pool", summary="上报词池快照",
                   response_model=PoolSnapshotResponse)
async def report_pool(device_id: DeviceId, body: PoolSnapshotIn) -> dict[str, Any]:
    """整份替换这个学习者的词池快照。

    **PUT 而不是 POST**：它是替换，不是追加。语义上说对了，重试也就天然安全——
    同一份快照报两遍和报一遍结果一样。
    """
    return replace_snapshot(auth.learner_for_device(device_id),
                            device_id, body)


@admin_router.get("/progress/pool", summary="词池快照的状态")
async def pool_status() -> dict[str, Any]:
    return snapshot_status(1)


MODULE = Module(
    name="progress",
    title="进度报告",
    description="学习者上报的词池快照。服务端靠它避开在学的词、保证每篇有生词",
    migrations=MIGRATIONS,
    client_router=client_router,
    admin_router=admin_router,
)
