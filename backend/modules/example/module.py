"""Example module — a working template for adding a feature.

Everything a feature can contribute is exercised here, in one file plus a
template:

* its own table, created by its own migration
* an event subscription, so it reacts without editing what emits the event
* an admin API endpoint
* an admin page, with its own template directory
* a runtime setting, declared where it is owned
* a navigation entry that appears automatically

**Nothing outside this directory was modified to make any of that work.** That
is the property Phase 0 exists to establish, and this module is how it stays
verifiable: if adding a feature ever starts requiring edits elsewhere, copying
this template will make that obvious immediately.

To create a real module: copy this package, rename it, replace the contents.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from backend.admin.templating import add_template_dir, render, require_page_auth
from backend.core import auth, runtime_config
from backend.core.db import Migration, get_connection
from backend.core.events import Event
from backend.core.logging import get_logger
from backend.core.registry import AdminPage, Module

log = get_logger("example")

add_template_dir(Path(__file__).parent / "templates")


# --------------------------------------------------------------------------- #
# 1. Schema — owned by this module, versioned independently of every other one
# --------------------------------------------------------------------------- #

MIGRATIONS = [
    Migration(
        version=1,
        name="example observation log",
        database="learning",
        apply="""
        CREATE TABLE IF NOT EXISTS example_observations (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            observed TEXT NOT NULL,
            note     TEXT
        );
        """,
    ),
]


# --------------------------------------------------------------------------- #
# 2. Configuration — declared by whoever owns the setting
# --------------------------------------------------------------------------- #

runtime_config.register(
    runtime_config.ConfigSpec(
        key="example_keep_observations",
        default=50,
        value_type="int",
        title="示例模块保留的观察条数",
        description="仅用于演示模块可以声明自己的配置项。删除示例模块时这一项也随之消失。",
        group="general",
        order=900,
    )
)


# --------------------------------------------------------------------------- #
# 3. Event subscription — reacts without touching the emitter
# --------------------------------------------------------------------------- #


def on_app_started(event: Event) -> None:
    """Record each service start.

    Note what is *not* happening: nothing in the startup path knows this module
    exists. Adding this behaviour required no change to ``main.py``.
    """
    conn = get_connection("learning")
    conn.execute(
        "INSERT INTO example_observations (observed, note) VALUES (?, ?)",
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            f"服务启动，装载于 {event.get('data_dir', '?')}",
        ),
    )
    keep = int(runtime_config.get("example_keep_observations"))
    conn.execute(
        "DELETE FROM example_observations WHERE id NOT IN"
        " (SELECT id FROM example_observations ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    conn.commit()
    log.debug("observation.recorded", "示例模块记录了一次服务启动")


# --------------------------------------------------------------------------- #
# 4. API — mounted under /v1/admin automatically
# --------------------------------------------------------------------------- #

admin_router = APIRouter(dependencies=[Depends(auth.require_admin)])
pages_router = APIRouter()


@admin_router.get("/example/observations", summary="示例模块的观察记录")
async def observations() -> dict[str, Any]:
    rows = get_connection("learning").execute(
        "SELECT id, observed, note FROM example_observations ORDER BY id DESC LIMIT 100"
    ).fetchall()
    return {"count": len(rows), "observations": [dict(r) for r in rows]}


# --------------------------------------------------------------------------- #
# 5. Page — template lives in this package
# --------------------------------------------------------------------------- #


@pages_router.get("/admin/example", response_class=HTMLResponse)
async def page(request: Request) -> Response:
    if (redirect := require_page_auth(request)) is not None:
        return redirect
    data = await observations()
    return render(request, "example.html", observations=data["observations"])


# --------------------------------------------------------------------------- #
# 6. Declaration — the only thing the registry looks for
# --------------------------------------------------------------------------- #

MODULE = Module(
    name="example",
    title="示例",
    description="演示模块：证明新增功能不需要改动任何已有代码。可随时整目录删除",
    migrations=MIGRATIONS,
    admin_router=admin_router,
    admin_router_pages=pages_router,
    admin_pages=[
        AdminPage(
            title="示例",
            path="/admin/example",
            order=900,
            description="扩展机制的活样板",
        )
    ],
    subscriptions={"app.started": [on_app_started]},
)
