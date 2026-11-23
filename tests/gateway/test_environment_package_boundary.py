"""The environment core lives in ``lemoncrow.pro.environment``, not under Review.

Runners, execution identity and fingerprint, the preview proxy and state, the
dependency cache, the services runner, host tools, sandbox limits and the macOS
sandbox moved out of ``review/`` so Workspaces can import them. These checks keep
the old module paths from coming back, the core free of Review imports (Review
hands the runners its pieces through ``runner_host``), and the identity contract
free of product imports.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENT = ROOT / "src/lemoncrow/pro/environment"
REVIEW = ROOT / "src/lemoncrow/pro/capabilities/review"
MOVED = {
    "runtimes": "runners",
    "execution": "execution",
    "preview_proxy": "preview_proxy",
    "preview_state": "preview_state",
    "host_tools": "host_tools",
    "sandbox_limits": "sandbox_limits",
    "macos_sandbox": "macos_sandbox",
}
REVIEW_PACKAGE = "lemoncrow.pro.capabilities.review"
SCANNED = ("src", "tests", "enterprise/server/src", "enterprise/server/tests", "scripts")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_the_moved_modules_live_only_in_the_environment_package() -> None:
    for old, new in MOVED.items():
        assert not (REVIEW / f"{old}.py").exists(), old
        assert (ENVIRONMENT / f"{new}.py").is_file(), new
    assert (ENVIRONMENT / "dependency_cache.py").is_file()
    assert (ENVIRONMENT / "services.py").is_file()


def test_nothing_imports_the_old_review_paths() -> None:
    stale = {f"lemoncrow.pro.capabilities.review.{old}" for old in MOVED}
    offenders = [
        f"{path.relative_to(ROOT)}: {name}"
        for top in SCANNED
        for path in sorted((ROOT / top).rglob("*.py"))
        for name in sorted(_imports(path) & stale)
    ]
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("module", ["execution", "dependency_cache", "services"])
def test_identity_cache_and_services_import_no_product_code(module: str) -> None:
    # Only the standard library, the public client's definition types and the identity contract.
    allowed = ("lemoncrow_client.environment", "lemoncrow.pro.environment.execution")
    foreign = sorted(
        name
        for name in _imports(ENVIRONMENT / f"{module}.py")
        if name.split(".")[0] not in sys.stdlib_module_names and name != "__future__" and not name.startswith(allowed)
    )
    assert not foreign, foreign


def test_the_core_imports_nothing_from_review() -> None:
    # Both products import the core; the core imports neither, not even lazily or by name.
    offenders: list[str] = []
    for path in sorted(ENVIRONMENT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = _imports(path)
        names.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and " " not in node.value
        )
        offenders.extend(
            f"{path.name}: {name}"
            for name in sorted(names)
            if name == REVIEW_PACKAGE or name.startswith(REVIEW_PACKAGE + ".")
        )
    assert not offenders, "\n".join(offenders)


def test_importing_the_core_loads_no_review_module() -> None:
    modules = sorted(
        f"lemoncrow.pro.environment.{path.stem}" for path in ENVIRONMENT.glob("*.py") if path.stem != "__init__"
    )
    probe = (
        "import importlib, sys\n"
        f"for name in {modules!r}: importlib.import_module(name)\n"
        f"print([m for m in sys.modules if m == {REVIEW_PACKAGE!r} or m.startswith({REVIEW_PACKAGE + '.'!r})])"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]", done.stdout


def test_review_hands_the_runners_its_host() -> None:
    from lemoncrow.pro.capabilities.review import runner_host
    from lemoncrow.pro.capabilities.review.surfaces import SurfaceContext
    from lemoncrow.pro.environment.runner_host import RunnerHost

    members = {name for name, value in vars(RunnerHost).items() if isinstance(value, property)}
    assert members and not sorted(name for name in members if not hasattr(runner_host, name))
    assert SurfaceContext.runner_host.fget is not None
    assert SurfaceContext.runner_host.fget(None) is runner_host  # type: ignore[arg-type]


def test_hermetic_uses_the_shared_dependency_cache() -> None:
    from lemoncrow.pro.capabilities.review import hermetic
    from lemoncrow.pro.environment.dependency_cache import DependencyCache

    assert hermetic.DependencyCache is DependencyCache
