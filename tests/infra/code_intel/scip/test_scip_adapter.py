from __future__ import annotations

import hashlib
import json
from pathlib import Path

from atelier.core.capabilities.code_context.engine import CodeContextEngine
from atelier.core.capabilities.code_context.models import SymbolRecord


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n",
        encoding="utf-8",
    )
    (root / "src" / "checkout.py").write_text(
        "from src.orders import OrderService\n\n"
        "def checkout(items: list[int]) -> int:\n"
        "    return OrderService().calculate_total(items)\n",
        encoding="utf-8",
    )


def _write_scip_fixture(
    engine: CodeContextEngine,
    *,
    symbol_id: str = "scip-order-service",
    include_references: bool = False,
    include_call_graph: bool = False,
    call_graph: dict[str, object] | None = None,
) -> Path:
    source = (engine.repo_root / "src" / "orders.py").read_text(encoding="utf-8")
    checkout_source = (engine.repo_root / "src" / "checkout.py").read_text(encoding="utf-8")
    artifact_dir = engine.repo_root / ".atelier" / "cache" / "scip" / engine.repo_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "python.scip"
    payload: dict[str, object] = {
        "version": 1,
        "repo_id": engine.repo_id,
        "language": "python",
        "symbols": [
            {
                "symbol_id": symbol_id,
                "repo_id": engine.repo_id,
                "file_path": "src/orders.py",
                "language": "python",
                "symbol_name": "OrderService",
                "qualified_name": "OrderService",
                "kind": "class",
                "signature": "class OrderService:",
                "start_byte": 0,
                "end_byte": len(source.encode("utf-8")),
                "start_line": 1,
                "end_line": 3,
                "content_hash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "source": source,
                "provenance": "scip",
            }
        ]
        + (
            [
                {
                    "symbol_id": "scip-checkout",
                    "repo_id": engine.repo_id,
                    "file_path": "src/checkout.py",
                    "language": "python",
                    "symbol_name": "checkout",
                    "qualified_name": "checkout",
                    "kind": "function",
                    "signature": "def checkout(items: list[int]) -> int:",
                    "start_byte": 33,
                    "end_byte": len(checkout_source.encode("utf-8")),
                    "start_line": 3,
                    "end_line": 4,
                    "content_hash": hashlib.sha256(checkout_source.encode("utf-8")).hexdigest(),
                    "source": checkout_source,
                    "provenance": "scip",
                }
            ]
            if include_call_graph or call_graph is not None
            else []
        ),
    }
    if include_references:
        payload["references"] = {
            symbol_id: [
                {
                    "file_path": "src/checkout.py",
                    "line": 4,
                    "column": 12,
                    "end_line": 4,
                    "end_column": 23,
                    "snippet": "    return OrderService().calculate_total(items)",
                    "provenance": "scip",
                }
            ]
        }
    if include_call_graph:
        payload["call_graph"] = {
            "callers": {
                symbol_id: [
                    {
                        "symbol_id": "scip-checkout",
                        "symbol_name": "checkout",
                        "qualified_name": "checkout",
                        "file_path": "src/checkout.py",
                        "kind": "function",
                        "start_line": 3,
                        "end_line": 4,
                        "provenance": "scip",
                    }
                ]
            },
            "callees": {
                "scip-checkout": [
                    {
                        "symbol_id": symbol_id,
                        "symbol_name": "OrderService",
                        "qualified_name": "OrderService",
                        "file_path": "src/orders.py",
                        "kind": "class",
                        "start_line": 1,
                        "end_line": 3,
                        "provenance": "scip",
                    }
                ]
            },
        }
    if call_graph is not None:
        payload["call_graph"] = call_graph
    artifact_path.write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    return artifact_path


