from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from atelier.core.capabilities.code_context import CodeContextEngine
from atelier.core.capabilities.memory_arbitration import ArbitrationDecision
from atelier.core.foundation.models import Trace
from atelier.core.foundation.store import ContextStore
from atelier.gateway.adapters import mcp_server
from atelier.gateway.adapters.mcp_server import TOOLS, _handle
from atelier.infra.storage.memory_store import MemorySidecarUnavailable


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    response = _handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
    )
    assert response is not None
    return cast(dict[str, Any], response)


def _payload(response: dict[str, Any]) -> Any:
    assert "result" in response, response
    return json.loads(response["result"]["content"][0]["text"])


def _memory_args(op: str, **kwargs: Any) -> dict[str, Any]:
    return {"op": op, **kwargs}


def _write_symbol_recall_fixture(workspace: Path) -> str:
    (workspace / "src").mkdir(parents=True, exist_ok=True)
    (workspace / "tests").mkdir(parents=True, exist_ok=True)
    (workspace / "docs" / "decisions").mkdir(parents=True, exist_ok=True)
    (workspace / "src" / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "src" / "auth.py").write_text(
        "class AuthService:\n"
        "    def verify_session(self, token: str) -> bool:\n"
        "        return token.startswith('session:')\n",
        encoding="utf-8",
    )
    (workspace / "tests" / "test_auth.py").write_text(
        "from src.auth import AuthService\n\n"
        "def test_verify_session_accepts_session_tokens() -> None:\n"
        "    assert AuthService().verify_session('session:ok') is True\n",
        encoding="utf-8",
    )
    (workspace / "docs" / "decisions" / "001-session-auth.md").write_text(
        "# Session auth\n\nAuthService.verify_session is the session token validation seam.\n",
        encoding="utf-8",
    )
    engine = CodeContextEngine(workspace)
    engine.index_repo()
    symbol_id = engine.search_symbols("AuthService.verify_session", limit=1)[0].symbol_id
    return symbol_id


@pytest.fixture()
def mcp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".atelier"
    monkeypatch.setenv("ATELIER_ROOT", str(root))
    monkeypatch.setenv("ATELIER_DEV_MODE", "1")
    monkeypatch.setattr(mcp_server, "_REMOTE_TOOLS", frozenset())
    mcp_server._current_ledger = None
    mcp_server._realtime_ctx = None
    return root


def test_memory_tools_are_registered() -> None:
    assert "memory" in TOOLS


def test_memory_upsert_and_get_round_trip(mcp_root: Path) -> None:
    _ = mcp_root
    result = _payload(
        _call(
            "memory",
            _memory_args(
                "block_upsert",
                agent_id="atelier:code",
                label="scratch",
                value="hello",
                pinned=True,
                metadata={"source": "test"},
            ),
        )
    )
    assert result["version"] == 1

    block = _payload(
        _call(
            "memory",
            _memory_args("block_get", agent_id="atelier:code", label="scratch"),
        )
    )
    assert block["id"] == result["id"]
    assert block["value"] == "hello"
    assert block["pinned"] is True


def test_memory_get_returns_null_on_miss(mcp_root: Path) -> None:
    _ = mcp_root
    # Updated: it returns None or null if missing, not an error payload
    assert _payload(_call("memory", _memory_args("block_get", agent_id="atelier:code", label="missing"))) is None


def test_memory_stale_version_maps_to_409(mcp_root: Path) -> None:
    _ = mcp_root
    _payload(
        _call(
            "memory",
            _memory_args("block_upsert", agent_id="atelier:code", label="scratch", value="v1"),
        )
    )
    _payload(
        _call(
            "memory",
            _memory_args(
                "block_upsert",
                agent_id="atelier:code",
                label="scratch",
                value="v2",
                expected_version=1,
            ),
        )
    )
    response = _call(
        "memory",
        _memory_args(
            "block_upsert",
            agent_id="atelier:code",
            label="scratch",
            value="stale",
            expected_version=1,
        ),
    )
    assert response["error"]["code"] == 409


