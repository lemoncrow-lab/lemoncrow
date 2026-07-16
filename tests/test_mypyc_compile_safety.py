"""Static safety net for the mypyc-compiled release build.

``hatch_build.py`` compiles nearly all of ``src/lemoncrow/`` to mypyc ``.so``
extensions for the shipped wheel (editable installs / ``uv run`` stay pure
Python -- see ``CustomBuildHook.initialize``'s early return on
``version == "editable"``). A compiled ("native") class instance behaves
differently from a plain Python object in ways an ordinary ``pytest -q`` run
never exercises, because the whole suite normally runs against the
uncompiled ``.py`` source:

* mypyc-native instances have **no** ``__dict__``. ``self.__dict__``
  access (bare, ``.get``, ``.setdefault``, ``[...]``) that works fine
  interpreted raises ``AttributeError`` only in the compiled build. This bit
  ``CodeContextEngine`` for real: ``self.__dict__.setdefault("_hef_anchor_cache", {})``
  et al. worked under ``uv run`` but raised
  ``AttributeError: 'CodeContextEngine' object has no attribute '__dict__'``
  from the shipped ``lc`` MCP server's ``code_search`` tool. Fixed by
  declaring the caches as real ``__init__``-assigned instance attributes
  instead of lazy ``self.__dict__`` entries.
* mypyc-native instances also have **no** ``__weakref__`` slot --
  ``weakref.ref(self, ...)`` / ``weakref.finalize(self, ...)`` raise
  ``TypeError: cannot create weak reference``. See
  ``lemoncrow/core/foundation/weakref_token.py``'s ``WeakRefToken`` sentinel,
  which every native class must route through instead.

Building the real wheel to catch these takes minutes (see
``tests/gateway/test_mcp_compiled_so.py``, which does exactly that for a
sibling mypyc gotcha). This module is the cheap, always-on guard: no compile
step, just an AST scan of every file mypyc will actually compile in the next
release build. It exists so a reappearance of either pattern fails an
ordinary ``pytest -q`` run immediately, long before a release build.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_HATCH_BUILD = REPO_ROOT / "hatch_build.py"

# hatch_build.py imports `hatchling` only to subclass BuildHookInterface for the
# real build hook -- irrelevant to `_find_compilable`, which this module only
# needs for its module-level regexes/constants. hatchling is a build-backend
# dependency (present under `uv build`'s isolated env), not a dev/test
# dependency, so `uv run pytest` doesn't have it installed. Stub the import
# rather than pulling in a real build dependency just to read a file list.
if "hatchling.builders.hooks.plugin.interface" not in sys.modules:
    _interface_mod = types.ModuleType("hatchling.builders.hooks.plugin.interface")
    _interface_mod.BuildHookInterface = type("BuildHookInterface", (), {})  # type: ignore[attr-defined]
    for _name in (
        "hatchling",
        "hatchling.builders",
        "hatchling.builders.hooks",
        "hatchling.builders.hooks.plugin",
    ):
        sys.modules.setdefault(_name, types.ModuleType(_name))
    sys.modules["hatchling.builders.hooks.plugin.interface"] = _interface_mod

_spec = importlib.util.spec_from_file_location("_hatch_build_under_test", _HATCH_BUILD)
assert _spec and _spec.loader
hatch_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hatch_build)


def _compiled_files() -> list[Path]:
    """Every file the next release wheel will mypyc-compile, per hatch_build.py."""
    src_dir = REPO_ROOT / "src"
    lemoncrow_src = src_dir / "lemoncrow"
    rels = hatch_build._find_compilable(lemoncrow_src, src_dir)
    return [src_dir / rel for rel in rels]


def _is_self(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "self"


def _find_self_dict_access(tree: ast.AST) -> list[int]:
    """Line numbers of any ``self.__dict__`` attribute access in *tree*.

    Matches regardless of what wraps it (``self.__dict__.get(...)``,
    ``self.__dict__[...]``, ``self.__dict__.setdefault(...)``, bare
    ``self.__dict__``) because ``ast.Attribute(value=self, attr="__dict__")``
    is the shared inner node in every one of those forms.
    """
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "__dict__" and _is_self(node.value)
        }
    )


def _find_weakref_on_self(tree: ast.AST) -> list[int]:
    """Line numbers of ``weakref.ref(self, ...)`` / ``weakref.finalize(self, ...)``.

    Covers both ``import weakref; weakref.ref(self)`` and
    ``from weakref import ref, finalize; ref(self)`` call shapes.
    """
    hits: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args or not _is_self(node.args[0]):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if name in {"ref", "finalize"} and (
            isinstance(func, ast.Name)
            or (isinstance(func, ast.Attribute) and getattr(func.value, "id", None) == "weakref")
        ):
            hits.add(node.lineno)
    return sorted(hits)


def test_no_self_dict_access_in_compiled_modules() -> None:
    """Regression guard: mypyc-native instances have no ``__dict__``.

    ``self.__dict__`` (get/setdefault/subscript/bare) must never reappear in a
    module mypyc will compile -- see ``CodeContextEngine``'s
    ``_hef_anchor_cache``/``_line_fts_total_cache``/``_line_fts_df_cache``, now
    real ``__init__`` attributes instead of ``self.__dict__`` lazy entries.
    """
    offenders: dict[str, list[int]] = {}
    for path in _compiled_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = _find_self_dict_access(tree)
        if lines:
            offenders[str(path.relative_to(REPO_ROOT))] = lines
    assert not offenders, (
        "self.__dict__ access found in module(s) the release wheel compiles to a "
        "mypyc-native class with no __dict__ -- use a real __init__-declared "
        f"instance attribute instead:\n{offenders}"
    )


def test_no_weakref_on_self_in_compiled_modules() -> None:
    """Regression guard: mypyc-native instances have no ``__weakref__`` slot.

    ``weakref.ref(self, ...)`` / ``weakref.finalize(self, ...)`` raise
    ``TypeError: cannot create weak reference`` in the compiled build. Route
    through ``lemoncrow.core.foundation.weakref_token.WeakRefToken`` (a
    non-native sentinel) instead -- see ``CodeContextEngine._gc_sentinel``.
    """
    offenders: dict[str, list[int]] = {}
    for path in _compiled_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = _find_weakref_on_self(tree)
        if lines:
            offenders[str(path.relative_to(REPO_ROOT))] = lines
    assert not offenders, (
        "weakref.ref(self, ...)/weakref.finalize(self, ...) found in module(s) "
        "the release wheel compiles to a mypyc-native class with no __weakref__ "
        f"slot -- route through WeakRefToken instead:\n{offenders}"
    )
