"""Module declaration for phrases and usage (P11).

词组 phrase / 用法 usage

Two things the reader meets constantly and the app could not explain: a run of
words that means something as a unit (``account for``), and the collocation a
single sense is actually used in (``contribute`` 的「促成」带着 ``contribute
to``). Collins separates them by block type, and so does this module: phrases
get their own tables, usage hangs off ``senses``.
"""

from __future__ import annotations

from backend.core.registry import Module
from backend.modules.phrases import schema

MODULE = Module(
    name="phrases",
    title="词组与用法",
    description="词组清单（考纲表 ∩ 柯林斯）、词组义项、义项的搭配",
    migrations=schema.MIGRATIONS,
)
