"""The summary's Risk line is the Reader's rule (``brief_summary.change_risk``) on what the Action measured.

The facts come from the Snapshot (its packet and targets, :class:`impact.RiskBasis`) and from what CI finished on the
head (:func:`proof_of_work.recorded_checks`). Nothing a model says is an input, and what cannot be read is Not measured.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.brief_summary import change_risk
from lemoncrow.pro.capabilities.review.consumer_inventory import consumer_inventory
from lemoncrow.pro.capabilities.review.pr_bot import callers
from lemoncrow.pro.capabilities.review.pr_bot.callers import Bounds, checkout_callers
from lemoncrow.pro.capabilities.review.pr_bot.impact import RiskBasis
from lemoncrow.pro.capabilities.review.pr_bot.proof_of_work import CiCheck, CiReport, recorded_checks

HEAD = "c" * 40


@dataclass(frozen=True)
class _Target:
    target_id: str
    kind: str
    path: str
    symbol: str
    start_line: int
    end_line: int
    additions: int = 3
    deletions: int = 1


AUTH = _Target("t-view", "symbol", "app/auth.py", "can_view_order", 1, 5)
TOTAL = _Target("t-total", "symbol", "app/orders.py", "order_total", 1, 5)
README = _Target("t-readme", "file", "README.md", "", 1, 3, additions=4, deletions=0)


def packet(targets: tuple[_Target, ...], *, indexed: bool = True, callers: bool = True) -> dict[str, Any]:
    """A Snapshot packet; *indexed* is whether the capture had a code index to ask for callers."""

    code = [target for target in targets if target.symbol]
    return {
        "index_status": "fresh" if indexed else "absent",
        "degraded": [] if indexed else ["symbol_relations"],
        "files": [
            {"path": path, "status": "modified", "hunks": [{"patch": "@@ -2,1 +2,1 @@\n-    x = 1\n+    x = 2\n"}]}
            for path in dict.fromkeys(item.path for item in targets)
        ],
        "symbols": [
            {"symbol_name": item.symbol, "file_path": item.path, "start_line": item.start_line, "change": "modified"}
            for item in code
        ],
        "impact": [
            {
                "kind": "untouched_caller",
                "path": "app/views.py:L3",
                "old": item.symbol,
                "source_path": item.path,
                "in_patch": False,
                "snippet": item.symbol,
            }
            for item in code
            if callers and indexed and item is TOTAL
        ],
    }


def risk(targets: tuple[_Target, ...], ci: CiReport | None, **fields: Any) -> Any:
    recorded, unmeasured = recorded_checks(ci)
    return RiskBasis.of(packet(targets, **fields), targets).assess(recorded, checks_unmeasured=unmeasured)


PASSED = CiReport(HEAD, (CiCheck("unit", "passed"),))
NOTHING = CiReport(HEAD)


def test_a_sensitive_path_is_high_whether_or_not_a_check_ran_and_names_the_rest() -> None:
    for ci in (PASSED, NOTHING, None):
        found = risk((AUTH, TOTAL), ci)
        assert (found.level, found.label) == ("high", "High")
        assert found.reason == "can_view_order is access-control code · and 1 more"
    assert (found.files, found.lines) == (2, 8)


def test_outside_callers_are_high_when_nothing_ran_and_medium_once_a_check_did() -> None:
    assert risk((TOTAL,), NOTHING).reason == "order_total is called from app/views.py and no check ran on this revision"
    assert risk((TOTAL,), NOTHING).level == "high"
    ran = risk((TOTAL,), PASSED)
    assert (ran.level, ran.reason) == ("medium", "order_total is called from app/views.py")


def test_a_check_still_running_has_not_run() -> None:
    running = CiReport(HEAD, (CiCheck("unit", "running"), CiCheck("lint", "did_not_pass", "skipped")))
    assert recorded_checks(running) == ({}, "")
    assert risk((TOTAL,), running).level == "high"


def test_ci_that_could_not_be_read_is_not_measured_never_none_ran() -> None:
    refused = CiReport(HEAD, readable=False, reason="CI results not readable: the workflow needs `checks: read`")
    for ci in (refused, None):
        found = risk((TOTAL,), ci)
        # Outside callers are a measured fact: Medium stands with the checks unknown, never High on a guess.
        assert (found.level, found.reason) == ("medium", "order_total is called from app/views.py")
        assert found.not_measured[-1].startswith("whether any check ran on this commit")
    plain = risk((README,), None)
    assert plain.level == "not_measured" and plain.reason.startswith("could not measure whether any check ran")


def test_callers_the_capture_could_not_ask_about_are_not_measured_never_low() -> None:
    found = risk((TOTAL,), PASSED, indexed=False)
    assert found.level == "not_measured" and found.label == "Not measured"
    assert found.reason == "could not measure callers of the changed code (no code index)"
    assert found.not_measured == (
        "callers of the changed code (no code index)",
        "which tests exercise the changed code",
    )


def test_low_needs_callers_and_checks_measured_and_nothing_found() -> None:
    found = risk((README,), NOTHING)
    assert (found.level, found.reason) == (
        "low",
        "no sensitive code, outside callers, removed safeguards or unrun tests found",
    )
    assert found.not_measured == ()


def gapped(targets: tuple[_Target, ...], ci: CiReport | None, **packet_fields: Any) -> Any:
    """The Risk for an indexed capture with no callers found, whose packet carries *packet_fields*."""

    recorded, unmeasured = recorded_checks(ci)
    built = {**packet(targets, callers=False), **packet_fields}
    return RiskBasis.of(built, targets).assess(recorded, checks_unmeasured=unmeasured)


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"degraded": ["ambiguous_symbol_counts"]}, "a name is defined in more than one place"),
        ({"degraded": ["caller_queries_truncated"]}, "the caller search was capped"),
        ({"degraded": ["index_outline_incomplete"]}, "the code index had not read every definition"),
        ({"index_status": "stale"}, "the code index is stale"),
    ],
)
def test_an_index_gap_that_can_hide_callers_is_not_measured_never_low(fields: dict[str, Any], why: str) -> None:
    found = gapped((TOTAL,), PASSED, **fields)
    assert (found.level, found.reason) == ("not_measured", f"could not measure callers of the changed code ({why})")
    assert found.not_measured == (f"callers of the changed code ({why})", "which tests exercise the changed code")


def test_an_ast_grep_fallback_still_measures_callers() -> None:
    # Without ast-grep, structural matching falls back to text: it over-matches rather than misses, so it is no gap.
    assert gapped((TOTAL,), PASSED, degraded=["astgrep_unavailable"]).level == "low"


def test_the_same_change_gives_the_same_risk() -> None:
    assert risk((AUTH, TOTAL), NOTHING) == risk((AUTH, TOTAL), NOTHING)


# --------------------------------------------------------------------------- #
# who calls the changed code, searched at the head commit (pr_bot.callers)
# --------------------------------------------------------------------------- #

AUTH_PY = "def can_view_order(user, order):\n    return user.id == order.owner\n"
ORDERS_PY = "def order_total(items):\n    return sum(items)\n"
VIEWS_PY = (
    "from app import orders\nfrom app.auth import can_view_order\n\n\n"
    "def show(user, order):\n    if can_view_order(user, order):\n        return orders.order_total([1])\n"
)
TEST_PY = "from app.orders import order_total\n\n\ndef test_total():\n    assert order_total([1]) == 1\n"


def git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    return done.stdout.strip()


def repository(tmp_path: Path, base: Mapping[str, str], head: Mapping[str, str]) -> tuple[Path, str]:
    """A repository with *base* committed, then *head* on top; the base stays checked out, as in the Action."""

    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for files, message in ((base, "base"), (head, "head")):
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        git(root, "add", "-A")
        git(root, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", message)
    sha = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "HEAD~1")
    return root, sha


def head_packet(sha: str, targets: tuple[_Target, ...], *, change: str = "modified") -> dict[str, Any]:
    """The packet an indexless capture writes: the changed symbols, no callers, index absent."""

    return {
        **packet(targets, indexed=False),
        "head_sha": sha,
        "symbols": [
            {"symbol_name": item.symbol, "file_path": item.path, "start_line": item.start_line, "change": change}
            for item in targets
            if item.symbol
        ],
    }


def searched(root: Path, sha: str, targets: tuple[_Target, ...], ci: CiReport | None) -> tuple[Any, list[str]]:
    """The Action's Risk for the change, and every fact the Reader's rule found."""

    basis = RiskBasis.of(head_packet(sha, targets), targets, repo_root=root)
    recorded, unmeasured = recorded_checks(ci)
    facts = change_risk({"context": basis.context}, recorded, packet=basis.packet, targets=basis.targets)["facts"]
    return basis.assess(recorded, checks_unmeasured=unmeasured), [str(fact["text"]) for fact in facts]


APP = {"app/__init__.py": "", "app/auth.py": AUTH_PY, "app/orders.py": ORDERS_PY, "app/views.py": VIEWS_PY}
CHANGED = {
    "app/auth.py": AUTH_PY.replace("order.owner", "order.owner or user.admin"),
    "app/orders.py": ORDERS_PY.replace("sum(items)", "sum(items) + 0"),
}
VIEW = _Target("t-view", "symbol", "app/auth.py", "can_view_order", 1, 2)
ORDER = _Target("t-total", "symbol", "app/orders.py", "order_total", 1, 2)


def test_an_access_control_change_called_from_another_file_is_high_and_names_the_caller(tmp_path: Path) -> None:
    root, sha = repository(tmp_path, APP, {"app/auth.py": CHANGED["app/auth.py"]})
    found, facts = searched(root, sha, (VIEW,), NOTHING)
    assert (found.level, found.reason) == ("high", "can_view_order is access-control code · and 1 more")
    assert "can_view_order is called from app/views.py and no check ran on this revision" in facts
    assert found.not_measured == ()


def test_a_plain_change_called_from_another_file_with_no_check_run_is_high(tmp_path: Path) -> None:
    root, sha = repository(tmp_path, APP, {"app/orders.py": CHANGED["app/orders.py"]})
    found, _facts = searched(root, sha, (ORDER,), NOTHING)
    # ``orders.order_total(...)``: an attribute call is a call.
    assert (found.level, found.reason) == (
        "high",
        "order_total is called from app/views.py and no check ran on this revision",
    )
    assert searched(root, sha, (ORDER,), PASSED)[0].level == "medium"


def test_no_caller_anywhere_is_low_and_a_test_that_did_not_run_is_medium(tmp_path: Path) -> None:
    alone = {**APP, "app/views.py": "def show():\n    return 1\n"}
    root, sha = repository(tmp_path, alone, {"app/orders.py": CHANGED["app/orders.py"]})
    found, _facts = searched(root, sha, (ORDER,), NOTHING)
    assert (found.level, found.not_measured) == ("low", ())

    tested = tmp_path / "tested"
    tested.mkdir()
    root, sha = repository(
        tested, {**alone, "tests/test_orders.py": TEST_PY}, {"app/orders.py": CHANGED["app/orders.py"]}
    )
    found, _facts = searched(root, sha, (ORDER,), NOTHING)
    assert (found.level, found.reason) == (
        "medium",
        "order_total is exercised by tests/test_orders.py, which did not run",
    )
    assert searched(root, sha, (ORDER,), PASSED)[0].level == "low"


class _ReaderIndex:
    """The Reader's current index for the same commit, as its provider answers ``file_relationships``."""

    def file_relationships(self, paths: tuple[str, ...], *, limit: int = 320) -> Mapping[str, Any]:
        del limit
        rows = [
            ("app/views.py", "app/auth.py", "can_view_order"),
            ("app/views.py", "app/orders.py", "order_total"),
            ("tests/test_orders.py", "app/orders.py", "order_total"),
        ]
        edges = [
            {
                "source_path": source,
                "target_path": target,
                "kind": "calls",
                "weight": 1,
                "evidence": [],
                "symbols": [{"name": name, "count": 1}],
            }
            for source, target, name in rows
            if target in paths
        ]
        return {"edges": edges, "degraded": True, "degraded_reason": "legacy_name_resolution", "truncated": False}


