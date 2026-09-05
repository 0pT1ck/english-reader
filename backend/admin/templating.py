"""Template environment shared by the console and by feature modules.

Split out of ``routes.py`` so a module can contribute its own pages without
importing the console's routes — which would be a circular import, and more
importantly would make module pages depend on core internals.

A module ships its own ``templates/`` directory, registers it here, and extends
``base.html`` like any core page. Its navigation entry comes from the ``Module``
declaration, so nothing central needs editing (architecture rule 6).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

from backend.core import auth
from backend.core.registry import AdminPage, navigation

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Replace the single-directory loader with a chain, so module template
# directories can be appended at import time.
templates.env.loader = ChoiceLoader([FileSystemLoader(str(TEMPLATES_DIR))])


def add_template_dir(path: Path | str) -> None:
    """Let a module serve templates from its own directory."""
    loader = templates.env.loader
    assert isinstance(loader, ChoiceLoader)  # noqa: S101 - set immediately above
    loader.loaders.append(FileSystemLoader(str(path)))


# Pages provided by the framework itself. Module pages are appended from the
# registry, so navigation always reflects what is actually installed.
CORE_PAGES = [
    AdminPage(title="状态", path="/admin", order=10, description="服务与数据概览"),
    AdminPage(title="日志", path="/admin/logs", order=20, description="技术日志与决策日志"),
    AdminPage(title="配置", path="/admin/config", order=30, description="可热改的运行参数"),
    AdminPage(title="备份", path="/admin/backup", order=40, description="下载与恢复学习数据"),
]


def nav() -> list[AdminPage]:
    return sorted(CORE_PAGES + navigation(), key=lambda p: (p.order, p.title))


def render(request: Request, template: str, **context: Any) -> HTMLResponse:
    """Render a console page with navigation already resolved."""
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={"nav": nav(), "current_path": request.url.path, **context},
    )


def require_page_auth(request: Request) -> RedirectResponse | None:
    """Guard a server-rendered page, redirecting to login instead of erroring.

    Shared with feature modules so their pages behave identically to core ones
    without importing the console's routes.
    """
    if auth.is_admin_request(request):
        return None
    return RedirectResponse(url="/admin/login", status_code=303)
