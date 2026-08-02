from __future__ import annotations

import sqlite3

from lemoncrow.pro.capabilities.optimization.local_retrieval import (
    cached_local_evidence_packet,
    retrieval_eligible,
)


def _workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(8):
        (workspace / f"module_{index}.py").write_text(
            f"def helper_{index}():\n    return {index}\n",
            encoding="utf-8",
        )
    (workspace / "auth.py").write_text(
        "def validate_authentication_token(token):\n"
        "    if not token:\n"
        "        raise ValueError('missing token')\n"
        "    return token\n",
        encoding="utf-8",
    )
    return workspace


def test_obvious_explicit_file_task_skips_micro_agent_without_scanning(tmp_path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    eligible, reason = retrieval_eligible("Fix `auth.py`", workspace, "auto")
    assert not eligible
    assert "explicit source path" in reason

    def forbidden(_workspace):
        raise AssertionError("explicit-file gate must run before workspace fingerprinting")

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.optimization.local_retrieval._workspace_fingerprint",
        forbidden,
    )
    result = cached_local_evidence_packet(
        tmp_path / "store",
        "Fix `auth.py`",
        workspace,
        mode="auto",
        model="ollama/qwen2.5-coder:7b",
    )
    assert not result.invoked
    assert result.model_calls == 0


def test_ambiguous_task_returns_cached_exact_source_hashed_packet(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    first = cached_local_evidence_packet(
        tmp_path / "store",
        "Find where authentication token is validated",
        workspace,
        mode="auto",
        max_turns=3,
    )
    second = cached_local_evidence_packet(
        tmp_path / "store",
        "Find where authentication token is validated",
        workspace,
        mode="auto",
        max_turns=3,
    )

    assert first.invoked
    assert first.text
    assert "auth.py:L1-" in first.text
    assert "sha256=" in first.text
    assert 1 <= first.turns <= 3
    assert first.model_calls == 0
    assert second.cache_hit
    assert second.text == first.text


def test_nonlocal_model_is_rejected_without_network_or_provider_call(tmp_path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("non-local model must never be called")

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.optimization.local_retrieval._local_model_query",
        forbidden,
    )
    result = cached_local_evidence_packet(
        tmp_path / "store",
        "Find where authentication token is validated",
        workspace,
        mode="force",
        model="openai/gpt-5",
    )
    assert result.invoked
    assert not result.used_model
    assert result.model_calls == 0


def test_allowed_local_planner_is_bounded_and_cannot_return_an_answer(tmp_path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    calls: list[str] = []

    def planner(_model, *, query, **_kwargs):
        calls.append(query)
        if len(calls) == 1:
            return "validate_authentication_token", False
        return None, True

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.optimization.local_retrieval._local_model_query",
        planner,
    )
    result = cached_local_evidence_packet(
        tmp_path / "store",
        "Investigate the session behavior",
        workspace,
        mode="force",
        model="ollama/qwen2.5-coder:7b",
        max_turns=3,
    )

    assert result.used_model
    assert result.model_calls == len(calls)
    assert result.turns == 2
    assert result.span_count > 0
    assert "auth.py:L1-" in result.text
    assert calls == ["investigate session behavior"]


def test_symlinked_source_outside_workspace_is_never_read(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside_secret.py"
    outside.write_text("LEAKED_LOCAL_RETRIEVAL_SECRET = True\n", encoding="utf-8")
    (workspace / "linked_secret.py").symlink_to(outside)

    result = cached_local_evidence_packet(
        tmp_path / "store",
        "Find the leaked local retrieval secret",
        workspace,
        mode="force",
    )

    assert "LEAKED_LOCAL_RETRIEVAL_SECRET" not in result.text
    assert "linked_secret.py" not in result.text


def test_local_packet_cache_is_bounded(tmp_path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    store = tmp_path / "store"
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.optimization.local_retrieval._MAX_CACHE_PACKETS",
        2,
    )
    for index in range(3):
        cached_local_evidence_packet(
            store,
            f"Find authentication token validation variant {index}",
            workspace,
            mode="force",
        )

    with sqlite3.connect(store / "cache" / "local-retrieval.sqlite3") as connection:
        count = connection.execute("SELECT COUNT(*) FROM local_retrieval_packets").fetchone()[0]
    assert count == 2


def test_local_packet_cache_invalidates_when_workspace_changes(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    store = tmp_path / "store"
    task = "Find where authentication token is validated"
    first = cached_local_evidence_packet(store, task, workspace, mode="force")
    (workspace / "auth.py").write_text(
        "def validate_authentication_token(token):\n    return token.strip()\n",
        encoding="utf-8",
    )
    second = cached_local_evidence_packet(store, task, workspace, mode="force")

    assert first.text
    assert second.text
    assert not second.cache_hit
    assert second.text != first.text
