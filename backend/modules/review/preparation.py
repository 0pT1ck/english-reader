"""Content preparation / 内容准备: persistent stock state, not learning state.

The existing LLM job runner owns execution, pause and retry. This module only
coalesces requests for the same content identity and exposes safe client status.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from backend.core.db import get_connection
from backend.modules.llm import jobs
from backend.modules.review import repository, sentences

_request_lock = threading.Lock()


def set_state(item_type: str, item_key: str, sense_id: int,
              state: str, error: str | None = None) -> None:
    conn = get_connection("content")
    conn.execute(
        "INSERT INTO review_preparations"
        " (item_type,item_key,sense_id,state,error,updated_at) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(item_type,item_key,sense_id) DO UPDATE SET"
        " state=excluded.state,error=excluded.error,updated_at=excluded.updated_at",
        (item_type, item_key, sense_id, state, error, repository.now_iso()),
    )
    conn.commit()


def _row(item_type: str, item_key: str, sense_id: int) -> dict[str, Any] | None:
    row = get_connection("content").execute(
        "SELECT p.*, j.status AS job_status,j.pause_requested,"
        " i.status AS item_status FROM review_preparations p"
        " LEFT JOIN llm_jobs j ON j.id=p.job_id"
        " LEFT JOIN llm_job_items i ON i.job_id=p.job_id AND i.seq=p.seq"
        " WHERE p.item_type=? AND p.item_key=? AND p.sense_id=?",
        (item_type, item_key, sense_id),
    ).fetchone()
    return dict(row) if row else None


def status(item_type: str, item_key: str, sense_id: int) -> dict[str, Any]:
    # Stock remains usable even if an obsolete job failed or was paused.
    if sentences.bilingual_count(item_key, sense_id, item_type=item_type) >= sentences.pool_target():
        return {"preparation_state": "ready", "preparation_error": None}
    row = _row(item_type, item_key, sense_id)
    state, error = "pending", None
    if row:
        job_status = row["job_status"]
        if row["pause_requested"]:
            state, error = "failed", "内容任务已请求暂停；可在原任务继续。"
        elif job_status == "capped":
            state, error = "failed", "内容任务达到配置的花费上限；调整上限后可在原任务继续。"
        elif job_status == "paused":
            state, error = "failed", row["error"] or "内容任务已暂停；可在原任务继续。"
        elif job_status == "failed":
            state, error = "failed", row["error"] or "内容准备失败；可在原任务继续。"
        elif job_status in ("pending", "running"):
            # A shared job may be retrying another item before this one obtains
            # a worker slot. Its previous failure must not stop client polling.
            state = job_status
        elif row["state"] == "failed":
            state, error = "failed", row["error"] or "内容准备失败；可在原任务继续。"
        elif row["item_status"] == "failed":
            state, error = "failed", row["error"] or "内容准备失败；可在原任务继续。"
        elif job_status == "done" or row["state"] == "ready":
            state, error = "failed", "完整双语句子库存仍不足，请继续补齐内容。"
    return {"preparation_state": state, "preparation_error": error}


def _bind(job_id: int, *, recovering: bool = False) -> None:
    conn = get_connection("content")
    items = conn.execute(
        "SELECT seq,payload FROM llm_job_items WHERE job_id=? ORDER BY seq", (job_id,)
    ).fetchall()
    with conn:
        for item in items:
            payload = json.loads(item["payload"])
            identity = (payload.get("item_type") or "word", payload["item_key"], int(payload["sense_id"]))
            conn.execute(
                "INSERT INTO review_preparations"
                " (item_type,item_key,sense_id,state,job_id,seq,updated_at)"
                " VALUES (?,?,?,'pending',?,?,?)"
                " ON CONFLICT(item_type,item_key,sense_id) DO UPDATE SET"
                " state='pending',error=NULL,job_id=excluded.job_id,seq=excluded.seq,"
                " updated_at=excluded.updated_at"
                + (" WHERE review_preparations.job_id IS NULL OR review_preparations.job_id < excluded.job_id"
                   if recovering else ""),
                (*identity, job_id, item["seq"], repository.now_iso()),
            )


def start(wanted: list[dict[str, Any]] | None, *, learner_id: int,
          provider_id: str | None) -> int | None:
    params: dict[str, Any] = {"learner_id": learner_id, "preparation": True}
    if wanted is not None:
        params["senses"] = wanted
    with _request_lock:
        units = sentences._plan(params)
        if not units:
            return None
        params["senses"] = [payload for _, payload in units]
        try:
            job_id = jobs.create(sentences.KIND, params=params, provider_id=provider_id)
            _bind(job_id)
            jobs.start(job_id)
        except Exception:
            # Failed starts are not empty plans. Never send the provider's raw
            # exception (which may contain credentials) to clients.
            for _, payload in units:
                set_state(payload["item_type"], payload["item_key"], payload["sense_id"],
                          "failed", "内容任务未能启动，请检查服务端模型配置后重试。")
            raise
        return job_id


def is_claimed(item_type: str, item_key: str, sense_id: int) -> bool:
    row = _row(item_type, item_key, sense_id)
    # A paused or failed job keeps its completed work and original attempt cap;
    # frequent snapshots must not bypass those controls by creating fresh jobs.
    return bool(row and row["job_status"] in (*jobs.RESUMABLE, "failed")
                and row["item_status"] != "done")


def on_job_finished(event: Any) -> None:
    if event.get("kind") != sentences.KIND or event.get("status") not in ("paused", "capped"):
        return
    conn = get_connection("content")
    conn.execute(
        "UPDATE review_preparations SET state='failed',"
        " error=COALESCE(error,?),updated_at=? WHERE job_id=? AND state!='ready'",
        ("内容任务已暂停；可在原任务继续。", repository.now_iso(), event.get("job_id")),
    )
    conn.commit()


def resume_interrupted() -> None:
    # Re-bind first: a crash between job creation and binding must not create a
    # duplicate batch after restart. Explicitly paused/capped jobs stay paused.
    conn = get_connection("content")
    rows = conn.execute(
        "SELECT id,status,params,pause_requested FROM llm_jobs WHERE kind=? AND status!='done' ORDER BY id",
        (sentences.KIND,),
    ).fetchall()
    recoverable = [row for row in rows if json.loads(row["params"] or "{}").get("preparation")]
    for row in recoverable:
        _bind(int(row["id"]), recovering=True)
    for row in recoverable:
        claimed = conn.execute(
            "SELECT p.state,i.status AS item_status FROM review_preparations p"
            " LEFT JOIN llm_job_items i ON i.job_id=p.job_id AND i.seq=p.seq WHERE p.job_id=?",
            (row["id"],),
        ).fetchall()
        interrupted = any(r["state"] in ("pending", "running")
                          or (r["state"] == "ready" and r["item_status"] != "done") for r in claimed)
        if claimed and not row["pause_requested"] and (row["status"] in ("pending", "running")
                        or (row["status"] == "paused" and interrupted)):
            jobs.start(int(row["id"]))
