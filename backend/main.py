"""Application entry point.

Assembly order here is deliberate and worth preserving:

1. Core migrations run before anything can touch a table.
2. Notifications attach to logging, so failures during module install are
   already capable of alerting.
3. Modules are discovered and mounted **at import time**, not inside the
   lifespan handler. Routes added after startup would not appear in the OpenAPI
   contract, and that contract is what architecture rule 5 obliges us to keep
   accurate for clients that update months late.
4. The lifespan handler is left for genuinely runtime concerns: log pruning at
   start, connection cleanup at shutdown.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse

from backend.admin.routes import admin_api, admin_pages
from backend.core import auth, events, notifications, runtime_config, tasks
from backend.core.config import get_settings
from backend.core.db import apply_pending_restore, close_connections, run_migrations
from backend.core.errors import install_error_handlers
from backend.core.logging import MIGRATIONS as LOG_MIGRATIONS
from backend.core.logging import get_logger, prune_logs, trace
from backend.core.registry import install_modules

log = get_logger("core.app")


def _run_core_migrations() -> None:
    """Schema owned by the framework itself.

    Core is treated as three small modules rather than one, so that a future
    change to, say, device tokens does not have to share a version number with
    an unrelated change to the logging tables.
    """
    run_migrations("core.logging", LOG_MIGRATIONS)
    run_migrations("core.config", runtime_config.MIGRATIONS)
    run_migrations("core.auth", auth.MIGRATIONS)
    run_migrations("core.tasks", tasks.MIGRATIONS)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    with trace() as tid:
        log.info(
            "app.started",
            "服务已启动",
            trace=tid,
            data_dir=str(settings.data_dir),
            dev_mode=settings.dev_mode,
        )
        # Announced on the bus as well as logged: modules react to the event,
        # and nothing here needs to know which ones. Emitted after migrations,
        # so a subscriber can safely touch its own tables.
        events.emit("app.started", data_dir=str(settings.data_dir), dev_mode=settings.dev_mode)

        try:
            removed = prune_logs(int(runtime_config.get("log_retention_days")))
            if removed:
                log.info("logs.pruned", f"清理了 {removed} 条过期技术日志", removed=removed)
        except Exception:  # noqa: BLE001 - never block startup on housekeeping
            log.exception("logs.prune.failed", "清理过期日志失败，服务继续启动")

        # Started here rather than at import time: the loop is a runtime concern
        # and needs a running event loop, and a task that fires during module
        # assembly would be touching half-installed state.
        try:
            tasks.start_loop()
        except Exception:  # noqa: BLE001 - a broken scheduler must not stop the service
            log.exception("task.loop.start.failed", "定时任务调度启动失败，服务继续运行")

    yield

    await tasks.stop_loop()
    with trace():
        log.info("app.stopping", "服务正在关闭")
    close_connections()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="English Reader API",
        version="0.1.0",
        summary="面向 CET-4/6 的自适应英语阅读学习系统",
        description=(
            "两套接口完全分离：`/v1/client/…` 供阅读客户端使用，"
            "`/v1/admin/…` 供管理控制台使用，客户端令牌无法访问管理接口。\n\n"
            "接口遵循只增不减的兼容规则——可以新增字段，不会删除字段或改变字段含义。"
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    install_error_handlers(app)

    @app.middleware("http")
    async def _trace_requests(request: Request, call_next) -> Response:
        """Give every request a trace, and report it back in a header.

        The header is what lets a client surface the id when something fails,
        which is the first link in the diagnostic chain described in the design.
        """
        started = time.perf_counter()
        with trace() as trace_id:
            response = await call_next(request)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            response.headers["X-Trace-Id"] = trace_id

            # DEBUG: buffered in memory, only written if this trace later fails.
            log.debug(
                "request.completed",
                f"{request.method} {request.url.path} -> {response.status_code}",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                elapsed_ms=elapsed_ms,
            )
            return response

    @app.get("/health", tags=["system"], summary="健康检查")
    async def health() -> dict[str, object]:
        """Unauthenticated liveness probe. Deliberately reveals nothing."""
        return {"status": "ok", "version": app.version}

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/admin")

    # --- assembly ---------------------------------------------------------- #

    # Before anything opens a connection: if the admin console staged a restore,
    # this is the only safe moment to swap it in.
    restored = apply_pending_restore()

    _run_core_migrations()

    for name, backup_path in restored.items():
        log.warning(
            "restore.applied",
            f"已用上传的数据替换 {name} 数据库，替换前的版本已备份",
            database=name,
            backup=str(backup_path),
        )

    notifications.install()

    app.include_router(admin_api, prefix="/v1/admin", tags=["admin:core"])
    app.include_router(admin_pages, include_in_schema=False)

    modules = install_modules(app)
    log.info(
        "app.modules.ready",
        f"已装载 {len(modules)} 个功能模块",
        modules=[m.name for m in modules],
    )

    return app


app = create_app()