def test_the_reader_and_the_action_give_the_same_risk_for_the_same_snapshot_and_checks(tmp_path: Path) -> None:
    root, sha = repository(tmp_path, {**APP, "tests/test_orders.py": TEST_PY}, CHANGED)
    targets = (VIEW, ORDER)
    snapshot = head_packet(sha, targets)
    paths = tuple(str(item["path"]) for item in snapshot["files"])
    # The Reader: the capture had no index, so its current index answers (api._current_callers).
    reader = consumer_inventory(snapshot, targets, current=_ReaderIndex().file_relationships(paths, limit=640))
    action = RiskBasis.of(snapshot, targets, repo_root=root)
    for ci in (NOTHING, PASSED, None):
        recorded, unmeasured = recorded_checks(ci)
        read = change_risk(
            {"context": reader}, recorded, packet=snapshot, targets=targets, checks_unmeasured=unmeasured
        )
        acted = action.assess(recorded, checks_unmeasured=unmeasured)
        assert (acted.level, acted.reason, list(acted.not_measured)) == (
            read["level"],
            read["reason"],
            read["not_measured"],
        )
    assert acted.level == "high"


# The bounds: at a bound the symbol is measured, past it the symbol reads as not measured, never as no callers.


def callers_of(root: Path, sha: str, targets: tuple[_Target, ...], **bounds: Any) -> dict[str, Any]:
    return checkout_callers(root, head_packet(sha, targets), bounds=Bounds(**bounds))