def test_memory_upsert_returns_and_applies_arbitration(monkeypatch: pytest.MonkeyPatch, mcp_root: Path) -> None:
    _ = mcp_root
    first = _payload(
        _call(
            "memory",
            _memory_args(
                "block_upsert",
                agent_id="atelier:code",
                label="style",
                value="prefer compact patches",
            ),
        )
    )

    monkeypatch.setattr(
        "atelier.core.capabilities.memory_arbitration.arbitrate",
        lambda block, store, embedder: ArbitrationDecision(
            op="UPDATE",
            target_block_id=first["id"],
            merged_value="prefer compact scoped patches",
            reason="refines existing style memory",
        ),
    )

    result = _payload(
        _call(
            "memory",
            _memory_args(
                "block_upsert",
                agent_id="atelier:code",
                label="style",
                value="prefer compact scoped edits",
            ),
        )
    )

    assert result["arbitration"]["op"] == "UPDATE"
    stored = _payload(
        _call(
            "memory",
            _memory_args("block_get", agent_id="atelier:code", label="style"),
        )
    )
    assert stored["value"] == "prefer compact scoped patches"


def test_memory_sidecar_unavailable_maps_to_503(monkeypatch: pytest.MonkeyPatch, mcp_root: Path) -> None:
    _ = mcp_root

    class DownStore:
        def get_block(self, agent_id: str, label: str) -> None:
            _ = (agent_id, label)
            raise MemorySidecarUnavailable("sidecar down")

    monkeypatch.setattr(mcp_server, "_memory_store", lambda: DownStore())
    response = _call(
        "memory",
        _memory_args("block_upsert", agent_id="atelier:code", label="scratch", value="hello"),
    )
    assert response["error"]["code"] == 503


def test_memory_upsert_rejects_likely_secret_leakage(mcp_root: Path) -> None:
    _ = mcp_root
    response = _call(
        "memory",
        _memory_args(
            "block_upsert",
            agent_id="atelier:code",
            label="leak",
            value="AKIAIOSFODNN7EXAMPLE secretvalue",
        ),
    )
    assert "error" in response
    assert "likely secret leakage" in response["error"]["message"]


def test_memory_recall_symbol_returns_fused_bundle_on_existing_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mcp_root: Path,
) -> None:
    _ = mcp_root
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    symbol_id = _write_symbol_recall_fixture(tmp_path)
    _payload(
        _call(
            "memory",
            _memory_args(
                "block_upsert",
                agent_id="shared",
                label="edits/auth-verify-session",
                value="Remember the stale-session regression.",
                metadata={"symbol_id": symbol_id},
            ),
        )
    )
    _payload(
        _call(
            "memory",
            _memory_args(
                "archive",
                agent_id="shared",
                text="AuthService.verify_session rejected stale sessions in incident review.",
                source="user",
                tags=[f"symbol:{symbol_id}", "incident"],
            ),
        )
    )

    payload = _payload(
        _call(
            "memory",
            _memory_args("recall_symbol", agent_id="shared", query="AuthService.verify_session"),
        )
    )

    assert payload["included"] == ["definition", "memory"]
    assert payload["definition"]["qualified_name"] == "AuthService.verify_session"
    assert [item["item_type"] for item in payload["memory"]] == ["block", "passage"]
    assert "traces" not in payload
    assert "tests" not in payload


def test_memory_recall_symbol_explicit_includes_widen_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mcp_root: Path,
) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    symbol_id = _write_symbol_recall_fixture(tmp_path)
    trace_store = ContextStore(mcp_root)
    trace_store.init()
    trace_store.record_trace(
        Trace(
            id=Trace.make_id("recall trace", "gsd-executor"),
            agent="gsd-executor",
            domain="code-intel",
            task="Validate AuthService.verify_session recall",
            status="success",
            files_touched=["src/auth.py"],
            output_summary="Touched AuthService.verify_session during token validation.",
        )
    )
    _payload(
        _call(
            "memory",
            _memory_args(
                "archive",
                agent_id="shared",
                text="AuthService.verify_session rejected stale sessions in incident review.",
                source="user",
                tags=[f"symbol:{symbol_id}", "incident"],
            ),
        )
    )

    payload = _payload(
        _call(
            "memory",
            _memory_args(
                "recall_symbol",
                agent_id="shared",
                query="AuthService.verify_session",
                include=["traces", "decisions", "tests"],
                budget_tokens=1200,
            ),
        )
    )

    assert payload["included"] == ["definition", "memory", "traces", "decisions", "tests"]
    assert payload["traces"][0]["task"] == "Validate AuthService.verify_session recall"
    assert payload["decisions"][0]["path"] == "docs/decisions/001-session-auth.md"
    assert payload["tests"][0]["file_path"] == "tests/test_auth.py"


def test_memory_recall_symbol_does_not_register_new_top_level_or_code_recall_tools() -> None:
    assert "recall_symbol" not in TOOLS
    assert "recall_symbol" in json.dumps(TOOLS["memory"]["inputSchema"])
    assert "recall" not in json.dumps(TOOLS["code"]["inputSchema"])
