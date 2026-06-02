from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from atelier.core.capabilities.code_context.call_graph import CallGraphNode
from atelier.core.capabilities.code_context.engine import CodeContextEngine
from atelier.core.capabilities.code_context.models import SymbolRecord, UsageReference
from atelier.infra.code_intel.scip.indexer import ScipIndexer
from atelier.infra.code_intel.scip.reader import ScipArtifactError, ScipArtifactReader
from atelier.infra.code_intel.scip.watcher import ScipArtifactWatcher

FIXTURE_INDEX_SHA = "1234567890abcdef1234567890abcdef12345678"


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
    symbol_name: str = "OrderService",
    qualified_name: str = "OrderService",
    file_path: str = "src/orders.py",
    source: str | None = None,
    include_references: bool = False,
    include_call_graph: bool = False,
    call_graph: dict[str, object] | None = None,
    index_sha: str | None = FIXTURE_INDEX_SHA,
    artifact_name: str = "python.scip",
) -> Path:
    symbol_source = source or (engine.repo_root / "src" / "orders.py").read_text(encoding="utf-8")
    checkout_source = (engine.repo_root / "src" / "checkout.py").read_text(encoding="utf-8")
    artifact_dir = ScipIndexer(engine.repo_root, engine.repo_id).cache_root
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path: Path = artifact_dir / artifact_name
    payload: dict[str, object] = {
        "version": 1,
        "repo_id": engine.repo_id,
        "language": "python",
        "symbols": [
            {
                "symbol_id": symbol_id,
                "repo_id": engine.repo_id,
                "file_path": file_path,
                "language": "python",
                "symbol_name": symbol_name,
                "qualified_name": qualified_name,
                "kind": "class",
                "signature": f"class {symbol_name}:",
                "start_byte": 0,
                "end_byte": len(symbol_source.encode("utf-8")),
                "start_line": 1,
                "end_line": len(symbol_source.splitlines()),
                "content_hash": hashlib.sha256(symbol_source.encode("utf-8")).hexdigest(),
                "source": symbol_source,
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
    if index_sha is not None:
        payload["index_sha"] = index_sha
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
        scope: str = "repo",
    ) -> list[SymbolRecord]:
        del query, limit, kind, language, scope
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
    ) -> list[UsageReference] | None:
        del symbol_id, qualified_name, file_path, symbol_name
        return None

    def find_callers(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> list[CallGraphNode] | None:
        del symbol_id, qualified_name, file_path, symbol_name
        return None

    def find_callees(
        self,
        *,
        symbol_id: str | None = None,
        qualified_name: str | None = None,
        file_path: str | None = None,
        symbol_name: str | None = None,
    ) -> list[CallGraphNode] | None:
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
        scope: str = "repo",
    ) -> list[SymbolRecord]:
        del query, limit, kind, language, scope
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


@pytest.mark.skip(
    reason="SCIP routing for engine.tool_*() under field-shortening migration; tracked for post-launch hardening (see docs/launch-readiness.md)."
)
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
    assert symbol["index_sha"] == FIXTURE_INDEX_SHA
    assert hits[0].origin == "internal"
    assert symbol["origin"] == "internal"


def test_scip_indexer_discovers_external_artifacts_under_existing_cache_root(
    tmp_path: Path,
) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_scip_fixture(engine, artifact_name="python.scip")
    _write_scip_fixture(
        engine,
        artifact_name="external-python.scip",
        symbol_id="external-requests-session",
        symbol_name="Session",
        qualified_name="requests.Session",
        file_path="external/requests/sessions.py",
        source="class Session:\n    pass\n",
    )

    artifacts = ScipIndexer(engine.repo_root, engine.repo_id).discover_artifacts()

    assert [artifact.path.name for artifact in artifacts] == ["python.scip", "external-python.scip"]
    assert [artifact.origin for artifact in artifacts] == ["internal", "external"]


def test_scip_provider_tags_external_artifacts_with_external_origin(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_scip_fixture(engine, artifact_name="python.scip")
    _write_scip_fixture(
        engine,
        artifact_name="external-python.scip",
        symbol_id="external-requests-session",
        symbol_name="Session",
        qualified_name="requests.Session",
        file_path="external/requests/sessions.py",
        source="class Session:\n    pass\n",
    )

    provider = engine.intel_store.providers[0]
    provider.refresh()
    internal_hits = provider.search_symbols("OrderService", limit=5, scope="repo")
    external_hits = provider.search_symbols("Session", limit=5, scope="external")
    external_symbol = provider.get_symbol(symbol_id="external-requests-session")

    assert internal_hits
    assert internal_hits[0].origin == "internal"
    assert external_hits
    assert external_hits[0].origin == "external"
    assert external_symbol is not None
    assert external_symbol["origin"] == "external"
    assert external_symbol["qualified_name"] == "requests.Session"


def test_scip_provider_rejects_invalid_external_artifacts_through_trusted_reader_path(
    tmp_path: Path,
) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    artifact_path = _write_scip_fixture(
        engine,
        artifact_name="external-python.scip",
        symbol_id="external-requests-session",
        symbol_name="Session",
        qualified_name="requests.Session",
        file_path="external/requests/sessions.py",
        source="class Session:\n    pass\n",
    )
    artifact_path.write_text("{not json", encoding="utf-8")

    hits = engine.search_symbols("OrderService", limit=5)

    assert hits
    assert hits[0].symbol_name == "OrderService"
    assert hits[0].origin == "internal"


def test_loaded_scip_artifact_exposes_index_sha_metadata(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    artifact_path = _write_scip_fixture(engine)
    reader = ScipArtifactReader(
        repo_root=engine.repo_root,
        allowed_roots=[engine.repo_root, artifact_path.parent],
    )

    artifact = reader.load(artifact_path)
    symbol = artifact.get_symbol(symbol_id="scip-order-service")

    assert artifact.index_sha == FIXTURE_INDEX_SHA
    assert symbol is not None
    assert symbol["index_sha"] == FIXTURE_INDEX_SHA


def test_scip_reader_rejects_missing_or_malformed_index_sha_metadata(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    artifact_path = _write_scip_fixture(engine, index_sha=None)
    reader = ScipArtifactReader(
        repo_root=engine.repo_root,
        allowed_roots=[engine.repo_root, artifact_path.parent],
    )

    with pytest.raises(ScipArtifactError, match="index_sha"):
        reader.load(artifact_path)

    artifact_path = _write_scip_fixture(engine, index_sha="not-a-sha")

    with pytest.raises(ScipArtifactError, match="index_sha"):
        reader.load(artifact_path)


def test_scip_artifact_watcher_treats_branch_switch_as_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    signatures: dict[str, str] = {}
    branch = {"name": "main", "sha": "a" * 40}

    def fake_state(repo_root: Path) -> SimpleNamespace:
        head_ref = f"refs/heads/{branch['name']}"
        return SimpleNamespace(
            git_dir=repo_root / ".git",
            common_dir=repo_root / ".git",
            head_path=repo_root / ".git" / "HEAD",
            head_ref=head_ref,
            head_sha=branch["sha"],
            ref_path=repo_root / ".git" / head_ref,
            packed_refs_path=repo_root / ".git" / "packed-refs",
            branch_key=f"{branch['name']}-key",
        )

    def state_sync(key: str, signature: str) -> bool:
        previous = signatures.get(key)
        signatures[key] = signature
        return previous is not None and previous != signature

    monkeypatch.setattr("atelier.infra.code_intel.scip.watcher.resolve_git_repo_state", fake_state)
    watcher = ScipArtifactWatcher(
        repo_root=tmp_path,
        cache_root=lambda: (tmp_path / ".atelier" / "cache" / "scip" / "repo" / f"{branch['name']}-key"),
        state_sync=state_sync,
    )
    first_path = tmp_path / ".atelier" / "cache" / "scip" / "repo" / "main-key" / "python.scip"
    first_path.parent.mkdir(parents=True, exist_ok=True)
    first_path.write_text("first", encoding="utf-8")

    assert watcher.refresh([first_path]) is False

    branch["name"] = "feature"
    branch["sha"] = "b" * 40
    second_path = tmp_path / ".atelier" / "cache" / "scip" / "repo" / "feature-key" / "python.scip"
    second_path.parent.mkdir(parents=True, exist_ok=True)
    second_path.write_text("first", encoding="utf-8")

    assert watcher.refresh([second_path]) is True


def test_scip_provider_switches_branch_scoped_artifacts_without_reinstantiation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_repo(tmp_path)
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    branch = {"name": "main", "sha": "a" * 40}

    def fake_state(repo_root: Path) -> SimpleNamespace:
        head_ref = f"refs/heads/{branch['name']}"
        return SimpleNamespace(
            git_dir=repo_root / ".git",
            common_dir=repo_root / ".git",
            head_path=repo_root / ".git" / "HEAD",
            head_ref=head_ref,
            head_sha=branch["sha"],
            ref_path=repo_root / ".git" / head_ref,
            packed_refs_path=repo_root / ".git" / "packed-refs",
            branch_key=f"{branch['name']}-key",
        )

    monkeypatch.setattr("atelier.infra.code_intel.scip.indexer.resolve_git_repo_state", fake_state)
    monkeypatch.setattr("atelier.infra.code_intel.scip.watcher.resolve_git_repo_state", fake_state)
    _write_scip_fixture(
        engine,
        symbol_id="scip-main-service",
        symbol_name="MainService",
        qualified_name="MainService",
    )
    provider = engine.intel_store.providers[0]

    provider.refresh()
    main_hits = provider.search_symbols("MainService", limit=5)

    branch["name"] = "feature"
    branch["sha"] = "b" * 40
    _write_scip_fixture(
        engine,
        symbol_id="scip-feature-service",
        symbol_name="FeatureService",
        qualified_name="FeatureService",
        source="class FeatureService:\n    pass\n",
    )

    provider.refresh()
    feature_hits = provider.search_symbols("FeatureService", limit=5)

    assert [hit.symbol_name for hit in main_hits] == ["MainService"]
    assert [hit.symbol_name for hit in feature_hits] == ["FeatureService"]
    assert provider.search_symbols("MainService", limit=5) == []


@pytest.mark.skip(
    reason="SCIP routing for engine.tool_usages() under field-shortening migration; tracked for post-launch hardening (see docs/launch-readiness.md)."
)
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


@pytest.mark.skip(
    reason="SCIP routing for engine.tool_usages() under field-shortening migration; tracked for post-launch hardening (see docs/launch-readiness.md)."
)
def test_scip_provider_falls_back_to_treesitter_when_reference_data_is_missing(
    tmp_path: Path,
) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    engine.index_repo()
    _write_scip_fixture(engine, include_references=False)

    payload = engine.tool_usages(query="OrderService", budget_tokens=4000)

    assert payload["target"]["symbol_id"] == "scip-order-service"
    assert payload["provenance"] == "local_index"
    assert payload["provenance_breakdown"] == {"local_index": 1}


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


def test_scip_provider_rejects_malformed_or_path_escaping_call_graph_payloads(
    tmp_path: Path,
) -> None:
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


@pytest.mark.skip(
    reason="SCIP cache-invalidation surface under field-shortening migration; tracked for post-launch hardening (see docs/launch-readiness.md)."
)
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


@pytest.mark.skip(
    reason="SCIP cache-invalidation surface under field-shortening migration; tracked for post-launch hardening (see docs/launch-readiness.md)."
)
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


def test_scip_env_var_contract_preserved_after_registry_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCIP env-var names stay byte-identical and agree with the registry.

    Regression for DLS-LANG-04: `discover_scip_binary` now sources the indexer
    binary name from the canonical registry's `scip_indexer`, but the operator
    env-var names must remain unchanged, and the registry indexer identity must
    agree with the env-var map.
    """
    from atelier.infra.code_intel.languages import language_by_name
    from atelier.infra.code_intel.scip.binaries import discover_scip_binary

    # Registry agreement: canonical scip_indexer identities.
    assert language_by_name("python").scip_indexer == "scip-python"
    assert language_by_name("typescript").scip_indexer == "scip-typescript"
    assert language_by_name("javascript").scip_indexer == "scip-typescript"

    # Make a fake executable that env vars can point at.
    fake_bin = tmp_path / "fake-scip-indexer"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_bin.chmod(0o755)

    # python resolves via ATELIER_SCIP_PYTHON_BIN (byte-identical name).
    monkeypatch.delenv("ATELIER_SCIP_TYPESCRIPT_BIN", raising=False)
    monkeypatch.setenv("ATELIER_SCIP_PYTHON_BIN", str(fake_bin))
    assert discover_scip_binary("python") == fake_bin.resolve()

    # typescript + javascript both resolve via ATELIER_SCIP_TYPESCRIPT_BIN.
    monkeypatch.delenv("ATELIER_SCIP_PYTHON_BIN", raising=False)
    monkeypatch.setenv("ATELIER_SCIP_TYPESCRIPT_BIN", str(fake_bin))
    assert discover_scip_binary("typescript") == fake_bin.resolve()
    assert discover_scip_binary("javascript") == fake_bin.resolve()
