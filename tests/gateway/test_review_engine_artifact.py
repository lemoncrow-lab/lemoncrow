from __future__ import annotations

import ast
import functools
import re
from collections.abc import Iterator
from pathlib import Path

from lemoncrow.pro.capabilities.review.analytics.facts import OPTIONAL_SOURCES

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = ROOT / "server" / "review-engine-files.txt"
OPTIONAL = ROOT / "server" / "review-engine-optional.txt"
BUILDER = ROOT / "scripts" / "build_review_engine.sh"
INSTALLER = ROOT / "scripts" / "review_engine.sh"
_PUBLIC_WORKFLOWS = ROOT / ".github" / "public-workflows"
RELEASE = (_PUBLIC_WORKFLOWS if _PUBLIC_WORKFLOWS.is_dir() else ROOT / ".github" / "workflows") / "release.yml"


def _listed(path: Path) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and (not line.lstrip().startswith("#"))
    )


def _allowlisted() -> tuple[str, ...]:
    return _listed(ALLOWLIST)


def _optional() -> tuple[str, ...]:
    return _listed(OPTIONAL)


def test_review_engine_frontend_bundle_refresh(tmp_path: Path) -> None:
    import os
    import subprocess

    # Execute the builder's real frontend preparation, without rebuilding wheels.
    text = BUILDER.read_text()
    block = text.split("FRONTEND_DIST=", 1)[1].split('[[ -f "$FRONTEND_DIST/index.html" ]]', 1)[0]
    script = "set -euo pipefail\nfunction lc_log() { :; }\nFRONTEND_DIST=" + block
    frontend = tmp_path / "frontend"
    (frontend / "dist").mkdir(parents=True)
    (frontend / "src").mkdir()
    (frontend / "node_modules").mkdir()
    source = frontend / "src" / "menu.tsx"
    source.write_text("Settings")
    bundle = frontend / "dist" / "index.html"
    bundle.write_text("old")
    os.utime(source, (100, 100))
    os.utime(bundle, (200, 200))
    tools_dir = tmp_path / "bin"
    tools_dir.mkdir()
    npm = tools_dir / "npm"
    npm.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> ../calls\nprintf "new" > dist/index.html\n')
    npm.chmod(0o755)
    env = {**os.environ, "PATH": str(tools_dir) + os.pathsep + os.environ["PATH"]}
    env.pop("LEMONCROW_REVIEW_ENGINE_FRONTEND_DIR", None)

    def run() -> None:
        subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, check=True, capture_output=True)

    run()
    assert not (tmp_path / "calls").exists()
    os.utime(source, (300, 300))
    run()
    assert (tmp_path / "calls").read_text().splitlines() == ["run build"]
    assert bundle.read_text() == "new"
    run()
    assert (tmp_path / "calls").read_text().splitlines() == ["run build"]
    # Build configuration changes invalidate an existing bundle too.
    (frontend / "package.json").write_text("{}")
    os.utime(frontend / "package.json", (bundle.stat().st_mtime + 1, bundle.stat().st_mtime + 1))
    run()
    assert (tmp_path / "calls").read_text().splitlines() == ["run build", "run build"]
    # An explicit prebuilt frontend is an intentional override.
    env["LEMONCROW_REVIEW_ENGINE_FRONTEND_DIR"] = "custom"
    run()
    assert (tmp_path / "calls").read_text().splitlines() == ["run build", "run build"]


