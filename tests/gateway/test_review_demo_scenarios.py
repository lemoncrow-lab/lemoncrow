"""File-level smoke tests for the Review demo scenarios (no LemonCrow server needed)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.surface_providers import BrunoSurfaceProvider
from lemoncrow.pro.capabilities.review.surfaces import SurfaceContext, load_review_surface_config

DEMO_ROOT = Path(__file__).resolve().parents[2] / "examples" / "demo"

# sha256 over (path, bytes) of the 19 files the `standard` change touches, in
# sorted order. Same files and bytes as before scenarios existed, except
# README.md, whose baseline gained the "Scenarios" section. A deliberate edit
# to the standard change or the demo README must update this value.
# Refreshed when the demo moved to examples/demo: the README's checkout paths
# changed (demo/review-surfaces -> examples/demo); no other file bytes changed.
# Refreshed when the demo README described the recorded before/after API runs
# (scripts/record-demo-runs.py); no other file bytes changed.
# Refreshed when the API began reading data/plans.csv and /pricing fetching GET /api/plans:
# backend/app.py, PricingPage.jsx, compose.yml and README.md changed, and the change gained
# frontend/public/api/plans (the static builds' copy of the API's answer).
# Refreshed when the README's Scenarios table gained `tenant-cache`; no other file bytes changed.
STANDARD_CHANGE_SHA256 = "0db151c067beafc0cf6b2723739337bf6b442c72cf37f1c645526fc0d7cb8a40"


def _run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=check)


def _demo(tmp_path: Path, scenario: str | None = None, *, commit: bool = True) -> Path:
    """Mimic bootstrap.sh up to (and including) the baseline commit."""

    root = tmp_path / "demo"
    shutil.copytree(
        DEMO_ROOT, root, ignore=shutil.ignore_patterns("node_modules", "__pycache__", "dist", "storybook-static")
    )
    shutil.copytree(root / ".fixtures" / "media" / "before", root / "media")
    if scenario not in (None, "standard", "empty"):
        _apply(root, scenario, "--prepare-baseline")
    if commit:
        _run("git", "init", "-b", "main", cwd=root)
        _run("git", "config", "user.name", "Demo Tester", cwd=root)
        _run("git", "config", "user.email", "demo@example.invalid", cwd=root)
        _run("git", "add", "-A", cwd=root)
        _run("git", "commit", "-m", "baseline", cwd=root)
    return root


def _apply(root: Path, scenario: str | None, *flags: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    scenario_args = ["--scenario", scenario] if scenario else []
    return _run(
        sys.executable, str(root / "scripts" / "apply-demo-change.py"), *scenario_args, *flags, cwd=root, check=check
    )


def _backend_failures(root: Path) -> tuple[set[str], str]:
    result = _run(sys.executable, "-m", "unittest", "discover", "-s", "tests", cwd=root / "backend", check=False)
    return set(re.findall(r"^(?:FAIL|ERROR): (\w+)", result.stderr, re.MULTILINE)), result.stderr


def _diff(root: Path) -> str:
    return _run("git", "diff", cwd=root).stdout


def _changed(root: Path) -> list[str]:
    return _run("git", "diff", "--name-only", cwd=root).stdout.split()


def test_standard_is_default_and_unchanged(tmp_path: Path) -> None:
    default = _demo(tmp_path / "a")
    explicit = _demo(tmp_path / "b", "standard")
    assert not (default / "backend" / "orders.py").exists()
    assert not (default / "PLAN.md").exists()

    out = _apply(default, None).stdout
    _apply(explicit, "standard")
    files = [line.strip() for line in out.splitlines()[1:]]
    assert len(files) == 19
    digest = hashlib.sha256()
    for relative in sorted(files):
        digest.update(relative.encode() + b"\0" + (default / relative).read_bytes() + b"\0")
    assert digest.hexdigest() == STANDARD_CHANGE_SHA256
    assert _diff(default) == _diff(explicit)

    phased = _demo(tmp_path / "c")
    _apply(phased, "standard", "--phase", "initial")
    assert _changed(phased) == ["README.md", "docs/architecture.md"]
    _apply(phased, "standard", "--phase", "final")
    assert _diff(phased) == _diff(default)
    assert _backend_failures(default)[0] == set()


def _api_plans(root: Path) -> dict[str, Any]:
    script = "import json, app; print(json.dumps(app.plans()))"
    return json.loads(_run(sys.executable, "-c", script, cwd=root / "backend").stdout)


def test_the_api_serves_plans_csv_and_the_static_copy_matches_it_on_both_sides(tmp_path: Path) -> None:
    root = _demo(tmp_path)
    fixture = root / "frontend" / "public" / "api" / "plans"
    before = _api_plans(root)
    assert before == json.loads(fixture.read_text(encoding="utf-8"))
    assert [(plan["name"], plan["monthly"]) for plan in before["plans"]] == [
        ("Starter", 0),
        ("Team", 49),
        ("Business", 199),
    ]
    _apply(root, "standard")
    after = _api_plans(root)
    assert after == json.loads(fixture.read_text(encoding="utf-8"))
    assert [(plan["name"], plan["monthly"]) for plan in after["plans"]] == [
        ("Starter", 0),
        ("Team", 59),
        ("Business", 199),
        ("Enterprise", 899),
    ]
    # The page asks the API; no copy of the plans is left in it.
    page = (root / "frontend" / "src" / "pages" / "PricingPage.jsx").read_text(encoding="utf-8")
    assert 'fetch("/api/plans")' in page and "monthly" not in page.split("function usePlans")[0]


def test_empty_has_clean_baseline_and_no_change(tmp_path: Path) -> None:
    root = _demo(tmp_path, "empty")
    assert _apply(root, "empty").stdout.strip().endswith(":")
    assert _changed(root) == []
    assert _run("git", "status", "--porcelain", cwd=root).stdout == ""
    assert _backend_failures(root)[0] == set()


def test_orders_baseline_is_green_and_change_plants_three_issues(tmp_path: Path) -> None:
    root = _demo(tmp_path, "orders")
    assert "def can_view_order(" in (root / "backend" / "orders.py").read_text()
    assert "order_detail" in (root / "backend" / "app.py").read_text()
    assert "1. Allow guest orders before sign-in" in (root / "PLAN.md").read_text()
    assert "2. Let promo codes stack" in (root / "PLAN.md").read_text()
    assert "orders.py" in (root / "backend" / "Dockerfile").read_text()
    assert _backend_failures(root)[0] == set()

    _apply(root, "orders", "--phase", "change")
    assert _changed(root) == ["backend/orders.py"]
    diff = _diff(root)
    assert "+    # Guest checkout: orders placed before sign-in have no owner yet." in diff
    assert '+    if order["owner_id"] is None:\n+        return True' in diff
    assert "+    # Promo codes can now stack, so percent may exceed 100." in diff
    assert '+        raise ValueError("percent must not be negative")' in diff
    assert "-    for index in range(len(items)):\n+    for index in range(len(items) - 1):" in diff
    # Only the discount test notices; nothing reaches the guest rule or order_total.
    assert _backend_failures(root)[0] == {"test_rejects_percent_over_100"}


def test_orders_revision_2_fixes_two_issues_and_adds_one_unrequested_change(tmp_path: Path) -> None:
    root = _demo(tmp_path, "orders-revision-2")
    _apply(root, "orders-revision-2", "--phase", "change")
    revision_1 = (root / "backend" / "orders.py").read_text()
    _apply(root, "orders-revision-2", "--phase", "revision-2")
    revision_2 = (root / "backend" / "orders.py").read_text()

    assert "Guest checkout" in revision_1 and "Guest checkout" not in revision_2
    assert "range(len(items) - 1)" in revision_1 and "range(len(items) - 1)" not in revision_2
    assert "min(percent, 100)" not in revision_1 and "    percent = min(percent, 100)\n" in revision_2
    assert 'if order["owner_id"] is None:\n        return False' in revision_2

    # The new tests fail on Revision 1 and pass on Revision 2.
    new_tests = {"test_guest_order_is_admin_only", "test_total_sums_every_item"}
    assert new_tests <= set(re.findall(r"def (test_\w+)", (root / "backend/tests/test_orders.py").read_text()))
    failures_2 = _backend_failures(root)[0]
    assert not new_tests & failures_2
    (root / "backend" / "orders.py").write_text(revision_1)
    assert new_tests <= _backend_failures(root)[0]

    # Phases are checked against the scenario.
    assert _apply(root, "orders", "--phase", "revision-2", check=False).returncode != 0


def test_large_change_touches_thirty_parts_deterministically(tmp_path: Path) -> None:
    first = _demo(tmp_path / "a", "large")
    second = _demo(tmp_path / "b", "large")
    assert _backend_failures(first)[0] == set()
    _apply(first, "large", "--phase", "change")
    _apply(second, "large", "--phase", "change")
    changed = _changed(first)
    assert len(changed) == 30
    assert {"backend/requirements.lock", "frontend/src/generated/areas.json"} <= set(changed)
    assert {path.split("/")[0] for path in changed} == {"backend", "frontend", "docs"}
    assert _diff(first) == _diff(second)
    assert len(_diff(first).encode()) < 20_000
    assert _backend_failures(first)[0] == set()


# The reviewer's sequences, in one process like one server: Acme's export, then Globex's list; Acme's
# dashboard, a correction, then the dashboard again.
_TENANT_SEQUENCES = """
import json, invoices
invoices.export_invoices_csv("tenant=acme&status=paid")
globex = invoices.public_invoices("tenant=globex&status=paid&page=1")[1]
invoices.dashboard_invoices("tenant=acme&status=paid&page=1")
invoices.edit_invoice("tenant=acme&id=ACME-1002&amount_usd=680")
after_edit = invoices.dashboard_invoices("tenant=acme&status=paid&page=1")[1]
print(json.dumps({
    "globex": [[row["tenant"], row["id"]] for row in globex["invoices"]],
    "after_edit": {row["id"]: row["amount_usd"] for row in after_edit["invoices"]},
}))
"""


def _tenant_sequences(root: Path) -> dict[str, Any]:
    return json.loads(_run(sys.executable, "-c", _TENANT_SEQUENCES, cwd=root / "backend").stdout)


def test_tenant_cache_baseline_keeps_tenants_apart_and_the_change_serves_globex_acmes_invoices(
    tmp_path: Path,
) -> None:
    root = _demo(tmp_path, "tenant-cache")
    assert "COPY *.py ./" in (root / "backend" / "Dockerfile").read_text()
    assert _backend_failures(root)[0] == set()
    assert _tenant_sequences(root) == {
        "globex": [["globex", "GLX-2001"], ["globex", "GLX-2003"]],
        "after_edit": {"ACME-1001": 1200, "ACME-1002": 680},
    }

    touched = [line.strip() for line in _apply(root, "tenant-cache", "--phase", "change").stdout.splitlines()[1:]]
    assert touched == [
        "backend/cache.py",
        "backend/invoices.py",
        "bruno/invoices/AcmeDashboardAfterEdit.bru",
        "bruno/invoices/AcmeExport.bru",
        "bruno/invoices/GlobexAfterAcme.bru",
    ]
    diff = _diff(root)
    # The story's change: a shared key by status and page only, and nothing clears it on update.
    assert '+    key = f"invoices:{status}:{page}"' in diff
    assert "+    return cache.wrap(key, 60, lambda: find_invoices(" in diff
    backend = "".join(path.read_text() for path in (root / "backend").glob("*.py") if path.name != "cache.py")
    assert "cache.clear" not in backend and "def clear(" in (root / "backend" / "cache.py").read_text()
    # The agent's claim holds for the backend tests, which never mix tenants or list after an edit.
    assert _backend_failures(root)[0] == set()
    assert _tenant_sequences(root) == {
        "globex": [["acme", "ACME-1001"], ["acme", "ACME-1002"]],
        "after_edit": {"ACME-1001": 1200, "ACME-1002": 860},
    }


def test_tenant_cache_recipes_replay_their_bruno_steps_and_are_the_ones_recorded(tmp_path: Path) -> None:
    root = _demo(tmp_path, "tenant-cache")
    _apply(root, "tenant-cache", "--phase", "change")
    changed = tuple(_changed(root))
    revision = SimpleNamespace(id="r", review_id="v", revision_number=1, base_sha="b", head_sha="h")
    context = SurfaceContext(root, tmp_path / "store", revision, changed, load_review_surface_config(root))
    surfaces = {surface.id: surface for surface in BrunoSurfaceProvider().discover(context)}

    spec = importlib.util.spec_from_file_location("record_demo_runs", root / "scripts" / "record-demo-runs.py")
    assert spec is not None and spec.loader is not None
    record = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(record)
    assert set(record.REQUESTS["tenant-cache"]) == set(surfaces)

    globex = surfaces["bruno/invoices/GlobexAfterAcme.bru"]
    assert [(step["method"], step["request"]) for step in globex.metadata["setup"]] == [("GET", "invoices/AcmeExport")]
    assert "auto_compare" in globex.capabilities
    after_edit = surfaces["bruno/invoices/AcmeDashboardAfterEdit.bru"]
    assert [(step["method"], step["request"]) for step in after_edit.metadata["setup"]] == [
        ("GET", "invoices/AcmeDashboard"),
        ("POST", "invoices/CorrectAcmeInvoice"),
    ]
    # Replaying the correction writes, so that recipe runs only when asked (the recording asks).
    assert "execute" in after_edit.capabilities and "auto_compare" not in after_edit.capabilities
    assert "setup" not in surfaces["bruno/invoices/AcmeExport.bru"].metadata


@pytest.mark.parametrize("name", ["busy", "agent-failed", "github-pr"])
def test_unimplemented_scenarios_say_so_and_list_valid_ones(tmp_path: Path, name: str) -> None:
    root = _demo(tmp_path, None)
    result = _apply(root, name, check=False)
    assert result.returncode != 0
    assert "not implemented yet" in result.stderr
    for valid in ("empty", "standard", "orders", "orders-revision-2", "large", "tenant-cache"):
        assert valid in result.stderr


def test_bootstrap_rejects_unknown_scenario_before_touching_anything(tmp_path: Path) -> None:
    target = tmp_path / "target"
    result = _run("bash", str(DEMO_ROOT / "bootstrap.sh"), str(target), "--scenario", "nope", cwd=tmp_path, check=False)
    assert result.returncode != 0
    assert "unknown scenario 'nope'" in result.stderr and "orders-revision-2" in result.stderr
    assert not target.exists()
