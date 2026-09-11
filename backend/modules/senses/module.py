"""Module declaration for sense sets (P1b).

The body of P1b. Without sense sets there is no three-layer gloss in P2, no
sense-level mastery later, and no way to arrange a familiar word's unfamiliar
meaning — which is the thing CET reading tests most.

Everything expensive here runs as a batch job, so it resumes after an
interruption and stops at a spend cap.
"""

from __future__ import annotations

from pathlib import Path

from backend.admin.templating import add_template_dir
from backend.core.registry import AdminPage, Module
from backend.modules.senses import routes, schema, workers

add_template_dir(Path(__file__).parent / "templates")

workers.register()

MODULE = Module(
    name="senses",
    title="义项",
    description="义项集：按英文概念划分，中文只是投影；主题标签搭车产出",
    migrations=schema.MIGRATIONS,
    admin_router=routes.admin_router,
    admin_router_pages=routes.pages_router,
    admin_pages=[
        AdminPage(
            title="义项",
            path="/admin/senses",
            order=58,
            description="义项集与三层验证",
        )
    ],
)
