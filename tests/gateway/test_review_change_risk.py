"""Grounded Risk: the removed-safeguard detector on real git diffs, and the level rule row by row."""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.change_risk import (
    removed_safeguards,
    risk_level,
    unread_safeguard_files,
    weakened_safeguards,
)

_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _git_hunks(tmp_path: Path, name: str, base: str, head: str) -> list[dict[str, object]]:
    """The hunks ``git diff`` prints for *name* going from *base* to *head*, shaped like a packet file's."""

    repo = Path(tempfile.mkdtemp(dir=tmp_path))

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    (repo / name).write_text(base, encoding="utf-8")
    git("add", name)
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "base")
    (repo / name).write_text(head, encoding="utf-8")
    hunks: list[dict[str, object]] = []
    for line in git("diff", "--no-color", "-U3", "--", name).splitlines():
        match = _HEADER.match(line)
        if match:
            old_start, old_lines, new_start, new_lines = match.groups()
            hunks.append(
                {
                    "old_start": int(old_start),
                    "old_lines": int(old_lines if old_lines is not None else 1),
                    "new_start": int(new_start),
                    "new_lines": int(new_lines if new_lines is not None else 1),
                    "patch": "",
                }
            )
        elif hunks and line[:1] in (" ", "+", "-", "\\"):
            hunks[-1]["patch"] = f"{hunks[-1]['patch']}{line}\n"
    return hunks


def _found(tmp_path: Path, name: str, base: str, head: str, symbol: str = "f") -> list[str]:
    language = "python" if name.endswith(".py") else "script"
    return weakened_safeguards(_git_hunks(tmp_path, name, base, head), language, symbol)


_DISCOUNT_BASE = """def apply_discount(total, percent):
    if percent < 0 or percent > 100:
        raise ValueError("percent must be between 0 and 100")
    return round(total * (100 - percent) / 100, 2)
"""
# The shop demo's agent change (test plan A2): the raise stays, its guard loses `or percent > 100`.
_DISCOUNT_V1 = """def apply_discount(total, percent):
    # Promo codes can now stack, so percent may exceed 100.
    if percent < 0:
        raise ValueError("percent must not be negative")
    return round(total * (100 - percent) / 100, 2)
"""


def test_a_guard_that_loses_an_or_clause_is_a_narrowed_check(tmp_path: Path) -> None:
    assert _found(tmp_path, "orders.py", _DISCOUNT_BASE, _DISCOUNT_V1, "apply_discount") == [
        "apply_discount no longer rejects a percent over 100 "
        "(check narrowed from `percent < 0 or percent > 100` to `percent < 0`)"
    ]


def test_python_deleted_raise_guard_and_assert_are_each_named(tmp_path: Path) -> None:
    base = """def withdraw(account, amount):
    assert amount > 0, "amount must be positive"
    if account.frozen:
        return None
    if amount > account.balance:
        raise InsufficientFunds(account.id, amount)
    account.balance -= amount
    return amount
"""
    head = """def withdraw(account, amount):
    account.balance -= amount
    return amount
"""
    assert _found(tmp_path, "bank.py", base, head, "withdraw") == [
        'withdraw no longer checks that `amount > 0` (removed `assert amount > 0, "amount must be positive"`)',
        "withdraw no longer returns early for `account.frozen` (removed `if account.frozen:`)",
        "withdraw no longer rejects `amount > account.balance` (removed `if amount > account.balance:`)",
    ]


def test_a_raise_left_without_its_guard_and_a_bare_raise(tmp_path: Path) -> None:
    base = """def load(path):
    try:
        return read(path)
    except OSError:
        log("failed")
        raise
    if not path:
        raise ValueError("path required")
"""
    head = """def load(path):
    try:
        return read(path)
    except OSError:
        log("failed")
    if not path:
        log("path required")
"""
    assert _found(tmp_path, "load.py", base, head, "load") == [
        "load no longer raises the error it caught (removed `raise`)",
        "load no longer raises ValueError (removed `raise ValueError(...)`)",
    ]


def test_an_assert_that_loses_an_and_clause_is_narrowed(tmp_path: Path) -> None:
    base = "def f(x):\n    assert x is not None and x >= 0\n    return x\n"
    head = "def f(x):\n    assert x is not None\n    return x\n"
    assert _found(tmp_path, "f.py", base, head) == [
        "f no longer checks that `x >= 0` (assert narrowed from `x is not None and x >= 0` to `x is not None`)"
    ]


