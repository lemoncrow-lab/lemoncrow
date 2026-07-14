"""Prompt-compiler error types (open, uncompiled).

Held open so the prompt-compiler algorithm (``compiler.py`` under
``lemoncrow.pro``) can compile to ``.so``: mypyc cannot compile a class that
subclasses a builtin (``ValueError``). ``lemoncrow.pro.capabilities.prompt_compilation.compiler``
re-exports this name, so existing imports keep working.
"""

from __future__ import annotations


class BudgetTooSmall(ValueError):
    """Raised when the dynamic tail budget cannot retain required user task blocks."""


__all__ = ["BudgetTooSmall"]
