"""Module declaration for word families (P1b).

Solves two things at once, which is why the design does them together: the
reader-facing morphological hint (``nationality ← nation + -ity``), and the
biggest single source of false out-of-syllabus counts. ``carefully``,
``readiness`` and ``supervisor`` are in no syllabus list because every syllabus
assumes that if you know the root, the rest is grammar — and the checker had no
way to know that until this module existed.
"""

from __future__ import annotations

from pathlib import Path

from backend.admin.templating import add_template_dir
from backend.core.registry import AdminPage, Module
from backend.modules.wordfamily import routes, schema, workers

add_template_dir(Path(__file__).parent / "templates")

workers.register()

MODULE = Module(
    name="wordfamily",
    title="词族",
    description="词根词缀、派生关系与 A/B/C 分级；规则先跑，模型只做判定",
    migrations=schema.MIGRATIONS,
    admin_router=routes.admin_router,
    admin_router_pages=routes.pages_router,
    admin_pages=[
        AdminPage(
            title="词族",
            path="/admin/wordfamily",
            order=57,
            description="构词分解与分级",
        )
    ],
)
