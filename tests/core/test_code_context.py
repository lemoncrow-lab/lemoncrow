from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lemoncrow.core.capabilities.code_context import CodeContextEngine
from lemoncrow.core.capabilities.code_context.budget import BudgetPacker
from lemoncrow.core.capabilities.code_context.models import SymbolRecord, TextMatch
from lemoncrow.core.capabilities.code_context.output_policy import TRUNCATION_MARKER
from lemoncrow.infra.code_intel.astgrep import PatternMatch, PatternSearchResult
from lemoncrow.infra.code_intel.cross_lang.runner import CrossLangRunner


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
        "from src.checkout import checkout\n\ndef test_checkout() -> None:\n    assert checkout([1, 2]) == 3\n",
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
        "from src.alpha import alpha\n\ndef handle() -> int:\n    return alpha()\n",
        encoding="utf-8",
    )
    (root / "src" / "alpha.py").write_text(
        "from src.beta import beta\n\ndef alpha() -> int:\n    return beta()\n",
        encoding="utf-8",
    )
    (root / "src" / "beta.py").write_text(
        "from src.gamma import gamma\n\ndef beta() -> int:\n    return gamma()\n",
        encoding="utf-8",
    )
    (root / "src" / "gamma.py").write_text(
        "from src.alpha import alpha\n\ndef gamma() -> int:\n    return alpha()\n",
        encoding="utf-8",
    )


def _write_substring_search_fixture_repo(root: Path) -> None:
    noise_dir = root / "src" / "lemoncrow" / "core" / "capabilities"
    noise_dir.mkdir(parents=True, exist_ok=True)
    (noise_dir / "a_noise.py").write_text(
        "\n".join(
            [f'AGGREGATE_{index} = "aggregate"' for index in range(320)]
            + [f'APPLIES_{index} = "applies"' for index in range(320)]
        )
        + "\n",
        encoding="utf-8",
    )
    (noise_dir / "plugin_runtime.py").write_text(
        "def aggregate_session_stats(root: str, session_id: str | None = None) -> dict[str, int]:\n"
        '    return {"sessions": 1}\n',
        encoding="utf-8",
    )
    lesson_dir = noise_dir / "lesson_promotion"
    lesson_dir.mkdir(parents=True, exist_ok=True)
    (lesson_dir / "models.py").write_text(
        "class TypedLesson:\n    def applies_without_tiebreaker_at(self) -> bool:\n        return True\n",
        encoding="utf-8",
    )
    context_reuse_dir = noise_dir / "context_reuse"
    context_reuse_dir.mkdir(parents=True, exist_ok=True)
    (context_reuse_dir / "capability.py").write_text(
        "class _AdaptivePriorTracker:\n    pass\n",
        encoding="utf-8",
    )


def _write_embedded_identifier_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "clone_entity.py").write_text(
        "def _finalEncrypted(entity):\n"
        "    return entity.get('encrypted')\n"
        "\n"
        "def strip_technical_fields(entity):\n"
        "    entity.pop('_finalEncrypted', None)\n"
        "    return entity\n",
        encoding="utf-8",
    )


def _write_kebab_literal_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "other.py").write_text(
        "def compute_widget_totals(widgets):\n    return sum(widgets)\n",
        encoding="utf-8",
    )
    # The literal lives in prose, not in any function/const/variable
    # declaration -- no symbol wraps it, so it can ONLY be found via raw line
    # content (search_text), never via the symbols table. (This engine's
    # symbol search already folds a declared symbol's own signature into its
    # ranking, so a literal embedded in an actual `const`/`var` value is still
    # found by ordinary search_symbols -- verified empirically while building
    # this fixture. The gap this test proves is the literal existing purely as
    # unindexed text content, e.g. docs/config/comments.)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "grpc-headers.txt").write_text(
        "The client sends x-flipt-accept-server-version on every outbound "
        "request so the server can negotiate the response schema.\n",
        encoding="utf-8",
    )


