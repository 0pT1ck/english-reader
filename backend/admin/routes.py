"""Admin console: JSON API and server-rendered pages.

Two routers live here. ``admin_api`` is mounted under ``/v1/admin`` and is the
*only* way data reaches the console — the pages call the same endpoints anyone
else would (architecture rule: the interface has no privileged back channel).
That has a practical payoff: whoever is debugging can curl these endpoints
directly and see exactly what the page sees.

``admin_pages`` renders HTML. Server-side, no build step, no package manager.
Adding a page is a template plus a route.
"""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from backend.core import auth, events, notifications, runtime_config, tasks
from backend.core.config import get_settings
from backend.core.db import (
    BACKED_UP,
    DatabaseName,
    backup_database,
    database_size_bytes,
    get_connection,
)
from backend.core.errors import InvalidRequest
from backend.core.logging import get_logger
from backend.admin.templating import render, require_page_auth, templates
from backend.core.registry import installed_modules

log = get_logger("admin")

admin_api = APIRouter(dependencies=[Depends(auth.require_admin)])
admin_pages = APIRouter()

# SQLite files start with this exact string — a cheap guard against someone
# uploading the wrong file entirely.
SQLITE_MAGIC = b"SQLite format 3\x00"


def _staged_path(name: DatabaseName) -> Path:
    """Where a restore waits until the next start. See ``apply_pending_restore``."""
    settings = get_settings()
    return settings.data_dir / f"{name}.db.pending"


def _serialise(name: DatabaseName) -> bytes:
    """Consistent bytes for one database.

    Uses SQLite's serialize rather than reading the file: with WAL enabled a raw
    file read can miss committed data still in the sidecar, producing a backup
    that looks valid and silently isn't. The checkpoint folds the write-ahead
    log in first, so the result covers everything committed.
    """
    conn = get_connection(name)
    conn.execute("PRAGMA wal_checkpoint(FULL)")
    # "main" is the database itself; anything ATTACHed to the connection is
    # excluded, which is exactly what a per-database backup wants.
    return conn.serialize(name="main")


# --------------------------------------------------------------------------- #
# API — status
# --------------------------------------------------------------------------- #


@admin_api.get("/status", summary="系统状态")
async def status() -> dict[str, Any]:
    """Everything the status page shows, and what to check first when something
    looks wrong: are the databases growing, which modules loaded, who subscribes
    to what."""
    settings = get_settings()

    log_counts: dict[str, int] = {}
    try:
        rows = get_connection("logs").execute(
            "SELECT level, COUNT(*) AS n FROM logs"
            " WHERE ts > datetime('now', '-1 day') GROUP BY level"
        ).fetchall()
        log_counts = {row["level"]: row["n"] for row in rows}
    except sqlite3.Error:
        pass

    try:
        decisions_total = get_connection("events").execute(
            "SELECT COUNT(*) AS n FROM decisions"
        ).fetchone()["n"]
    except sqlite3.Error:
        decisions_total = 0

    return {
        "version": "0.1.0",
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dev_mode": settings.dev_mode,
        "databases": {
            name: {
                "path": str(path),
                "exists": path.exists(),
                "size_bytes": database_size_bytes(name),  # type: ignore[arg-type]
            }
            for name, path in (
                ("dictionary", settings.dictionary_db),
                ("content", settings.content_db),
                ("events", settings.events_db),
                ("ops", settings.ops_db),
                ("logs", settings.logs_db),
            )
        },
        "modules": [
            {"name": m.name, "title": m.title, "description": m.description}
            for m in installed_modules().values()
        ],
        "event_subscribers": events.subscriber_summary(),
        "logs_last_24h": log_counts,
        "decisions_total": decisions_total,
        "devices": auth.list_devices(),
        "restore_pending": sorted(
            name for name in BACKED_UP if _staged_path(name).exists()
        ),
    }


# --------------------------------------------------------------------------- #
# API — logs
# --------------------------------------------------------------------------- #


@admin_api.get("/logs", summary="查询技术日志")
async def query_logs(
    level: Annotated[str | None, Query(description="最低级别")] = None,
    module: Annotated[str | None, Query()] = None,
    event: Annotated[str | None, Query(description="事件名前缀匹配")] = None,
    trace_id: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query(description="ISO 时间，包含")] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> dict[str, Any]:
    """Filtered technical logs, newest first.

    ``trace_id`` is the one that matters most: given the id from an error
    response, this returns that request's entire chain including the DEBUG
    records flushed when it failed.
    """
    clauses: list[str] = []
    params: list[Any] = []

    if level:
        order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        floor = order.get(level.upper())
        if floor is None:
            raise InvalidRequest("未知的日志级别", level=level)
        allowed = [name for name, value in order.items() if value >= floor]
        clauses.append(f"level IN ({','.join('?' * len(allowed))})")
        params.extend(allowed)
    if module:
        clauses.append("module = ?")
        params.append(module)
    if event:
        clauses.append("event LIKE ?")
        params.append(f"{event}%")
    if trace_id:
        clauses.append("trace_id = ?")
        params.append(trace_id)
    if since:
        clauses.append("ts >= ?")
        params.append(since)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = get_connection("logs").execute(
        f"SELECT * FROM logs {where} ORDER BY id DESC LIMIT ?", (*params, limit)
    ).fetchall()

    return {"count": len(rows), "records": [dict(row) for row in rows]}