@pytest.mark.parametrize(
    ("base", "head"),
    [
        # The message changed; the same exception is still raised.
        (
            'def f(x):\n    if x < 0:\n        raise ValueError("a")\n    return x\n',
            'def f(x):\n    if x < 0:\n        raise ValueError("negative x")\n    return x\n',
        ),
        # Widened, not narrowed.
        (
            "def f(x):\n    if x < 0:\n        raise ValueError()\n    return x\n",
            "def f(x):\n    if x < 0 or x > 9:\n        raise ValueError()\n    return x\n",
        ),
        # Rewritten into an equivalent form: an edit the detector does not judge.
        (
            "def f(x):\n    if x < 0 or x > 100:\n        raise ValueError()\n    return x\n",
            "def f(x):\n    if not 0 <= x <= 100:\n        raise ValueError()\n    return x\n",
        ),
        # Dropping an `and` clause makes a guard fire more often.
        (
            "def f(x, y):\n    if x and y:\n        return None\n    return x\n",
            "def f(x, y):\n    if x:\n        return None\n    return x\n",
        ),
        # A guard moved below a new line, unchanged.
        (
            "def f(x):\n    if x is None:\n        return 0\n    return x\n",
            "def f(x):\n    x = clean(x)\n    if x is None:\n        return 0\n    return x\n",
        ),
        # A final return is not an early return; comments and strings are not code.
        (
            "def f(x):\n    # raise ValueError here?\n    y = 'raise'\n    return x\n",
            "def f(x):\n    y = 'raise'\n    return x + 1\n",
        ),
        # A raise of another type replaces it: still raises.
        (
            "def f(x):\n    raise ValueError(x)\n",
            "def f(x):\n    raise TypeError(x)\n",
        ),
    ],
)
def test_python_edits_that_keep_the_safeguard_stay_quiet(tmp_path: Path, base: str, head: str) -> None:
    assert _found(tmp_path, "f.py", base, head) == []


def test_an_early_return_folded_into_a_conditional_expression_is_not_a_removed_safeguard(tmp_path: Path) -> None:
    # 2026-11-19 on a real commit: `if not rows: return None` became `return rows[0].wire if rows else None`.
    base = "def f(rows):\n    if not rows:\n        return None\n    return rows[0].wire\n"
    assert _found(tmp_path, "f.py", base, "def f(rows):\n    return rows[0].wire if rows else None\n") == []
    assert _found(tmp_path, "f.py", base, "def f(rows):\n    return rows[0].wire if ready else None\n") == [
        "f no longer returns early for a missing rows (removed `if not rows:`)"
    ]
    script = "function f(rows) {\n  if (!rows) return null;\n  return rows[0];\n}\n"
    assert _found(tmp_path, "f.ts", script, "function f(rows) {\n  return rows?.[0] ?? null;\n}\n") == []


def test_typescript_throw_guard_and_early_return(tmp_path: Path) -> None:
    base = """export function applyDiscount(total: number, percent: number): number {
  if (percent < 0 || percent > 100) {
    throw new RangeError(`bad percent ${percent}`);
  }
  if (!total) return 0;
  assert(total > 0 && Number.isFinite(total), "total");
  return total * (100 - percent) / 100;
}
"""
    head = """export function applyDiscount(total: number, percent: number): number {
  if (percent < 0) {
    throw new RangeError(`bad percent ${percent}`);
  }
  assert(total > 0, "total");
  return total * (100 - percent) / 100;
}
"""
    assert _found(tmp_path, "orders.ts", base, head, "applyDiscount") == [
        "applyDiscount no longer rejects a percent over 100 "
        "(check narrowed from `percent < 0 || percent > 100` to `percent < 0`)",
        "applyDiscount no longer returns early for a missing total (removed `if (!total) return 0;`)",
        "applyDiscount no longer checks that `Number.isFinite(total)` "
        "(assert narrowed from `total > 0 && Number.isFinite(total)` to `total > 0`)",
    ]


def test_javascript_removed_throw_and_quiet_edits(tmp_path: Path) -> None:
    base = """function load(id) {
  if (!id) {
    throw new Error("id required");
  }
  // throw new Error("unused");
  return fetchIt(id);
}
"""
    head = """function load(id) {
  // throw new Error("unused");
  return fetchIt(id ?? "default");
}
"""
    assert _found(tmp_path, "load.js", base, head, "load") == [
        "load no longer rejects a missing id (removed `if (!id) {`)"
    ]
    kept = base.replace('"id required"', '"an id is required"')
    assert _found(tmp_path, "load.js", base, kept, "load") == []


@dataclass(frozen=True)
class _Target:
    target_id: str
    kind: str
    path: str
    symbol: str
    start_line: int
    end_line: int


