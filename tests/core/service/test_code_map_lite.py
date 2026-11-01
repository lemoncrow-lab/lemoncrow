"""The deterministic `/code` data source over the open-source Runtime Lite index."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.service import code_map, code_map_lite, code_surface
from lemoncrow.core.service.code_map_lite import (
    DEGRADED_REASON,
    LITE_BACKEND,
    RELATIONSHIP_BASIS,
    LiteCodeMapEngine,
)

_FILES = {
    "src/pkg/__init__.py": "",
    "src/pkg/core.py": "def charge():\n    return 1\n\n\nclass Gateway:\n    pass\n",
    "src/pkg/util.py": "def helper():\n    return 2\n",
    "src/pkg/api.py": (
        "import os\nfrom pkg.core import charge, Gateway\nfrom pkg import util\n\n\n"
        "def handler():\n    return charge() + util.helper()\n"
    ),
    "src/pkg/cli.py": "from pkg.core import charge\n\n\ndef main():\n    return charge()\n",
    "web/view.ts": "export function render() {\n  return 1;\n}\n",
    "web/app.ts": 'import { render } from "./view";\nexport function main() {\n  return render();\n}\n',
    "README.md": "# demo\ncharge the card\n",
}
# Rows in the Lite ``imports`` table for _FILES: api.py x3, cli.py x1, app.ts x1.
_IMPORT_ROWS = 5
_LINKS = {
    ("src/pkg/api.py", "src/pkg/core.py"),
    ("src/pkg/api.py", "src/pkg/util.py"),
    ("src/pkg/cli.py", "src/pkg/core.py"),
    ("web/app.ts", "web/view.ts"),
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative, text in _FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    code_surface._MODULES_CACHE.clear()
    return root


@pytest.fixture()
def engine(repo: Path) -> LiteCodeMapEngine:
    return LiteCodeMapEngine(repo)


def _symbol(engine: LiteCodeMapEngine, name: str) -> dict[str, Any]:
    found = [row for row in code_map.search_symbols(engine, name, limit=10) if row["label"] == name]
    assert found, name
    return code_map.symbol_row(engine, str(found[0]["id"]))


def _assert_import_level(payload: dict[str, Any]) -> None:
    context = payload["context"]
    assert context["relationship_basis"] == RELATIONSHIP_BASIS
    assert context["degraded"] is True
    assert context["degraded_reason"] == DEGRADED_REASON
    assert context["backend"] == LITE_BACKEND


def test_files_symbols_and_source_come_from_the_lite_index(engine: LiteCodeMapEngine, repo: Path) -> None:
    files = code_map.list_file_nodes(engine, repo)
    assert {row["path"] for row in files["files"]} == set(_FILES)

    symbols = code_map.list_file_symbols(engine, "src/pkg/core.py")
    assert [(row["label"], row["kind"], row["line"]) for row in symbols["symbols"]] == [
        ("charge", "function", 1),
        ("Gateway", "class", 5),
    ]
    assert symbols["truncated"] is False
    assert all(str(row["id"]).startswith("lite:") for row in symbols["symbols"])

    charge = _symbol(engine, "charge")
    assert charge["path"] == "src/pkg/core.py"
    detail = code_map.symbol_detail(engine, str(charge["id"]))
    assert detail["provenance"] == LITE_BACKEND
    assert detail["source"] == "def charge():"


def test_a_worktree_overlay_shows_every_visible_file_symbol_and_link(repo: Path) -> None:
    """A linked worktree's index holds only its own rows; the map still shows the whole tree."""

    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    worktree = repo.parent / "wt"
    subprocess.run([*git, "worktree", "add", "-q", "-b", "feat", str(worktree)], cwd=repo, check=True)
    LiteCodeMapEngine(repo).prepare()
    (worktree / "src/pkg/util.py").write_text("def helper_v2():\n    return 3\n", encoding="utf-8")
    (worktree / "web/view.ts").unlink()
    overlay = LiteCodeMapEngine(worktree)
    overlay.prepare()
    with overlay.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM main.files").fetchone()[0] == 1  # util.py only

    with overlay.connection() as connection:
        indexed = {str(row[0]) for row in connection.execute("SELECT DISTINCT file_path FROM symbols")}
    assert indexed == {"src/pkg/core.py", "src/pkg/util.py", "src/pkg/api.py", "src/pkg/cli.py", "web/app.ts"}
    assert _symbol(overlay, "charge")["path"] == "src/pkg/core.py"  # inherited from the main checkout
    assert _symbol(overlay, "helper_v2")["path"] == "src/pkg/util.py"  # the worktree's own version
    assert not [row for row in code_map.search_symbols(overlay, "helper", limit=10) if row["label"] == "helper"]
    links = {(row["source_path"], row["target_path"]) for row in overlay.import_links().rows}
    assert ("src/pkg/cli.py", "src/pkg/core.py") in links
    assert ("web/app.ts", "web/view.ts") not in links