@admin_api.get("/decisions", summary="查询决策日志")
async def query_decisions(
    kind: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Why the system did what it did. Never pruned — see core.logging."""
    clause = "WHERE kind = ?" if kind else ""
    params: tuple[Any, ...] = (kind, limit) if kind else (limit,)
    rows = get_connection("events").execute(
        f"SELECT * FROM decisions {clause} ORDER BY id DESC LIMIT ?", params
    ).fetchall()
    return {"count": len(rows), "records": [dict(row) for row in rows]}


@admin_api.get("/diagnostic-bundle", summary="导出诊断包")
async def diagnostic_bundle(
    hours: Annotated[int, Query(ge=1, le=720)] = 24,
) -> Response:
    """One file containing everything needed to diagnose a problem remotely.

    Exists for the case the user cannot describe: "something's wrong, look for
    yourself". Bundles status, recent logs, recent decisions and current
    settings — with secrets already redacted by the logging layer.
    """
    since = f"-{hours} hours"
    logs = get_connection("logs").execute(
        "SELECT * FROM logs WHERE ts > datetime('now', ?) ORDER BY id DESC LIMIT 5000",
        (since,),
    ).fetchall()
    decisions = get_connection("events").execute(
        "SELECT * FROM decisions WHERE ts > datetime('now', ?) ORDER BY id DESC LIMIT 1000",
        (since,),
    ).fetchall()

    # Whitelist, not blacklist: every setting whose owner declared it a
    # credential is dropped. A blacklist here would silently leak the first
    # secret a later phase forgets to add to it.
    settings_snapshot = runtime_config.all_values(include_secrets=False)

    bundle = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_hours": hours,
        "status": await status(),
        "settings": settings_snapshot,
        "logs": [dict(row) for row in logs],
        "decisions": [dict(row) for row in decisions],
    }
    payload = json.dumps(bundle, ensure_ascii=False, indent=2, default=str)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log.info("diagnostic.bundle.created", f"导出了诊断包，覆盖最近 {hours} 小时", hours=hours)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="diagnostic-{stamp}.json"'},
    )


# --------------------------------------------------------------------------- #
# API — configuration
# --------------------------------------------------------------------------- #


@admin_api.get("/config", summary="读取运行配置")
async def read_config() -> dict[str, Any]:
    return {
        "specs": [
            {
                "key": s.key,
                "title": s.title,
                "description": s.description,
                "group": s.group,
                "value_type": s.value_type,
                "default": s.default,
                "value": runtime_config.get(s.key),
            }
            for s in runtime_config.specs()
        ]
    }


@admin_api.put("/config/{key}", summary="修改一项运行配置")
async def write_config(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if "value" not in payload:
        raise InvalidRequest("请求体需要包含 value 字段")
    try:
        runtime_config.set(key, payload["value"])
    except KeyError as exc:
        raise InvalidRequest(str(exc)) from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise InvalidRequest(f"值的格式不正确：{exc}") from exc
    return {"key": key, "value": runtime_config.get(key)}


@admin_api.post("/notifications/test", summary="发送测试通知")
async def test_notification() -> dict[str, Any]:
    ok = notifications.test_push()
    return {"sent": ok}


# --------------------------------------------------------------------------- #
# API — devices
# --------------------------------------------------------------------------- #


@admin_api.get("/devices", summary="列出客户端设备")
async def get_devices() -> dict[str, Any]:
    return {"devices": auth.list_devices()}


@admin_api.post("/devices", summary="注册设备并签发令牌")
async def add_device(payload: dict[str, str]) -> dict[str, Any]:
    name = (payload.get("name") or "").strip()
    if not name:
        raise InvalidRequest("设备名称不能为空")
    token = auth.create_device(name)
    return {"name": name, "token": token, "note": "该令牌只显示这一次，请立即保存"}


@admin_api.delete("/devices/{device_id}", summary="吊销设备令牌")
async def delete_device(device_id: int) -> dict[str, Any]:
    return {"revoked": auth.revoke_device(device_id)}


# --------------------------------------------------------------------------- #
# API — backup and restore
# --------------------------------------------------------------------------- #


@admin_api.get("/backup", summary="下载完整备份")
async def download_backup() -> Response:
    """Download everything irreplaceable as one zip.

    Two databases go in, not one: ``learning.db`` is the user's own record, and
    ``content.db`` holds senses, examples and word families that cost real money
    to generate. The two rebuildable databases stay out — that is what keeps
    this file small enough to move around casually.

    A zip rather than a single file because the pair must travel together; a
    restore that brought back study state but not the content it references
    would leave a half-working install.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    buffer = io.BytesIO()
    sizes: dict[str, int] = {}

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in BACKED_UP:
            payload = _serialise(name)
            sizes[name] = len(payload)
            archive.writestr(f"{name}.db", payload)
        archive.writestr(
            "MANIFEST.json",
            json.dumps(
                {
                    "product": "english-reader",
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "databases": sizes,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

    payload = buffer.getvalue()
    log.info(
        "backup.downloaded",
        "下载了完整备份",
        size_bytes=len(payload),
        databases=sizes,
    )
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="english-reader-{stamp}.zip"'
        },
    )


@admin_api.get("/backup/{name}", summary="下载单个数据库")
async def download_one(name: str) -> Response:
    """One database on its own, for when only one needs moving."""
    if name not in BACKED_UP:
        raise InvalidRequest("只能下载 learning 或 content", name=name)
    payload = _serialise(name)  # type: ignore[arg-type]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log.info("backup.downloaded", f"下载了 {name} 数据库", database=name, size_bytes=len(payload))
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}-{stamp}.db"'},
    )


