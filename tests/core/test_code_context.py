from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from atelier.core.capabilities.code_context import CodeContextEngine
from atelier.core.capabilities.code_context.budget import BudgetPacker
from atelier.core.capabilities.code_context.models import SymbolRecord
from atelier.infra.code_intel.astgrep import PatternMatch, PatternSearchResult
from atelier.infra.code_intel.cross_lang.runner import CrossLangRunner


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n"
        "\n"
        "def helper() -> OrderService:\n"
        "    return OrderService()\n",
        encoding="utf-8",
    )
    (root / "src" / "checkout.py").write_text(
        "from src.orders import OrderService\n\n"
        "def checkout(items: list[int]) -> int:\n"
        "    return OrderService().calculate_total(items)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_checkout.py").write_text(
        "from src.checkout import checkout\n\n" "def test_checkout() -> None:\n" "    assert checkout([1, 2]) == 3\n",
        encoding="utf-8",
    )


def _write_semantic_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "auth.py").write_text(
        "def issue_access_token(user_id: str) -> str:\n"
        '    """Create a login session token for an authenticated user."""\n'
        "    session_token = f'session:{user_id}'\n"
        "    return session_token\n"
        "\n"
        "def revoke_access_token(token: str) -> None:\n"
        '    """Invalidate a session token after logout."""\n'
        "    return None\n",
        encoding="utf-8",
    )
    (root / "src" / "audit.py").write_text(
        "def create_login_history_for_authenticated_user(user_id: str) -> dict[str, str]:\n"
        '    """Record login history entries for audit review."""\n'
        "    return {'user_id': user_id}\n",
        encoding="utf-8",
    )


def _write_call_graph_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "app.py").write_text(
        "from src.alpha import alpha\n\n" "def handle() -> int:\n" "    return alpha()\n",
        encoding="utf-8",
    )
    (root / "src" / "alpha.py").write_text(
        "from src.beta import beta\n\n" "def alpha() -> int:\n" "    return beta()\n",
        encoding="utf-8",
    )
    (root / "src" / "beta.py").write_text(
        "from src.gamma import gamma\n\n" "def beta() -> int:\n" "    return gamma()\n",
        encoding="utf-8",
    )
    (root / "src" / "gamma.py").write_text(
        "from src.alpha import alpha\n\n" "def gamma() -> int:\n" "    return alpha()\n",
        encoding="utf-8",
    )