class _HealthyScipProvider:
    name = "scip"

    def __init__(self, repo_id: str) -> None:
        self.repo_id = repo_id

    def refresh(self) -> bool:
        return False

    def health(self) -> object:
        return object()

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        kind: str | None = None,
        language: str | None = None,
    ) -> list[SymbolRecord]:
        del query, limit, kind, language
        return [
            SymbolRecord(
                symbol_id="scip-order-service",
                repo_id=self.repo_id,
                file_path="src/orders.py",
                language="python",
                symbol_name="OrderService",
                qualified_name="OrderService",
                kind="class",
                signature="class OrderService:",
                start_byte=0,
                end_byte=89,
                start_line=1,
                end_line=3,
                content_hash="scip",
                provenance="scip",
            )
        ]

    def get_symbol(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> dict[str, object] | None:
        del symbol_id, qualified_name, file_path, symbol_name
        return None

    def find_references(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> list[object] | None:
        del symbol_id, qualified_name, file_path, symbol_name
        return None


class _UnhealthyScipProvider(_HealthyScipProvider):
    def health(self) -> None:
        return None

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        kind: str | None = None,
        language: str | None = None,
    ) -> list[SymbolRecord]:
        raise AssertionError("unhealthy provider should not be used")


def test_store_prefers_healthy_scip_provider(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    engine.intel_store.register(_HealthyScipProvider(engine.repo_id))

    hits = engine.search_symbols("OrderService", limit=5)

    assert hits
    assert hits[0].symbol_id == "scip-order-service"
    assert hits[0].provenance == "scip"


def test_store_falls_back_to_local_provider(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()

    engine.intel_store.register(_UnhealthyScipProvider(engine.repo_id))

    hits = engine.search_symbols("OrderService", limit=5)

    assert hits
    assert hits[0].symbol_name == "OrderService"
    assert hits[0].provenance == "local"


def test_scip_provider_routes_search_and_symbol_payloads(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_scip_fixture(engine)

    hits = engine.search_symbols("OrderService", limit=5)
    symbol = engine.tool_symbol(qualified_name="OrderService", file_path="src/orders.py", budget_tokens=4000)

    assert hits
    assert hits[0].symbol_id == "scip-order-service"
    assert hits[0].provenance == "scip"
    assert symbol["symbol_id"] == "scip-order-service"
    assert symbol["provenance"] == "scip"
    assert "class OrderService" in symbol["source"]


def test_scip_provider_routes_usages_payloads(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_scip_fixture(engine, include_references=True)

    payload = engine.tool_usages(query="OrderService", budget_tokens=4000)

    assert payload["target"]["symbol_id"] == "scip-order-service"
    assert payload["provenance"] == "scip"
    assert payload["provenance_breakdown"] == {"scip": 1}
    assert payload["references"]["src/checkout.py"][0]["provenance"] == "scip"


def test_scip_provider_falls_back_to_treesitter_when_reference_data_is_missing(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_scip_fixture(engine, include_references=False)

    payload = engine.tool_usages(query="OrderService", budget_tokens=4000)

    assert payload["target"]["provenance"] == "scip"
    assert payload["provenance"] == "treesitter"
    assert payload["provenance_breakdown"] == {"treesitter": 1}


def test_scip_provider_routes_call_graph_payloads(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_scip_fixture(engine, include_call_graph=True)

    callers = engine.intel_store.find_callers(symbol_id="scip-order-service")
    callees = engine.intel_store.find_callees(symbol_id="scip-checkout")

    assert callers is not None
    assert callees is not None
    assert callers[0].symbol_id == "scip-checkout"
    assert callers[0].file_path == "src/checkout.py"
    assert callees[0].symbol_id == "scip-order-service"
    assert callees[0].provenance == "scip"


def test_scip_provider_preserves_missing_call_graph_data(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_scip_fixture(engine, include_call_graph=False)

    callers = engine.intel_store.find_callers(symbol_id="scip-order-service")
    callees = engine.intel_store.find_callees(symbol_id="scip-order-service")

    assert callers is None
    assert callees is None


def test_scip_provider_rejects_malformed_or_path_escaping_call_graph_payloads(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_scip_fixture(
        engine,
        call_graph={
            "callers": {
                "scip-order-service": [
                    {
                        "symbol_id": "escape",
                        "symbol_name": "escape",
                        "qualified_name": "escape",
                        "file_path": "../secrets.py",
                        "kind": "function",
                        "start_line": 1,
                        "end_line": 1,
                        "provenance": "scip",
                    }
                ]
            },
            "callees": [],
        },
    )

    hits = engine.search_symbols("OrderService", limit=5)

    assert hits
    assert hits[0].symbol_name == "OrderService"
    assert hits[0].provenance == "local"


def test_scip_provider_falls_back_when_artifact_is_invalid(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    artifact_path = engine.repo_root / ".atelier" / "cache" / "scip" / engine.repo_id / "python.scip"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("{not json", encoding="utf-8")

    hits = engine.search_symbols("OrderService", limit=5)

    assert hits
    assert hits[0].symbol_name == "OrderService"
    assert hits[0].provenance == "local"


def test_scip_refresh_invalidates_cached_search(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    artifact_path = _write_scip_fixture(engine, symbol_id="scip-v1")

    first = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    cached = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    artifact_path.write_text(artifact_path.read_text(encoding="utf-8").replace("scip-v1", "scip-v2"), encoding="utf-8")
    fresh = engine.tool_search("OrderService", limit=5, budget_tokens=4000)

    assert first["cache_hit"] is False
    assert first["provenance"] == "scip"
    assert cached["cache_hit"] is True
    assert fresh["cache_hit"] is False
    assert fresh["provenance"] == "scip"
    assert fresh["items"][0]["symbol_id"] == "scip-v2"


def test_scip_refresh_invalidates_cached_search_for_new_engine_instance(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    artifact_path = _write_scip_fixture(engine, symbol_id="scip-v1")

    cached = engine.tool_search("OrderService", limit=5, budget_tokens=4000)
    artifact_path.write_text(artifact_path.read_text(encoding="utf-8").replace("scip-v1", "scip-v2"), encoding="utf-8")
    fresh_engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    fresh = fresh_engine.tool_search("OrderService", limit=5, budget_tokens=4000)

    assert cached["provenance"] == "scip"
    assert fresh["cache_hit"] is False
    assert fresh["provenance"] == "scip"
    assert fresh["items"][0]["symbol_id"] == "scip-v2"