def unmeasured(answer: Mapping[str, Any]) -> dict[str, str]:
    return {str(item["symbol"]): str(item["reason"]) for item in answer["unmeasured"]}


def test_the_symbol_bound(tmp_path: Path) -> None:
    root, sha = repository(tmp_path, APP, CHANGED)
    assert unmeasured(callers_of(root, sha, (VIEW, ORDER), symbols=2)) == {}
    # Symbols are taken in path order: app/auth.py's, then app/orders.py's.
    assert unmeasured(callers_of(root, sha, (VIEW, ORDER), symbols=1)) == {"order_total": "past the 1-symbol bound"}


def test_the_files_per_name_bound(tmp_path: Path) -> None:
    root, sha = repository(tmp_path, {**APP, "tests/test_orders.py": TEST_PY}, CHANGED)
    # order_total is named in app/orders.py, app/views.py and tests/test_orders.py.
    assert unmeasured(callers_of(root, sha, (ORDER,), files_per_name=3)) == {}
    assert unmeasured(callers_of(root, sha, (ORDER,), files_per_name=2)) == {
        "order_total": "3 files mention it, past the 2-file bound"
    }


def test_the_files_bound(tmp_path: Path) -> None:
    root, sha = repository(tmp_path, APP, CHANGED)
    # can_view_order needs app/auth.py and app/views.py, order_total app/orders.py and app/views.py: three files.
    assert unmeasured(callers_of(root, sha, (VIEW, ORDER), files=3)) == {}
    past = callers_of(root, sha, (VIEW, ORDER), files=2)
    assert unmeasured(past) == {"order_total": "past the 2-file bound"}
    # What was read before the bound stands: app/views.py was, so both callers are listed; order_total's are a lower
    # bound (its own file was not read), and it says so.
    assert [edge["target_path"] for edge in past["edges"]] == ["app/auth.py", "app/orders.py"]