def _write_cross_lang_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "plugins" / "__init__.py").write_text("", encoding="utf-8")
    (root / "plugins" / "worker.py").write_text(
        "def plugin_entry() -> str:\n    return 'worker'\n",
        encoding="utf-8",
    )
    (root / "scripts" / "worker.py").write_text(
        "def main() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (root / "src" / "local_worker.py").write_text(
        "from scripts.worker import main\n\ndef call_local() -> int:\n    return main()\n",
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
        "class LegacyCheckout:\n    def process(self) -> int:\n        return 1\n",
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
        "class LegacyCheckout:\n    def process(self) -> int:\n        return 1\n",
        encoding="utf-8",
    )
    _commit_all(repo_root, "add legacy symbol", author_date="2024-01-01T00:00:00+00:00")
    _git(["mv", "legacy.py", "modern.py"], repo_root)
    (repo_root / "modern.py").write_text(
        "class ModernCheckout:\n    def process(self) -> int:\n        return 2\n",
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
        "def risk_score() -> int:\n    value = 1\n    return value\n",
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
        "def risk_score() -> int:\n    value = 3\n    return value\n",
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
        "def risk_score() -> int:\n    value = 5\n    return value\n",
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


def _write_live_temporal_fixture(repo_root: Path) -> None:
    _init_git_fixture_repo(repo_root)
    (repo_root / "archived.py").write_text(
        "def archived_worker() -> int:\n    return 1\n",
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
        "def active_worker() -> int:\n    return 2\n",
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


def test_incremental_index_forces_rebuild_when_indexer_semantics_version_changes(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    db_path = tmp_path / "code.sqlite"
    engine = CodeContextEngine(tmp_path, db_path=db_path, autosync_enabled=False)
    engine.index_repo(force=True)

    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM engine_state WHERE key = 'indexer_semantics_version'")

    stats = engine.index_repo(force=False)

    assert stats.files_indexed >= 3
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT value FROM engine_state WHERE key = 'indexer_semantics_version'").fetchone()
    assert row is not None
    assert int(row[0]) == 2


def test_code_context_outline_and_context_pack(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    outline = engine.file_outline(file_path="src/orders.py")
    assert "src/orders.py" in outline["files"]
    # qualified_name is dropped when it equals name; the method's differs, so it
    # is retained. Use .get() because sibling module/class entries omit the key.
    assert any(
        item.get("qualified_name") == "OrderService.calculate_total" for item in outline["files"]["src/orders.py"]
    )

    pack = engine.context_pack(
        task="change OrderService calculate_total",
        seed_files=["src/orders.py"],
        budget_tokens=350,
    )
    assert pack.token_count <= pack.budget_tokens
    assert "OrderService" in pack.content
    assert "src/checkout.py" in pack.import_neighbors


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


def test_context_pack_prioritizes_exact_prefix_and_compound_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    pack = engine.context_pack(
        task="issue access token", seed_files=["src/auth.py"], budget_tokens=5000, max_symbols=10
    )

    assert pack.entry_points
    assert pack.entry_points[0]["qualified_name"] == "issue_access_token"
    assert pack.code_blocks[0]["qualified_name"] == "issue_access_token"
    assert len(pack.code_blocks) <= 3


def test_context_pack_overfetches_search_candidates_before_per_file_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    for file_name, symbol_names in {
        "a.py": ["a1", "a2", "a3", "a4", "a5"],
        "b.py": ["b1"],
    }.items():
        (tmp_path / "src" / file_name).write_text(
            "\n".join(f"def {symbol_name}() -> str:\n    return '{symbol_name}'\n" for symbol_name in symbol_names),
            encoding="utf-8",
        )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    def symbol(file_path: str, name: str, line: int) -> SymbolRecord:
        return SymbolRecord(
            symbol_id=f"{file_path}:{name}",
            repo_id=engine.repo_id,
            file_path=file_path,
            language="python",
            symbol_name=name,
            qualified_name=name,
            kind="function",
            signature=f"def {name}() -> str",
            start_byte=0,
            end_byte=10,
            start_line=line,
            end_line=line + 1,
            content_hash=f"hash-{file_path}-{name}",
            score=1.0,
        )

    ranked_symbols = [
        symbol("src/a.py", "a1", 1),
        symbol("src/a.py", "a2", 3),
        symbol("src/a.py", "a3", 5),
        symbol("src/a.py", "a4", 7),
        symbol("src/a.py", "a5", 9),
        symbol("src/b.py", "b1", 1),
    ]
    requested_limits: list[int] = []

    def fake_search_symbols(*_args: object, limit: int, **_kwargs: object) -> list[SymbolRecord]:
        requested_limits.append(limit)
        return ranked_symbols[:limit]

    monkeypatch.setattr(engine, "repo_map", lambda **kwargs: {"outline": "repo map outline"})
    monkeypatch.setattr(engine, "search_symbols", fake_search_symbols)
    monkeypatch.setattr(engine, "_symbols_for_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "_import_neighbors", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "get_symbol", lambda **kwargs: {"source": "def x():\n    return 1"})

    pack = engine.context_pack(task="service changes", seed_files=[], budget_tokens=5000, max_symbols=5)

    assert requested_limits and requested_limits[0] > 5
    assert len(pack.entry_points) == 5
    assert [item["qualified_name"] for item in pack.entry_points][-1] == "b1"


def test_context_pack_ignores_commit_history_hits_for_current_code_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text(
        "def issue_access_token() -> str:\n    return 'token'\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    commit_hit = SymbolRecord(
        symbol_id="commit:auth-fix",
        repo_id=engine.repo_id,
        file_path="src/auth.py",
        language="text",
        symbol_name="auth-fix",
        qualified_name="Fix auth token regression",
        kind="commit",
        signature="Fix auth token regression",
        start_byte=0,
        end_byte=10,
        start_line=1,
        end_line=1,
        content_hash="commit-hash",
        score=10.0,
        provenance="commit",
    )
    live_symbol = SymbolRecord(
        symbol_id="src/auth.py:issue_access_token",
        repo_id=engine.repo_id,
        file_path="src/auth.py",
        language="python",
        symbol_name="issue_access_token",
        qualified_name="issue_access_token",
        kind="function",
        signature="def issue_access_token() -> str",
        start_byte=0,
        end_byte=10,
        start_line=1,
        end_line=2,
        content_hash="live-hash",
        score=1.0,
        provenance="local",
    )
    requested_symbols: list[str] = []

    def fake_get_symbol(*, symbol_id: str, **_kwargs: object) -> dict[str, str]:
        requested_symbols.append(symbol_id)
        if symbol_id == commit_hit.symbol_id:
            raise AssertionError("context_pack should not request source for commit history hits")
        return {"source": "def issue_access_token() -> str:\n    return 'token'\n"}

    monkeypatch.setattr(engine, "repo_map", lambda **kwargs: {"outline": "repo map outline"})
    monkeypatch.setattr(engine, "search_symbols", lambda *args, **kwargs: [commit_hit, live_symbol])
    monkeypatch.setattr(engine, "_symbols_for_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "_import_neighbors", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "get_symbol", fake_get_symbol)

    pack = engine.context_pack(task="fix auth regression", seed_files=[], budget_tokens=5000, max_symbols=2)

    assert [item["qualified_name"] for item in pack.entry_points] == ["issue_access_token"]
    assert requested_symbols == [live_symbol.symbol_id]


def test_context_pack_uses_call_graph_to_add_same_file_helpers(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "worker.py").write_text(
        "def helper() -> str:\n    return 'ok'\n\ndef run_worker() -> str:\n    return helper()\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    pack = engine.context_pack(task="trace run_worker flow", seed_files=[], budget_tokens=5000, max_symbols=1)

    assert [item["qualified_name"] for item in pack.entry_points] == ["run_worker"]
    assert [item["qualified_name"] for item in pack.related_symbols] == ["helper"]
    assert [item["qualified_name"] for item in pack.code_blocks] == ["run_worker", "helper"]
    assert pack.telemetry["call_graph_related_symbols"] == 1


def test_context_pack_prunes_overlapping_container_symbols_for_method_focus(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "svc.py").write_text(
        "class Runtime:\n"
        "    def helper(self) -> int:\n"
        "        return 1\n"
        "\n"
        "    def get_context(self) -> int:\n"
        "        return self.helper()\n"
        "\n"
        "def bootstrap() -> Runtime:\n"
        "    return Runtime()\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    pack = engine.context_pack(
        task="trace Runtime.get_context in src/svc.py",
        seed_files=[],
        budget_tokens=5000,
        max_symbols=5,
    )

    entry_names = [item["qualified_name"] for item in pack.entry_points]
    block_names = [item["qualified_name"] for item in pack.code_blocks]

    assert pack.entry_points[0]["qualified_name"] == "Runtime.get_context"
    assert "Runtime" not in entry_names
    assert "Runtime" not in block_names
    assert "Runtime.helper" in block_names
    assert "bootstrap" in block_names


def test_context_pack_keeps_container_symbol_when_query_targets_class(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "svc.py").write_text(
        "class Runtime:\n"
        "    def helper(self) -> int:\n"
        "        return 1\n"
        "\n"
        "    def get_context(self) -> int:\n"
        "        return self.helper()\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    pack = engine.context_pack(task="trace Runtime in src/svc.py", seed_files=[], budget_tokens=5000)

    assert pack.entry_points[0]["qualified_name"] == "Runtime"
    assert pack.code_blocks[0]["qualified_name"] == "Runtime"


def test_context_pack_truncates_top_symbol_to_fit_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ctx.py").write_text("def oversized_primary() -> int:\n    return 1\n", encoding="utf-8")
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    primary = SymbolRecord(
        symbol_id="src/ctx.py:oversized_primary",
        repo_id=engine.repo_id,
        file_path="src/ctx.py",
        language="python",
        symbol_name="oversized_primary",
        qualified_name="oversized_primary",
        kind="function",
        signature="oversized_primary()",
        start_byte=0,
        end_byte=10,
        start_line=1,
        end_line=2,
        content_hash="h-oversized-primary",
        score=1.0,
    )
    huge_source = "\n".join(
        [
            "def oversized_primary() -> int:",
            *[f"    value_{index} = {index}" for index in range(240)],
            "    return value_239",
        ]
    )

    monkeypatch.setattr(engine, "repo_map", lambda **kwargs: {"outline": "repo map outline"})
    monkeypatch.setattr(engine, "search_symbols", lambda *args, **kwargs: [primary])
    monkeypatch.setattr(engine, "_symbols_for_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "_import_neighbors", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "get_symbol", lambda **kwargs: {"source": huge_source})

    pack = engine.context_pack(task="oversized primary", seed_files=["src/ctx.py"], budget_tokens=120, max_symbols=1)

    assert pack.code_blocks
    assert pack.code_blocks[0]["qualified_name"] == "oversized_primary"
    assert TRUNCATION_MARKER in pack.code_blocks[0]["source"]
    assert pack.token_count <= pack.budget_tokens
    assert pack.telemetry["token_budget_fit"] is True


def test_context_pack_skips_oversized_symbols_that_do_not_fit_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ctx.py").write_text(
        "\n".join(
            [
                "def primary_match() -> int:",
                "    return 1",
                "",
                "def oversized_helper() -> int:",
                "    return 2",
                "",
                "def trailing_match() -> int:",
                "    return 3",
            ]
        ),
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    def symbol(name: str, line: int) -> SymbolRecord:
        return SymbolRecord(
            symbol_id=f"src/ctx.py:{name}",
            repo_id=engine.repo_id,
            file_path="src/ctx.py",
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
        symbol("primary_match", 1),
        symbol("oversized_helper_with_verbose_context_payload_marker", 4),
        symbol("trailing_match", 7),
    ]
    source_by_id = {
        "src/ctx.py:primary_match": "def primary_match() -> int:\n    return 1\n",
        "src/ctx.py:oversized_helper_with_verbose_context_payload_marker": "\n".join(
            [
                "def oversized_helper_with_verbose_context_payload_marker() -> int:",
                *[f"    value_{index} = {index}" for index in range(320)],
                "    return value_319",
            ]
        ),
        "src/ctx.py:trailing_match": "def trailing_match() -> int:\n    return 3\n",
    }

    monkeypatch.setattr(engine, "repo_map", lambda **kwargs: {"outline": "repo map outline"})
    monkeypatch.setattr(engine, "search_symbols", lambda *args, **kwargs: ranked_symbols)
    monkeypatch.setattr(engine, "_symbols_for_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "_import_neighbors", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "get_symbol", lambda **kwargs: {"source": source_by_id[kwargs["symbol_id"]]})

    pack = engine.context_pack(task="primary trailing", seed_files=["src/ctx.py"], budget_tokens=140, max_symbols=3)

    assert [block["qualified_name"] for block in pack.code_blocks] == ["primary_match", "trailing_match"]
    assert [symbol.qualified_name for symbol in pack.symbols] == ["primary_match", "trailing_match"]
    assert "oversized_helper_with_verbose_context_payload_marker" in {
        item["qualified_name"] for item in pack.entry_points
    }
    assert pack.token_count <= pack.budget_tokens
    assert pack.telemetry["selected_symbols"] == 2


def test_code_context_search_text_uses_literal_matches(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    matches = engine.search_text("calculate_total", path="src", limit=10)
    assert {match.file_path for match in matches} == {"src/orders.py", "src/checkout.py"}


def test_explore_pins_exact_symbol_name(tmp_path: Path) -> None:
    # Concept-mode explore must surface an exact symbol-name match, rank it first,
    # and render its full body -- never let lexical/semantic cousins bury it or the
    # max_symbols cap drop it. Regression: explore("_pack_single_payload") used to
    # return only "_payload_looks_empty" / "_response_payload" cousins, omitting the
    # exact definition entirely.
    src = tmp_path / "src"
    src.mkdir()
    (src / "payloads.py").write_text(
        "def payload_looks_empty(payload):\n    return not payload\n\n\n"
        "def iter_payloads(body):\n    return list(body)\n\n\n"
        "def response_payload(result):\n    return {}\n\n\n"
        "def transport_payload(data):\n    return data\n\n\n"
        "def render_payload(payload):\n    return str(payload)\n\n\n"
        "def pack_single_payload(data):\n    return {'packed': data}\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    res = engine.tool_explore(query="pack_single_payload", max_symbols=2, budget_tokens=4000)

    names = [entry["qualified_name"] for entry in res["entry_points"]]
    assert names, "explore returned no entry points"
    assert names[0] == "pack_single_payload", f"exact match not pinned first: {names}"

    sections = [section for file in res["files"] for section in file.get("source_sections", [])]
    exact = [s for s in sections if s.get("qualified_name") == "pack_single_payload"]
    assert exact, "exact match has no source section"
    assert not exact[0].get("skeleton"), "exact match must render full body, not a skeleton"


def test_symbol_query_regex_separates_identifiers_from_concepts() -> None:
    # The exact-name lookup is gated on this regex: bare identifiers / dotted
    # paths trigger it; multi-word concept queries skip it (no extra search).
    from lemoncrow.core.capabilities.code_context.engine import _SYMBOL_QUERY_RE

    assert _SYMBOL_QUERY_RE.match("_pack_single_payload")
    assert _SYMBOL_QUERY_RE.match("module.Class.method")
    assert not _SYMBOL_QUERY_RE.match("pack the payload data")
    assert not _SYMBOL_QUERY_RE.match("")


def test_tool_search_text_prefers_symbol_hits_for_substring_queries(tmp_path: Path) -> None:
    _write_substring_search_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    aggregate = engine.tool_search(
        "aggregate",
        mode="lexical",
        intent="text",
        file_glob="src/lemoncrow/**/*.py",
        limit=10,
        budget_tokens=4000,
    )
    applies = engine.tool_search(
        "applies",
        mode="lexical",
        intent="text",
        file_glob="src/lemoncrow/**/*.py",
        limit=10,
        budget_tokens=4000,
    )
    adaptive = engine.tool_search(
        "adaptivepriortracker",
        mode="lexical",
        intent="text",
        file_glob="src/lemoncrow/**/*.py",
        limit=10,
        budget_tokens=4000,
    )

    assert any(item["name"] == "aggregate_session_stats" for item in aggregate["items"])
    assert any(item["name"] == "applies_without_tiebreaker_at" for item in applies["items"])
    assert any(item["name"] == "_AdaptivePriorTracker" for item in adaptive["items"])
    assert aggregate["text_search"] is True
    assert applies["text_search"] is True
    assert adaptive["text_search"] is True


def test_tool_search_auto_routes_multiword_query_with_embedded_identifier_to_substring(
    tmp_path: Path,
) -> None:
    # Regression: a natural-language query embedding an exact identifier (e.g.
    # "clone entity strip technical fields _finalEncrypted") used to be shut out
    # of substring fallback entirely -- ANY whitespace or underscore in the
    # *whole* query disqualified it, even though the embedded token
    # "_finalEncrypted" is itself an exact symbol name that a substring search
    # would nail instantly.
    #
    # Under the existence-gated fallback architecture this case is resolved by
    # ordinary symbol/semantic search itself: this engine's symbol search
    # already folds a declared symbol's own name into its lexical ranking, so
    # a query containing "_finalEncrypted" verbatim surfaces that exact symbol
    # on its own -- search_symbols never comes back empty here (verified
    # empirically), so the substring-fallback branch correctly never fires for
    # this shape. That is the "natural consequence" the more general mechanism
    # was meant to produce instead of a separately-maintained special case, so
    # the assertion checks the OUTCOME (the right symbol is found) rather than
    # which internal path resolved it. See
    # test_tool_search_auto_falls_back_to_substring_on_embedded_kebab_literal
    # for the regression that actually exercises the new fallback mechanism.
    _write_embedded_identifier_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    payload = engine.tool_search(
        "clone entity strip technical fields _finalEncrypted",
        mode="lexical",
        intent="auto",
        limit=5,
        budget_tokens=4000,
    )

    assert any(item["name"] == "_finalEncrypted" for item in payload["items"])


def test_tool_search_auto_falls_back_to_substring_on_embedded_kebab_literal(
    tmp_path: Path,
) -> None:
    # Regression (flipt-io/flipt shape): a query embedding a literal string
    # VALUE -- not a declared symbol name -- used to have no path to a hit at
    # all. "x-flipt-accept-server-version" is a kebab-case HTTP header whose
    # only occurrence in the fixture is inside prose text, with no enclosing
    # symbol at all, so search_symbols legitimately returns nothing (unlike a
    # `const`/`var` declaration, whose signature this engine's symbol search
    # already indexes and would find on its own -- verified empirically while
    # building this fixture). The old shape-based multi-word probe
    # (snake_case/camelCase only, no hyphens) never even extracted the token
    # as a candidate either way. This proves the existence-gated fallback is
    # genuinely shape-agnostic: on a real search_symbols miss, it finds the
    # token via raw line-content search (search_text), never the symbol
    # table.
    _write_kebab_literal_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    assert engine.search_symbols("parse x-flipt-accept-server-version header", mode="lexical", limit=5) == []

    payload = engine.tool_search(
        "parse x-flipt-accept-server-version header",
        mode="lexical",
        intent="auto",
        limit=5,
        budget_tokens=4000,
    )

    assert payload.get("text_search") is True
    assert any(
        item.get("path") == "docs/grpc-headers.txt" and "x-flipt-accept-server-version" in item.get("signature", "")
        for item in payload["items"]
    )


def test_search_symbols_skips_fuzzy_scan_for_precise_snake_case_misses(
    tmp_path: Path,
) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    # The engine uses rapidfuzz.DamerauLevenshtein, not difflib.SequenceMatcher,
    # for fuzzy matching.  A precise snake_case query that matches no symbol
    # should return an empty list without needing a fuzzy fallback.
    hits = engine.search_symbols("missing_symbol_name_never_exists", limit=5, mode="lexical")

    assert hits == []


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
    engine.index_repo()

    _ = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    cached = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    assert cached["cache_hit"] is True

    # force=True guarantees a version bump even if no files changed;
    # this tests the cache-invalidation mechanism, not incremental detection.
    indexed = engine.tool_index(force=True, budget_tokens=4000)
    fresh = engine.tool_search("OrderService", limit=5, budget_tokens=4000)

    assert indexed["index_version"] >= 2
    assert fresh["cache_hit"] is False
    assert fresh["provenance"] == "local"


def test_tool_search_deleted_scope_returns_graveyard_items_with_provenance_and_cache_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_HISTORY_ENABLED", "1")  # feature is opt-in (default off)
    repo_root = tmp_path / "repo"
    delete_sha = _write_deleted_history_fixture(repo_root)
    engine = CodeContextEngine(repo_root, db_path=tmp_path / "code.sqlite")

    first = engine.tool_search("LegacyCheckout", scope="deleted", limit=5, budget_tokens=4000)
    second = engine.tool_search("LegacyCheckout", scope="deleted", limit=5, budget_tokens=4000)

    assert first["cache_hit"] is False
    assert first["provenance"] == "graveyard"
    assert first["items"][0]["name"] == "LegacyCheckout"
    assert first["items"][0]["deleted_sha"] == delete_sha
    assert first["items"][0]["author"] == "history@example.com"
    assert second["cache_hit"] is True
    assert second["provenance"] == "cached"


def test_tool_search_deleted_scope_is_rename_aware_on_current_public_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_HISTORY_ENABLED", "1")  # feature is opt-in (default off)
    repo_root = tmp_path / "repo"
    rename_sha = _write_rename_history_fixture(repo_root)
    engine = CodeContextEngine(repo_root, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    payload = engine.tool_search("ModernCheckout", scope="deleted", limit=5, budget_tokens=4000)

    assert payload["items"][0]["name"] == "LegacyCheckout"
    assert payload["items"][0]["renamed_to"] == "modern.py"
    assert payload["items"][0]["deleted_sha"] == rename_sha
    assert payload["items"][0]["rename"]


def test_tool_search_deleted_scope_applies_temporal_and_touched_by_filters_and_widens_cache_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_HISTORY_ENABLED", "1")  # feature is opt-in (default off)
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
        "LegacyCheckout",
        scope="deleted",
        touched_by="history@example.com",
        limit=5,
        budget_tokens=4000,
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
    assert unfiltered["items"][0]["author"] == "history@example.com"
    assert additive["cache_hit"] is False
    assert additive["items"][0]["author"] == "history@example.com"


def test_tool_search_deleted_scope_dispatches_via_git_history_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    from lemoncrow.infra.code_intel.git_history.adapter import DeletedHistorySearchAdapter

    def fake_search(
        self: object,
        query: str,
        *,
        limit: int,
        since_ts: int | None,
        touched_by: str | None,
        language: str | None,
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

    assert payload["items"][0]["deleted_sha"] == "abc123"
    assert payload["provenance"] == "graveyard"


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

    assert {item["path"] for item in unfiltered["items"]} == {"archived.py", "recent.py"}
    assert [item["path"] for item in filtered["items"]] == ["recent.py"]


def test_budget_packer_drops_optional_keys_first() -> None:
    packer = BudgetPacker()
    items = [
        {
            "id": f"sym-{index}",
            "name": f"Symbol{index}",
            "qualified_name": f"pkg.Symbol{index}",
            "path": f"src/mod_{index}.py",
            "kind": "function",
            "signature": f"def symbol_{index}(value: str) -> str",
            "line": index + 1,
            "end_line": index + 2,
            "language": "python",
            "provenance": "local",
            "doc": "summary " * 20,
        }
        for index in range(10)
    ]

    packed, _, token_count = packer.pack(
        items,
        200,
        essential_keys=[
            "id",
            "name",
            "qualified_name",
            "path",
            "kind",
            "signature",
            "line",
            "end_line",
            "language",
            "provenance",
        ],
        optional_keys_in_drop_order=["doc"],
    )

    assert token_count > 0
    assert len(packed) >= 3
    assert all("doc" not in item for item in packed[3:])
    for item in packed[:3]:
        assert item["id"].startswith("sym-")
        assert "signature" in item
        assert "path" in item


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
    engine.index_repo()

    search_payload = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    symbol_payload = engine.tool_symbol(qualified_name="OrderService", file_path="src/orders.py", budget_tokens=4000)
    context_payload = engine.tool_context(
        task="change OrderService calculate_total",
        seed_files=["src/orders.py"],
        budget_tokens=350,
    )
    cached_search = engine.tool_search("OrderService", limit=5, budget_tokens=4000)

    assert search_payload["provenance"] == "local"
    assert all(
        "provenance" not in item for item in search_payload["items"]
    )  # per-item provenance stripped; top-level covers it
    assert symbol_payload["provenance"] == "local"
    assert context_payload["provenance"] == "local"
    assert cached_search["provenance"] == "cached"


def test_tool_usages_groups_local_references_and_reports_treesitter_fallback(
    tmp_path: Path,
) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    payload = engine.tool_usages(query="OrderService", budget_tokens=4000)

    assert payload["target"]["qualified_name"] == "OrderService"
    assert payload["group_by"] == "file"
    assert payload["references"]["src/checkout.py"][0]["provenance"] in {
        "treesitter",
        "local_index",
    }
    assert payload["reference_count"] >= 1
    if "provenance_breakdown" in payload:
        assert (
            payload["provenance_breakdown"].get("treesitter", 0) + payload["provenance_breakdown"].get("local_index", 0)
            >= 1
        )
    assert payload["cache_hit"] is False
    flattened = [item for refs in payload["references"].values() for item in refs]
    assert all("snippet" not in item for item in flattened)


def test_tool_symbol_adds_cross_lang_refs_without_dropping_existing_symbol_fields(
    tmp_path: Path,
) -> None:
    _write_cross_lang_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    CrossLangRunner(repo_root=tmp_path, repo_id=engine.repo_id, connection_factory=engine.connection).resolve_all()

    payload = engine.tool_symbol(qualified_name="load_plugin", file_path="src/bootstrap.py", budget_tokens=4000)

    assert payload["qualified_name"] == "load_plugin"
    assert payload["name"] == "load_plugin"
    assert payload["source"]
    assert payload["cross_lang_refs"][0]["edge_kind"] == "dynamic_import"
    assert payload["cross_lang_refs"][0]["confidence"] >= 0.7


def test_tool_usages_appends_cross_lang_references_and_preserves_local_groups(
    tmp_path: Path,
) -> None:
    _write_cross_lang_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    CrossLangRunner(repo_root=tmp_path, repo_id=engine.repo_id, connection_factory=engine.connection).resolve_all()

    payload = engine.tool_usages(symbol_name="main", file_path="scripts/worker.py", budget_tokens=4000)

    assert payload["target"]["qualified_name"] == "main"
    assert payload["references"]["src/local_worker.py"][0]["provenance"] in {
        "treesitter",
        "local_index",
    }
    assert payload["references"]["src/bootstrap.py"][0]["provenance"] == "cross_lang"
    assert payload["references"]["src/bootstrap.py"][0]["edge_kind"] == "subprocess"
    assert payload["references"]["src/bootstrap.py"][0]["confidence"] >= 0.7
    assert (
        payload["provenance_breakdown"].get("treesitter", 0) + payload["provenance_breakdown"].get("local_index", 0)
        >= 1
    )
    assert payload["provenance_breakdown"]["cross_lang"] >= 1


def test_tool_usages_aggregates_results_for_ambiguous_name(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def helper() -> int:\n    return 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("def helper() -> int:\n    return 2\n", encoding="utf-8")
    (tmp_path / "src" / "use_a.py").write_text("from src.a import helper\n\nvalue = helper()\n", encoding="utf-8")
    (tmp_path / "src" / "use_b.py").write_text("from src.b import helper\n\nvalue = helper()\n", encoding="utf-8")
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    payload = engine.tool_usages(query="helper", budget_tokens=4000)

    assert "error" not in payload
    assert payload["reference_count"] >= 2
    assert payload["ambiguity"]["merged_target_count"] == 2
    assert {item["path"] for item in payload["ambiguity"]["matches"]} == {"src/a.py", "src/b.py"}
    assert payload["cache_hit"] is False


def test_tool_callers_and_callees_aggregate_results_for_ambiguous_name(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "def helper() -> int:\n    return 1\n\ndef run() -> int:\n    return helper()\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "b.py").write_text(
        "def helper() -> int:\n    return 2\n\ndef run() -> int:\n    return helper()\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    callers = engine.tool_callers(query="helper", budget_tokens=4000)
    callees = engine.tool_callees(query="run", budget_tokens=4000)

    assert "error" not in callers
    assert callers["ambiguity"]["merged_target_count"] == 2
    assert {item["name"] for item in callers["related"]} == {"run"}
    assert {item["path"] for item in callers["related"]} == {"src/a.py", "src/b.py"}

    assert "error" not in callees
    assert callees["ambiguity"]["merged_target_count"] == 2
    assert {item["name"] for item in callees["related"]} == {"helper"}
    assert callees["edge_count"] >= 1


def test_tool_callees_resolves_indexed_targets_for_ambiguous_callee_name(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a_helpers.py").write_text("def helper() -> int:\n    return 1\n", encoding="utf-8")
    (tmp_path / "src" / "b_helpers.py").write_text("def helper() -> int:\n    return 2\n", encoding="utf-8")
    (tmp_path / "src" / "a.py").write_text(
        "from src.a_helpers import helper\n\ndef run() -> int:\n    return helper()\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "b.py").write_text(
        "from src.b_helpers import helper\n\ndef run() -> int:\n    return helper()\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    payload = engine.tool_callees(query="run", budget_tokens=4000)

    assert "error" not in payload
    assert payload["ambiguity"]["merged_target_count"] == 2
    assert {item["name"] for item in payload["related"]} == {"helper"}
    assert {item["path"] for item in payload["related"]} == {"src/a_helpers.py", "src/b_helpers.py"}
    assert payload["edge_count"] >= 2


def test_tool_search_snippet_none_omits_snippets_and_keeps_exact_match_first(
    tmp_path: Path,
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
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    payload = engine.tool_search("OrderService", limit=5, snippet="none", budget_tokens=4000)

    assert payload["items"][0]["name"] == "OrderService"
    assert all("snippet" not in item for item in payload["items"])
    assert all("content_hash" not in item for item in payload["items"])


def test_tool_search_seed_files_prioritize_grounded_results(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "legacy").mkdir()
    (tmp_path / "app" / "orders.py").write_text("class OrderService:\n    pass\n", encoding="utf-8")
    (tmp_path / "legacy" / "orders.py").write_text("class OrderService:\n    pass\n", encoding="utf-8")
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    payload = engine.tool_search(
        "OrderService",
        limit=5,
        mode="lexical",
        seed_files=["legacy/orders.py"],
        budget_tokens=4000,
    )

    assert payload["items"][0]["path"] == "legacy/orders.py"
    assert payload["items"][0]["name"] == "OrderService"


def test_tool_search_high_limit_forces_location_only_compaction(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    payload = engine.tool_search(
        "OrderService",
        mode="lexical",
        limit=120,
        snippet="head",
        snippet_lines=30,
        budget_tokens=12000,
    )

    assert payload["snippet"] == "none"
    assert payload["items"]
    assert all("snippet" not in item for item in payload["items"])


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
    monkeypatch.setattr(engine, "search_symbols", lambda *args, **kwargs: [symbol, symbol])

    payload = engine.tool_search("OrderService", snippet="none", budget_tokens=4000)

    assert len(payload["items"]) == 1


def test_auto_mode_keeps_identifier_queries_on_exact_lexical_order(tmp_path: Path) -> None:
    _write_semantic_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    hits = engine.search_symbols("issue_access_token", limit=5, mode="auto")
    payload = engine.tool_search("issue_access_token", limit=5, mode="auto", budget_tokens=4000)

    assert hits
    assert hits[0].symbol_name == "issue_access_token"
    assert payload["items"][0]["name"] == "issue_access_token"
    assert payload["mode"] == "lexical"


def test_search_symbols_lexical_planner_prioritizes_exact_and_case_insensitive_matches(
    tmp_path: Path,
) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    ci_hits = engine.search_symbols("orderservice", limit=5, mode="lexical")
    qualified_hits = engine.search_symbols("OrderService.calculate_total", limit=5, mode="lexical")

    assert ci_hits
    assert ci_hits[0].symbol_name == "OrderService"
    assert qualified_hits
    assert qualified_hits[0].qualified_name == "OrderService.calculate_total"


def test_search_symbols_lexical_planner_demotes_test_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "order_service_factory.py").write_text(
        "class OrderServiceFactory:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_order_service_factory.py").write_text(
        "class OrderServiceFactory:\n    pass\n\ndef test_order_service_factory() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    # A non-test query ranks the production definition above its test-file twin and
    # is deterministic across repeated calls.
    prod_first = engine.search_symbols("OrderServiceFactory", limit=5, mode="lexical")
    prod_second = engine.search_symbols("OrderServiceFactory", limit=5, mode="lexical")
    # A test-scoped query lifts the demotion so the test file surfaces first.
    test_query_hits = engine.search_symbols("test_order_service_factory", limit=5, mode="lexical")

    assert prod_first
    assert prod_first[0].file_path == "src/order_service_factory.py"
    assert [hit.symbol_id for hit in prod_first] == [hit.symbol_id for hit in prod_second]
    assert test_query_hits
    assert test_query_hits[0].file_path == "tests/test_order_service_factory.py"


def test_tool_search_skips_artifact_snapshot_hits(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "live.py").write_text(
        "def classify_command(command: str) -> str:\n    return command\n",
        encoding="utf-8",
    )
    (tmp_path / ".bench-work" / "snapshot" / "src").mkdir(parents=True)
    (tmp_path / ".bench-work" / "snapshot" / "src" / "live.py").write_text(
        "def classify_command(command: str) -> str:\n    return command\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    payload = engine.tool_search("classify_command", limit=10, mode="lexical", budget_tokens=4000)

    assert [item["path"] for item in payload["items"]] == ["src/live.py"]


def test_tool_search_exact_identifier_query_returns_only_exact_symbol_hits(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "live.py").write_text(
        "def classify_command(command: str) -> str:\n    return command\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "helpers.py").write_text(
        "def run_command(command: str) -> str:\n"
        "    return classify_command(command)\n\n"
        "def classify_command_wrapper(command: str) -> str:\n"
        "    return classify_command(command)\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "debug.py").write_text(
        "TARGET = 'classify_command'\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    payload = engine.tool_search("classify_command", limit=10, mode="lexical", budget_tokens=4000)

    assert [item["path"] for item in payload["items"]] == ["src/live.py"]
    assert [item["name"] for item in payload["items"]] == ["classify_command"]


def test_tool_search_routes_lowercase_substrings_to_text_but_keeps_exact_symbols(
    tmp_path: Path,
) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    substring_payload = engine.tool_search(
        "total",
        mode="lexical",
        intent="text",
        file_glob="src/**/*.py",
        limit=5,
        budget_tokens=4000,
    )
    assert substring_payload["text_search"] is True
    assert any(item["name"] == "calculate_total" for item in substring_payload["items"])

    exact_payload = engine.tool_search(
        "helper",
        mode="lexical",
        intent="symbol",
        limit=5,
        budget_tokens=4000,
    )
    assert "text_search" not in exact_payload
    assert exact_payload["items"][0]["name"] == "helper"


def test_search_symbols_lexical_planner_does_not_fuzzy_match_typos(
    tmp_path: Path,
) -> None:
    # LLM-issued queries don't contain typos, so symbol search no longer pays for a
    # full-symbol fuzzy scan: a misspelled identifier must NOT surface the real
    # symbol (only the exact / substring / token channels remain).
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    typo_hits = engine.search_symbols("OrdreServce", limit=5, mode="lexical")
    exact_hits = engine.search_symbols("OrderService", limit=5, mode="lexical")
    exact_repeat = engine.search_symbols("OrderService", limit=5, mode="lexical")

    assert all(hit.symbol_name != "OrderService" for hit in typo_hits)
    assert exact_hits
    assert exact_hits[0].symbol_name == "OrderService"
    assert [hit.symbol_id for hit in exact_hits] == [hit.symbol_id for hit in exact_repeat]


def test_retrieval_cache_diagnostics_hide_payloads_and_invalidate_one_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

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

    # force=True guarantees a full rebuild so files_indexed reflects actual file count
    # regardless of any prior autosync activity.
    payload = engine.tool_index(force=True, budget_tokens=4000)

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
    engine.index_repo()

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
    assert flat["files"][0]["path"].startswith("src/")
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
    engine.index_repo()

    payload = engine.tool_files(format="flat", include_metadata=True, budget_tokens=360)

    assert payload["total_tokens"] <= 360
    assert payload["truncated"] is True
    assert payload["file_count"] < 40
    assert payload["files"]


def test_tool_explore_returns_grouped_sources_and_entry_points(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

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
    assert payload["files"][0]["path"].startswith("src/")
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
    engine.index_repo()

    payload = engine.tool_routes(limit=20, budget_tokens=4000)

    assert payload["route_count"] >= 6
    routes = payload["routes"]
    assert any(
        route["framework"] == "fastapi" and route["method"] == "GET" and route["route"] == "/health" for route in routes
    )
    assert any(route["framework"] == "express" and route["route"] == "/ping" for route in routes)
    assert any(route["framework"] == "django" and route["route"] == "admin/" for route in routes)
    assert any(route["framework"] == "django" and route["route"] == "^legacy/$" for route in routes)
    assert any(route["framework"] == "flask" and route["route"] == "/healthz" for route in routes)
    assert any(
        route["framework"] == "express" and route["method"] == "POST" and route["route"] == "/orders"
        for route in routes
    )


def test_tool_explore_respects_budget_and_keeps_identity_fields(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    payload = engine.tool_explore(
        "OrderService",
        include_source=True,
        include_relationships=True,
        budget_tokens=320,
    )

    assert payload["total_tokens"] <= 320
    assert "entry_points" in payload
    if payload["entry_points"]:
        assert "id" in payload["entry_points"][0]
        assert "name" in payload["entry_points"][0]
        assert "path" in payload["entry_points"][0]


def test_tool_status_reports_index_cache_and_freshness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_CODE_AUTOSYNC", raising=False)
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

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
    monkeypatch.setenv("LEMONCROW_CODE_AUTOSYNC_DEBOUNCE_MS", "50")
    # No live worker: drive the autosync reindex deterministically below so this
    # test exercises the change-detection + reindex path without a thread race.
    monkeypatch.setattr(CodeContextEngine, "_start_autosync_worker", lambda self: None)
    # The file watcher is a live worker too: since the write-class event filter
    # landed, the orders.py write below reliably fires _notify_watcher_event ->
    # _maybe_autosync_reindex(_from_watcher=True) on the watchdog thread, whose
    # non-blocking autosync lock makes this test's own manual reindex a silent
    # no-op mid-race (previously read-event noise kept the debounce window hot,
    # masking the race by starving the watcher path).
    monkeypatch.setattr(CodeContextEngine, "_start_file_watcher", lambda self: None)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite", autosync_enabled=True)
    engine.index_repo()
    engine._maybe_autosync_reindex()  # seed the change-detection signature

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

    # Change detection is the background autosync worker's job now -- read tools
    # no longer reindex inline (that whole-repo stat walk was the per-call tax on
    # large repos). Drive one worker poll; the read then serves the fresh symbol.
    engine._maybe_autosync_reindex()

    second = engine.tool_search("NewService", limit=5, budget_tokens=4000)
    status = engine.tool_status(budget_tokens=4000)

    assert second["items"]
    assert second["items"][0]["name"] == "NewService"
    assert engine._current_index_version() > version_before
    assert status["autosync"]["enabled"] is True
    assert status["autosync"]["mode"] == "incremental"
    assert status["autosync"]["reindex_count"] >= 1
    assert any(event["event"] == "reindex" for event in status["autosync"]["history"])


def test_autosync_worker_reindexes_without_search_trigger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_fixture_repo(tmp_path)
    monkeypatch.setenv("LEMONCROW_CODE_AUTOSYNC_DEBOUNCE_MS", "50")
    monkeypatch.setenv("LEMONCROW_CODE_AUTOSYNC_POLL_MS", "100")
    # Bypass the production-code poll floor (1000ms) so the worker detects
    # changes within ~200ms instead of ~2s.
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.code_context.engine.CodeContextEngine._parse_autosync_poll_ms",
        lambda self, raw_value: max(100, int(raw_value)) if raw_value else 100,
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    for _ in range(40):
        if engine._current_index_version() > 0:
            break
        time.sleep(0.05)
    if engine._current_index_version() <= 0:
        engine.index_repo()
    version_before = engine._current_index_version()
    assert version_before > 0

    # Wait for the autosync worker to seed its initial source-tree signature
    # so the file write happens *after* the seed, guaranteeing the next
    # worker poll detects the change.
    for _ in range(40):
        if engine._autosync_signature is not None:
            break
        time.sleep(0.05)

    # Modern filesystems (tmpfs, ext4, xfs) have nanosecond timestamps;
    # a brief pause is sufficient to ensure the edit timestamp advances.
    time.sleep(0.05)
    (tmp_path / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n"
        "\n"
        "class BackgroundSyncedService:\n"
        "    pass\n",
        encoding="utf-8",
    )

    for _ in range(40):
        if engine._current_index_version() > version_before:
            break
        time.sleep(0.05)
    if engine._current_index_version() <= version_before:
        engine.index_repo(force=False)

    found = engine.search_symbols("BackgroundSyncedService", mode="lexical", limit=5, auto_index=False)
    assert found
    assert found[0].symbol_name == "BackgroundSyncedService"


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
    # autosync_enabled=False: this test asserts exact index_repo(force=False)
    # file counts, which a live background worker (it now owns the initial build)
    # would race by reindexing the edit first. Disable it for determinism.
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite", autosync_enabled=False)
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


def test_search_symbols_filters_with_zoekt_candidate_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_fixture_repo(tmp_path)
    (tmp_path / "src" / "other.py").write_text(
        "class OrderFactory:\n    pass\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    monkeypatch.setattr(engine, "_zoekt_candidate_files", lambda *args, **kwargs: {"src/orders.py"})
    hits = engine.search_symbols("Order", mode="lexical", limit=20, auto_index=False)

    assert hits
    assert all(hit.file_path == "src/orders.py" for hit in hits)


def test_context_pack_uses_zoekt_anchor_files_as_seeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    captured_seed_files: list[str] = []

    def fake_symbols_for_files(files: list[str], *, limit: int) -> list[SymbolRecord]:
        del limit
        captured_seed_files.extend(files)
        return []

    monkeypatch.setattr(engine, "_zoekt_candidate_files", lambda *args, **kwargs: {"src/orders.py"})
    monkeypatch.setattr(engine, "_symbols_for_files", fake_symbols_for_files)
    monkeypatch.setattr(engine, "search_symbols", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "_import_neighbors", lambda *args, **kwargs: [])

    _ = engine.context_pack(task="Order service changes", seed_files=[], budget_tokens=2000, max_symbols=4)

    assert "src/orders.py" in captured_seed_files


def test_usages_prefers_zoekt_fallback_before_text_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    monkeypatch.setattr(
        engine,
        "_zoekt_text_matches",
        lambda *args, **kwargs: [
            TextMatch(file_path="src/orders.py", line=1, column=1, text="def helper() -> OrderService:")
        ],
    )

    def fail_search_text(*args: object, **kwargs: object) -> list[TextMatch]:
        raise AssertionError("search_text should not be called when Zoekt fallback returned hits")

    monkeypatch.setattr(engine, "search_text", fail_search_text)

    payload = engine.tool_usages(symbol_name="helper", limit=5, budget_tokens=4000)
    refs = payload.get("references", {})
    flat = [item for group in refs.values() for item in group] if isinstance(refs, dict) else refs
    assert flat
    assert any(str(item.get("provenance")) == "zoekt_text" for item in flat if isinstance(item, dict))


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
    engine.index_repo()

    search_default = engine.tool_search("OrderService", limit=5, snippet="none", budget_tokens=4000)
    search_heavy = engine.tool_search("OrderService", limit=5, snippet="full", budget_tokens=4000)

    assert search_default["total_tokens"] < search_heavy["total_tokens"]

    tight = engine.tool_search("fetch_", limit=20, snippet="full", budget_tokens=320)
    assert tight["total_tokens"] <= 320
    assert tight["items"]
    for key in ("id", "name", "path", "line", "signature"):
        assert key in tight["items"][0]

    monkeypatch.setattr(
        "lemoncrow.core.capabilities.code_context.engine.AstGrepAdapter.search",
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


def test_native_pattern_uses_index_and_cache_for_def_patterns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "orders.py").write_text(
        "def add_node(value: int) -> int:\n    return value + 1\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    def fail_ast_parse(_source: str) -> object:
        raise AssertionError("indexed def patterns should not parse source")

    monkeypatch.setattr("lemoncrow.core.capabilities.code_context.engine.ast.parse", fail_ast_parse)

    first = engine.tool_pattern(pattern="def add_node($$$):", language="python", file_glob="src/**/*.py")
    second = engine.tool_pattern(pattern="def add_node($$$):", language="python", file_glob="src/**/*.py")

    assert [match["path"] for match in first["matches"]] == ["src/orders.py"]
    assert second["cache_hit"] is True


def test_search_text_uses_index_before_ripgrep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "orders.py").write_text(
        "def aggregate_session_stats() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    def fail_rg(*args: object, **kwargs: object) -> object:
        raise AssertionError("search_text should use indexed line text before rg")

    monkeypatch.setattr("lemoncrow.core.capabilities.code_context.engine.subprocess.run", fail_rg)

    matches = engine.search_text("aggregate", path="src", limit=5, ignore_case=True)

    assert [(match.file_path, match.line) for match in matches] == [("src/orders.py", 1)]


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
    engine.index_repo()

    full_payload = engine.tool_search("OrderService", limit=5, snippet="full", budget_tokens=4000)
    near_budget = max(1, int(full_payload["total_tokens"]) - 1)
    near_payload = engine.tool_search("OrderService", limit=5, snippet="full", budget_tokens=near_budget)

    assert near_payload["total_tokens"] <= near_budget
    assert "overflow" not in near_payload


def test_overflow_metadata_and_artifact_payload_are_compact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lemoncrow.core.capabilities.code_context.engine.default_store_root",
        lambda: tmp_path / ".lemoncrow-store",
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
    engine.index_repo()

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


def test_cold_reads_do_not_block_on_missing_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Neuter the autosync worker so the test is deterministic (no async build race).
    monkeypatch.setattr(CodeContextEngine, "_start_autosync_worker", lambda self: None)
    _write_fixture_repo(tmp_path)

    # MCP transport mode: a read on a cold index returns nothing (no blocking build).
    mcp_engine = CodeContextEngine(tmp_path, db_path=tmp_path / "mcp.sqlite")
    assert mcp_engine.index_ready() is False
    warming = mcp_engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    assert not warming.get("items")
    assert mcp_engine.index_ready() is False

    # Direct mode also does not block; the index must be pre-built to serve results.
    # autosync always on in practice; index will be built by the worker
    direct_engine = CodeContextEngine(tmp_path, db_path=tmp_path / "direct.sqlite")
    direct_engine.index_repo()
    result = direct_engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    assert any(item["name"] == "OrderService" for item in result["items"])
    assert direct_engine.index_ready() is True
