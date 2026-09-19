"""Reads and writes for the sense tables."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from backend.core import events
from backend.core.db import get_connection
from backend.core.logging import get_logger

TOPICS = (
    "社会", "科技", "文化", "日常生活", "环境", "经济", "教育", "健康", "心理", "历史", "通用",
)



log = get_logger("senses.repository")

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store_senses(headword: str, senses: list[dict[str, Any]], *, model: str = "") -> int:
    """Store one word's sense set: update what is still there, retire what is not.

    **2026-09-17（P9 §8）：从「删了重插」改成「按稳定键更新 ＋ 退休」。**

    在这之前这个函数是 ``DELETE`` 然后 ``INSERT``，于是**每一次调用都会让这个词的
    义项 id 全变**——而那跟语义变没变毫无关系。补一个词的义项、重跑一次生成器、
    做一次迁移，都会把指着这些 id 的东西推到悬崖边上：九万三千条语境标注
    （实测 93,063）、学习者的标记、按义项记的学习状态。
    旧版的文档里写着这条路「is the wrong path for replacing the whole inventory」，
    而**它对补一个词也一样危险**，只是量小所以没人注意。

    现在：

    * 键还在的 → ``UPDATE``，**id 不变**，标注自动存活；
    * 键是新的 → ``INSERT``；
    * 这次没出现的 → **搬进 ``senses_retired``，不删**。按 id 反查的地方会回落
      到那张表，所以旧标注仍然指得到一个说得出话的义项。

    **``senses.replaced`` 现在只在真的有义项退休时才发。** 那个订阅者（重新标注）
    从前每次补词都被触发一遍；而它存在的理由是「有 id 没了」，
    所以没有 id 没掉的时候本来就不该叫它。

    **键的边界要一起记住**（见 :mod:`.keys`）：键认的是那句 ``concept_en``，
    不是那个意思。把同一个意思换个说法写，键会变、旧的那条会退休——那时对应关系
    是个判断题，走 ``sense_key_map``。**那一半躲不掉，这个函数没打算解决它。**
    """
    from backend.modules.senses import keys as sense_keys

    conn = get_connection("content")
    headword = (headword or "").strip().lower()
    incoming = sense_keys.assign_keys(headword, [s["concept_en"] for s in senses])

    existing = {
        row["sense_key"]: dict(row)
        for row in conn.execute(
            "SELECT id, sense_key, ordinal FROM senses WHERE headword = ?", (headword,)
        ).fetchall()
        if row["sense_key"]
    }
    # **退休过又回来的，要复活原来那一行，不是插一条新的。**
    # 不然「同一个键总是同一个 id」这条性质就不成立了:退休一次再加回来，
    # 指着它的标注照旧作废——而那正是稳定键要消掉的那种无谓变动。
    # 2026-09-17 实测:少一条再加回来，id 从 6672 变成了 14729。
    retired_rows = {
        row["sense_key"]: dict(row)
        for row in conn.execute(
            "SELECT id, sense_key FROM senses_retired WHERE headword = ?", (headword,)
        ).fetchall()
        if row["sense_key"]
    }
    retired_keys = [key for key in existing if key not in set(incoming)]
    retired_ids = [existing[key]["id"] for key in retired_keys]

    try:
        # ① 先退休。这一步也把它们的 ordinal 让出来，
        #    否则下面重排序号会撞上 UNIQUE (headword, ordinal)。
        for key in retired_keys:
            conn.execute(
                "INSERT INTO senses_retired SELECT *, ?, ? FROM senses WHERE id = ?",
                (_now(), "not in the new set", existing[key]["id"]),
            )
            conn.execute("DELETE FROM senses WHERE id = ?", (existing[key]["id"],))

        # ② 把留下来的序号先挪到负数。**两趟，因为一趟会撞。**
        #    把 #1 和 #2 对调时，中间必然有一刻两行都想要同一个序号。
        conn.execute(
            "UPDATE senses SET ordinal = -id WHERE headword = ?", (headword,)
        )

        # ③ 复活退休过又回来的那些。序号照样先给负数，下一步统一排。
        reviving = set(incoming) & set(retired_rows)
        if reviving:
            # 列名现取，不写死:`senses_retired` 是 `CREATE TABLE AS SELECT` 建的，
            # 它比 `senses` 多两列（退休时间与原因），所以两边不能用 `SELECT *`。
            names = ", ".join(
                row["name"]
                for row in conn.execute("PRAGMA table_info(senses)").fetchall()
            )
        for key in reviving:
            conn.execute(
                f"INSERT INTO senses ({names})"  # noqa: S608 - 列名来自 PRAGMA
                f" SELECT {names} FROM senses_retired WHERE id = ?",
                (retired_rows[key]["id"],),
            )
            conn.execute(
                "UPDATE senses SET ordinal = -id WHERE id = ?",
                (retired_rows[key]["id"],),
            )
            conn.execute(
                "DELETE FROM senses_retired WHERE id = ?", (retired_rows[key]["id"],)
            )
            existing[key] = {"id": retired_rows[key]["id"], "ordinal": None}
            log.info(
                "senses.revived",
                f"{headword}：一条退休过的义项回来了，id 不变（{retired_rows[key]['id']}）",
                headword=headword, sense_id=retired_rows[key]["id"], sense_key=key,
            )

        # ④ 按键更新或插入。
        for ordinal, (sense, key) in enumerate(zip(senses, incoming), start=1):
            gloss = json.dumps(sense["gloss_zh"], ensure_ascii=False)
            row = existing.get(key)
            if row is not None:
                conn.execute(
                    "UPDATE senses SET ordinal = ?, concept_en = ?, gloss_zh = ?,"
                    " pos = ?, topic = ?, covers = ?, source = ?, model = ?"
                    " WHERE id = ?",
                    (ordinal, sense["concept_en"], gloss, sense.get("pos"),
                     sense.get("topic"), sense.get("covers"), sense.get("source"),
                     model, row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO senses (headword, ordinal, concept_en, gloss_zh,"
                    " pos, topic, covers, source, model, created_at, sense_key)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (headword, ordinal, sense["concept_en"], gloss, sense.get("pos"),
                     sense.get("topic"), sense.get("covers"), sense.get("source"),
                     model, _now(), key),
                )
        conn.commit()
    except Exception:
        # 半份义项集比旧的那份糟得多。
        conn.rollback()
        raise

    if retired_ids:
        # **只有真的有 id 没掉才发。** 订阅者是「重新标注」，
        # 而它存在的理由就是「有 id 没了」。
        events.emit("senses.replaced", headword=headword,
                    previous_ids=retired_ids, count=len(senses))
        log.info(
            "senses.retired",
            f"{headword}：{len(retired_ids)} 条义项退休，{len(senses)} 条现行",
            headword=headword, retired=len(retired_ids), live=len(senses),
        )
    return len(senses)


def sense_by_id(sense_id: int) -> dict[str, Any] | None:
    """按 id 取一个义项，**退休的也找得到**。

    旧标注指着已经退休的义项是正常的——那是 P9 §8 用「退休而不是删除」换来的
    性质：一条两个月前的标记仍然指得到一个说得出话的义项，而不是指到空气。
    界面照旧只列出现行义项（``senses_of`` 只读活表），这条只用于**反查**。
    """
    conn = get_connection("content")
    row = conn.execute("SELECT * FROM senses WHERE id = ?", (sense_id,)).fetchone()
    if row is not None:
        return dict(row)
    row = conn.execute(
        "SELECT * FROM senses_retired WHERE id = ? ORDER BY retired_at DESC LIMIT 1",
        (sense_id,),
    ).fetchone()
    return dict(row) if row else None


def map_sense_key(from_key: str, to_key: str | None, *, reason: str = "",
                  model: str = "") -> None:
    """记下一条「旧义项对应哪个新义项」。**这张表永不删除。**

    ``to_key`` 为空表示「这个义项没有对应的新义项」——那也是一个答案，
    而且是要记下来的答案:没有它的话，「我们把它漏了吗」和「我们决定它没有对应」
    看起来一样。
    """
    get_connection("content").execute(
        "INSERT OR IGNORE INTO sense_key_map (from_key, to_key, decided_at, reason, model)"
        " VALUES (?,?,?,?,?)",
        (from_key, to_key, _now(), reason or None, model or None),
    )
    get_connection("content").commit()


def senses_of(headword: str) -> list[dict[str, Any]]:
    try:
        rows = get_connection("content").execute(
            "SELECT * FROM senses WHERE headword = ? ORDER BY ordinal",
            (headword.lower(),),
        ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for row in rows:
        sense = dict(row)
        try:
            sense["gloss_zh"] = json.loads(sense["gloss_zh"])
        except (TypeError, json.JSONDecodeError):
            sense["gloss_zh"] = [sense["gloss_zh"]]
        out.append(sense)
    return out


def pending_words(limit: int = 20000) -> list[str]:
    """Target words with no sense set yet — the job's input.

    The screening verdict is deliberately ignored. It judged English polysemy by
    counting commas in a Chinese gloss, and measuring it against an external
    inventory showed that 65% of the words it dismissed as "simple" are in fact
    polysemous — ``bank``, ``come``, ``do`` and ``be`` among them. Building
    everything costs about twice the tokens and removes both the threshold and
    the blind spot. The table is kept for reference, not for filtering.
    """
    rows = get_connection("content").execute(
        "SELECT s.headword FROM sense_screening s"
        " LEFT JOIN senses n ON n.headword = s.headword"
        " WHERE n.id IS NULL"
        " GROUP BY s.headword ORDER BY s.headword LIMIT ?",
        (limit,),
    ).fetchall()
    return [row["headword"] for row in rows]


def add_examples(sense_id: int, examples: list[dict[str, str]], *, source: str = "llm") -> int:
    conn = get_connection("content")
    conn.executemany(
        "INSERT INTO sense_examples (sense_id, text_en, gloss_zh, source, created_at)"
        " VALUES (?,?,?,?,?)",
        [
            (sense_id, item["text_en"], item.get("gloss_zh"), source, _now())
            for item in examples
        ],
    )
    conn.commit()
    return len(examples)


def examples_of(sense_id: int) -> list[dict[str, Any]]:
    try:
        rows = get_connection("content").execute(
            "SELECT id, text_en, gloss_zh, source FROM sense_examples"
            " WHERE sense_id = ? ORDER BY id",
            (sense_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def stats() -> dict[str, Any]:
    try:
        conn = get_connection("content")
        words = conn.execute(
            "SELECT COUNT(DISTINCT headword) AS n FROM senses"
        ).fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM senses").fetchone()["n"]
        examples = conn.execute(
            "SELECT COUNT(*) AS n FROM sense_examples"
        ).fetchone()["n"]
        by_topic = {
            row["topic"] or "未标": row["n"]
            for row in conn.execute(
                "SELECT topic, COUNT(*) AS n FROM senses GROUP BY topic ORDER BY n DESC"
            )
        }
    except sqlite3.Error:
        return {"words": 0, "senses": 0, "examples": 0, "by_topic": {}}
    return {
        "words": int(words),
        "senses": int(total),
        "examples": int(examples),
        "per_word": round(total / words, 2) if words else 0,
        "by_topic": by_topic,
    }


def words_by_topic(topic: str, limit: int = 200) -> list[str]:
    """Target words whose main sense belongs to a topic.

    This is what lets a batch of target words be picked around a subject instead
    of at random — the fix for models having to force eight unrelated words into
    one article.
    """
    rows = get_connection("content").execute(
        "SELECT DISTINCT headword FROM senses WHERE topic = ? AND ordinal = 1"
        " ORDER BY headword LIMIT ?",
        (topic, limit),
    ).fetchall()
    return [row["headword"] for row in rows]