def test_the_file_size_and_total_read_bounds(tmp_path: Path) -> None:
    root, sha = repository(tmp_path, APP, {"app/orders.py": CHANGED["app/orders.py"]})
    biggest = max(len(VIEWS_PY), len(CHANGED["app/orders.py"]))
    assert unmeasured(callers_of(root, sha, (ORDER,), file_bytes=biggest)) == {}
    assert unmeasured(callers_of(root, sha, (ORDER,), file_bytes=biggest - 1)) == {
        "order_total": f"a file that mentions it is over {biggest - 1} bytes: app/views.py"
    }
    total = len(VIEWS_PY) + len(CHANGED["app/orders.py"])
    assert unmeasured(callers_of(root, sha, (ORDER,), total_bytes=total)) == {}
    assert unmeasured(callers_of(root, sha, (ORDER,), total_bytes=total - 1)) == {
        "order_total": f"past the {callers._size(total - 1)} read bound"
    }


def test_the_search_output_bound(tmp_path: Path) -> None:
    root, sha = repository(tmp_path, APP, {"app/orders.py": CHANGED["app/orders.py"]})
    answer = callers_of(root, sha, (ORDER,))
    assert unmeasured(answer) == {} and answer["edges"]
    symbols, _ = callers._symbols(head_packet(sha, (ORDER,)))
    pattern = ["-P", "(?<![\\w$])(?:order_total)(?![\\w$])"]
    printed, _failed = callers._grep_once(root, sha, pattern, callers._pathspecs(frozenset({"python"})), Bounds(), 1e9)
    assert symbols and printed
    assert unmeasured(callers_of(root, sha, (ORDER,), grep_bytes=len(printed))) == {}
    past = callers_of(root, sha, (ORDER,), grep_bytes=len(printed) - 1)
    assert unmeasured(past) == {
        "order_total": f"the lines that name them run past the {callers._size(len(printed) - 1)} search-output bound"
    }
    assert past["edges"] == []