def test_symbol_ids_resolve_only_to_the_symbol_they_name(engine: LiteCodeMapEngine) -> None:
    charge = _symbol(engine, "charge")
    prefix, digest, line, name = str(charge["id"]).split(":", 3)
    for foreign in ("charge", f"{prefix}:{digest}:{line}:Gateway", f"{prefix}:{'0' * 16}:{line}:{name}", "lite:x:y:z"):
        with pytest.raises(LookupError):
            code_map.symbol_row(engine, foreign)


def test_search_finds_symbols_and_bounded_text(engine: LiteCodeMapEngine) -> None:
    results = code_map.search_symbols(engine, "charge", limit=5)
    assert [(row["label"], row["path"]) for row in results] == [("charge", "src/pkg/core.py")]
    assert code_map.search_symbols(engine, "   ") == []

    every = code_map.search_text(engine, "charge", limit=12)
    assert {row["path"] for row in every} == {"README.md", "src/pkg/api.py", "src/pkg/cli.py", "src/pkg/core.py"}
    total = len(every)
    # The limit is the bound: exactly at it everything returns; under it, that many.
    assert len(code_map.search_text(engine, "charge", limit=total)) == total
    assert len(code_map.search_text(engine, "charge", limit=total - 1)) == total - 1
    # The index's own workspace is never searched.
    assert not any(row["path"].startswith(".lemoncrow") for row in every)


