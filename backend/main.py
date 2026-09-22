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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from backend.admin.routes import admin_api, admin_pages
from backend.core import auth, events, notifications, runtime_config, tasks
from backend.core.config import get_settings
from backend.core.db import apply_pending_restore, close_connections, run_migrations
from backend.core.errors import install_error_handlers
from backend.core.logging import MIGRATIONS as LOG_MIGRATIONS
from backend.core.logging import get_logger, prune_logs, trace
from backend.core.registry import install_modules, run_core_migrations
from backend.core import resplit

log = get_logger("core.app")


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

    @app.middleware("http")
    async def _require_contract_version(request: Request, call_next) -> Response:
        """Turn away clients whose contract is older than the server's floor.

        **This is what makes "clients must be current" a rule instead of a wish**
        (P11 决定 ⑲). Since 2026-09-22 the API may delete fields and change what
        one means, and a client that missed the change does not crash — it shows
        the wrong thing, quietly. 426 stops it at the door instead.

        Three things it deliberately does not do:

        * **it does not trust a missing header.** No header means a client built
          before the header existed, which is the oldest kind there is. Reading
          it as "unknown, let it through" would wave through exactly the clients
          this exists to stop;
        * **it does not touch `/v1/admin/`.** Those go through another
          credential (架构铁律 6's recorded exception), and a phone whose app is
          too old is precisely when the developer options are needed;
        * **it does not touch `/health`.** A liveness probe that fails because
          of a version floor reports an outage that is not happening.
        """
        path = request.url.path
        if path.startswith("/v1/client/"):
            floor = int(runtime_config.get("min_contract_version"))
            if floor > 0:
                try:
                    sent = int(request.headers.get("X-Contract-Version") or 0)
                except ValueError:
                    sent = 0
                if sent < floor:
                    log.info(
                        "client.version.rejected",
                        f"客户端契约版本 {sent} 低于下限 {floor}，拒绝",
                        path=path, sent=sent, floor=floor,
                    )
                    return JSONResponse(
                        status_code=426,
                        content={"error": "upgrade_required",
                                 "message": f"这份客户端太旧了（契约版本 {sent}，"
                                            f"服务端要求至少 {floor}），请更新 App。",
                                 "min_contract_version": floor},
                    )
        return await call_next(request)

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

    # **重切要排在迁移之前**（P9 §10）。它搬的是表和 `schema_migrations` 的行，
    # 而迁移框架正是照那些行判断「这条跑过没有」——先搬完，框架才会在每个文件里
    # 找到它该找到的记录，从而一条都不重跑。反过来的话，每个模块的全部迁移会在
    # 新库上重跑一遍，而 `ALTER TABLE ADD COLUMN` 重跑是会失败的。
    #
    # 已经切过的装机上它什么也不做（`pending()` 是空的），所以留在这里不花钱。
    split = resplit.run()

    run_core_migrations()

    if split:
        log.warning(
            "resplit.applied",
            f"learning.db 已经拆成 content / events / ops："
            f"{len(split['tables'])} 张表、{split['records']} 条迁移记录。"
            f"拆之前那份备份在 {split['snapshot']}",
            tables=len(split["tables"]), records=split["records"],
        )

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