def test_the_time_bound(tmp_path: Path) -> None:
    root, sha = repository(tmp_path, APP, CHANGED)
    assert unmeasured(callers_of(root, sha, (VIEW, ORDER), seconds=0)) == {
        "can_view_order": "the search stopped at its 0 s bound",
        "order_total": "the search stopped at its 0 s bound",
    }


def test_past_a_bound_the_risk_says_which_callers_were_not_measured(tmp_path: Path) -> None:
    root, sha = repository(tmp_path, APP, {"app/orders.py": CHANGED["app/orders.py"]})
    snapshot = head_packet(sha, (ORDER,))
    bounded = consumer_inventory(
        snapshot, (ORDER,), current=checkout_callers(root, snapshot, bounds=Bounds(files_per_name=1))
    )
    gap = "callers of order_total (2 files mention it, past the 1-file bound)"
    found = change_risk({"context": bounded}, {"unit": "passed"}, packet=snapshot, targets=(ORDER,))
    assert (found["level"], found["reason"]) == ("not_measured", f"could not measure {gap}")
    assert found["not_measured"] == [gap, "which tests exercise order_total"]
    # Another symbol's measured caller still sets the level; the gap is still named.
    (tmp_path / "two").mkdir()
    root2, sha2 = repository(tmp_path / "two", APP, CHANGED)
    both = head_packet(sha2, (VIEW, ORDER))
    bounded = consumer_inventory(both, (VIEW, ORDER), current=checkout_callers(root2, both, bounds=Bounds(symbols=1)))
    found = change_risk({"context": bounded}, {"unit": "passed"}, packet=both, targets=(VIEW, ORDER))
    assert found["level"] == "high" and found["reason"] == "can_view_order is access-control code · and 1 more"
    assert found["not_measured"] == [
        "callers of order_total (past the 1-symbol bound)",
        "which tests exercise order_total",
    ]


def test_many_unmeasured_names_are_one_count_the_first_few_and_each_reason_once() -> None:
    def gap(*rows: tuple[str, str]) -> str:
        unmeasured = [{"path": f"web/{name}.ts", "symbol": name, "reason": why} for name, why in rows]
        readiness = {"impact_pass": "ran", "index_status": "absent", "unmeasured": unmeasured}
        found = change_risk({"context": {"readiness": readiness}}, {"unit": "passed"}, packet={}, targets=())
        assert found["reason"] == f"could not measure {found['not_measured'][0]}"
        return str(found["not_measured"][0])

    also = "the name is also defined in web/other.ts"
    assert gap(("panel", also)) == f"callers of panel ({also})"
    assert gap(
        ("a", also), ("b", "the name is also defined in x.ts and 3 more"), ("a", also), ("c", also), ("d", also)
    ) == ("callers of 4 names: a, b, c and 1 more (each is also defined in another file)")
    assert gap(("a", also), ("b", "past the 1-file bound")) == (
        "callers of 2 names: a, b (some are also defined in another file; past the 1-file bound)"
    )


