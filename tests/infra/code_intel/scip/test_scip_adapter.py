from __future__ import annotations

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
