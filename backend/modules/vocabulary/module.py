"""Module declaration for vocabulary.

This is the file the registry looks for. Everything the feature contributes is
declared here in one place; nothing outside this package needs to know it
exists. Copy this shape to add a feature.
"""

from __future__ import annotations

from pathlib import Path

from backend.admin.templating import add_template_dir
from backend.core.registry import AdminPage, Module
from backend.modules.vocabulary import analyzer, routes, schema

# Templates live with the module rather than in the console's directory, so the
# feature stays self-contained.
add_template_dir(Path(__file__).parent / "templates")

MODULE = Module(
    name="vocabulary",
    title="词汇",
    description="词典数据与词形还原：把文中任何词形归到正确词条，并附带词频与大纲标签",
    migrations=schema.MIGRATIONS,
    admin_router=routes.admin_router,
    admin_router_pages=routes.pages_router,
    admin_pages=[
        AdminPage(
            title="词汇",
            path="/admin/vocabulary",
            order=50,
            description="词典状态与分析工具",
        )
    ],
    # Loads the language model at startup so the first analysis is not slow.
    # Failure here is logged and tolerated — a missing model disables analysis
    # but must not stop the service.
    on_startup=analyzer.warm,
)