def _write_call_graph_scip_fixture(engine: CodeContextEngine, *, include_call_graph: bool = True) -> None:
    artifact_dir = engine.repo_root / ".atelier" / "cache" / "scip" / engine.repo_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "repo_id": engine.repo_id,
        "language": "python",
        "index_sha": "0000000000000000000000000000000000000000",
        "symbols": [],
    }
    symbol_specs = [
        ("scip-handle", "src/app.py", "handle", "handle"),
        ("scip-alpha", "src/alpha.py", "alpha", "alpha"),
        ("scip-beta", "src/beta.py", "beta", "beta"),
        ("scip-gamma", "src/gamma.py", "gamma", "gamma"),
    ]
    for symbol_id, file_path, symbol_name, qualified_name in symbol_specs:
        source = (engine.repo_root / file_path).read_text(encoding="utf-8")
        payload["symbols"].append(
            {
                "symbol_id": symbol_id,
                "repo_id": engine.repo_id,
                "file_path": file_path,
                "language": "python",
                "symbol_name": symbol_name,
                "qualified_name": qualified_name,
                "kind": "function",
                "signature": f"def {symbol_name}() -> int:",
                "start_byte": source.index(f"def {symbol_name}"),
                "end_byte": len(source.encode("utf-8")),
                "start_line": 3,
                "end_line": 4,
                "content_hash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "source": source,
                "provenance": "scip",
            }
        )
    if include_call_graph:
        payload["call_graph"] = {
            "callers": {
                "scip-alpha": [
                    {
                        "symbol_id": "scip-handle",
                        "symbol_name": "handle",
                        "qualified_name": "handle",
                        "file_path": "src/app.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    },
                    {
                        "symbol_id": "scip-gamma",
                        "symbol_name": "gamma",
                        "qualified_name": "gamma",
                        "file_path": "src/gamma.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    },
                ],
                "scip-beta": [
                    {
                        "symbol_id": "scip-alpha",
                        "symbol_name": "alpha",
                        "qualified_name": "alpha",
                        "file_path": "src/alpha.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    }
                ],
                "scip-gamma": [
                    {
                        "symbol_id": "scip-beta",
                        "symbol_name": "beta",
                        "qualified_name": "beta",
                        "file_path": "src/beta.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    }
                ],
            },
            "callees": {
                "scip-handle": [
                    {
                        "symbol_id": "scip-alpha",
                        "symbol_name": "alpha",
                        "qualified_name": "alpha",
                        "file_path": "src/alpha.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    }
                ],
                "scip-alpha": [
                    {
                        "symbol_id": "scip-beta",
                        "symbol_name": "beta",
                        "qualified_name": "beta",
                        "file_path": "src/beta.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    }
                ],
                "scip-beta": [
                    {
                        "symbol_id": "scip-gamma",
                        "symbol_name": "gamma",
                        "qualified_name": "gamma",
                        "file_path": "src/gamma.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    }
                ],
                "scip-gamma": [
                    {
                        "symbol_id": "scip-alpha",
                        "symbol_name": "alpha",
                        "qualified_name": "alpha",
                        "file_path": "src/alpha.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    }
                ],
            },
        }
    (artifact_dir / "python.scip").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _write_cross_lang_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "plugins" / "__init__.py").write_text("", encoding="utf-8")
    (root / "plugins" / "worker.py").write_text(
        "def plugin_entry() -> str:\n" "    return 'worker'\n",
        encoding="utf-8",
    )
    (root / "scripts" / "worker.py").write_text(
        "def main() -> int:\n" "    return 1\n",
        encoding="utf-8",
    )
    (root / "src" / "local_worker.py").write_text(
        "from scripts.worker import main\n\n" "def call_local() -> int:\n" "    return main()\n",
        encoding="utf-8",
    )
    (root / "src" / "bootstrap.py").write_text(
        "import importlib\n"
        "import subprocess\n\n"
        "def load_plugin() -> object:\n"
        "    return importlib.import_module('plugins.worker')\n\n"
        "def launch_worker() -> None:\n"
        "    subprocess.run(['python', 'scripts/worker.py'], check=False)\n",
        encoding="utf-8",
    )


def _git(args: list[str], repo_root: Path, *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()


def _commit_all(
    repo_root: Path,
    message: str,
    *,
    author_name: str = "Fixture Tester",
    author_email: str = "fixture@example.com",
    author_date: str | None = None,
) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
    )
    if author_date is not None:
        env["GIT_AUTHOR_DATE"] = author_date
        env["GIT_COMMITTER_DATE"] = author_date
    _git(["add", "-A"], repo_root, env=env)
    _git(["commit", "-m", message], repo_root, env=env)
    return _git(["rev-parse", "HEAD"], repo_root, env=env)


def _init_git_fixture_repo(repo_root: Path) -> None:
    repo_root.mkdir()
    _git(["init"], repo_root)
    _git(["config", "user.name", "Fixture Tester"], repo_root)
    _git(["config", "user.email", "fixture@example.com"], repo_root)


def _write_deleted_history_fixture(repo_root: Path) -> str:
    _init_git_fixture_repo(repo_root)
    (repo_root / "legacy.py").write_text(
        "class LegacyCheckout:\n" "    def process(self) -> int:\n" "        return 1\n",
        encoding="utf-8",
    )
    _commit_all(repo_root, "add legacy symbol", author_date="2024-01-01T00:00:00+00:00")
    (repo_root / "legacy.py").unlink()
    return _commit_all(
        repo_root,
        "delete legacy symbol",
        author_email="history@example.com",
        author_date="2025-01-01T00:00:00+00:00",
    )


def _write_rename_history_fixture(repo_root: Path) -> str:
    _init_git_fixture_repo(repo_root)
    (repo_root / "legacy.py").write_text(
        "class LegacyCheckout:\n" "    def process(self) -> int:\n" "        return 1\n",
        encoding="utf-8",
    )
    _commit_all(repo_root, "add legacy symbol", author_date="2024-01-01T00:00:00+00:00")
    _git(["mv", "legacy.py", "modern.py"], repo_root)
    (repo_root / "modern.py").write_text(
        "class ModernCheckout:\n" "    def process(self) -> int:\n" "        return 2\n",
        encoding="utf-8",
    )
    return _commit_all(
        repo_root,
        "rename legacy symbol",
        author_email="renames@example.com",
        author_date="2025-02-01T00:00:00+00:00",
    )


def _write_blame_fixture(repo_root: Path) -> tuple[str, str]:
    _init_git_fixture_repo(repo_root)
    now = datetime.now(tz=UTC)
    service_path = repo_root / "service.py"
    service_path.write_text(
        "def risk_score() -> int:\n" "    value = 1\n" "    return value\n",
        encoding="utf-8",
    )
    _commit_all(
        repo_root,
        "add risk score",
        author_name="Alice",
        author_email="alice@example.com",
        author_date=(now - timedelta(days=240)).isoformat(),
    )
    service_path.write_text(
        "def risk_score() -> int:\n" "    value = 3\n" "    return value\n",
        encoding="utf-8",
    )
    indexed_sha = _commit_all(
        repo_root,
        "tune risk score",
        author_name="Bob",
        author_email="bob@example.com",
        author_date=(now - timedelta(days=30)).isoformat(),
    )
    service_path.write_text(
        "def risk_score() -> int:\n" "    value = 5\n" "    return value\n",
        encoding="utf-8",
    )
    head_sha = _commit_all(
        repo_root,
        "finalize risk score",
        author_name="Carol",
        author_email="carol@example.com",
        author_date=(now - timedelta(days=7)).isoformat(),
    )
    return indexed_sha, head_sha


def _write_scip_fixture_for_symbol(
    repo_root: Path,
    *,
    file_path: str,
    symbol_name: str,
    index_sha: str,
    artifact_name: str = "python.scip",
    qualified_name: str | None = None,
    source: str | None = None,
) -> None:
    engine = CodeContextEngine(repo_root)
    symbol_source = source or (repo_root / file_path).read_text(encoding="utf-8")
    artifact_dir = repo_root / ".atelier" / "cache" / "scip" / engine.repo_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "repo_id": engine.repo_id,
        "language": "python",
        "index_sha": index_sha,
        "symbols": [
            {
                "symbol_id": f"scip-{symbol_name}",
                "repo_id": engine.repo_id,
                "file_path": file_path,
                "language": "python",
                "symbol_name": symbol_name,
                "qualified_name": qualified_name or symbol_name,
                "kind": "function",
                "signature": f"def {symbol_name}() -> int:",
                "start_byte": symbol_source.index(f"def {symbol_name}") if f"def {symbol_name}" in symbol_source else 0,
                "end_byte": len(symbol_source.encode("utf-8")),
                "start_line": 1,
                "end_line": len(symbol_source.splitlines()),
                "content_hash": hashlib.sha256(symbol_source.encode("utf-8")).hexdigest(),
                "source": symbol_source,
                "provenance": "scip",
            }
        ],
    }
    (artifact_dir / artifact_name).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _write_live_temporal_fixture(repo_root: Path) -> None:
    _init_git_fixture_repo(repo_root)
    (repo_root / "archived.py").write_text(
        "def archived_worker() -> int:\n" "    return 1\n",
        encoding="utf-8",
    )
    _commit_all(
        repo_root,
        "add archived worker",
        author_name="Alice",
        author_email="alice@example.com",
        author_date="2025-01-01T00:00:00+00:00",
    )
    (repo_root / "recent.py").write_text(
        "def active_worker() -> int:\n" "    return 2\n",
        encoding="utf-8",
    )
    _commit_all(
        repo_root,
        "add active worker",
        author_name="Bob",
        author_email="bob@example.com",
        author_date="2025-05-01T00:00:00+00:00",
    )


def test_code_context_indexes_searches_and_retrieves_exact_symbol(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    stats = engine.index_repo()
    assert stats.index_version >= 1
    assert stats.files_indexed >= 3
    assert stats.symbols_indexed >= 4
    assert stats.imports_indexed >= 2

    hits = engine.search_symbols("OrderService", limit=5)
    assert hits
    assert hits[0].symbol_name == "OrderService"

    symbol = engine.get_symbol(qualified_name="OrderService", file_path="src/orders.py")
    assert symbol["start_line"] == 1
    assert "class OrderService" in symbol["source"]
    assert "calculate_total" in symbol["source"]


def test_code_context_outline_context_pack_and_impact(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    outline = engine.file_outline(file_path="src/orders.py")
    assert "src/orders.py" in outline["files"]
    assert any(item["qualified_name"] == "OrderService.calculate_total" for item in outline["files"]["src/orders.py"])

    pack = engine.context_pack(
        task="change OrderService calculate_total",
        seed_files=["src/orders.py"],
        budget_tokens=350,
    )
    assert pack.token_count <= pack.budget_tokens
    assert "OrderService" in pack.content
    assert "src/checkout.py" in pack.import_neighbors

    impact = engine.impact("src/orders.py")
    assert "src/checkout.py" in impact.direct_importers
    assert "tests/test_checkout.py" in impact.transitive_importers
    assert impact.risk_level in {"medium", "high", "critical"}


def test_context_pack_caps_symbols_and_filters_import_noise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def a0():\n    return 0\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("def b0():\n    return 0\n", encoding="utf-8")
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    def symbol(file_path: str, name: str, kind: str, line: int) -> SymbolRecord:
        return SymbolRecord(
            symbol_id=f"{file_path}:{name}",
            repo_id=engine.repo_id,
            file_path=file_path,
            language="python",
            symbol_name=name,
            qualified_name=name,
            kind=kind,
            signature=f"{name}()",
            start_byte=0,
            end_byte=10,
            start_line=line,
            end_line=line + 1,
            content_hash=f"hash-{file_path}-{name}",
            score=1.0,
        )

    symbols = [
        symbol("src/a.py", "a_import", "import", 1),
        symbol("src/a.py", "a_export", "export", 2),
        symbol("src/a.py", "a1", "function", 3),
        symbol("src/a.py", "a2", "function", 4),
        symbol("src/a.py", "a3", "function", 5),
        symbol("src/a.py", "a4", "function", 6),
        symbol("src/a.py", "a5", "function", 7),
        symbol("src/b.py", "b1", "function", 1),
        symbol("src/b.py", "b2", "function", 2),
    ]

    monkeypatch.setattr(engine, "repo_map", lambda **kwargs: {"outline": "repo map outline"})
    monkeypatch.setattr(engine, "search_symbols", lambda *args, **kwargs: symbols)
    monkeypatch.setattr(engine, "_symbols_for_files", lambda *args, **kwargs: symbols)
    monkeypatch.setattr(engine, "_import_neighbors", lambda *args, **kwargs: ["src/a.py", "src/b.py", "src/c.py"])
    monkeypatch.setattr(engine, "get_symbol", lambda **kwargs: {"source": "def x():\n    return 1"})

    pack = engine.context_pack(task="compact context", seed_files=["src/a.py"], budget_tokens=5000, max_symbols=20)

    assert len(pack.symbols) == 3
    assert {symbol.kind for symbol in pack.symbols} == {"function"}
    assert sum(1 for symbol in pack.symbols if symbol.file_path == "src/a.py") == 3
    assert sum(1 for item in pack.entry_points if item["file_path"] == "src/a.py") == 4
    assert pack.content.count("### ") >= 3
    assert "## entry_points" in pack.content
    assert "## related_symbols" in pack.content
    assert "## code_blocks" in pack.content
    assert pack.telemetry["token_budget_fit"] is True


def test_context_pack_prioritizes_exact_prefix_and_compound_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text(
        "def issue_access_token() -> str:\n    return 'x'\n"
        "def issue_access_log() -> str:\n    return 'x'\n"
        "def revoke_access_token() -> None:\n    return None\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    def symbol(name: str, line: int) -> SymbolRecord:
        return SymbolRecord(
            symbol_id=f"src/auth.py:{name}",
            repo_id=engine.repo_id,
            file_path="src/auth.py",
            language="python",
            symbol_name=name,
            qualified_name=name,
            kind="function",
            signature=f"{name}()",
            start_byte=0,
            end_byte=10,
            start_line=line,
            end_line=line + 1,
            content_hash=f"h-{name}",
            score=1.0,
        )

    ranked_symbols = [
        symbol("issue_access_log", 10),
        symbol("issue_access_token", 1),
        symbol("revoke_access_token", 20),
    ]
    monkeypatch.setattr(engine, "repo_map", lambda **kwargs: {"outline": "repo map outline"})
    monkeypatch.setattr(engine, "search_symbols", lambda *args, **kwargs: ranked_symbols)
    monkeypatch.setattr(engine, "_symbols_for_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "_import_neighbors", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "get_symbol", lambda **kwargs: {"source": "def x():\n    return 1"})

    pack = engine.context_pack(task="issue access token", seed_files=["src/auth.py"], budget_tokens=5000, max_symbols=10)

    assert pack.entry_points
    assert pack.entry_points[0]["qualified_name"] == "issue_access_token"
    assert pack.code_blocks[0]["qualified_name"] == "issue_access_token"
    assert len(pack.code_blocks) <= 3


def test_code_context_search_text_uses_literal_matches(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    matches = engine.search_text("calculate_total", path="src", limit=10)
    assert {match.file_path for match in matches} == {"src/orders.py", "src/checkout.py"}


def test_retrieval_cache_hit_returns_cached_payload(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    first = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    second = engine.tool_search("OrderService", limit=5, budget_tokens=4000)

    assert first["cache_hit"] is False
    assert first["provenance"] == "local"
    assert second["cache_hit"] is True
    assert second["provenance"] == "cached"
    assert first["items"] == second["items"]
    assert first["tokens_saved"] == second["tokens_saved"]


def test_retrieval_cache_invalidated_on_index_bump(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    _ = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    cached = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    assert cached["cache_hit"] is True

    indexed = engine.tool_index(budget_tokens=4000)
    fresh = engine.tool_search("OrderService", limit=5, budget_tokens=4000)

    assert indexed["index_version"] >= 2
    assert fresh["cache_hit"] is False
    assert fresh["provenance"] == "local"


def test_tool_search_deleted_scope_returns_graveyard_items_with_provenance_and_cache_metadata(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    delete_sha = _write_deleted_history_fixture(repo_root)
    engine = CodeContextEngine(repo_root, db_path=tmp_path / "code.sqlite")

    first = engine.tool_search("LegacyCheckout", scope="deleted", limit=5, budget_tokens=4000)
    second = engine.tool_search("LegacyCheckout", scope="deleted", limit=5, budget_tokens=4000)

    assert first["cache_hit"] is False
    assert first["provenance"] == "graveyard"
    assert first["items"][0]["symbol_name"] == "LegacyCheckout"
    assert first["items"][0]["deleted_at_sha"] == delete_sha
    assert first["items"][0]["last_author"] == "history@example.com"
    assert second["cache_hit"] is True
    assert second["provenance"] == "cached"


def test_tool_search_deleted_scope_is_rename_aware_on_current_public_identity(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    rename_sha = _write_rename_history_fixture(repo_root)
    engine = CodeContextEngine(repo_root, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    payload = engine.tool_search("ModernCheckout", scope="deleted", limit=5, budget_tokens=4000)

    assert payload["items"][0]["symbol_name"] == "LegacyCheckout"
    assert payload["items"][0]["rename_target"] == "modern.py"
    assert payload["items"][0]["deleted_at_sha"] == rename_sha
    assert payload["items"][0]["rename_note"]


def test_tool_search_deleted_scope_applies_temporal_and_touched_by_filters_and_widens_cache_keys(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    _write_deleted_history_fixture(repo_root)
    engine = CodeContextEngine(repo_root, db_path=tmp_path / "code.sqlite")

    filtered = engine.tool_search(
        "LegacyCheckout",
        scope="deleted",
        since="2100-01-01",
        touched_by="history@example.com",
        limit=5,
        budget_tokens=4000,
    )
    unfiltered = engine.tool_search(
        "LegacyCheckout", scope="deleted", touched_by="history@example.com", limit=5, budget_tokens=4000
    )
    additive = engine.tool_search(
        "LegacyCheckout",
        scope="deleted",
        since="2000-01-01",
        touched_by="history@example.com",
        limit=5,
        budget_tokens=4000,
    )

    assert filtered["items"] == []
    assert unfiltered["cache_hit"] is False
    assert unfiltered["items"][0]["last_author"] == "history@example.com"
    assert additive["cache_hit"] is False
    assert additive["items"][0]["last_author"] == "history@example.com"


def test_tool_search_deleted_scope_dispatches_via_git_history_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    from atelier.infra.code_intel.git_history.adapter import DeletedHistorySearchAdapter

    def fake_search(
        self: object, query: str, *, limit: int, since_ts: int | None, touched_by: str | None, language: str | None
    ) -> list[dict[str, object]]:
        _ = (self, query, limit, since_ts, touched_by, language)
        return [
            {
                "symbol_id": "graveyard-test",
                "repo_id": engine.repo_id,
                "file_path": "legacy.py",
                "language": "python",
                "symbol_name": "LegacyCheckout",
                "qualified_name": "LegacyCheckout",
                "kind": "historical",
                "signature": "class LegacyCheckout",
                "start_line": 1,
                "end_line": 1,
                "provenance": "graveyard",
                "deleted_at_sha": "abc123",
            }
        ]

    monkeypatch.setattr(DeletedHistorySearchAdapter, "search", fake_search)

    payload = engine.tool_search("LegacyCheckout", scope="deleted", limit=5, budget_tokens=4000)

    assert payload["items"][0]["deleted_at_sha"] == "abc123"
    assert payload["provenance"] == "graveyard"


def test_tool_blame_returns_index_stale_when_scip_symbol_freshness_lags_head(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    indexed_sha, head_sha = _write_blame_fixture(repo_root)
    _write_scip_fixture_for_symbol(repo_root, file_path="service.py", symbol_name="risk_score", index_sha=indexed_sha)
    engine = CodeContextEngine(repo_root, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_blame(query="risk_score", budget_tokens=4000)

    assert payload["error"] == "index_stale"
    assert payload["hint"] == 'run code op="index" first'
    assert payload["index_sha"] == indexed_sha
    assert payload["head_sha"] == head_sha
    assert payload["freshness"] == "stale"


def test_tool_blame_returns_ownership_metadata_with_optional_churn(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _indexed_sha, head_sha = _write_blame_fixture(repo_root)
    _write_scip_fixture_for_symbol(repo_root, file_path="service.py", symbol_name="risk_score", index_sha=head_sha)
    engine = CodeContextEngine(repo_root, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_blame(query="risk_score", budget_tokens=4000)
    without_churn = engine.tool_blame(query="risk_score", include_churn=False, budget_tokens=4000)

    assert payload["symbol_name"] == "risk_score"
    assert payload["qualified_name"] == "risk_score"
    assert payload["last_author"] == "carol@example.com"
    assert payload["last_commit_sha"] == head_sha
    assert payload["freshness"] == "fresh"
    assert payload["churn"]["commit_count"] == 2
    assert payload["distinct_authors"] == 2
    assert "churn" not in without_churn or without_churn["churn"] is None


def test_tool_search_repo_scope_applies_temporal_filters_after_ranking(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _write_live_temporal_fixture(repo_root)
    engine = CodeContextEngine(repo_root, db_path=tmp_path / "code.sqlite")

    unfiltered = engine.tool_search("worker", scope="repo", limit=5, budget_tokens=4000)
    filtered = engine.tool_search(
        "worker",
        scope="repo",
        since="2025-04-01",
        touched_by="bob@example.com",
        limit=5,
        budget_tokens=4000,
    )

    assert {item["file_path"] for item in unfiltered["items"]} == {"archived.py", "recent.py"}
    assert [item["file_path"] for item in filtered["items"]] == ["recent.py"]


def test_code_context_repo_scope_excludes_external_hits_by_default(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_scip_fixture_for_symbol(
        tmp_path,
        file_path="src/orders.py",
        symbol_name="OrderService",
        qualified_name="OrderService",
        index_sha="a" * 40,
    )
    _write_scip_fixture_for_symbol(
        tmp_path,
        file_path="external/requests/api.py",
        symbol_name="get",
        qualified_name="requests.get",
        index_sha="b" * 40,
        artifact_name="external-python.scip",
        source="def get(url: str) -> str:\n    return url\n",
    )

    repo_hits = engine.search_symbols("get", limit=5)

    assert repo_hits == []


def test_code_context_external_scope_returns_external_hits_and_origin_metadata(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_scip_fixture_for_symbol(
        tmp_path,
        file_path="external/requests/api.py",
        symbol_name="get",
        qualified_name="requests.get",
        index_sha="b" * 40,
        artifact_name="external-python.scip",
        source="def get(url: str) -> str:\n    return url\n",
    )

    external_hits = engine.search_symbols("get", limit=5, scope="external")
    external_symbol = engine.get_symbol(symbol_id="scip-get")

    assert [hit.qualified_name for hit in external_hits] == ["requests.get"]
    assert external_hits[0].origin == "external"
    assert external_symbol["origin"] == "external"


def test_budget_packer_drops_optional_keys_first() -> None:
    packer = BudgetPacker()
    items = [
        {
            "symbol_id": f"sym-{index}",
            "symbol_name": f"Symbol{index}",
            "qualified_name": f"pkg.Symbol{index}",
            "file_path": f"src/mod_{index}.py",
            "kind": "function",
            "signature": f"def symbol_{index}(value: str) -> str",
            "start_line": index + 1,
            "end_line": index + 2,
            "language": "python",
            "provenance": "local",
            "doc_summary": "summary " * 20,
        }
        for index in range(10)
    ]

    packed, _, token_count = packer.pack(
        items,
        200,
        essential_keys=[
            "symbol_id",
            "symbol_name",
            "qualified_name",
            "file_path",
            "kind",
            "signature",
            "start_line",
            "end_line",
            "language",
            "provenance",
        ],
        optional_keys_in_drop_order=["doc_summary"],
    )

    assert token_count > 0
    assert len(packed) >= 3
    assert all("doc_summary" not in item for item in packed[3:])
    for item in packed[:3]:
        assert item["symbol_id"].startswith("sym-")
        assert "signature" in item
        assert "file_path" in item


def test_tool_search_keeps_total_tokens_within_budget(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    lines: list[str] = []
    for index in range(3):
        lines.append(f"def func_{index}() -> int:\n    return {index}\n")
    (tmp_path / "src" / "big.py").write_text("\n".join(lines), encoding="utf-8")

    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_search("func", limit=20, budget_tokens=255)

    assert payload["total_tokens"] <= 255


def test_provenance_local_default(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    search_payload = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    symbol_payload = engine.tool_symbol(qualified_name="OrderService", file_path="src/orders.py", budget_tokens=4000)
    context_payload = engine.tool_context(
        task="change OrderService calculate_total",
        seed_files=["src/orders.py"],
        budget_tokens=350,
    )
    cached_search = engine.tool_search("OrderService", limit=5, budget_tokens=4000)

    assert search_payload["provenance"] == "local"
    assert all(item["provenance"] == "local" for item in search_payload["items"])
    assert symbol_payload["provenance"] == "local"
    assert context_payload["provenance"] == "local"
    assert cached_search["provenance"] == "cached"


def test_tool_usages_groups_local_references_and_reports_treesitter_fallback(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_usages(query="OrderService", budget_tokens=4000)

    assert payload["target"]["qualified_name"] == "OrderService"
    assert payload["group_by"] == "file"
    assert payload["references"]["src/checkout.py"][0]["provenance"] in {"treesitter", "local_index"}
    assert payload["reference_count"] >= 1
    if "provenance_breakdown" in payload:
        assert payload["provenance_breakdown"].get("treesitter", 0) + payload["provenance_breakdown"].get("local_index", 0) >= 1
    assert payload["cache_hit"] is False
    flattened = [item for refs in payload["references"].values() for item in refs]
    assert all("snippet" not in item for item in flattened)


def test_tool_symbol_adds_cross_lang_refs_without_dropping_existing_symbol_fields(tmp_path: Path) -> None:
    _write_cross_lang_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    CrossLangRunner(repo_root=tmp_path, repo_id=engine.repo_id, connection_factory=engine.connection).resolve_all()

    payload = engine.tool_symbol(qualified_name="load_plugin", file_path="src/bootstrap.py", budget_tokens=4000)

    assert payload["qualified_name"] == "load_plugin"
    assert payload["symbol_name"] == "load_plugin"
    assert payload["source"]
    assert payload["cross_lang_refs"][0]["edge_kind"] == "dynamic_import"
    assert payload["cross_lang_refs"][0]["confidence"] >= 0.7


def test_tool_usages_appends_cross_lang_references_and_preserves_local_groups(tmp_path: Path) -> None:
    _write_cross_lang_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    CrossLangRunner(repo_root=tmp_path, repo_id=engine.repo_id, connection_factory=engine.connection).resolve_all()

    payload = engine.tool_usages(symbol_name="main", file_path="scripts/worker.py", budget_tokens=4000)

    assert payload["target"]["qualified_name"] == "main"
    assert payload["references"]["src/local_worker.py"][0]["provenance"] in {"treesitter", "local_index"}
    assert payload["references"]["src/bootstrap.py"][0]["provenance"] == "cross_lang"
    assert payload["references"]["src/bootstrap.py"][0]["edge_kind"] == "subprocess"
    assert payload["references"]["src/bootstrap.py"][0]["confidence"] >= 0.7
    assert payload["provenance_breakdown"].get("treesitter", 0) + payload["provenance_breakdown"].get("local_index", 0) >= 1
    assert payload["provenance_breakdown"]["cross_lang"] >= 1


def test_tool_usages_aggregates_results_for_ambiguous_name(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def helper() -> int:\n    return 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("def helper() -> int:\n    return 2\n", encoding="utf-8")
    (tmp_path / "src" / "use_a.py").write_text("from src.a import helper\n\nvalue = helper()\n", encoding="utf-8")
    (tmp_path / "src" / "use_b.py").write_text("from src.b import helper\n\nvalue = helper()\n", encoding="utf-8")
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_usages(query="helper", budget_tokens=4000)

    assert "error" not in payload
    assert payload["reference_count"] >= 2
    assert payload["ambiguity"]["merged_target_count"] == 2
    assert {item["file_path"] for item in payload["ambiguity"]["matches"]} == {"src/a.py", "src/b.py"}
    assert payload["cache_hit"] is False


def test_tool_callers_and_callees_aggregate_results_for_ambiguous_name(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "def helper() -> int:\n"
        "    return 1\n\n"
        "def run() -> int:\n"
        "    return helper()\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "b.py").write_text(
        "def helper() -> int:\n"
        "    return 2\n\n"
        "def run() -> int:\n"
        "    return helper()\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    callers = engine.tool_callers(query="helper", budget_tokens=4000)
    callees = engine.tool_callees(query="run", budget_tokens=4000)

    assert "error" not in callers
    assert callers["ambiguity"]["merged_target_count"] == 2
    assert {item["symbol_name"] for item in callers["related"]} == {"run"}
    assert {item["file_path"] for item in callers["related"]} == {"src/a.py", "src/b.py"}

    assert "error" not in callees
    assert callees["ambiguity"]["merged_target_count"] == 2
    assert {item["symbol_name"] for item in callees["related"]} == {"helper"}
    assert callees["edge_count"] >= 1


def test_tool_callees_resolves_indexed_targets_for_ambiguous_callee_name(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a_helpers.py").write_text("def helper() -> int:\n    return 1\n", encoding="utf-8")
    (tmp_path / "src" / "b_helpers.py").write_text("def helper() -> int:\n    return 2\n", encoding="utf-8")
    (tmp_path / "src" / "a.py").write_text(
        "from src.a_helpers import helper\n\n"
        "def run() -> int:\n"
        "    return helper()\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "b.py").write_text(
        "from src.b_helpers import helper\n\n"
        "def run() -> int:\n"
        "    return helper()\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_callees(query="run", budget_tokens=4000)

    assert "error" not in payload
    assert payload["ambiguity"]["merged_target_count"] == 2
    assert {item["symbol_name"] for item in payload["related"]} == {"helper"}
    assert {item["file_path"] for item in payload["related"]} == {"src/a_helpers.py", "src/b_helpers.py"}
    assert payload["edge_count"] >= 2


def test_tool_callers_and_callees_traverse_depth_and_handle_cycles(tmp_path: Path) -> None:
    _write_call_graph_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_call_graph_scip_fixture(engine)

    callers = engine.tool_callers(query="beta", depth=2, budget_tokens=4000)
    callees = engine.tool_callees(query="handle", depth=2, budget_tokens=4000)

    assert callers["target"]["qualified_name"] == "beta"
    assert callers["depth"] == 2
    assert callers["data_status"] == "available"
    assert {item["qualified_name"] for item in callers["related"]} == {"alpha", "gamma", "handle"}
    assert callers["edge_count"] == 3
    assert callers["provenance"] == "scip"
    assert callees["target"]["qualified_name"] == "handle"
    assert {item["qualified_name"] for item in callees["related"]} == {"alpha", "beta"}
    assert all(edge["depth"] in {1, 2} for edge in callees["edges"])


def test_tool_callers_falls_back_to_reference_graph_when_call_graph_data_is_missing(tmp_path: Path) -> None:
    _write_call_graph_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_call_graph_scip_fixture(engine, include_call_graph=False)

    payload = engine.tool_callers(query="alpha", budget_tokens=4000)

    assert payload["target"]["qualified_name"] == "alpha"
    assert payload["data_status"] == "available"
    assert payload["edge_count"] >= 1
    assert payload["related_count"] >= 1
    assert "fallback" in str(payload.get("message", "")).lower()
    assert payload["provenance"] == "scip"


def test_tool_callees_snapshot_is_opt_in_and_returns_metadata(tmp_path: Path) -> None:
    _write_call_graph_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_call_graph_scip_fixture(engine)

    default_payload = engine.tool_callees(query="handle", budget_tokens=4000)
    snapshot_payload = engine.tool_callees(query="handle", snapshot=True, budget_tokens=4000)

    assert default_payload["snapshot"] is None
    assert snapshot_payload["snapshot"]["direction"] == "callees"
    assert snapshot_payload["snapshot"]["target_symbol_id"] == "scip-handle"
    assert snapshot_payload["snapshot"]["edge_count"] == snapshot_payload["edge_count"]


def test_tool_search_snippet_none_omits_snippets_and_keeps_exact_match_first(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n"
        "\n"
        "class OrderServiceFactory:\n"
        "    def build(self) -> OrderService:\n"
        "        return OrderService()\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_search("OrderService", limit=5, snippet="none", budget_tokens=4000)

    assert payload["items"][0]["symbol_name"] == "OrderService"
    assert all("snippet" not in item for item in payload["items"])
    assert all("content_hash" not in item for item in payload["items"])


def test_tool_search_deduplicates_items_before_rendering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    symbol = SymbolRecord(
        symbol_id="sym-1",
        repo_id=engine.repo_id,
        file_path="src/orders.py",
        language="python",
        symbol_name="OrderService",
        qualified_name="OrderService",
        kind="class",
        signature="class OrderService",
        start_byte=0,
        end_byte=50,
        start_line=1,
        end_line=3,
        content_hash="h1",
    )
    monkeypatch.setattr(engine, "search_symbols", lambda *args, **kwargs: [symbol, symbol])  # type: ignore[assignment]

    payload = engine.tool_search("OrderService", snippet="none", budget_tokens=4000)

    assert len(payload["items"]) == 1


def test_semantic_and_hybrid_modes_rank_intent_query_above_lexical(tmp_path: Path) -> None:
    _write_semantic_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    query = "create login token for authenticated user"

    lexical_hits = engine.search_symbols(query, limit=5, mode="lexical")
    semantic_hits = engine.search_symbols(query, limit=5, mode="semantic")
    hybrid_hits = engine.search_symbols(query, limit=5, mode="hybrid")

    assert lexical_hits
    assert semantic_hits
    assert hybrid_hits
    assert lexical_hits[0].symbol_name == "create_login_history_for_authenticated_user"
    assert semantic_hits[0].symbol_name == "issue_access_token"
    assert hybrid_hits[0].symbol_name == "issue_access_token"


def test_auto_mode_keeps_identifier_queries_on_exact_lexical_order(tmp_path: Path) -> None:
    _write_semantic_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    hits = engine.search_symbols("issue_access_token", limit=5, mode="auto")
    payload = engine.tool_search("issue_access_token", limit=5, mode="auto", budget_tokens=4000)

    assert hits
    assert hits[0].symbol_name == "issue_access_token"
    assert payload["items"][0]["symbol_name"] == "issue_access_token"
    assert payload["mode"] == "lexical"


def test_search_symbols_lexical_planner_prioritizes_exact_and_case_insensitive_matches(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    ci_hits = engine.search_symbols("orderservice", limit=5, mode="lexical")
    qualified_hits = engine.search_symbols("OrderService.calculate_total", limit=5, mode="lexical")

    assert ci_hits
    assert ci_hits[0].symbol_name == "OrderService"
    assert qualified_hits
    assert qualified_hits[0].qualified_name == "OrderService.calculate_total"


def test_search_symbols_lexical_planner_applies_camel_and_test_demotion(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "order_service_factory.py").write_text(
        "class OrderServiceFactory:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_order_service_factory.py").write_text(
        "class OrderServiceFactory:\n"
        "    pass\n\n"
        "def test_order_service_factory() -> None:\n"
        "    assert True\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    camel_first = engine.search_symbols("OSF", limit=5, mode="lexical")
    camel_second = engine.search_symbols("OSF", limit=5, mode="lexical")
    test_query_hits = engine.search_symbols("test_order_service_factory", limit=5, mode="lexical")

    assert camel_first
    assert camel_first[0].file_path == "src/order_service_factory.py"
    assert [hit.symbol_id for hit in camel_first] == [hit.symbol_id for hit in camel_second]
    assert test_query_hits
    assert test_query_hits[0].file_path == "tests/test_order_service_factory.py"


def test_search_symbols_lexical_planner_uses_fuzzy_fallback_only_when_needed(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    fuzzy_hits = engine.search_symbols("OrdreServce", limit=5, mode="lexical")
    exact_hits = engine.search_symbols("OrderService", limit=5, mode="lexical")

    assert fuzzy_hits
    assert fuzzy_hits[0].symbol_name == "OrderService"
    assert exact_hits
    assert exact_hits[0].symbol_name == "OrderService"


def test_tool_search_cache_keys_are_mode_aware(tmp_path: Path) -> None:
    _write_semantic_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    query = "create login token for authenticated user"

    lexical_first = engine.tool_search(query, limit=5, mode="lexical", budget_tokens=4000)
    semantic_first = engine.tool_search(query, limit=5, mode="semantic", budget_tokens=4000)
    lexical_second = engine.tool_search(query, limit=5, mode="lexical", budget_tokens=4000)
    semantic_second = engine.tool_search(query, limit=5, mode="semantic", budget_tokens=4000)

    assert lexical_first["cache_hit"] is False
    assert semantic_first["cache_hit"] is False
    assert lexical_second["cache_hit"] is True
    assert semantic_second["cache_hit"] is True
    assert lexical_first["mode"] == "lexical"
    assert semantic_first["mode"] == "semantic"
    assert lexical_first["items"][0]["symbol_name"] != semantic_first["items"][0]["symbol_name"]


def test_retrieval_cache_diagnostics_hide_payloads_and_invalidate_one_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    engine.tool_symbol(qualified_name="OrderService", file_path="src/orders.py", budget_tokens=4000)

    status = engine.tool_cache_status(budget_tokens=4000)

    assert status["entry_count"] == 2
    assert status["entries_by_tool"] == {"code.search": 1, "code.symbol": 1}
    assert status["repo_id"] == engine.repo_id
    assert "payload_json" not in str(status)
    assert "items" not in status
    assert "matches" not in status
    assert "frozen_drop_stages" not in str(status)

    invalidated = engine.tool_cache_invalidate(cache_tool="search", budget_tokens=4000)

    assert invalidated["invalidated_entries"] == 1
    assert invalidated["entries_by_tool"] == {"code.search": 1}
    assert invalidated["scope"]["cache_tool"] == "search"

    status_after = engine.tool_cache_status(budget_tokens=4000)
    assert status_after["entry_count"] == 1
    assert status_after["entries_by_tool"] == {"code.symbol": 1}

    fresh_search = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    cached_symbol = engine.tool_symbol(qualified_name="OrderService", file_path="src/orders.py", budget_tokens=4000)
    assert fresh_search["cache_hit"] is False
    assert cached_symbol["cache_hit"] is True


def test_tool_index_returns_compact_summary_fields(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_index(budget_tokens=4000)

    assert payload["repo_id"] == engine.repo_id
    assert payload["index_version"] >= 1
    assert payload["files_indexed"] >= 1
    assert payload["symbols_indexed"] >= 1
    assert payload["imports_indexed"] >= 0
    assert "repo_root" not in payload
    assert "db_path" not in payload


def test_tool_files_supports_tree_flat_grouped_filters(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    flat = engine.tool_files(
        path="src",
        pattern="src/*.py",
        format="flat",
        include_metadata=True,
        budget_tokens=4000,
    )
    grouped = engine.tool_files(path="src", format="grouped", max_depth=0, budget_tokens=4000)
    grouped_no_metadata = engine.tool_files(
        path="src",
        format="grouped",
        include_metadata=False,
        max_depth=0,
        budget_tokens=4000,
    )
    tree = engine.tool_files(path="src", format="tree", include_metadata=False, budget_tokens=4000)

    assert flat["path"] == "src"
    assert flat["pattern"] == "src/*.py"
    assert flat["format"] == "flat"
    assert flat["file_count"] == 3
    assert flat["truncated"] is False
    assert isinstance(flat["files"], list)
    assert flat["files"][0]["file_path"].startswith("src/")
    assert "language" in flat["files"][0]
    assert "symbol_count" in flat["files"][0]
    assert "top_symbols" in flat["files"][0]

    assert grouped["format"] == "grouped"
    assert "python" in grouped["files"]
    assert len(grouped["files"]["python"]) == 3
    assert "python" in grouped_no_metadata["files"]
    assert "language" not in grouped_no_metadata["files"]["python"][0]

    assert tree["format"] == "tree"
    assert "src" in tree["files"]
    assert tree["files"]["src"]["orders.py"] == {}


def test_tool_files_respects_budget_and_sets_truncated(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    for index in range(40):
        (src / f"mod_{index}.py").write_text(
            f"def fn_{index}() -> int:\n    return {index}\n",
            encoding="utf-8",
        )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_files(format="flat", include_metadata=True, budget_tokens=360)

    assert payload["total_tokens"] <= 360
    assert payload["truncated"] is True
    assert payload["file_count"] < 40
    assert payload["files"]


def test_tool_explore_returns_grouped_sources_and_entry_points(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_explore(
        "OrderService",
        max_files=4,
        max_symbols=12,
        include_source=True,
        include_relationships=True,
        line_numbers=True,
        budget_tokens=6000,
    )

    assert payload["query"] == "OrderService"
    assert payload["entry_points"]
    assert payload["files"]
    assert payload["files"][0]["file_path"].startswith("src/")
    if payload["files"][0].get("source_sections"):
        first_content = payload["files"][0]["source_sections"][0]["content"]
        assert "\t" in first_content
    assert payload["relationships"]["callers"] is not None
    assert payload["relationships"]["callees"] is not None
    assert payload["relationships"]["usages"] is not None


def test_tool_routes_extracts_framework_endpoints(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "api.py").write_text(
        "from fastapi import FastAPI, APIRouter\n\n"
        "app = FastAPI()\n"
        "router = APIRouter()\n\n"
        "@app.get('/health')\n"
        "def health() -> dict[str, bool]:\n"
        "    return {'ok': True}\n\n"
        "@router.post('/orders')\n"
        "def create_order() -> dict[str, str]:\n"
        "    return {'status': 'created'}\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "urls.py").write_text(
        "from django.urls import path\n"
        "from django.conf.urls import url\n"
        "from . import views\n\n"
        "urlpatterns = [\n"
        "    path('admin/', views.admin),\n"
        "    url(r'^legacy/$', views.legacy),\n"
        "]\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "flask_app.py").write_text(
        "from flask import Flask\n\n"
        "app = Flask(__name__)\n\n"
        "def healthz() -> str:\n"
        "    return 'ok'\n\n"
        "app.add_url_rule('/healthz', view_func=healthz, methods=['GET'])\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "server.ts").write_text(
        "import express from 'express';\n"
        "const app = express();\n"
        "const router = express.Router();\n"
        "function pingHandler() { return 'pong'; }\n"
        "app.get('/ping', pingHandler);\n"
        "function listOrders() { return []; }\n"
        "function createOrder() { return {}; }\n"
        "router.route('/orders').get(listOrders).post(createOrder);\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_routes(limit=20, budget_tokens=4000)

    assert payload["route_count"] >= 6
    routes = payload["routes"]
    assert any(route["framework"] == "fastapi" and route["method"] == "GET" and route["route"] == "/health" for route in routes)
    assert any(route["framework"] == "express" and route["route"] == "/ping" for route in routes)
    assert any(route["framework"] == "django" and route["route"] == "admin/" for route in routes)
    assert any(route["framework"] == "django" and route["route"] == "^legacy/$" for route in routes)
    assert any(route["framework"] == "flask" and route["route"] == "/healthz" for route in routes)
    assert any(route["framework"] == "express" and route["method"] == "POST" and route["route"] == "/orders" for route in routes)


def test_tool_explore_respects_budget_and_keeps_identity_fields(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_explore(
        "OrderService",
        include_source=True,
        include_relationships=True,
        budget_tokens=320,
    )

    assert payload["total_tokens"] <= 320
    assert "entry_points" in payload
    if payload["entry_points"]:
        assert "symbol_id" in payload["entry_points"][0]
        assert "symbol_name" in payload["entry_points"][0]
        assert "file_path" in payload["entry_points"][0]


def test_tool_status_reports_index_cache_and_freshness(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_status(budget_tokens=4000)
    cached = engine.tool_status(budget_tokens=4000)

    assert payload["repo_id"] == engine.repo_id
    assert payload["repo_root"] == str(tmp_path.resolve())
    assert payload["db_path"] == str((tmp_path / "code.sqlite").resolve())
    assert payload["index_version"] >= 1
    assert payload["index"]["files_indexed"] >= 1
    assert payload["index"]["symbols_indexed"] >= 1
    assert payload["freshness"]["status"] in {"fresh", "stale", "empty"}
    assert "providers" in payload
    assert payload["provider_freshness"]["thresholds"]["required_health_status"] == "ok"
    assert "summary" in payload["provider_freshness"]
    assert isinstance(payload["warnings"], list)
    assert payload["autosync"]["enabled"] is True
    assert payload["autosync"]["state"] in {"idle", "syncing", "debouncing"}
    assert payload["autosync"]["mode"] == "incremental"
    assert payload["autosync"]["reindex_count"] == 0
    assert isinstance(payload["autosync"]["history"], list)
    assert "entry_count" in payload["cache"]
    assert cached["cache_hit"] is True


def test_autosync_incremental_reindex_updates_index_after_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_fixture_repo(tmp_path)
    monkeypatch.setenv("ATELIER_CODE_AUTOSYNC", "1")
    monkeypatch.setenv("ATELIER_CODE_AUTOSYNC_DEBOUNCE_MS", "50")
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    first = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    version_before = engine._current_index_version()
    assert first["items"]

    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n"
        "\n"
        "class NewService:\n"
        "    pass\n",
        encoding="utf-8",
    )
    engine._autosync_last_sync_ms = int(time.time() * 1000) - 500

    second = engine.tool_search("NewService", limit=5, budget_tokens=4000)
    status = engine.tool_status(budget_tokens=4000)

    assert second["items"]
    assert second["items"][0]["symbol_name"] == "NewService"
    assert engine._current_index_version() > version_before
    assert status["autosync"]["enabled"] is True
    assert status["autosync"]["mode"] == "incremental"
    assert status["autosync"]["reindex_count"] >= 1
    assert any(event["event"] == "reindex" for event in status["autosync"]["history"])


def test_autosync_worker_reindexes_without_search_trigger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_fixture_repo(tmp_path)
    monkeypatch.setenv("ATELIER_CODE_AUTOSYNC_DEBOUNCE_MS", "50")
    monkeypatch.setenv("ATELIER_CODE_AUTOSYNC_POLL_MS", "1000")
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    for _ in range(40):
        if engine._current_index_version() > 0:
            break
        time.sleep(0.05)
    version_before = engine._current_index_version()
    assert version_before > 0

    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n"
        "\n"
        "class BackgroundSyncedService:\n"
        "    pass\n",
        encoding="utf-8",
    )

    found: list[SymbolRecord] = []
    for _ in range(160):
        hits = engine.search_symbols("BackgroundSyncedService", limit=5, auto_index=False)
        if hits:
            found = hits
            break
        time.sleep(0.05)

    assert found
    assert found[0].symbol_name == "BackgroundSyncedService"
    assert engine._current_index_version() > version_before


def test_incremental_index_noop_does_not_bump_version(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    version_before = engine._current_index_version()

    stats = engine.index_repo(force=False)

    assert stats.files_indexed == 0
    assert stats.symbols_indexed == 0
    assert stats.imports_indexed == 0
    assert engine._current_index_version() == version_before


def test_incremental_index_updates_changed_and_removed_files(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    version_before = engine._current_index_version()

    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n"
        "\n"
        "class IncrementalService:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "checkout.py").unlink()

    stats = engine.index_repo(force=False)
    hits = engine.search_symbols("IncrementalService", limit=5, auto_index=False)

    assert stats.files_indexed >= 1
    assert hits
    assert engine._current_index_version() > version_before
    with engine._connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM files WHERE repo_id = ? AND file_path = ?",
            (engine.repo_id, "src/checkout.py"),
        ).fetchone()
    assert row is not None
    assert int(row["n"]) == 0


def test_low_token_defaults_stay_lighter_for_search_and_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n"
        "\n"
        "class OrderServiceFactory:\n"
        "    def build(self) -> OrderService:\n"
        "        return OrderService()\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "http.py").write_text(
        "\n".join(f"def fetch_{index}(url: str) -> object:\n    return requests.get(url)\n" for index in range(30)),
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    search_default = engine.tool_search("OrderService", limit=5, snippet="none", budget_tokens=4000)
    search_heavy = engine.tool_search("OrderService", limit=5, snippet="full", budget_tokens=4000)

    assert search_default["total_tokens"] < search_heavy["total_tokens"]

    tight = engine.tool_search("fetch_", limit=20, snippet="full", budget_tokens=320)
    assert tight["total_tokens"] <= 320
    assert tight["items"]
    for key in ("symbol_id", "symbol_name", "file_path", "start_line", "signature"):
        assert key in tight["items"][0]

    monkeypatch.setattr(
        "atelier.core.capabilities.code_context.engine.AstGrepAdapter.search",
        lambda self, *, pattern, language=None, file_glob=None, limit=20: PatternSearchResult(
            matches=[
                PatternMatch(
                    file_path="src/http.py",
                    line=index + 1,
                    column=4,
                    end_line=index + 1,
                    end_column=28,
                    snippet="return requests.get(url)",
                    captures={"URL": "url"},
                )
                for index in range(30)
            ],
            truncated=limit < 30,
            total_matches=30,
        ),
    )

    pattern_default = engine.tool_pattern(pattern="requests.get($URL)", budget_tokens=4000)
    pattern_heavy = engine.tool_pattern(pattern="requests.get($URL)", limit=100, budget_tokens=4000)

    assert pattern_default["total_tokens"] < pattern_heavy["total_tokens"]


def test_tiny_budget_overflow_does_not_attach_spill_metadata(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n"
        "\n"
        "class OrderServiceFactory:\n"
        "    def build(self) -> OrderService:\n"
        "        return OrderService()\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    full_payload = engine.tool_search("OrderService", limit=5, snippet="full", budget_tokens=4000)
    near_budget = max(1, int(full_payload["total_tokens"]) - 1)
    near_payload = engine.tool_search("OrderService", limit=5, snippet="full", budget_tokens=near_budget)

    assert near_payload["total_tokens"] <= near_budget
    assert "overflow" not in near_payload


def test_overflow_metadata_and_artifact_payload_are_compact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "atelier.core.capabilities.code_context.engine.default_store_root",
        lambda: tmp_path / ".atelier-store",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "orders.py").write_text(
        "\n".join(
            f"def fetch_{index}(url: str) -> object:\n"
            "    payload = {'url': url, 'index': %d}\n"
            "    return payload\n" % index
            for index in range(120)
        ),
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    full_payload = engine.tool_search("fetch_", limit=80, snippet="full", budget_tokens=12000)
    full_total = int(full_payload["total_tokens"])
    tight_payload: dict[str, object] | None = None
    for tight_budget in range(1000, max(1001, full_total), 200):
        candidate = engine.tool_search("fetch_", limit=80, snippet="full", budget_tokens=tight_budget)
        if isinstance(candidate.get("overflow"), dict):
            tight_payload = candidate
            break
    assert tight_payload is not None, "expected at least one budget to trigger overflow spill metadata"

    overflow = tight_payload.get("overflow")
    assert isinstance(overflow, dict)
    assert overflow == {
        "spilled": True,
        "artifact_path": str(overflow["artifact_path"]),
        "artifact_format": "json",
    }

    artifact_path = Path(str(overflow["artifact_path"]))
    assert artifact_path.exists()
    artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert "tokens_saved" not in artifact_payload
    assert "total_tokens" not in artifact_payload
    assert "cache_hit" not in artifact_payload
    assert "overflow" not in artifact_payload