def _stage(name: DatabaseName, payload: bytes) -> int:
    """Validate one database and park it for the next start.

    Verifying before accepting matters: a restart that swapped in an unrelated
    database would leave the app broken with no obvious cause.
    """
    if not payload.startswith(SQLITE_MAGIC):
        raise InvalidRequest(f"{name}.db 不是 SQLite 数据库")

    staged = _staged_path(name)
    staged.write_bytes(payload)
    try:
        probe = sqlite3.connect(staged)
        try:
            probe.execute("SELECT 1 FROM schema_migrations LIMIT 1").fetchone()
        finally:
            probe.close()
    except sqlite3.Error as exc:
        staged.unlink(missing_ok=True)
        raise InvalidRequest(f"这个 {name}.db 不是 English Reader 的数据库") from exc
    return len(payload)


@admin_api.post("/restore", summary="上传备份以恢复")
async def upload_restore(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    """Stage an uploaded backup for restore on next start.

    Accepts either the zip produced by the backup button or a bare ``.db``
    file — the latter both for backups taken before the zip existed and for the
    case where only one database needs replacing. A bare file is identified by
    the tables it contains rather than by its name, since browsers rename
    downloads freely.

    Files are *staged*, not swapped in place. Live connections are per-thread
    and cannot all be closed from here, and on Windows an open file cannot be
    replaced at all. Staging plus a restart is the version of this operation
    that cannot corrupt anything: the swap happens at startup before a single
    connection exists.
    """
    payload = await file.read()
    staged: dict[str, int] = {}

    if payload[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = {Path(n).name: n for n in archive.namelist()}
            for name in BACKED_UP:
                member = members.get(f"{name}.db")
                if member is None:
                    continue
                staged[name] = _stage(name, archive.read(member))
        if not staged:
            raise InvalidRequest("这个压缩包里没有 learning.db 或 content.db")
    else:
        name = _identify(payload)
        staged[name] = _stage(name, payload)

    log.warning(
        "restore.staged",
        "已接收恢复用的数据，将在下次启动时生效",
        databases=staged,
    )
    return {
        "staged": sorted(staged),
        "size_bytes": staged,
        "note": "重启服务后生效。当前数据会在替换前自动备份。",
    }


def _identify(payload: bytes) -> DatabaseName:
    """Work out which database a bare uploaded file is, by its tables.

    Filenames are unreliable — browsers append "(1)" and users rename things —
    so the content decides: each file is recognised by a table only it has.

    **2026-09-18 (P9 §10): it has to be recognised, not assumed.** Before the
    split there were two restorable files and "not content.db" was a safe
    default. Now a wrong guess writes one file's tables over another's, so an
    unrecognised upload is refused instead — and a ``learning.db`` from before
    the split is refused by name, because restoring it would silently reinstate
    the very layout this phase took apart.
    """
    if not payload.startswith(SQLITE_MAGIC):
        raise InvalidRequest("上传的文件既不是压缩包也不是 SQLite 数据库")

    settings = get_settings()
    probe_path = settings.data_dir / "restore-probe.db"
    probe_path.write_bytes(payload)
    try:
        probe = sqlite3.connect(probe_path)
        try:
            names = {
                row[0]
                for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            probe.close()
    except sqlite3.Error as exc:
        raise InvalidRequest("无法读取这个数据库") from exc
    finally:
        probe_path.unlink(missing_ok=True)

    if "senses" in names or "word_families" in names:
        return "content"
    if "client_events" in names or "study_marks" in names:
        return "events"
    if {"reading_articles", "devices", "settings"} <= names:
        raise InvalidRequest(
            "这是拆库之前的 learning.db。它里面混着内容、学习记录和本机配置，"
            "恢复它会把现在这三个库覆盖成旧的那一份——"
            "要用它请先在一个副本上跑一次重切（P9 §10）")
    raise InvalidRequest(
        "认不出这是哪个库。可恢复的只有 content.db 与 events.db，"
        "而认错了会把一个库的表写到另一个库上——那种错是静默的")


@admin_api.post("/backup/snapshot", summary="立即生成一次本地备份")
async def snapshot() -> dict[str, Any]:
    made = [backup_database(name, "manual") for name in BACKED_UP]
    return {
        "paths": [str(p) for p in made],
        "size_bytes": {p.name: p.stat().st_size for p in made},
    }


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


@admin_pages.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    if auth.is_admin_request(request):
        return RedirectResponse(url="/admin", status_code=303)  # type: ignore[return-value]
    return templates.TemplateResponse(request=request, name="login.html", context={})


@admin_pages.post("/admin/login")
async def login_submit(
    request: Request, secret: Annotated[str, Form()]
) -> Response:
    if not auth.check_admin_secret(secret):
        log.warning("admin.login.failed", "管理控制台登录失败")
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "密码不正确"},
            status_code=401,
        )

    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.issue_session(),
        max_age=auth.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    log.info("admin.login.ok", "管理控制台登录成功")
    return response


@admin_pages.get("/admin/logout")
async def logout() -> Response:
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


@admin_pages.get("/admin", response_class=HTMLResponse)
async def status_page(request: Request) -> Response:
    if (redirect := require_page_auth(request)) is not None:
        return redirect
    return render(request, "status.html", data=await status())


@admin_pages.get("/admin/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    level: str | None = None,
    event: str | None = None,
    trace_id: str | None = None,
    view: str = "technical",
) -> Response:
    if (redirect := require_page_auth(request)) is not None:
        return redirect

    if view == "decisions":
        payload = await query_decisions(limit=200)
    else:
        payload = await query_logs(level=level, event=event, trace_id=trace_id, limit=300)

    return render(
        request,
        "logs.html",
        view=view,
        records=payload["records"],
        filters={"level": level or "", "event": event or "", "trace_id": trace_id or ""},
    )


@admin_pages.get("/admin/config", response_class=HTMLResponse)
async def config_page(request: Request) -> Response:
    if (redirect := require_page_auth(request)) is not None:
        return redirect
    payload = await read_config()
    groups: dict[str, list[dict[str, Any]]] = {}
    for spec in payload["specs"]:
        groups.setdefault(spec["group"], []).append(spec)
    return render(request, "config.html", groups=groups)


@admin_pages.get("/admin/backup", response_class=HTMLResponse)
async def backup_page(request: Request) -> Response:
    if (redirect := require_page_auth(request)) is not None:
        return redirect
    backups = sorted(
        (p for name in BACKED_UP for p in get_settings().backup_dir.glob(f"{name}-*.db")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:12]
    return render(
        request,
        "backup.html",
        sizes={name: database_size_bytes(name) for name in BACKED_UP},
        backups=[
            {
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(
                    timespec="seconds"
                ),
            }
            for p in backups
        ],
        restore_pending=sorted(
            name for name in BACKED_UP if _staged_path(name).exists()
        ),
    )


# --------------------------------------------------------------------------- #
# Scheduled tasks
# --------------------------------------------------------------------------- #


@admin_api.get("/tasks", summary="定时任务的状态")
async def task_status() -> dict[str, Any]:
    return {"tick_seconds": tasks.TICK_SECONDS, "tasks": tasks.status()}


@admin_api.post("/tasks/{name}/run", summary="立刻跑一次这个任务")
async def task_run(name: str) -> dict[str, Any]:
    """The third of the three safety nets (主文档 §H): read, clock, and by hand.

    Runs in the request thread on purpose. These jobs take minutes, so the
    response is slow — but a fire-and-forget button that returns instantly and
    then fails silently is exactly the shape this project keeps getting bitten
    by, and the console is the diagnostic channel.
    """
    try:
        return tasks.run_now(name)
    except KeyError as exc:
        raise InvalidRequest(f"没有这个任务：{name}", task=name) from exc


@admin_pages.get("/admin/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request) -> Response:
    if (redirect := require_page_auth(request)) is not None:
        return redirect
    return render(request, "tasks.html",
                  tasks=tasks.status(), tick=tasks.TICK_SECONDS)
