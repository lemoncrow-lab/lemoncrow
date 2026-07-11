"""Tests for retrieval ranking signals: usage-frequency/churn tiebreakers (G7)
and generated/scaffolding-file deprioritization (N9).

These cover the hard correctness coupling for Workstream 2 Tier 1:
- Popularity/churn must only ever break ties; an exact-symbol hit stays
  authoritative and is never buried by a more-popular non-exact symbol.
- Generated files rank last and are dropped from "Related Symbols".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.core.capabilities.code_context import CodeContextEngine
from lemoncrow.core.capabilities.code_context.generated_files import is_generated_path
from lemoncrow.core.capabilities.code_context.models import SymbolRecord


def _symbol(
    engine: CodeContextEngine,
    *,
    name: str,
    file_path: str = "src/mod.py",
    line: int = 1,
    score: float = 1.0,
    qualified_name: str | None = None,
) -> SymbolRecord:
    return SymbolRecord(
        symbol_id=f"{file_path}:{name}:{line}",
        repo_id=engine.repo_id,
        file_path=file_path,
        language="python",
        symbol_name=name,
        qualified_name=qualified_name or name,
        kind="function",
        signature=f"{name}()",
        start_byte=0,
        end_byte=10,
        start_line=line,
        end_line=line + 1,
        content_hash=f"h-{name}-{line}",
        score=score,
    )


# --------------------------------------------------------------------------- #
# G7 — usage-frequency / churn as ranking tiebreakers
# --------------------------------------------------------------------------- #


def test_popularity_breaks_ties_between_equally_matched_symbols(tmp_path: Path) -> None:
    """A more-referenced symbol wins when match quality and score are equal."""
    (tmp_path / "src").mkdir()
    # popular_helper is referenced from three call sites; rare_helper from none.
    (tmp_path / "src" / "mod.py").write_text(
        "def popular_helper() -> int:\n    return 1\n\n" "def rare_helper() -> int:\n    return 2\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "caller.py").write_text(
        "from src.mod import popular_helper\n\n"
        "def a() -> int:\n    return popular_helper()\n\n"
        "def b() -> int:\n    return popular_helper()\n\n"
        "def c() -> int:\n    return popular_helper()\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    popular = _symbol(engine, name="popular_helper", file_path="src/mod.py", line=1)
    rare = _symbol(engine, name="rare_helper", file_path="src/mod.py", line=4)

    scores = engine._symbol_popularity_scores([popular, rare])
    assert scores[popular.symbol_id] > scores[rare.symbol_id]
    assert scores[rare.symbol_id] == 0.0

    # Query matches neither exactly nor by prefix -> same match tier + same
    # score, so popularity is the only differentiator.
    ordered = engine._prioritize_context_symbols("helper utilities", [rare, popular])
    assert [item.symbol_name for item in ordered] == ["popular_helper", "rare_helper"]


def test_exact_hit_outranks_more_popular_non_exact_symbol(tmp_path: Path) -> None:
    """Popularity must NEVER override an exact-symbol match (authority preserved)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        "def popular_helper() -> int:\n    return 1\n\n" "def target() -> int:\n    return 2\n",
        encoding="utf-8",
    )
    # popular_helper is heavily referenced; target is never referenced.
    refs = "\n\n".join(f"def use_{i}() -> int:\n    return popular_helper()" for i in range(8))
    (tmp_path / "src" / "caller.py").write_text(
        "from src.mod import popular_helper\n\n" + refs + "\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    popular = _symbol(engine, name="popular_helper", file_path="src/mod.py", line=1, score=1.0)
    exact = _symbol(engine, name="target", file_path="src/mod.py", line=4, score=0.1)

    scores = engine._symbol_popularity_scores([popular, exact])
    assert scores[popular.symbol_id] > 0.0
    assert scores[exact.symbol_id] == 0.0

    # Even though popular_helper is far more popular AND has a higher lexical
    # score, the exact-name match for "target" must rank first.
    ordered = engine._prioritize_context_symbols("target", [popular, exact])
    assert ordered[0].symbol_name == "target"


def test_exact_hit_authority_holds_end_to_end_in_context_pack(tmp_path: Path) -> None:
    """context_pack must surface the exact symbol first despite a popular rival."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text(
        "def issue_token() -> str:\n    return 'x'\n\n" "def issue_token_helper() -> str:\n    return 'y'\n",
        encoding="utf-8",
    )
    # Make issue_token_helper wildly popular via many call sites.
    calls = "\n\n".join(f"def use_{i}() -> str:\n    return issue_token_helper()" for i in range(10))
    (tmp_path / "src" / "caller.py").write_text(
        "from src.auth import issue_token_helper\n\n" + calls + "\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    pack = engine.context_pack(task="issue_token", seed_files=[], budget_tokens=5000, max_symbols=5)
    assert pack.entry_points
    assert pack.entry_points[0]["qualified_name"] == "issue_token"


def test_churn_provider_is_a_tiebreaker_not_an_override(tmp_path: Path) -> None:
    """An injected churn provider breaks ties but never beats an exact match."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        "def alpha() -> int:\n    return 1\n\ndef beta() -> int:\n    return 2\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    alpha = _symbol(engine, name="alpha", file_path="src/mod.py", line=1)
    beta = _symbol(engine, name="beta", file_path="src/mod.py", line=4)

    # Inject a churn provider that strongly favours beta.
    engine._churn_score_provider = lambda symbols: {beta.symbol_id: 1.0}

    # No reference counts (neither is called) and a non-matching query -> churn
    # is the only differentiator, so beta sorts first.
    ordered = engine._prioritize_context_symbols("unrelated query", [alpha, beta])
    assert [item.symbol_name for item in ordered] == ["beta", "alpha"]

    # But an exact match for alpha still wins over high-churn beta.
    ordered_exact = engine._prioritize_context_symbols("alpha", [alpha, beta])
    assert ordered_exact[0].symbol_name == "alpha"


def test_churn_provider_failure_is_fail_open(tmp_path: Path) -> None:
    """A raising churn provider must not break ranking (fail-open)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def alpha() -> int:\n    return 1\n", encoding="utf-8")
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    def boom(_symbols: list[SymbolRecord]) -> dict[str, float]:
        raise RuntimeError("churn backend down")

    engine._churn_score_provider = boom
    alpha = _symbol(engine, name="alpha", file_path="src/mod.py", line=1)
    # Should not raise and should still return a result.
    assert engine._symbol_churn_scores([alpha]) == {}
    ordered = engine._prioritize_context_symbols("alpha", [alpha])
    assert ordered[0].symbol_name == "alpha"


# --------------------------------------------------------------------------- #
# N9 — generated/scaffolding-file deprioritization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "api/service.pb.go",
        "proto/messages_pb2.py",
        "proto/messages_pb2_grpc.py",
        "web/bundle.min.js",
        "styles/app.min.css",
        "src/models.generated.ts",
        "src/models_generated.dart",
        "app/__generated__/schema.ts",
        "tests/__mocks__/client.ts",
        "service/mock_client.go",
        "service/client_mock.go",
        "vendor/lib/thing.go",
        "node_modules/pkg/index.js",
    ],
)
def test_is_generated_path_flags_generated_files(path: str) -> None:
    assert is_generated_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/service.py",
        "src/order_service.py",
        "src/protobuf_utils.py",  # not a *_pb2.py
        "src/mockingbird.py",  # 'mock' is a substring but not a token
        "src/generator.py",  # 'generated' is a substring but not a token/dir
        "web/main.js",
        "",
    ],
)
def test_is_generated_path_keeps_handwritten_files(path: str) -> None:
    assert is_generated_path(path) is False


def test_generated_symbols_rank_last_in_prioritization(tmp_path: Path) -> None:
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    handwritten = _symbol(engine, name="handler", file_path="src/handler.py", line=1)
    generated = _symbol(engine, name="handler", file_path="src/handler_pb2.py", line=1)

    # Equal match tier and score -> generated demotion is the differentiator.
    ordered = engine._prioritize_context_symbols("request handler", [generated, handwritten])
    assert ordered[0].file_path == "src/handler.py"
    assert ordered[-1].file_path == "src/handler_pb2.py"


def test_exact_hit_in_generated_file_is_still_surfaced(tmp_path: Path) -> None:
    """Generated-file demotion must not bury a legitimate exact hit."""
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    generated_exact = _symbol(engine, name="GameState", file_path="game_pb2.py", line=1)
    handwritten_other = _symbol(engine, name="GameStateHelper", file_path="src/state.py", line=1)

    ordered = engine._prioritize_context_symbols("GameState", [handwritten_other, generated_exact])
    assert ordered[0].symbol_name == "GameState"
    assert ordered[0].file_path == "game_pb2.py"


def test_context_pack_drops_generated_files_from_related_symbols(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    # run_worker calls a hand-written helper and a generated stub helper.
    (tmp_path / "src" / "worker.py").write_text(
        "def helper() -> str:\n    return 'ok'\n\n" "def run_worker() -> str:\n    return helper() + stub_call()\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "service_pb2.py").write_text(
        "def stub_call() -> str:\n    return 'generated'\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    pack = engine.context_pack(task="trace run_worker flow", seed_files=[], budget_tokens=5000, max_symbols=1)
    assert pack.entry_points[0]["qualified_name"] == "run_worker"
    related_files = {item["file_path"] for item in pack.related_symbols}
    assert "src/service_pb2.py" not in related_files
    assert all(not is_generated_path(item["file_path"]) for item in pack.related_symbols)


def test_context_pack_caps_related_symbol_count(tmp_path: Path) -> None:
    """Related-symbol count stays within the context policy cap."""
    from lemoncrow.core.capabilities.code_context.output_policy import resolve_output_policy

    (tmp_path / "src").mkdir()
    helpers = "\n\n".join(f"def helper_{i}() -> int:\n    return {i}" for i in range(40))
    body = "\n".join(f"    helper_{i}()" for i in range(40))
    (tmp_path / "src" / "hub.py").write_text(
        helpers + "\n\ndef run_hub() -> None:\n" + body + "\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    cap = resolve_output_policy("context").max_related_symbols
    pack = engine.context_pack(task="trace run_hub flow", seed_files=[], budget_tokens=8000, max_symbols=1)
    assert len(pack.related_symbols) <= cap
