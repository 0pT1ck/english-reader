"""Module discovery and mounting.

A *module* here is one self-contained feature: its own tables, its own API
routes, its own admin pages, its own event subscriptions, its own background
tasks. Adding a feature means creating ``backend/modules/<name>/module.py`` with
a ``MODULE`` object in it — **and editing nothing else**. No route table, no
navigation menu, no migration index, no task list.

That "editing nothing else" is not a nicety; it is verification item 6 of Phase
0, and it is the property that keeps eight more phases of feature work from
turning into eight rounds of merge-conflict archaeology in the same three files.

Discovery is by directory scan rather than an explicit list, precisely so that
the "register it somewhere" step cannot be forgotten or become a conflict point.

How to add a module — the shape to copy::

    # backend/modules/example/module.py
    from fastapi import APIRouter
    from backend.core.db import Migration
    from backend.core.registry import AdminPage, Module

    client_router = APIRouter()
    admin_router = APIRouter()

    MODULE = Module(
        name="example",
        title="示例",
        description="what this feature is for",
        migrations=[Migration(version=1, name="...", database="learning", apply="...")],
        client_router=client_router,
        admin_router=admin_router,
        admin_pages=[AdminPage(title="示例", path="/admin/example", order=90)],
        subscriptions={"reading.finished": [on_reading_finished]},
        tasks=[Task(name="example.nightly", title="示例夜间任务",
                    run=do_the_thing, schedule="04:00")],
    )
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Any

from backend.core import events, tasks
from backend.core.db import Migration, run_migrations
from backend.core.logging import get_logger
from backend.core.tasks import Task

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter, FastAPI

log = get_logger("core.registry")

MODULES_PACKAGE = "backend.modules"


@dataclass(frozen=True)
class AdminPage:
    """One entry in the admin console navigation.

    Modules declare their own pages so the navigation is assembled from what is
    actually installed, rather than maintained by hand in a template.
    """

    title: str
    path: str
    order: int = 100
    description: str = ""


@dataclass
class Module:
    """Everything one feature contributes to the application."""

    name: str
    title: str
    description: str = ""

    #: Schema changes owned by this module. Versions are per-module, so two
    #: modules developed in parallel never need to agree on numbering.
    migrations: list[Migration] = field(default_factory=list)

    #: Mounted under /v1/client — consumed by the reading clients.
    client_router: "APIRouter | None" = None

    #: Mounted under /v1/admin — consumed by the admin console only. Client
    #: tokens can never reach these (architecture rule 4).
    admin_router: "APIRouter | None" = None

    #: Server-rendered admin pages, mounted at their own absolute paths.
    admin_router_pages: "APIRouter | None" = None

    #: Navigation entries for the pages above.
    admin_pages: list[AdminPage] = field(default_factory=list)

    #: event name -> handlers. Registered at startup.
    subscriptions: dict[str, list[events.Handler]] = field(default_factory=dict)

    #: Work that runs on a clock rather than on a request. Each task also gets
    #: two settings of its own (on/off, and the schedule) registered for it, so
    #: declaring one here is the whole job — see :mod:`backend.core.tasks`.
    tasks: list["Task"] = field(default_factory=list)

    #: Called once after migrations, for warm-up work (loading a model, etc).
    on_startup: Callable[[], None] | None = None


_installed: dict[str, Module] = {}


def discover_modules() -> list[Module]:
    """Import every package under ``backend/modules`` and collect its ``MODULE``.

    A package without a ``module.py``, or with one that lacks ``MODULE``, is
    skipped with a warning rather than crashing the app — a half-finished module
    directory should not stop the service from booting.
    """
    found: list[Module] = []

    try:
        package: ModuleType = importlib.import_module(MODULES_PACKAGE)
    except ModuleNotFoundError:
        log.warning("modules.package.missing", f"找不到模块包 {MODULES_PACKAGE}")
        return found

    for info in pkgutil.iter_modules(package.__path__):
        if not info.ispkg or info.name.startswith("_"):
            continue

        dotted = f"{MODULES_PACKAGE}.{info.name}.module"
        try:
            imported = importlib.import_module(dotted)
        except ModuleNotFoundError:
            log.warning(
                "module.entrypoint.missing",
                f"模块 {info.name} 缺少 module.py，已跳过",
                module=info.name,
            )
            continue
        except Exception:  # noqa: BLE001
            log.exception(
                "module.import.failed",
                f"模块 {info.name} 导入失败，已跳过",
                module=info.name,
            )
            continue

        candidate: Any = getattr(imported, "MODULE", None)
        if not isinstance(candidate, Module):
            log.warning(
                "module.declaration.missing",
                f"模块 {info.name} 没有声明 MODULE，已跳过",
                module=info.name,
            )
            continue

        found.append(candidate)

    return found


def install_modules(app: "FastAPI", modules: list[Module] | None = None) -> list[Module]:
    """Run migrations, wire subscriptions and mount routes for every module.

    Order matters: migrations first (so a subscriber cannot fire against a table
    that does not exist yet), then subscriptions, then routes, then start-up
    hooks.
    """
    modules = discover_modules() if modules is None else modules

    for module in modules:
        _installed[module.name] = module

        if module.migrations:
            applied = run_migrations(module.name, module.migrations)
            if applied:
                log.info(
                    "module.migrated",
                    f"模块 {module.name} 应用了 {len(applied)} 个迁移",
                    module=module.name,
                    versions=[m.version for m in applied],
                )

        for event_name, handlers in module.subscriptions.items():
            for handler in handlers:
                events.subscribe(event_name, handler, owner=module.name)

        if module.tasks:
            tasks.register(*module.tasks)

        if module.client_router is not None:
            app.include_router(
                module.client_router,
                prefix="/v1/client",
                tags=[f"client:{module.name}"],
            )
        if module.admin_router is not None:
            app.include_router(
                module.admin_router,
                prefix="/v1/admin",
                tags=[f"admin:{module.name}"],
            )
        if module.admin_router_pages is not None:
            app.include_router(module.admin_router_pages, include_in_schema=False)

        if module.on_startup is not None:
            try:
                module.on_startup()
            except Exception:  # noqa: BLE001
                log.exception(
                    "module.startup.failed",
                    f"模块 {module.name} 的启动钩子失败，服务继续运行",
                    module=module.name,
                )

        log.info(
            "module.installed",
            f"模块 {module.name} 已挂载",
            module=module.name,
            client_routes=module.client_router is not None,
            admin_routes=module.admin_router is not None,
            pages=[p.path for p in module.admin_pages],
            tasks=[t.name for t in module.tasks],
        )

    return modules


def installed_modules() -> dict[str, Module]:
    return dict(_installed)


def navigation() -> list[AdminPage]:
    """All admin navigation entries, ordered. Assembled from installed modules."""
    pages = [page for module in _installed.values() for page in module.admin_pages]
    return sorted(pages, key=lambda p: (p.order, p.title))