def test_a_language_with_no_reference_parser_and_a_file_that_does_not_parse_are_not_measured(tmp_path: Path) -> None:
    script = _Target("t-sh", "symbol", "bin/deploy.sh", "deploy", 1, 2)
    files = {**APP, "bin/deploy.sh": "deploy() {\n  echo 1\n}\n", "app/broken.py": "order_total(\n"}
    root, sha = repository(tmp_path, files, {**CHANGED, "bin/deploy.sh": "deploy() {\n  echo 2\n}\n"})
    assert unmeasured(callers_of(root, sha, (ORDER, script))) == {
        "deploy": "bash files are not searched",
        "order_total": "a file that mentions it could not be parsed: app/broken.py",
    }


def test_a_name_defined_in_two_files_is_not_measured_never_no_callers(tmp_path: Path) -> None:
    twice = {**APP, "app/legacy.py": "def order_total(items):\n    return 0\n"}
    root, sha = repository(tmp_path, twice, {"app/orders.py": CHANGED["app/orders.py"]})
    answer = callers_of(root, sha, (ORDER,))
    # A use of a name defined twice does not say which one it calls: a gap, never "no callers" (fail-safe).
    assert answer["edges"] == []
    assert unmeasured(answer) == {"order_total": "the name is also defined in app/legacy.py"}


def test_a_typescript_import_is_not_a_definition_and_its_call_is_a_caller(tmp_path: Path) -> None:
    lib = "export function priceOf(item: number): number {\n  return item;\n}\n"
    use = "import { priceOf } from './lib';\n\nexport const total = (n: number) => priceOf(n) + 1;\n"
    target = _Target("t-ts", "symbol", "web/lib.ts", "priceOf", 1, 3)
    root, sha = repository(
        tmp_path, {"web/lib.ts": lib, "web/use.ts": use}, {"web/lib.ts": lib.replace("item;", "item * 2;")}
    )
    answer = callers_of(root, sha, (target,))
    assert unmeasured(answer) == {}
    assert [(edge["source_path"], edge["kind"], edge["evidence"][0]["line"]) for edge in answer["edges"]] == [
        ("web/use.ts", "calls", 3)
    ]


def test_an_import_or_a_binding_no_other_file_can_name_is_not_searched_and_a_type_import_is_no_definition(
    tmp_path: Path,
) -> None:
    # A test's import, its unexported helper, a function's local and a callback's parameter cannot be called from another
    # file: their callers are never "not measured". `import { type Host }` does not define Host. A script's global names
    # still make an exported name ambiguous: the rule hides only what is scoped, never a name another file can reach.
    host = "export type Host = { open: () => void };\n"
    view = (
        "import { draw } from './draw';\n"
        "import { type Host } from './host';\n"
        "\n"
        "function Harness(host: Host) {\n"
        "  const panel = draw(host);\n"
        "  return [1].map((part) => part + panel);\n"
        "}\n"
        "\n"
        "export const run = () => Harness({ open() {} });\n"
    )
    script = "function Harness() {}\nvar panel = 1;\nvar part = 2;\nvar run = 3;\n"
    lib = "def outer(rows):\n    def helper(row):\n        return row\n    total = 0\n    return [helper(r) for r in rows]\n"
    other = "def helper():\n    pass\n\n\ntotal = 1\n"
    base = {"web/draw.ts": "export const draw = (x: unknown) => 1;\n", "web/legacy.js": script, "app/other.py": other}
    head = {"web/host.ts": host, "web/view.test.ts": view, "app/lib.py": lib}
    root, sha = repository(tmp_path, {**base, "web/host.ts": "", "web/view.test.ts": "", "app/lib.py": ""}, head)
    targets = tuple(
        _Target(f"t-{path}-{name}", "symbol", path, name, line, line)
        for path, name, line in (
            ("web/host.ts", "Host", 1),
            ("web/view.test.ts", "Host", 2),
            ("web/view.test.ts", "Harness", 4),
            ("web/view.test.ts", "panel", 5),
            ("web/view.test.ts", "part", 6),
            ("web/view.test.ts", "run", 9),
            ("app/lib.py", "outer", 1),
            ("app/lib.py", "helper", 2),
            ("app/lib.py", "total", 4),
        )
    )
    assert unmeasured(callers_of(root, sha, targets)) == {"run": "the name is also defined in web/legacy.js"}