def test_review_engine_distribution_contract() -> None:
    """case: test_review_engine_allowlist_contains_review_core_but_not_runtime_implementation"""
    files = _allowlisted()
    assert "src/lemoncrow/pro/capabilities/review/models.py" in files
    assert "src/lemoncrow/pro/capabilities/review/ask.py" in files
    assert "src/lemoncrow/pro/capabilities/review/intelligence_runtime.py" in files
    assert "src/lemoncrow/pro/capabilities/review/tool_server.py" in files
    assert "client/src/lemoncrow_client/kit/redaction.py" in files
    assert "server/src/lemoncrow_server_core/bootstrap.py" in files
    assert "server/src/lemoncrow_server_core/local_review.py" in files
    forbidden_fragments = (
        "/gateway/",
        "/capabilities/model_routing/",
        "/capabilities/code_context/",
        "/capabilities/memory/",
        "/capabilities/swarm/",
        "lemoncrow_client/mcpserver.py",
        "lemoncrow_client/dispatcher.py",
        "lemoncrow_client/session.py",
        "lemoncrow_client/routing.py",
        "lemoncrow_server_core/registry_dispatch.py",
        "lemoncrow_server_core/product_telemetry.py",
        "lemoncrow_server_core/tracing.py",
        "lemoncrow_server_core/dashboard_service.py",
    )
    for relative in files:
        assert not any(fragment in relative for fragment in forbidden_fragments), relative
    "case: test_review_engine_builder_audits_and_executes_the_isolated_wheel"
    text = BUILDER.read_text(encoding="utf-8")
    assert "server/review-engine-files.txt" in text
    assert 'name = "lemoncrow-review-engine"' in text
    assert "Smoke-testing Review engine in a clean venv" in text
    assert "--product review --ephemeral --check-only" in text
    assert '"lemoncrow/gateway/"' in text
    assert '"lemoncrow_client/mcpserver.py"' in text
    assert '"lemoncrow_server_core/registry_dispatch.py"' in text
    assert "frontend/dist" in text
    assert "vendor frontend" in text
    assert "lemoncrow_server_core/review_engine_build.json" in text
    assert '"override": source != "frontend/dist"' in text
    assert 'lemoncrow-review-server = "lemoncrow_server_core.cli:review_main"' in text
    # The engine ships its license (PEP 639 metadata and files in the wheel, the same files beside it).
    assert 'license = "Apache-2.0"' in text
    assert 'license-files = ["LICENSE", "LICENSE-APACHE", "NOTICE"]' in text
    assert 'authors = [{ name = "LemonCrow" }]' in text
    assert "LICENSE_FILES=(LICENSE LICENSE-APACHE NOTICE)" in text
    assert 'vendor frontend "${LICENSE_FILES[@]}"' in text
    assert "scripts/audit_release_boundary.py review-engine" in text
    "case: test_review_engine_installer_owns_only_review_engine_and_shared_frontend"
    text = INSTALLER.read_text(encoding="utf-8")
    assert "${STATE_DIR}/review/engine" in text
    assert 'ASSET="lemoncrow-review-engine.tar.gz"' in text
    assert "--product review --ephemeral --check-only" in text
    assert "${STATE_DIR}/install/frontend" in text
    assert "Runtime implementation: absent" in text
    assert "runtime/client" not in text
    assert "uv-tools/lemoncrow" not in text
    "case: test_release_publishes_and_reverifies_review_engine"
    text = RELEASE.read_text(encoding="utf-8")
    assert "bash scripts/build_review_engine.sh" in text
    assert "dist/lemoncrow-review-engine.tar.gz" in text
    assert "dist/lemoncrow-review-engine.tar.gz.sha256" in text
    assert 'review_engine="hosted/lemoncrow-review-engine.tar.gz"' in text
    assert "Review engine checksum mismatch" in text
    assert "bash scripts/stage_release.sh" in text
    assert "review_engine" in (ROOT / "scripts" / "stage_release.sh").read_text(encoding="utf-8")