def test_text_search_ends_at_its_timeout_with_what_it_found(
    engine: LiteCodeMapEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_popen = subprocess.Popen

    def slow_grep(command: list[str], **kwargs: Any) -> Any:
        assert command[:5] == ["git", "-c", "core.quotepath=false", "grep", "--untracked"]
        script = r"printf 'README.md\0002\0001\000charge the card\n'; exec sleep 30"
        return real_popen(["sh", "-c", script], **kwargs)

    monkeypatch.setattr(code_map_lite.subprocess, "Popen", slow_grep)
    monkeypatch.setattr(code_map_lite, "_TEXT_SEARCH_TIMEOUT_S", 0.3)
    started = time.monotonic()
    matches = engine.search_text("charge", limit=12)
    assert time.monotonic() - started < 10
    assert [(row.file_path, row.line, row.column, row.text) for row in matches] == [
        ("README.md", 2, 1, "charge the card")
    ]


def test_import_links_resolve_src_layout_submodules_and_relative_imports(engine: LiteCodeMapEngine) -> None:
    links = engine.import_links()
    assert links.truncated is False
    assert {(row["source_path"], row["target_path"]) for row in links.rows} == _LINKS
    assert {row["kind"] for row in links.rows} == {"imports"}
    assert {row["relationship_basis"] for row in links.rows} == {RELATIONSHIP_BASIS}
    by_pair = {(row["source_path"], row["target_path"]): row for row in links.rows}
    assert {item["name"] for item in by_pair[("src/pkg/api.py", "src/pkg/core.py")]["symbols"]} == {
        "charge",
        "Gateway",
    }
    # ``from pkg import util`` names the submodule file, not the package.
    assert by_pair[("src/pkg/api.py", "src/pkg/util.py")]["evidence"] == [
        {"path": "src/pkg/api.py", "line": 3, "symbol": "util"}
    ]
    # Cached per index generation.
    assert engine.import_links() is links


def test_links_resolve_through_the_public_lite_resolver(engine: LiteCodeMapEngine) -> None:
    from lemoncrow_client.lite.index import ImportResolver

    links = engine.import_links()
    assert isinstance(links.resolver, ImportResolver)
    # ``import os`` is the standard library: external, never missing.
    assert links.stats[("Python", "external")] == 1
    assert not any(status == "missing" for _language, status in links.stats)


def test_ambiguous_suffix_resolves_to_nothing(tmp_path: Path) -> None:
    from lemoncrow_client.lite.index import ImportResolver

    resolver = ImportResolver(tmp_path, ["a/pkg/core.py", "b/pkg/core.py", "c/only/one.py"])
    assert resolver.resolve("x/main.py", "pkg.core").targets == {}
    assert set(resolver.resolve("x/main.py", "only.one").targets) == {"c/only/one.py"}
    assert resolver.resolve("web/a.ts", "../../escape").targets == {}


def test_file_relationships_are_labelled_import_level(engine: LiteCodeMapEngine) -> None:
    provider = engine.provider()
    core = code_surface.file_relationships(provider, "src/pkg/core.py")
    _assert_import_level(core)
    assert core["degraded_reason"] == DEGRADED_REASON
    assert [(row["path"], row["kind"]) for row in core["used_by"]] == [
        ("src/pkg/api.py", "imports"),
        ("src/pkg/cli.py", "imports"),
    ]
    assert core["uses"] == []
    api = code_surface.file_relationships(provider, "src/pkg/api.py")
    assert {row["path"] for row in api["uses"]} == {"src/pkg/core.py", "src/pkg/util.py"}
    # No import is ever presented as a call or a reference.
    for payload in (core, api):
        for row in [*payload["used_by"], *payload["uses"]]:
            assert set(row["kinds"]) == {"imports"}


def test_file_relationships_flag_rows_past_their_limit(engine: LiteCodeMapEngine) -> None:
    provider = engine.provider()
    # core.py has exactly two import rows touching it.
    at = provider.file_relationships(("src/pkg/core.py",), limit=2)
    assert (len(at["edges"]), at["truncated"]) == (2, False)
    past = provider.file_relationships(("src/pkg/core.py",), limit=1)
    assert (len(past["edges"]), past["truncated"]) == (1, True)
    assert past["relationship_basis"] == RELATIONSHIP_BASIS and past["degraded"] is True


def test_module_overview_aggregates_import_links_by_module(engine: LiteCodeMapEngine, repo: Path) -> None:
    paths = [str(row["path"]) for row in code_map.list_file_nodes(engine, repo)["files"]]
    overview = code_surface.modules_overview(engine.provider(), engine, repo, paths, {})
    _assert_import_level(overview)
    assert overview["links_truncated"] is False
    assignment = code_surface.module_partition(paths)
    expected = {(assignment[source], assignment[target]) for source, target in _LINKS}
    expected = {(source, target) for source, target in expected if source != target}
    assert {(edge["source"], edge["target"]) for edge in overview["edges"]} == expected
    assert all(set(edge["kinds"]) == {"imports"} for edge in overview["edges"])
    assert {module["id"] for module in overview["modules"]} == set(assignment.values())


def test_import_links_are_whole_at_the_bound_and_flagged_past_it(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_map_lite, "_MAX_LINK_IMPORT_ROWS", _IMPORT_ROWS)
    at = LiteCodeMapEngine(repo)
    assert at.import_links().truncated is False
    assert {(row["source_path"], row["target_path"]) for row in at.import_links().rows} == _LINKS
    assert at.provider().file_relationships(("web/view.ts",))["truncated"] is False

    monkeypatch.setattr(code_map_lite, "_MAX_LINK_IMPORT_ROWS", _IMPORT_ROWS - 1)
    past = LiteCodeMapEngine(repo)
    links = past.import_links()
    assert links.truncated is True
    # Rows are read in path order, so the last one (web/app.ts) is the one left out.
    assert {(row["source_path"], row["target_path"]) for row in links.rows} == _LINKS - {("web/app.ts", "web/view.ts")}
    provider = past.provider()
    assert provider.repository_file_links()["truncated"] is True
    assert provider.file_relationships(("web/view.ts",))["truncated"] is True
    assert code_surface.file_relationships(provider, "web/view.ts")["truncated"] is True
    paths = [str(row["path"]) for row in code_map.list_file_nodes(past, repo)["files"]]
    code_surface._MODULES_CACHE.clear()
    assert code_surface.modules_overview(provider, past, repo, paths, {})["links_truncated"] is True


def test_symbol_neighborhood_lists_importing_files_by_path(engine: LiteCodeMapEngine) -> None:
    charge = _symbol(engine, "charge")
    hood = engine.provider().neighborhood(charge)
    assert hood["relationship_basis"] == RELATIONSHIP_BASIS and hood["degraded"] is True
    importers = [node for node in hood["nodes"] if node["id"] != charge["id"]]
    # Importers are files labelled by their path: no caller name is implied.
    assert [(node["label"], node["kind"]) for node in importers] == [
        ("src/pkg/api.py", "file"),
        ("src/pkg/cli.py", "file"),
    ]
    assert {edge["kind"] for edge in hood["edges"]} == {"imports"}
    assert hood["truncated"] is False

    focus = code_surface.symbol_focus(engine.provider(), engine, engine.repo_root, charge, {})
    _assert_import_level(focus)


def test_symbol_neighborhood_bounds_are_flagged(engine: LiteCodeMapEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    charge = _symbol(engine, "charge")
    provider = engine.provider()
    assert provider.neighborhood(charge, limit=2)["truncated"] is False
    one = provider.neighborhood(charge, limit=1)
    assert (len(one["edges"]), one["truncated"]) == (1, True)

    # Two import rows name ``charge``.
    monkeypatch.setattr(code_map_lite, "_MAX_NEIGHBORHOOD_IMPORTS", 2)
    assert provider.neighborhood(charge)["truncated"] is False
    monkeypatch.setattr(code_map_lite, "_MAX_NEIGHBORHOOD_IMPORTS", 1)
    past = provider.neighborhood(charge)
    assert (len(past["edges"]), past["truncated"]) == (1, True)


def test_readiness_is_degraded_with_an_index_and_unavailable_without(
    engine: LiteCodeMapEngine, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = engine.provider().readiness().to_wire()
    assert ready["backend"] == LITE_BACKEND
    assert ready["reason"] == DEGRADED_REASON
    assert (ready["lexical"]["state"], ready["graph"]["state"], ready["semantic"]["state"]) == (
        "ready",
        "degraded",
        "unavailable",
    )

    missing = LiteCodeMapEngine(repo.parent / "unindexed")
    (repo.parent / "unindexed").mkdir()

    def fail() -> None:
        raise RuntimeError("no index here")

    monkeypatch.setattr(missing._index, "maybe_refresh", fail)
    readiness = missing.provider().readiness().to_wire()
    assert readiness["reason"] == "lite_index_unavailable"
    assert readiness["graph"]["state"] == "unavailable"
    assert missing.refresh_error.startswith("RuntimeError")
    # Every view still answers, with nothing in it.
    assert code_map.list_file_symbols(missing, "a.py")["symbols"] == []
    assert code_map.search_symbols(missing, "charge") == []
    assert missing.import_links().rows == ()


def test_map_engine_falls_back_to_lite_only_without_the_private_engine(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.core.capabilities import code_intelligence_provider as seam

    def absent(project_root: str) -> Any:
        raise seam.ProEngineUnavailable("absent")

    monkeypatch.setattr(code_map, "get_engine", absent)
    code_map_lite.lite_engine.cache_clear()
    assert isinstance(code_map.get_map_engine(str(repo)), LiteCodeMapEngine)

    private = object()
    monkeypatch.setattr(code_map, "get_engine", lambda project_root: private)

    def no_lite(project_root: str) -> Any:
        raise AssertionError("Runtime Lite must not be read while the private engine is present")

    monkeypatch.setattr(code_map_lite, "lite_engine", no_lite)
    assert code_map.get_map_engine(str(repo)) is private


def test_map_projects_name_the_lite_index_only_without_the_private_engine(
    engine: LiteCodeMapEngine, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.core.capabilities import code_intelligence_provider as seam

    engine.connection()  # builds the Lite index
    unindexed = tmp_path / "other"
    unindexed.mkdir()
    listed = [
        {"project_id": "proj_a", "root": str(repo), "indexed": False},
        {"project_id": "proj_b", "root": str(unindexed), "indexed": True},
    ]
    monkeypatch.setattr(code_map, "list_projects", lambda: [dict(row) for row in listed])

    monkeypatch.setattr(seam, "private_code_intelligence_available", lambda: False)
    lite = code_map.list_map_projects()
    assert [(row["index"], row["indexed"]) for row in lite] == [(LITE_BACKEND, True), (LITE_BACKEND, False)]

    monkeypatch.setattr(seam, "private_code_intelligence_available", lambda: True)
    assert code_map.list_map_projects() == listed
