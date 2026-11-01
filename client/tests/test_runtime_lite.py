from __future__ import annotations

import io
import itertools
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from lemoncrow_client.lite import mcp as lite_mcp
from lemoncrow_client.lite.index import LiteIndex, LiteIndexProgress, ProgressCallback
from lemoncrow_client.lite.mcp import SERVER_NAME, LiteMcp, serve
from lemoncrow_client.lite.policy import LITE_VISIBLE_TOOLS, LOCAL_LITE_TOOLS, TOOL_PLACEMENT, Placement, placement_for

CLIENT_ROOT = Path(__file__).resolve().parents[1]


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "lite@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Runtime Lite"], cwd=root, check=True)
    return root


def _env(root: Path, tmp_path: Path) -> dict[str, str]:
    return {
        "LEMONCROW_WORKSPACE_ROOT": str(root),
        "LEMONCROW_HOME": str(tmp_path / "state"),
        "LEMONCROW_URL": "https://hosted.example.invalid",
        "LEMONCROW_TOKEN": "this-login-state-must-be-ignored",
        "LEMONCROW_INSTALL_MODE": "hosted",
        "HOME": str(tmp_path),
    }


@pytest.mark.parametrize("explicit", [True, False])
def test_non_git_workspace_uses_grep_without_indexing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "app.py").write_text("def target_symbol():\n    return 'a.b'\n", encoding="utf-8")
    (root / "other.py").write_text("VALUE = 'axb'\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (root / "ignored.py").write_text("a.b\n", encoding="utf-8")
    environment = _env(root, tmp_path)
    if not explicit:
        environment.pop("LEMONCROW_WORKSPACE_ROOT")
        monkeypatch.chdir(root)

    def no_index(*args, **kwargs):
        pytest.fail("non-Git mode must never prepare or refresh an index")

    monkeypatch.setattr(LiteIndex, "adopt_prepared", no_index)
    monkeypatch.setattr(LiteIndex, "refresh_needed", no_index)
    server = LiteMcp(environment=environment)
    try:
        initialized = server.handle({"id": 1, "method": "initialize"})
        assert "No Git: code_search uses literal grep" in initialized["result"]["instructions"]
        assert server._prepare_thread is None
        assert server.prepare()["mode"] == "grep"
        answer = server.call("code_search", {"query": "a.b"})
        assert not answer["isError"]
        text = json.dumps(answer["content"])
        assert answer["content"][0]["text"] == "[no git: literal grep]"
        assert "app.py" in text
        assert "other.py" not in text
        assert "ignored.py" not in text
        assert not server.call("read", {"files": ["app.py"]})["isError"]
        assert not server.call("edit", {"edits": [{"path": "new.py", "new": "NEW = 1\n", "replace": True}]})["isError"]
        assert (root / "new.py").read_text() == "NEW = 1\n"
        assert not server.call("bash", {"command": "pwd"})["isError"]
        for name, arguments in [
            ("relations", {"symbol": "target_symbol"}),
            ("graph", {}),
            ("blame", {"path": "app.py"}),
            ("read", {"symbol": "target_symbol"}),
        ]:
            refused = server.call(name, arguments)
            assert refused["isError"]
            assert "grep fallback mode" in json.dumps(refused["content"])
        assert not (root / ".lemoncrow" / "workspace").exists()
    finally:
        server.close()


def test_tool_placement_is_total_and_lite_surface_is_explicit() -> None:
    surface = json.loads((CLIENT_ROOT / "src/lemoncrow_client/toolsurface.json").read_text(encoding="utf-8"))["tools"]
    assert set(TOOL_PLACEMENT) == set(surface)
    assert LOCAL_LITE_TOOLS == (
        "bash",
        "edit",
        "grep",
        "read",
        "scan",
        "codemod",
        "blame",
        "code_search",
        "relations",
        "graph",
        "web_fetch",
    )
    from lemoncrow.core.environment import LLM_VISIBLE_TOOLS

    assert LITE_VISIBLE_TOOLS == LLM_VISIBLE_TOOLS
    assert set(TOOL_PLACEMENT.values()) == {
        Placement.LOCAL_LITE,
        Placement.HOSTED,
        Placement.HOSTED_ADMIN,
        Placement.NOT_EXPOSED,
    }
    with pytest.raises(KeyError):
        placement_for("future_tool_without_owner")


def test_lite_index_is_persistent_incremental_fresh_and_git_ignored(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text(
        "from util import helper\n\ndef target_symbol(value):\n    return helper(value)\n",
        encoding="utf-8",
    )
    (root / "util.py").write_text("def helper(value):\n    return value + 1\n", encoding="utf-8")

    index = LiteIndex(root)
    cold = index.prepare()
    assert cold.cold is True
    assert cold.files_changed == 2
    assert cold.path == root / ".lemoncrow" / "workspace" / "runtime-lite-v3.sqlite3"
    assert cold.path.is_file()
    assert (
        subprocess.run(
            ["git", "check-ignore", "-q", ".lemoncrow/workspace/runtime-lite-v3.sqlite3"],
            cwd=root,
            check=False,
        ).returncode
        == 0
    )

    exact = index.search("target_symbol")
    assert exact["files"][0]["path"] == "app.py"
    assert "symbol" in exact["files"][0]["channels"]

    warm = index.prepare()
    assert warm.cold is False
    assert warm.files_changed == 0
    assert warm.files_deleted == 0
    assert warm.generation == cold.generation

    (root / "app.py").write_text(
        "from util import helper\n\ndef replacement_symbol(value):\n    return helper(value)\n",
        encoding="utf-8",
    )
    index.mark_dirty()
    changed = index.search("replacement_symbol")
    assert changed["files"][0]["path"] == "app.py"
    assert changed["generation"] > cold.generation

    # External edits (for example the coding host writing directly) are picked
    # up by the polling freshness proof without a Lite tool marking the index.
    (root / "app.py").write_text(
        "from util import helper\n\ndef externally_changed_symbol(value):\n    return helper(value)\n",
        encoding="utf-8",
    )
    index._last_refresh_check = 0.0
    external = index.search("externally_changed_symbol")
    assert external["files"][0]["path"] == "app.py"
    assert external["generation"] > changed["generation"]

    (root / "util.py").unlink()
    deleted = index.prepare()
    assert deleted.files_deleted == 1
    assert all(item["path"] != "util.py" for item in index.search("helper")["files"])


def test_lite_indexes_python_module_bindings_without_local_assignment_noise(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "settings.py").write_text(
        "MODULE_LIMIT = 7\n"
        "typed_limit: int = 9\n"
        "comparison_only == 11\n\n"
        "def calculate():\n"
        "    local_limit = 11\n"
        "    return local_limit\n",
        encoding="utf-8",
    )

    index = LiteIndex(root)
    index.prepare()

    module_binding = index.exact_symbols("MODULE_LIMIT")
    typed_binding = index.exact_symbols("typed_limit")
    assert module_binding[0]["path"] == "settings.py"
    assert module_binding[0]["kind"] == "binding"
    assert typed_binding[0]["kind"] == "binding"
    assert index.exact_symbols("comparison_only") == []
    assert index.exact_symbols("local_limit") == []
    assert index.search("MODULE_LIMIT")["files"][0]["path"] == "settings.py"


def test_lite_index_migrates_v1_fts_to_porter_stemming(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "lexical.txt").write_text("combine vectors safely\n", encoding="utf-8")
    store = root / ".lemoncrow" / "workspace"
    store.mkdir(parents=True)
    db = store / "runtime-lite.sqlite3"
    connection = sqlite3.connect(db)
    connection.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta(key, value) VALUES('schema_version', '1');
        INSERT INTO meta(key, value) VALUES('generation', '1');
        INSERT INTO meta(key, value) VALUES('worktree_signature', 'legacy');
        CREATE VIRTUAL TABLE file_fts USING fts5(
            path UNINDEXED,
            content,
            tokenize='unicode61'
        );
        INSERT INTO file_fts(path, content) VALUES('stale.txt', 'unrelated');
        """)
    connection.commit()
    connection.close()

    index = LiteIndex(root)
    report = index.prepare()

    assert report.cold is True
    assert report.generation == 1
    with sqlite3.connect(index.db_path) as migrated:
        version = migrated.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert version == ("4",)
    with sqlite3.connect(db) as original:
        assert original.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ("1",)
    result = index.search("vector")
    assert result["files"][0]["path"] == "lexical.txt"
    assert "fts" in result["files"][0]["channels"]


def test_import_relations_list_module_matches_then_importers_by_name(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "a.py").write_text("from pkg.ui import Button as B\n", encoding="utf-8")
    (root / "b.ts").write_text('import {\n  Button,\n} from "./button";\n', encoding="utf-8")
    (root / "c.py").write_text("import Button\n", encoding="utf-8")
    (root / "d.py").write_text("from x import Buttons\n", encoding="utf-8")
    index = LiteIndex(root)
    index.prepare()

    items = index.relations("Button", kind="imports")["items"]
    assert [(item["path"], item["module"]) for item in items] == [
        ("c.py", "Button"),
        ("a.py", "pkg.ui"),
        ("b.ts", "./button"),
    ]
    assert len(index.relations("Button", kind="imports", limit=1)["items"]) == 1


def test_import_relations_by_name_bound_at_and_past(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_client.lite import index as lite_index

    root = _git_repo(tmp_path)
    # Rows whose names contain the text, in path order: a.py (not the name), b.py (the name).
    (root / "a.py").write_text("from x import Buttons\n", encoding="utf-8")
    (root / "b.py").write_text("from y import Button\n", encoding="utf-8")
    index = LiteIndex(root)
    index.prepare()

    monkeypatch.setattr(lite_index, "_MAX_NAMED_IMPORT_ROWS", 2)
    assert [item["path"] for item in index.relations("Button", kind="imports")["items"]] == ["b.py"]
    monkeypatch.setattr(lite_index, "_MAX_NAMED_IMPORT_ROWS", 1)
    assert index.relations("Button", kind="imports")["items"] == []


def test_lite_index_upgrades_v2_import_rows_once_keeping_old_rows_until_rebuilt(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "core.py").write_text("def charge():\n    return 1\n", encoding="utf-8")
    (root / "pkg" / "api.py").write_text("from .core import (\n    charge,\n)\nimport os\n", encoding="utf-8")
    first = LiteIndex(root).prepare()
    db = LiteIndex(root).db_path
    # A v2 index: its parser kept ``import os`` but had no row for the relative import.
    with sqlite3.connect(db) as legacy:
        legacy.execute("DELETE FROM imports WHERE module='.core'")
        legacy.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        legacy.execute("UPDATE meta SET value='proved' WHERE key='worktree_signature'")

    index = LiteIndex(root)
    index._connect(initialize=True).close()
    with sqlite3.connect(db) as migrated:
        assert migrated.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ("4",)
        assert migrated.execute("SELECT value FROM meta WHERE key='worktree_signature'").fetchone() == ("",)
        assert migrated.execute("SELECT DISTINCT identity FROM files").fetchall() == [("",)]
        # Until the rebuild commits, readers keep the old rows.
        assert migrated.execute("SELECT module FROM imports").fetchall() == [("os",)]

    # A prepared launch cannot adopt the migrated generation; it re-reads every file once.
    upgraded = index.adopt_prepared()
    assert upgraded.files_changed == 3
    assert upgraded.generation == first.generation + 1
    with sqlite3.connect(db) as rebuilt:
        rows = rebuilt.execute("SELECT path, module, imported, line FROM imports ORDER BY line").fetchall()
    assert rows == [("pkg/api.py", ".core", "charge", 1), ("pkg/api.py", "os", "", 4)]
    assert LiteIndex(root).prepare().files_changed == 0


def test_prepared_mcp_adopts_matching_generation_without_rewalking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def prepared_symbol():\n    return 1\n", encoding="utf-8")
    prepared = LiteIndex(root).prepare()

    env = _env(root, tmp_path)
    env["LEMONCROW_LITE_PREPARED"] = "1"
    server = LiteMcp(environment=env)

    def forbidden_walk(_root: Path):
        raise AssertionError("matching pre-launch generation should not be walked again")

    monkeypatch.setattr("lemoncrow_client.lite.index._iter_git_paths", forbidden_walk)
    initialized = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert initialized is not None
    assert server.index.generation() == prepared.generation
    found = server.call("code_search", {"query": "prepared_symbol"})
    assert found["structuredContent"]["files"][0]["path"] == "app.py"
    server.close()


def test_prepared_mcp_rechecks_when_worktree_changed_before_initialize(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    source = root / "app.py"
    source.write_text("def before_launch():\n    return 1\n", encoding="utf-8")
    prepared = LiteIndex(root).prepare()

    source.write_text("def changed_before_mcp():\n    return 2\n", encoding="utf-8")
    env = _env(root, tmp_path)
    env["LEMONCROW_LITE_PREPARED"] = "1"
    server = LiteMcp(environment=env)
    initialized = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert initialized is not None
    assert server.index.generation() == prepared.generation

    # Initialization itself is side-effect free. The first indexed request can
    # use the durable generation immediately and owns scheduling the catch-up.
    server.call("code_search", {"query": "before_launch"})
    deadline = time.monotonic() + 5.0
    while server.index.generation() <= prepared.generation and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.index.generation() > prepared.generation
    found = server.call("code_search", {"query": "changed_before_mcp"})
    assert found["structuredContent"]["files"][0]["path"] == "app.py"
    assert server.index.generation() > prepared.generation
    server.close()


def test_warm_initialize_does_not_wait_for_freshness_or_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lemoncrow_client.lite import index as lite_index_module

    root = _git_repo(tmp_path)
    source = root / "app.py"
    source.write_text("def old_symbol():\n    return 1\n", encoding="utf-8")
    prepared = LiteIndex(root).prepare()
    source.write_text("def new_symbol():\n    return 2\n", encoding="utf-8")

    server = LiteMcp(environment=_env(root, tmp_path))
    real_signature = lite_index_module._worktree_signature
    freshness_started = threading.Event()
    release_freshness = threading.Event()
    signature_calls = 0
    signature_lock = threading.Lock()

    def blocked_signature(repo_root: Path) -> str | None:
        nonlocal signature_calls
        with signature_lock:
            signature_calls += 1
        freshness_started.set()
        assert release_freshness.wait(timeout=5.0)
        return real_signature(repo_root)

    monkeypatch.setattr(lite_index_module, "_worktree_signature", blocked_signature)

    initialized = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert initialized is not None
    assert server.index.generation() == prepared.generation
    assert not freshness_started.is_set(), "warm initialize started git freshness work"
    with signature_lock:
        assert signature_calls == 0

    # The first request serves the previous WAL generation without waiting,
    # then schedules the catch-up in the owned refresh lane.
    stale = server.call("code_search", {"query": "old_symbol"})
    assert stale["structuredContent"]["files"][0]["path"] == "app.py"
    assert freshness_started.wait(timeout=2.0)
    assert server.index.generation() == prepared.generation

    release_freshness.set()
    deadline = time.monotonic() + 5.0
    while server.index.generation() <= prepared.generation and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.index.generation() > prepared.generation
    fresh = server.call("code_search", {"query": "new_symbol"})
    assert fresh["structuredContent"]["files"][0]["path"] == "app.py"
    server.close()


def _gated_prepare(server: LiteMcp, release: threading.Event, entered: threading.Event) -> object:
    """Wrap ``server.index.prepare`` so the build parks after reporting its first scan step."""

    real_prepare = server.index.prepare

    def gated_prepare(*, force: bool = False, progress: ProgressCallback | None = None) -> object:
        def gated(seen: LiteIndexProgress) -> None:
            if progress is not None:
                progress(seen)
            if seen.phase == "scan" and seen.done == 0:
                entered.set()
                assert release.wait(30)

        return real_prepare(force=force, progress=gated)

    return gated_prepare


def test_initialize_answers_before_a_slow_index_build_and_reports_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def background_symbol():\n    return 1\n", encoding="utf-8")
    server = LiteMcp(environment=_env(root, tmp_path))
    release, entered = threading.Event(), threading.Event()
    monkeypatch.setattr(server.index, "prepare", _gated_prepare(server, release, entered))
    monkeypatch.setattr(lite_mcp, "_INDEX_WAIT_S", 0.05)

    started = time.monotonic()
    initialized = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert initialized is not None
    assert initialized["result"]["serverInfo"]["name"] == SERVER_NAME
    assert time.monotonic() - started < 5
    assert entered.wait(30)

    pending = server.call("code_search", {"query": "background_symbol"})
    assert pending["isError"] is True
    text = pending["content"][0]["text"]
    assert text.startswith("[lc:index_building]")
    assert " files (0%)" in text
    assert "retryable=true" in text

    # File tools never wait for the index.
    read = server.call("read", {"files": ["app.py"]})
    assert read["isError"] is False
    assert "background_symbol" in read["content"][0]["text"]

    release.set()
    monkeypatch.setattr(lite_mcp, "_INDEX_WAIT_S", 30.0)
    found = server.call("code_search", {"query": "background_symbol"})
    assert found["isError"] is False
    assert found["structuredContent"]["files"][0]["path"] == "app.py"
    server.close()


def test_close_abandons_an_unfinished_background_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def abandoned_symbol():\n    return 1\n", encoding="utf-8")
    server = LiteMcp(environment=_env(root, tmp_path))
    release, entered = threading.Event(), threading.Event()
    monkeypatch.setattr(server.index, "prepare", _gated_prepare(server, release, entered))
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert entered.wait(30)

    threading.Timer(0.2, release.set).start()
    server.close()

    thread = server._prepare_thread
    assert thread is not None and not thread.is_alive()
    assert server._prepare_error is None
    # The cancelled build rolled back; nothing half-written was published.
    assert LiteIndex(root).generation() == 0


def test_background_build_failure_surfaces_on_the_next_index_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def retried_symbol():\n    return 1\n", encoding="utf-8")
    server = LiteMcp(environment=_env(root, tmp_path))
    real_prepare = server.index.prepare
    calls: list[int] = []

    def failing_once(*, force: bool = False, progress: ProgressCallback | None = None) -> object:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk vanished")
        return real_prepare(force=force, progress=progress)

    monkeypatch.setattr(server.index, "prepare", failing_once)
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    failed = server.call("code_search", {"query": "retried_symbol"})
    assert failed["isError"] is True
    assert failed["content"][0]["text"] == "[lc:lite_failed] disk vanished"

    retried = server.call("code_search", {"query": "retried_symbol"})
    assert retried["isError"] is False
    assert retried["structuredContent"]["files"][0]["path"] == "app.py"
    server.close()


def test_lite_prepare_reports_ordered_progress_with_a_known_total(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    for number in range(3):
        (root / f"module_{number}.py").write_text(f"def f{number}():\n    return {number}\n", encoding="utf-8")

    seen: list[LiteIndexProgress] = []
    report = LiteIndex(root).prepare(progress=seen.append)

    phases = [item.phase for item in seen]
    assert phases[0] == "seed"
    assert phases[-1] == "done"
    assert phases.index("lock") < phases.index("scan") < phases.index("prune") < phases.index("commit")
    first_scan = next(item for item in seen if item.phase == "scan")
    final = seen[-1]
    assert first_scan.total == final.total == final.done >= report.files_seen == 3
    assert final.changed == report.files_changed == 3
    assert all(earlier.elapsed_s <= later.elapsed_s for earlier, later in itertools.pairwise(seen))

    # A raising callback aborts the build without publishing a generation.
    (root / "module_9.py").write_text("def late():\n    return 9\n", encoding="utf-8")

    def stop(item: LiteIndexProgress) -> None:
        if item.phase == "commit":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        LiteIndex(root).prepare(progress=stop)
    assert LiteIndex(root).generation() == report.generation


def test_fts_path_imports_relations_and_basic_repo_map(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "billing_service.py").write_text(
        "from money import normalize_amount\n\n"
        "def calculate_invoice_total(lines):\n"
        "    # calculate invoice total including discounts\n"
        "    return normalize_amount(sum(lines))\n",
        encoding="utf-8",
    )
    (root / "money.py").write_text("def normalize_amount(value):\n    return round(value, 2)\n", encoding="utf-8")
    index = LiteIndex(root)
    index.prepare()

    lexical = index.search("calculate invoice total")
    assert lexical["files"][0]["path"] == "billing_service.py"
    assert "fts" in lexical["files"][0]["channels"]

    assert index.exact_symbols("normalize_amount")[0]["path"] == "money.py"
    imports = index.relations("money", kind="imports")
    assert any(item["path"] == "billing_service.py" for item in imports["items"])
    repo_map = index.repo_map(limit=10)
    assert {item["path"] for item in repo_map["items"]} >= {"billing_service.py", "money.py"}


def test_lite_search_uses_intent_terms_and_local_import_targets(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    auth = root / "auth"
    auth.mkdir()
    (auth / "context.py").write_text(
        "# Proxy permissions into template lookup objects.\n"
        "class PermLookupDict:\n"
        "    def __getitem__(self, permission):\n"
        '        return permission in {"read", "write"}\n',
        encoding="utf-8",
    )
    (auth / "consumer.py").write_text(
        "from auth.context import PermLookupDict\n\ndef template_permissions():\n    return PermLookupDict()\n",
        encoding="utf-8",
    )
    (root / "lookup_helpers.py").write_text(
        "def lookup_permission_name(value):\n    return value\n",
        encoding="utf-8",
    )

    index = LiteIndex(root)
    index.prepare()

    intent = index.search("permission lookup dictionary for templates", limit=8)
    target = next(item for item in intent["files"] if item["path"] == "auth/context.py")
    assert intent["files"].index(target) < 3
    assert "symbol-part" in target["channels"]
    assert "import-target" in target["channels"]

    syntax_heavy = index.search("def PermLookupDict|class PermLookupDict", limit=8)
    assert syntax_heavy["files"][0]["path"] == "auth/context.py"
    assert "symbol" in syntax_heavy["files"][0]["channels"]


def test_lite_search_keeps_a_bounded_ranked_candidate_tail(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    for number in range(20):
        (root / f"module_{number:02d}.py").write_text(
            "def shared_marker():\n    return True\n",
            encoding="utf-8",
        )

    index = LiteIndex(root)
    index.prepare()
    result = index.search("shared_marker", limit=8)

    assert len(result["files"]) == 8
    assert len(result["candidate_files"]) == 12
    assert not ({item["path"] for item in result["files"]} & set(result["candidate_files"]))


def test_lite_search_bridges_test_candidate_to_local_implementation(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "impl.py").write_text("def execute_widget():\n    return 1\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_widget.py").write_text(
        "from impl import execute_widget\n\ndef test_behavior():\n    return execute_widget()\n",
        encoding="utf-8",
    )

    index = LiteIndex(root)
    index.prepare()
    result = index.search("def test_behavior|test_behavior", limit=8)
    implementation = next(item for item in result["files"] if item["path"] == "impl.py")
    assert "test-import-neighbor" in implementation["channels"]


def test_lite_search_distinguishes_exact_identifier_boundaries(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "exact.txt").write_text(
        "struct state { int led_mode; };\n",
        encoding="utf-8",
    )
    (root / "compound.txt").write_text(
        "enum max8997_led_mode { OFF, ON };\n" * 4,
        encoding="utf-8",
    )

    index = LiteIndex(root)
    index.prepare()
    result = index.search("led_mode", limit=8)
    by_path = {item["path"]: item for item in result["files"]}

    assert "identifier-exact" in by_path["exact.txt"]["channels"]
    assert "identifier-exact" not in by_path["compound.txt"]["channels"]


def test_lite_search_uses_bm25_strength_not_only_fts_position(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "strong.txt").write_text(("needle " * 20) + "\n", encoding="utf-8")
    (root / "weak.txt").write_text(
        "needle filler filler filler filler filler filler filler\n",
        encoding="utf-8",
    )

    index = LiteIndex(root)
    index.prepare()
    result = index.search("needle", limit=8)

    assert [item["path"] for item in result["files"][:2]] == ["strong.txt", "weak.txt"]
    assert result["files"][0]["score"] - result["files"][1]["score"] > 5.0


def test_lite_search_does_not_flood_paths_for_short_tokens(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "happy.py").write_text("def unrelated():\n    return True\n", encoding="utf-8")

    index = LiteIndex(root)
    index.prepare()
    assert index.search("py")["files"] == []


def test_lite_natural_language_prior_respects_test_intent(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "config.py").write_text(
        "def parse_configuration(settings):\n    return settings\n",
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_config.py").write_text(
        "# parse configuration settings parse configuration settings\n"
        "def test_parse_configuration():\n"
        "    return True\n",
        encoding="utf-8",
    )

    index = LiteIndex(root)
    index.prepare()
    implementation = index.search("parse configuration settings", limit=8)
    assert implementation["files"][0]["path"] == "config.py"

    requested_test = index.search("test parse configuration settings", limit=8)
    assert requested_test["files"][0]["path"] == "tests/test_config.py"


def test_wal_reader_survives_writer_and_observes_next_generation_after_reopen(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    source = root / "app.py"
    source.write_text("def before():\n    return 1\n", encoding="utf-8")
    index = LiteIndex(root)
    first = index.prepare()

    reader = sqlite3.connect(index.db_path)
    reader.row_factory = sqlite3.Row
    reader.execute("BEGIN")
    assert int(reader.execute("SELECT value FROM meta WHERE key='generation'").fetchone()["value"]) == first.generation

    source.write_text("def after():\n    return 2\n", encoding="utf-8")
    written = LiteIndex(root).prepare()
    assert written.generation > first.generation

    # WAL snapshot semantics: the active reader remains valid on the last
    # committed generation while the writer commits the next one.
    assert int(reader.execute("SELECT value FROM meta WHERE key='generation'").fetchone()["value"]) == first.generation
    reader.commit()
    reader.close()

    assert LiteIndex(root).generation() == written.generation
    assert LiteIndex(root).search("after")["files"][0]["path"] == "app.py"


def test_two_processes_share_one_cold_index_without_duplicate_rebuild(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    for number in range(24):
        (root / f"module_{number}.py").write_text(
            f"def symbol_{number}():\n    return {number}\n",
            encoding="utf-8",
        )

    script = (
        "import json, os\n"
        "from pathlib import Path\n"
        "from lemoncrow_client.lite.index import LiteIndex\n"
        "r=LiteIndex(Path(os.environ['LITE_REPO'])).prepare()\n"
        "print(json.dumps(r.to_dict()))\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(CLIENT_ROOT / "src"),
        "LITE_REPO": str(root),
    }
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(2)
    ]
    reports: list[dict[str, object]] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        reports.append(json.loads(stdout.strip().splitlines()[-1]))

    assert sorted(int(report["files_indexed"]) for report in reports) == [0, 24]
    assert {int(report["generation"]) for report in reports} == {1}


def test_warm_prepare_skips_repository_walk_when_generation_is_already_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _git_repo(tmp_path)
    for number in range(12):
        (root / f"module_{number}.py").write_text(
            f"def warm_symbol_{number}():\n    return {number}\n",
            encoding="utf-8",
        )
    first = LiteIndex(root).prepare()

    def forbidden_walk(_root: Path):
        raise AssertionError("current generation should not walk the repository again")

    monkeypatch.setattr("lemoncrow_client.lite.index._iter_git_paths", forbidden_walk)
    second = LiteIndex(root).prepare()
    assert second.generation == first.generation
    assert second.files_changed == 0
    assert second.files_deleted == 0


def test_hot_index_reads_do_not_repeat_schema_or_wal_initialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def hot_path_symbol():\n    return 1\n", encoding="utf-8")
    index = LiteIndex(root)
    index.prepare()

    def forbidden_schema(_connection: sqlite3.Connection) -> None:
        raise AssertionError("hot read connection attempted schema initialization")

    monkeypatch.setattr(index, "_ensure_schema", forbidden_schema)
    found = index.search("hot_path_symbol")
    assert found["files"][0]["path"] == "app.py"
    assert index.exact_symbols("hot_path_symbol")[0]["path"] == "app.py"


def test_warm_freshness_check_is_singleflight_across_concurrent_readers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lemoncrow_client.lite import index as lite_index_module

    root = _git_repo(tmp_path)
    source = root / "app.py"
    source.write_text("def before_external_edit():\n    return 1\n", encoding="utf-8")
    index = LiteIndex(root, stale_while_refresh=True)
    index.prepare()
    source.write_text("def after_external_edit():\n    return 2\n", encoding="utf-8")
    index._last_refresh_check = 0.0

    real_signature = lite_index_module._worktree_signature
    signature_started = threading.Event()
    release_signature = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocked_signature(repo_root: Path) -> str | None:
        nonlocal calls
        with calls_lock:
            calls += 1
        signature_started.set()
        assert release_signature.wait(timeout=5.0)
        return real_signature(repo_root)

    monkeypatch.setattr(lite_index_module, "_worktree_signature", blocked_signature)
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def search() -> None:
        try:
            results.append(index.search("before_external_edit"))
        except BaseException as exc:  # surface thread failures in the test
            errors.append(exc)

    first_reader = threading.Thread(target=search)
    first_reader.start()
    assert signature_started.wait(timeout=2.0)

    siblings = [threading.Thread(target=search) for _ in range(2)]
    for thread in siblings:
        thread.start()
    for thread in siblings:
        thread.join(timeout=1.0)
        assert not thread.is_alive(), "sibling reader queued behind the freshness check"

    assert calls == 1, "concurrent readers repeated git freshness work"
    release_signature.set()
    first_reader.join(timeout=2.0)
    assert not first_reader.is_alive()
    assert not errors
    assert len(results) == 3


def test_lite_surface_narrows_full_runtime_only_arguments(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def surface_probe():\n    return 1\n", encoding="utf-8")
    server = LiteMcp(environment=_env(root, tmp_path))
    tools = {entry["name"]: entry["inputSchema"] for entry in server.tools}

    assert set(tools["code_search"]["properties"]) == {"query", "paths", "limit"}
    assert "send" not in tools["bash"]["properties"]["action"]["enum"]
    server.close()


def test_lite_read_refuses_paths_outside_the_repository(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def in_repo():\n    return 1\n", encoding="utf-8")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must not be readable through Runtime Lite", encoding="utf-8")

    server = LiteMcp(environment=_env(root, tmp_path))
    server.prepare()
    result = server.call("read", {"files": [str(outside)]})
    assert result["isError"] is True
    assert "outside the worktree" in result["content"][0]["text"]
    server.close()


def test_lite_mcp_ignores_hosted_login_and_never_uses_network_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def local_only_symbol():\n    return 1\n", encoding="utf-8")

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Runtime Lite attempted network socket activity")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    monkeypatch.setattr(socket.socket, "listen", forbidden)

    server = LiteMcp(environment=_env(root, tmp_path))
    initialized = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert initialized is not None
    assert initialized["result"]["serverInfo"]["name"] == SERVER_NAME
    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert listed is not None
    tools = listed["result"]["tools"]
    assert {item["name"] for item in tools} == LITE_VISIBLE_TOOLS
    schemas = {item["name"]: item["inputSchema"] for item in tools}
    for name in ("bash", "edit", "read", "code_search"):
        assert "project_id" not in schemas[name].get("properties", {})
    assert "interactive" not in schemas["bash"]["properties"]
    assert "input" not in schemas["bash"]["properties"]
    assert "send" not in schemas["bash"]["properties"]["action"]["enum"]

    result = server.call("code_search", {"query": "local_only_symbol"})
    assert result["isError"] is False
    assert result["structuredContent"]["files"][0]["path"] == "app.py"

    hosted = server.call("memory", {"action": "search"})
    assert hosted["isError"] is True
    assert "does not expose" in hosted["content"][0]["text"]

    outside = tmp_path / "outside.txt"
    outside.write_text("private host file", encoding="utf-8")
    escaped = server.call("read", {"files": [str(outside)]})
    assert escaped["isError"] is True
    assert "outside the worktree" in escaped["content"][0]["text"]
    server.close()


def test_lite_edit_marks_index_dirty_and_search_is_immediately_fresh(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def old_name():\n    return 1\n", encoding="utf-8")
    server = LiteMcp(environment=_env(root, tmp_path))
    server.prepare()

    edited = server.call(
        "edit",
        {
            "edits": [
                {
                    "path": "app.py:full",
                    "new": "def fresh_name():\n    return 2\n",
                }
            ]
        },
    )
    assert edited["isError"] is False
    found = server.call("code_search", {"query": "fresh_name"})
    assert found["isError"] is False
    assert found["structuredContent"]["files"][0]["path"] == "app.py"
    assert not server.call("code_search", {"query": "old_name"})["structuredContent"]["files"]
    server.close()


def _lite_usage_records(tmp_path: Path) -> list[dict[str, object]]:
    traces = tmp_path / "state" / "runtime" / "traces"
    return [
        json.loads(line)
        for path in sorted(traces.glob("*/client-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_lite_tool_calls_leave_a_local_usage_record_per_session(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def usage_probe():\n    return 1\n", encoding="utf-8")
    server = LiteMcp(environment=_env(root, tmp_path))
    server.prepare()

    searched = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "code_search", "arguments": {"query": "usage_probe"}},
        }
    )
    assert searched is not None and searched["result"]["isError"] is False
    assert "structuredContent" not in searched["result"]
    assert searched["result"]["content"][0]["text"].startswith("## app.py:L")
    edited = server.call("edit", {"edits": [{"path": "app.py:full", "new": "def private_body():\n    return 2\n"}]})
    assert edited["isError"] is False
    refused = server.call("memory", {"action": "search"})
    assert refused["isError"] is True
    server.close()

    records = _lite_usage_records(tmp_path)
    assert [record["tool"] for record in records] == ["code_search", "edit", "memory"]
    assert {record["writer"] for record in records} == {"client"}
    assert {record["execution"]["site"] for record in records} == {"lite"}
    assert {record["mode"] for record in records} == {"raw"}
    sessions = {record["session_id"] for record in records}
    assert len(sessions) == 1 and str(next(iter(sessions))).startswith("lite_")
    assert records[0]["request"]["arguments"] == {"query": "usage_probe"}
    assert records[0]["outcome"]["output_chars"] > 0
    assert [record["outcome"]["is_error"] for record in records] == [False, False, True]
    metrics = records[0]["execution"]["metrics"]
    assert metrics["queue_wait_ms"] >= 0
    assert metrics["query_ms"] >= 0
    assert metrics["sqlite_open_ms"] >= 0
    # Bulk content (edit bodies) is never stored, only its size and digest.
    assert "private_body" not in json.dumps(records[1])


def test_lite_usage_records_honour_trace_off(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def quiet_probe():\n    return 1\n", encoding="utf-8")
    server = LiteMcp(environment={**_env(root, tmp_path), "LEMONCROW_RUNTIME_TRACE": "off"})
    server.prepare()
    assert server.call("code_search", {"query": "quiet_probe"})["isError"] is False
    server.close()

    assert _lite_usage_records(tmp_path) == []


def test_lite_worktree_index_overlays_the_main_index_and_stores_only_divergence(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def parent_symbol():\n    return 1\n", encoding="utf-8")
    (root / "common.py").write_text("def common_symbol():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)

    parent = LiteIndex(root)
    parent_report = parent.prepare()
    assert parent_report.files_changed == 2

    linked = tmp_path / "feature-worktree"
    subprocess.run(["git", "worktree", "add", "-b", "feature", str(linked)], cwd=root, check=True, capture_output=True)
    (linked / "app.py").write_text("def child_symbol():\n    return 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=linked, check=True)
    subprocess.run(["git", "commit", "-m", "child"], cwd=linked, check=True, capture_output=True)

    child = LiteIndex(linked)
    child.prepare()
    with sqlite3.connect(child.db_path) as raw:
        assert raw.execute("SELECT path FROM files").fetchall() == [("app.py",)]
        assert raw.execute("SELECT path FROM inherit").fetchall() == [("common.py",)]
    assert child.search("child_symbol")["files"][0]["path"] == "app.py"
    assert child.search("common_symbol")["files"][0]["path"] == "common.py"
    assert not child.exact_symbols("parent_symbol")
    assert not parent.search("child_symbol")["files"]


def test_lite_mcp_routes_worktree_paths_to_that_worktrees_own_index(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def main_only_symbol():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)

    linked = tmp_path / "routed-worktree"
    subprocess.run(["git", "worktree", "add", "-b", "routed", str(linked)], cwd=root, check=True, capture_output=True)
    (linked / "app.py").write_text("def worktree_only_symbol():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=linked, check=True)
    subprocess.run(["git", "commit", "-m", "worktree"], cwd=linked, check=True, capture_output=True)

    server = LiteMcp(environment=_env(root, tmp_path))
    server.prepare()
    routed = server.call("code_search", {"query": "worktree_only_symbol", "paths": str(linked)})
    assert routed["isError"] is False
    assert routed["structuredContent"]["files"][0]["path"] == "app.py"
    assert routed["content"][0]["text"].startswith("[worktree routed-worktree]\n")
    assert (linked / ".lemoncrow" / "workspace" / "runtime-lite-v3.sqlite3").is_file()
    assert not server.index.search("worktree_only_symbol")["files"]

    # Worktree routing is request-scoped. A second subagent sharing this MCP
    # process must not inherit the first caller's linked checkout.
    pathless = server.call("code_search", {"query": "worktree_only_symbol"})
    assert pathless["isError"] is False
    assert not pathless["structuredContent"]["files"]
    assert not pathless["content"][0]["text"].startswith("[worktree ")

    switched = server.call("code_search", {"query": "main_only_symbol", "paths": str(root)})
    assert switched["isError"] is False
    assert switched["structuredContent"]["files"][0]["path"] == "app.py"
    assert not switched["content"][0]["text"].startswith("[worktree ")
    server.close()


def test_linked_worktree_adoption_does_not_hold_global_children_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow_client.worktrees import LinkedWorktree

    main = _git_repo(tmp_path)
    child_a = tmp_path / "child-a"
    child_b = tmp_path / "child-b"
    for child in (child_a, child_b):
        child.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=child, check=True)

    server = LiteMcp(environment=_env(main, tmp_path))
    original_adopt = LiteIndex.adopt_prepared
    first_started = threading.Event()
    release_first = threading.Event()

    def blocked_adopt(index: LiteIndex):
        if index.repo_root == child_a.resolve():
            first_started.set()
            assert release_first.wait(timeout=5.0)
        return original_adopt(index)

    monkeypatch.setattr(LiteIndex, "adopt_prepared", blocked_adopt)
    first = threading.Thread(target=lambda: server._worktree_workspace(LinkedWorktree(child_a, "child-a")))
    first.start()
    assert first_started.wait(timeout=2.0)

    second_done = threading.Event()

    def open_second() -> None:
        server._worktree_workspace(LinkedWorktree(child_b, "child-b"))
        second_done.set()

    second = threading.Thread(target=open_second)
    second.start()
    assert second_done.wait(timeout=1.0), "one cold linked worktree blocked unrelated routing"

    release_first.set()
    first.join(timeout=3.0)
    second.join(timeout=3.0)
    assert not first.is_alive()
    assert not second.is_alive()
    server.close()


def test_stdio_dispatches_three_subagent_reads_concurrently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def concurrent_probe():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "state"))

    barrier = threading.Barrier(3)
    original_call = LiteMcp.call

    def synchronized_call(
        self: LiteMcp,
        name: str,
        arguments: dict[str, object],
        *,
        queue_wait_ms: float = 0.0,
    ) -> dict[str, object]:
        if name == "read":
            barrier.wait(timeout=3.0)
        return original_call(self, name, arguments, queue_wait_ms=queue_wait_ms)

    monkeypatch.setattr(LiteMcp, "call", synchronized_call)
    requests = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            *[
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": "read", "arguments": {"files": ["app.py:full"]}},
                    }
                )
                for request_id in (2, 3, 4)
            ],
            "",
        ]
    )
    sink = io.StringIO()
    assert serve(source=io.StringIO(requests), sink=sink) == 0
    responses = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert {response["id"] for response in responses} == {1, 2, 3, 4}
    assert all("error" not in response for response in responses)

    read_records = [record for record in _lite_usage_records(tmp_path) if record["tool"] == "read"]
    assert len(read_records) == 3
    assert all(record["execution"]["metrics"]["queue_wait_ms"] >= 0 for record in read_records)


def test_stdio_bounds_index_heavy_subagent_concurrency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text(
        "def alpha_probe():\n    return 1\n\ndef beta_probe():\n    return 2\n\ndef gamma_probe():\n    return 3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "state"))

    original_call = LiteMcp.call
    lock = threading.Lock()
    release = threading.Event()
    two_started = threading.Event()
    started = 0
    active = 0
    max_active = 0

    def bounded_call(
        self: LiteMcp,
        name: str,
        arguments: dict[str, object],
        *,
        queue_wait_ms: float = 0.0,
    ) -> dict[str, object]:
        nonlocal started, active, max_active
        if name == "code_search":
            with lock:
                started += 1
                active += 1
                max_active = max(max_active, active)
                if active == 2:
                    two_started.set()
            try:
                assert release.wait(timeout=5.0)
            finally:
                with lock:
                    active -= 1
        return original_call(self, name, arguments, queue_wait_ms=queue_wait_ms)

    monkeypatch.setattr(LiteMcp, "call", bounded_call)
    requests = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            *[
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": "code_search", "arguments": {"query": query}},
                    }
                )
                for request_id, query in (
                    (2, "alpha_probe"),
                    (3, "beta_probe"),
                    (4, "gamma_probe"),
                )
            ],
            "",
        ]
    )
    sink = io.StringIO()
    runner = threading.Thread(target=lambda: serve(source=io.StringIO(requests), sink=sink))
    runner.start()
    assert two_started.wait(timeout=3.0)
    with lock:
        assert started == 2
        assert max_active == 2
    release.set()
    runner.join(timeout=5.0)
    assert not runner.is_alive()
    with lock:
        assert started == 3
        assert max_active == 2
    responses = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert {response["id"] for response in responses} == {1, 2, 3, 4}


def test_stdio_serial_mutation_waits_for_earlier_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _git_repo(tmp_path)
    source = root / "app.py"
    source.write_text("def before_edit():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "state"))

    read_started = threading.Event()
    release_read = threading.Event()
    original_call = LiteMcp.call

    def blocked_read(
        self: LiteMcp,
        name: str,
        arguments: dict[str, object],
        *,
        queue_wait_ms: float = 0.0,
    ) -> dict[str, object]:
        if name == "read":
            read_started.set()
            assert release_read.wait(timeout=5.0)
        return original_call(self, name, arguments, queue_wait_ms=queue_wait_ms)

    monkeypatch.setattr(LiteMcp, "call", blocked_read)
    requests = "\n".join(
        (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "read", "arguments": {"files": ["app.py:full"]}},
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "edit",
                        "arguments": {"edits": [{"path": "app.py:full", "new": "def after_edit():\n    return 2\n"}]},
                    },
                }
            ),
            "",
        )
    )
    sink = io.StringIO()
    runner = threading.Thread(target=lambda: serve(source=io.StringIO(requests), sink=sink))
    runner.start()
    assert read_started.wait(timeout=2.0)
    assert "before_edit" in source.read_text(encoding="utf-8")
    release_read.set()
    runner.join(timeout=5.0)
    assert not runner.is_alive()
    assert "after_edit" in source.read_text(encoding="utf-8")


def test_stdio_server_exits_cleanly_on_eof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def eof_probe():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("LEMONCROW_HOME", str(tmp_path / "state"))

    requests = "\n".join(
        (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            "",
        )
    )
    sink = io.StringIO()
    assert serve(source=io.StringIO(requests), sink=sink) == 0
    responses = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert responses[0]["result"]["serverInfo"]["name"] == SERVER_NAME
    assert len(responses[1]["result"]["tools"]) == len(LITE_VISIBLE_TOOLS)


def test_stdio_process_exits_when_its_input_owner_closes(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "app.py").write_text("def child_lifecycle():\n    return 1\n", encoding="utf-8")
    LiteIndex(root).prepare()
    env = {
        **os.environ,
        **_env(root, tmp_path),
        "LEMONCROW_LITE_PREPARED": "1",
        "PYTHONPATH": str(CLIENT_ROOT / "src"),
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from lemoncrow_client.lite.mcp import serve; raise SystemExit(serve())",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n")
    process.stdin.flush()
    initialized = json.loads(process.stdout.readline())
    assert initialized["result"]["serverInfo"]["name"] == SERVER_NAME

    process.stdin.close()
    assert process.wait(timeout=10) == 0
    assert process.poll() == 0


def test_lite_imports_when_heavy_and_hosted_modules_are_unavailable() -> None:
    code = """
import builtins
import sys
blocked = (
    'fastapi', 'uvicorn', 'torch', 'sentence_transformers', 'zoekt',
    'lemoncrow_client.mcpserver', 'lemoncrow_client.session',
    'lemoncrow_client.liveview', 'lemoncrow_client.transport',
)
real_import = builtins.__import__
def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and any(name == item or name.startswith(item + '.') for item in blocked):
        raise ModuleNotFoundError(name)
    return real_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded
import lemoncrow_client.lite.mcp
loaded = set(sys.modules)
assert not any(name == item or name.startswith(item + '.') for item in blocked for name in loaded)
print('ok')
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(CLIENT_ROOT / "src")},
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_lite_and_review_artifact_allowlists_are_physically_separate() -> None:
    lite = {
        line.strip()
        for line in (CLIENT_ROOT / "lite-files.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    review = {
        line.strip()
        for line in (CLIENT_ROOT / "review-files.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert "lite/index.py" in lite
    assert "worktrees.py" in lite
    assert not any(path.startswith("lite/") for path in review)
    for forbidden in (
        "mcpserver.py",
        "session.py",
        "liveview.py",
        "autosync.py",
        "blobs.py",
        "dispatcher.py",
        "transport.py",
        "config.py",
    ):
        assert forbidden not in lite