def test_review_engine_closure_contract() -> None:
    """case: test_review_engine_allowlist_is_closed_over_eager_imports"""
    "The wheel ships only allowlisted files, so every eager import must be one."
    shipped = {(ROOT / relative).resolve() for relative in _allowlisted()}
    missing: set[str] = set()
    for relative in _allowlisted():
        path = ROOT / relative
        if path.suffix != ".py":
            continue
        root = next(r for r in _FIRST_PARTY_ROOTS.values() if path.is_relative_to(r))
        package = ".".join(path.relative_to(root).parts[:-1])
        for module in _eager_imports(path, package):
            source = _module_source(module)
            if source is not None and source.resolve() not in shipped:
                missing.add(f"{relative}: {module}")
    assert sorted(missing) == []
    "case: test_review_engine_lists_are_disjoint_and_name_real_files"
    shipped, optional = (_allowlisted(), _optional())
    assert sorted(set(shipped) & set(optional)) == []
    assert sorted(item for item in shipped if shipped.count(item) > 1) == []
    assert sorted(item for item in optional if optional.count(item) > 1) == []
    assert sorted(item for item in optional if not (ROOT / item).is_file()) == []
    "case: test_review_engine_is_closed_over_every_import_and_data_file"
    "Every module shipped code imports (eager or lazy) and every data file it names\n    or keeps beside it ships, or is left out on purpose in review-engine-optional.txt."
    listed = set(_allowlisted()) | set(_optional())
    unlisted = sorted(
        (f"{target} <- {sorted(sources)[0]}" for target, sources in _reached().items() if target not in listed)
    )
    assert unlisted == []
    "case: test_review_engine_optional_entries_are_still_reached"
    "No stale entry: each left-out file is still imported, named or beside shipped code."
    reached = _reached()
    assert sorted(item for item in _optional() if item not in reached) == []
    "case: test_review_engine_ships_the_packages_of_its_modules"
    "A shipped module imports through its parent packages, so each ships too."
    shipped = set(_allowlisted())
    missing: set[str] = set()
    for relative, path in _shipped_python():
        root = next(r for r in _FIRST_PARTY_ROOTS.values() if path.is_relative_to(r))
        for depth in range(1, len(path.relative_to(root).parts)):
            init = root.joinpath(*path.relative_to(root).parts[:depth], "__init__.py")
            if init.is_file() and _relative(init) not in shipped:
                missing.add(f"{_relative(init)} <- {relative}")
    assert sorted(missing) == []
    "case: test_review_engine_ships_every_migration_its_stores_declare"
    "A store cannot open without its schema scripts: they ship, never optional."
    shipped = set(_allowlisted())
    declared = {
        f"{_MIGRATIONS_DIR}/{name}": relative
        for relative, path in _shipped_python()
        for name in _declared_migrations(path)
    }
    assert declared, "no MIGRATIONS declaration found in shipped code"
    assert sorted((f"{script} <- {owner}" for script, owner in declared.items() if script not in shipped)) == []


def test_review_engine_runtime_boundary_contract() -> None:
    """case: test_review_engine_allowlisted_sources_do_not_import_runtime_implementation"""
    forbidden = (
        "lemoncrow.gateway",
        "lemoncrow.pro.capabilities.code_context",
        "lemoncrow.pro.capabilities.model_routing",
        "lemoncrow.pro.capabilities.memory",
        "lemoncrow.pro.capabilities.swarm",
        "lemoncrow_client.mcpserver",
        "lemoncrow_client.dispatcher",
        "lemoncrow_client.session",
        "lemoncrow_client.routing",
        "lemoncrow_server_core.registry_dispatch",
        "lemoncrow_server_core.product_telemetry",
        "lemoncrow_server_core.tracing",
        "lemoncrow_server_core.dashboard_service",
        "lemoncrow_server_core.runtime_product_surface",
    )
    leaks: list[str] = []
    for relative in _allowlisted():
        if not relative.endswith(".py"):
            continue
        path = ROOT / relative
        for module in _imported_modules(path):
            if any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden):
                leaks.append(f"{relative}: {module}")
    assert leaks == []
    "case: test_review_capability_package_has_no_direct_runtime_imports"
    forbidden = (
        "lemoncrow.gateway",
        "lemoncrow.pro.capabilities.code_context",
        "lemoncrow.pro.capabilities.model_routing",
        "lemoncrow.pro.capabilities.memory",
        "lemoncrow.pro.capabilities.swarm",
    )
    leaks: list[str] = []
    review_root = ROOT / "src/lemoncrow/pro/capabilities/review"
    forbidden_refs = (
        "lemoncrow.gateway.adapters.mcp_server",
        "lemoncrow.gateway.cli.lemoncode_host",
        "lemoncrow.pro.capabilities.code_context.engine",
    )
    for path in sorted(review_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for module in _imported_modules(path):
            if any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden):
                leaks.append(f"{path.relative_to(ROOT)}: {module}")
        for reference in forbidden_refs:
            if reference in source:
                leaks.append(f"{path.relative_to(ROOT)}: executable reference {reference}")
    assert leaks == []