def test_a_grammar_that_is_not_downloaded_is_never_fetched_mid_review(tmp_path: Path, monkeypatch: Any) -> None:
    lib = "export function priceOf(item: number): number {\n  return item;\n}\n"
    use = "import { priceOf } from './lib';\n\nexport const total = (n: number) => priceOf(n) + 1;\n"
    target = _Target("t-ts", "symbol", "web/lib.ts", "priceOf", 1, 3)
    root, sha = repository(
        tmp_path, {"web/lib.ts": lib, "web/use.ts": use}, {"web/lib.ts": lib.replace("item;", "item * 2;")}
    )
    # A download cannot be cut by the time bound: a missing grammar is a gap, and no parser is asked for.
    monkeypatch.setattr(callers, "_grammar_ready", lambda language: False)
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.semantic_file_memory.treesitter_ast.tree_sitter_parser",
        lambda language: pytest.fail("the search asked for a parser it would have to download"),
    )
    answer = callers_of(root, sha, (target,))
    assert answer["edges"] == []
    assert unmeasured(answer) == {"priceOf": "the typescript grammar is not downloaded here: web/lib.ts"}


def test_a_name_alone_is_not_a_caller_an_import_has_to_tie_the_file_to_it(tmp_path: Path) -> None:
    # 2026-11-19 on a real commit: a new `_Client.pages` was "called" by 20 files with a local named pages.
    client = "class Client:\n    def pages(self):\n        return 1\n\n\ndef total():\n    return 1\n"
    files = {
        "pkg/__init__.py": "from .client import Client, total\n",
        "pkg/client.py": client,
        "local_only.py": "def count():\n    pages = 0\n    total = 1\n    return pages + total\n",
        "own_attr.py": "class Other:\n    def go(self):\n        return self.pages\n",
        "stranger.py": "def go(thing):\n    return thing.pages()\n",
        "direct.py": "from pkg.client import Client\n\n\ndef go():\n    return Client().pages()\n",
        "barrel.py": "from pkg import Client, total\n\n\ndef go(c: Client):\n    return c.pages(), total()\n",
        "dynamic.py": "import importlib\n\nmod = importlib.import_module('pkg.client')\nprint(total)\n",
    }
    root, sha = repository(tmp_path, files, {"pkg/client.py": client.replace("return 1", "return 2")})
    targets = (
        _Target("t-pages", "symbol", "pkg/client.py", "pages", 2, 3),
        _Target("t-total", "symbol", "pkg/client.py", "total", 6, 7),
    )
    answer = callers_of(root, sha, targets)

    found = {(edge["source_path"], edge["symbols"][0]["name"]) for edge in answer["edges"]}
    assert found == {("direct.py", "pages"), ("barrel.py", "pages"), ("barrel.py", "total")}
    assert unmeasured(answer) == {
        "pages": "1 file use the name without an import that ties it here",
        "total": "1 file use the name without an import that ties it here",
    }


def test_a_script_use_needs_an_import_and_a_global_script_does_not(tmp_path: Path) -> None:
    module = "export function total() {\n  return 1;\n}\n"
    files = {
        "src/a.ts": module,
        "src/used.ts": "import { total } from './a';\nexport const x = total();\n",
        "src/local.ts": "export function f(total: number) {\n  return total;\n}\n",
        "src/package.ts": "import { total } from 'some-workspace-package';\nexport const y = total();\n",
        "web/global.js": "function shared() {\n  return 1;\n}\n",
        "web/page.js": "shared();\n",
    }
    head = {"src/a.ts": module.replace("1", "2"), "web/global.js": "function shared() {\n  return 2;\n}\n"}
    root, sha = repository(tmp_path, files, head)
    targets = (
        _Target("t-total", "symbol", "src/a.ts", "total", 1, 3),
        _Target("t-shared", "symbol", "web/global.js", "shared", 1, 3),
    )
    answer = callers_of(root, sha, targets)
    if answer["unmeasured"] and "grammar" in answer["unmeasured"][0]["reason"]:
        pytest.skip("no JavaScript grammar on this machine")

    assert {(edge["source_path"], edge["symbols"][0]["name"]) for edge in answer["edges"]} == {
        ("src/used.ts", "total"),
        ("web/page.js", "shared"),
    }
    assert unmeasured(answer) == {"total": "1 file use the name without an import that ties it here"}