def test_removed_safeguards_attributes_each_fact_to_its_part_and_skips_added_and_deleted(tmp_path: Path) -> None:
    base = (
        "def order_total(items):\n    return sum(i['price'] for i in items)\n\n\n" + _DISCOUNT_BASE + "\n\n"
        "def legacy(x):\n    assert x\n    return x\n"
    )
    head = "def order_total(items):\n    return sum(i['price'] for i in items[:-1])\n\n\n" + _DISCOUNT_V1
    packet = {
        "files": [
            {"path": "app/orders.py", "status": "modified", "hunks": _git_hunks(tmp_path, "orders.py", base, head)},
            {"path": "cmd/main.go", "status": "modified", "hunks": [{"patch": "-\tpanic(err)\n"}]},
        ],
        "symbols": [
            {"symbol_name": "order_total", "file_path": "app/orders.py", "change": "modified"},
            {"symbol_name": "apply_discount", "file_path": "app/orders.py", "change": "modified"},
            # A deleted symbol carries its old-side definition line; its body is its own, not apply_discount's.
            {"symbol_name": "legacy", "file_path": "app/orders.py", "change": "deleted", "start_line": 11},
        ],
    }
    targets = (
        _Target("t-total", "symbol", "app/orders.py", "order_total", 1, 2),
        _Target("t-disc", "symbol", "app/orders.py", "apply_discount", 5, 9),
        _Target("t-legacy", "symbol", "app/orders.py", "legacy", 10, 10),
        _Target("t-main", "symbol", "cmd/main.go", "main", 1, 9),
    )
    found = removed_safeguards(packet, targets)
    assert list(found) == ["t-disc"]
    assert found["t-disc"] == [
        "no longer rejects a percent over 100 (check narrowed from `percent < 0 or percent > 100` to `percent < 0`)"
    ]
    assert unread_safeguard_files(packet, targets) == {
        "only Python, JavaScript and TypeScript are read": ["cmd/main.go"]
    }
    packet["files"][0]["hunks"] = [{**hunk, "patch": ""} for hunk in packet["files"][0]["hunks"]]
    assert removed_safeguards(packet, targets) == {}
    assert unread_safeguard_files(packet, targets)["diff text unavailable"] == ["app/orders.py"]


_FACTS = ("removed_safeguard", "sensitive", "outside_callers", "tested_not_run")


def _level(*facts: str, callers: bool = True, checks: bool | None = False) -> str:
    return risk_level(**{name: name in facts for name in _FACTS}, callers_measured=callers, checks_ran=checks)


def test_level_rule_every_row() -> None:
    # High: a removed safeguard stands on the diff alone, whatever else is unknown.
    assert _level("removed_safeguard", checks=True) == "high"
    assert _level("removed_safeguard", callers=False, checks=None) == "high"
    # High: a sensitive area always, whether or not a check ran.
    assert _level("sensitive") == "high"
    assert _level("sensitive", checks=True) == "high"
    assert _level("sensitive", callers=False, checks=None) == "high"
    # High: outside callers with no check run.
    assert _level("outside_callers") == "high"
    # Medium: outside callers with a check run (or the checks unknown), or tests nothing ran.
    assert _level("outside_callers", checks=True) == "medium"
    assert _level("outside_callers", checks=None) == "medium"
    assert _level("tested_not_run") == "medium"
    # Not measured: never Low when callers or checks are unknown.
    assert _level(callers=False, checks=None) == "not_measured"
    assert _level(callers=False, checks=True) == "not_measured"
    assert _level(callers=True, checks=None) == "not_measured"
    # Low: both measured, nothing found.
    assert _level(checks=True) == "low"
    assert _level(checks=False) == "low"


def test_a_removal_at_a_parts_last_line_is_that_parts_not_the_next_ones(tmp_path: Path) -> None:
    base = "def f(x):\n    log(x)\n    raise Stop()\ndef g(y):\n    return y\n"
    head = "def f(x):\n    log(x)\ndef g(y):\n    return y\n"
    packet = {
        "files": [{"path": "a.py", "status": "modified", "hunks": _git_hunks(tmp_path, "a.py", base, head)}],
        "symbols": [],
    }
    targets = (_Target("t-f", "symbol", "a.py", "f", 1, 2), _Target("t-g", "symbol", "a.py", "g", 3, 4))
    assert removed_safeguards(packet, targets) == {"t-f": ["no longer raises Stop (removed `raise Stop(...)`)"]}


def test_test_files_are_not_read_for_safeguards(tmp_path: Path) -> None:
    base = "def test_total():\n    assert total([1]) == 1\n"
    head = "def test_total():\n    total([1])\n"
    packet = {
        "files": [
            {"path": "tests/test_orders.py", "status": "modified", "hunks": _git_hunks(tmp_path, "t.py", base, head)},
            {"path": "tests/helpers.go", "status": "modified", "hunks": []},
        ],
        "symbols": [],
    }
    targets = (
        _Target("t-test", "symbol", "tests/test_orders.py", "test_total", 1, 2),
        _Target("t-go", "symbol", "tests/helpers.go", "helper", 1, 2),
    )
    assert removed_safeguards(packet, targets) == {}
    assert unread_safeguard_files(packet, targets) == {}


def test_a_thrown_value_is_shown_as_code(tmp_path: Path) -> None:
    base = "function f() {\n  try { g(); } catch (reason) {\n    log(reason);\n    throw reason;\n  }\n}\n"
    head = "function f() {\n  try { g(); } catch (reason) {\n    log(reason);\n  }\n}\n"
    assert _found(tmp_path, "f.js", base, head) == ["f no longer throws `reason` (removed `throw reason`)"]