def _imported_modules(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return tuple(modules)


_FIRST_PARTY_ROOTS = {
    "lemoncrow": ROOT / "src",
    "lemoncrow_client": ROOT / "client" / "src",
    "lemoncrow_server_core": ROOT / "server" / "src",
}


def _module_source(module: str) -> Path | None:
    root = _FIRST_PARTY_ROOTS.get(module.split(".", 1)[0])
    if root is None:
        return None
    base = root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _eager_imports(path: Path, package: str) -> tuple[str, ...]:
    """First-party modules a file imports at module level, outside guards."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                parent = ".".join(parts[: len(parts) - node.level + 1])
                base = f"{parent}.{node.module}" if node.module else parent
            else:
                base = node.module or ""
            modules.append(base)
            modules.extend(f"{base}.{alias.name}" for alias in node.names)
    return tuple(module for module in modules if module.split(".", 1)[0] in _FIRST_PARTY_ROOTS)


_MIGRATIONS_DIR = "src/lemoncrow/infra/storage/migrations"
_DATA_NAME = re.compile("^[\\w.-]+\\.(?:sql|json|ya?ml|toml|html|css|js|txt|scm|md|j2|jinja2?|csv)$")
_REVIEW_PACKAGE = "lemoncrow.pro.capabilities.review"
_DYNAMIC_IMPORTS = frozenset({"import_module", "find_spec", "__import__"})


@functools.cache
def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _package_of(path: Path) -> str:
    root = next(r for r in _FIRST_PARTY_ROOTS.values() if path.is_relative_to(r))
    return ".".join(path.relative_to(root).parts[:-1])


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _every_import(path: Path) -> Iterator[tuple[int, str]]:
    """``(line, module)`` for every first-party module a file can import, at any depth.

    Eager and lazy alike (function bodies, guards, TYPE_CHECKING): ``import a.b``,
    ``from a import b`` (``a`` and ``a.b``), relative imports, and string-named
    ``importlib.import_module`` / ``find_spec`` / ``__import__`` calls.
    """
    package = _package_of(path)
    for node in ast.walk(_tree(path)):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                parent = ".".join(parts[: len(parts) - node.level + 1])
                base = f"{parent}.{node.module}" if node.module else parent
            else:
                base = node.module or ""
            modules = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in _DYNAMIC_IMPORTS and isinstance(node.args[0].value, str):
                modules = [node.args[0].value]
        for module in modules:
            if module.split(".", 1)[0] in _FIRST_PARTY_ROOTS:
                yield (node.lineno, module)


def _shipped_python() -> Iterator[tuple[str, Path]]:
    for relative in _allowlisted():
        if relative.endswith(".py"):
            yield (relative, ROOT / relative)


def _declared_migrations(path: Path) -> Iterator[str]:
    """Script names a module's ``MIGRATIONS`` (class or module level) declares."""
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Assign):
            targets, value = (node.targets, node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = ([node.target], node.value)
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "MIGRATIONS" for target in targets):
            for item in ast.walk(value):
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    yield item.value


def _data_files_by_name() -> dict[str, list[str]]:
    """Every non-Python file of the first-party source trees, by file name."""
    out: dict[str, list[str]] = {}
    for root in _FIRST_PARTY_ROOTS.values():
        for path in root.rglob("*"):
            if path.is_file() and path.suffix not in {".py", ".pyc"} and ("__pycache__" not in path.parts):
                out.setdefault(path.name, []).append(_relative(path))
    return out


@functools.cache
def _reached() -> dict[str, set[str]]:
    """What shipped code can reach -> where: imported modules (with the parent
    packages a shipped module needs), named data files, and files beside it."""
    reached: dict[str, set[str]] = {}
    data_files = _data_files_by_name()
    for relative, path in _shipped_python():
        for lineno, module in _every_import(path):
            source = _module_source(module)
            if source is not None:
                reached.setdefault(_relative(source), set()).add(f"{relative}:{lineno}: import {module}")
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _DATA_NAME.match(node.value):
                for candidate in data_files.get(node.value, ()):
                    reached.setdefault(candidate, set()).add(f"{relative}:{node.lineno}: names {node.value}")
        for sibling in path.parent.iterdir():
            if sibling.is_file() and sibling.suffix not in {".py", ".pyc"}:
                reached.setdefault(_relative(sibling), set()).add(f"{relative}: beside it")
    for name, source in OPTIONAL_SOURCES.items():
        for module in source.modules:
            found = _module_source(f"{_REVIEW_PACKAGE}.{module}")
            assert found is not None, f"analytics source {name}: no module {module}"
            reached.setdefault(_relative(found), set()).add(f"analytics/facts.py: OPTIONAL_SOURCES[{name!r}]")
    return reached