def test_a_git_without_perl_regexes_gets_the_same_answer_and_a_failed_search_measures_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, sha = repository(tmp_path, APP, CHANGED)
    expected = callers_of(root, sha, (VIEW, ORDER))
    real = callers._grep_once

    def no_pcre(*args: Any) -> tuple[bytes, str]:
        return (b"", "exit 128") if args[2][0] == "-P" else real(*args)

    monkeypatch.setattr(callers, "_grep_once", no_pcre)
    assert callers_of(root, sha, (VIEW, ORDER))["edges"] == expected["edges"]
    assert len(expected["edges"]) == 2
    monkeypatch.setattr(callers, "_grep_once", lambda *args: (b"", "exit 2"))
    assert unmeasured(callers_of(root, sha, (VIEW, ORDER))) == {
        "can_view_order": "the search could not run (git grep exit 2)",
        "order_total": "the search could not run (git grep exit 2)",
    }


def test_a_working_tree_review_searches_the_working_tree_untracked_files_included(tmp_path: Path) -> None:
    # ``lc review --ai`` on uncommitted work: no head commit, so the search reads the files on disk (git grep
    # ``--untracked``, which must come before the pattern or git takes it for a revision and the search measures nothing).
    root, sha = repository(tmp_path, APP, {"app/orders.py": CHANGED["app/orders.py"]})
    git(root, "checkout", "-q", sha)
    (root / "app/auth.py").write_text(CHANGED["app/auth.py"])
    (root / "app/new_view.py").write_text(
        "from app.auth import can_view_order\n\n\ndef peek(u, o):\n    return can_view_order(u, o)\n"
    )
    answer = callers_of(root, "", (VIEW,))
    assert unmeasured(answer) == {}
    assert sorted({edge["source_path"] for edge in answer["edges"]}) == ["app/new_view.py", "app/views.py"]


def test_a_staged_review_never_searches_the_live_tree_so_its_callers_are_not_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``lc review --ai --staged`` through the real AI stage, no code index: the packet has no head commit, and the live
    # tree can differ from the staged content. Stage the change to orders.py, then remove its real caller in views.py
    # without staging that: a search of the working tree would find no caller and read Low.
    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
    from lemoncrow.pro.capabilities.review.pr_bot import engine

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.setenv("LEMONCROW_LIVE_REVIEW_PREWARM", "0")
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "app").mkdir()
    for name, text in APP.items():
        (root / name).write_text(text)
    git(root, "add", "-A")
    git(root, "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "base")
    (root / "app/orders.py").write_text(CHANGED["app/orders.py"])
    git(root, "add", "app/orders.py")
    (root / "app/views.py").write_text("def show():\n    return 1\n")
    (root / "app/stray.py").write_text(
        "from app.orders import order_total\n\n\ndef stray():\n    return order_total([1])\n"
    )

    captured = engine.capture_range(root, resolve_rev_range(root, None, staged=True), tmp_path / "store")
    assert captured.revision.range_mode == "staged"

    def no_model(**_: Any) -> Any:
        raise RuntimeError("no model in this test")

    stage = engine.run_ai_stage(captured, structured_executor=no_model)
    assert stage.risk_basis is not None
    basis = stage.risk_basis
    recorded, unmeasured_checks = recorded_checks(PASSED)
    found = basis.assess(recorded, checks_unmeasured=unmeasured_checks)
    assert found.level != "low"
    assert any(item.startswith("callers of") for item in found.not_measured), found
