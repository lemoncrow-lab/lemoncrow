"""``lemoncrow_client.kit`` is the shared library, so its imports are closed.

The main ``lemoncrow`` package imports kit too. If kit reached into the client's
session, transport, config or ``localtools``, the main package would inherit the
client's process model; if it imported a third-party package, the client would
lose its zero-dependency audit. Either way the two copies would start to drift
again, which is the problem kit exists to end.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

from _audit_source import PACKAGE_ROOT

KIT = PACKAGE_ROOT / "kit"


def _imported_modules(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                yield node.module or ""
                continue
            # Level 1 is kit itself; level 2 is the lemoncrow_client package.
            base = "lemoncrow_client.kit" if node.level == 1 else "lemoncrow_client"
            yield f"{base}.{node.module}" if node.module else base


def _allowed(module: str) -> bool:
    if module.split(".")[0] in sys.stdlib_module_names:
        return True
    return module in {"lemoncrow_client.errors", "lemoncrow_client.kit"} or module.startswith("lemoncrow_client.kit.")


def test_kit_is_a_package() -> None:
    assert (KIT / "__init__.py").is_file()


def test_kit_imports_only_the_standard_library_the_errors_module_and_itself() -> None:
    offenders = [
        f"{path.name} imports {module}"
        for path in sorted(KIT.glob("*.py"))
        for module in _imported_modules(path)
        if not _allowed(module)
    ]
    assert not offenders, offenders
