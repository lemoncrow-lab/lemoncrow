"""Unit tests for the verify-before-done completeness detectors (A + B).

These exercise the pure detector logic directly (the end-to-end block decision
is covered by test_verify_before_done_hook.py).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HOOK = Path("integrations/claude/plugin/hooks/verify_before_done.py")
_spec = importlib.util.spec_from_file_location("vbd_hook", _HOOK)
assert _spec and _spec.loader
vbd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vbd)


# --- Detector A -------------------------------------------------------------
def test_contract_change_detected() -> None:
    diffs = [
        (
            "sympy/combinatorics/permutations.py",
            "    @staticmethod\n    def _af_new(perm):\n        p = Basic.__new__(Perm, perm)",
            "    @classmethod\n    def _af_new(cls, perm):\n        p = Basic.__new__(cls, perm)",
        )
    ]
    assert vbd._contract_changed_symbols(diffs) == {"_af_new"}


def test_contract_change_ignores_body_only_edit() -> None:
    diffs = [("m.py", "    def foo(x):\n        return 1", "    def foo(x):\n        return 2")]
    assert vbd._contract_changed_symbols(diffs) == set()


def test_bare_call_sites_excludes_def_and_dotted(tmp_path) -> None:
    f = tmp_path / "m.py"
    f.write_text(
        "def _af_new(cls, p):\n    return p\nx = _af_new(a)\ny = self._af_new(b)\nz = cls._af_new(c)\n",
        encoding="utf-8",
    )
    sites = vbd._bare_call_sites("_af_new", str(tmp_path))
    assert len(sites) == 1
    assert sites[0].endswith(":3")  # only the bare `x = _af_new(a)` call


def test_detector_a_fires_on_unconverted_callsite(tmp_path) -> None:
    (tmp_path / "permutations.py").write_text(
        "class P:\n"
        "    @classmethod\n"
        "    def _af_new(cls, p):\n"
        "        return p\n"
        "    def mul(self):\n"
        "        return _af_new(x)\n",
        encoding="utf-8",
    )
    diffs = [
        (
            "permutations.py",
            "    @staticmethod\n    def _af_new(perm):",
            "    @classmethod\n    def _af_new(cls, perm):",
        )
    ]
    res = vbd.detector_a(diffs, root=str(tmp_path))
    assert res is not None
    sym, sites = res
    assert sym == "_af_new"
    assert len(sites) == 1


def test_detector_a_silent_when_all_converted(tmp_path) -> None:
    (tmp_path / "permutations.py").write_text(
        "class P:\n"
        "    @classmethod\n"
        "    def _af_new(cls, p):\n"
        "        return p\n"
        "    def mul(self):\n"
        "        return self._af_new(x)\n",
        encoding="utf-8",
    )
    diffs = [
        (
            "permutations.py",
            "    @staticmethod\n    def _af_new(perm):",
            "    @classmethod\n    def _af_new(cls, perm):",
        )
    ]
    assert vbd.detector_a(diffs, root=str(tmp_path)) is None


# --- Detector B -------------------------------------------------------------
def test_second_scenario_token_backtick() -> None:
    p = "The issue also reproduces if you create the mentioned plot using `scatterplot`."
    assert vbd._second_scenario_token(p) == "scatterplot"


def test_second_scenario_token_none() -> None:
    assert vbd._second_scenario_token("A normal bug report about label formatting.") is None


def test_detector_b_fires_single_source_module() -> None:
    p = "Also reproduces with `scatterplot`."
    res = vbd.detector_b(p, ["seaborn/_core/scales.py", "tests/_core/test_scales.py"])
    assert res is not None
    assert res[0] == "scatterplot"


def test_detector_b_silent_two_source_modules() -> None:
    p = "Also reproduces with `scatterplot`."
    assert vbd.detector_b(p, ["seaborn/_core/scales.py", "seaborn/utils.py"]) is None


def test_detector_b_silent_without_phrase() -> None:
    assert vbd.detector_b("normal report", ["seaborn/_core/scales.py"]) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
