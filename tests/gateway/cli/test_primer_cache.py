from __future__ import annotations

from lemoncrow.pro.capabilities.owned_agent_session.primer_cache import cached_task_primer


def test_primer_cache_hits_only_while_workspace_fingerprint_matches(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    store = tmp_path / "store"
    workspace.mkdir()
    source = workspace / "parser.py"
    source.write_text("def parse_token():\n    return 'old'\n", encoding="utf-8")

    first = cached_task_primer("fix parse_token", workspace, store)
    second = cached_task_primer("fix parse_token", workspace, store)
    source.write_text("def parse_token():\n    return 'new value'\n", encoding="utf-8")
    third = cached_task_primer("fix parse_token", workspace, store)

    assert not first.hit
    assert second.hit
    assert second.text == first.text
    assert not third.hit
    assert third.fingerprint != second.fingerprint


def test_explicit_file_task_skips_broad_primer_and_workspace_fingerprint(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "auth.py").write_text("TOKEN = 1\n", encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("explicit-file task must skip broad primer work")

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.owned_agent_session.primer_cache.workspace_fingerprint",
        forbidden,
    )
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.owned_agent_session.primer_cache.build_task_primer",
        forbidden,
    )
    result = cached_task_primer(
        "Fix `auth.py`",
        workspace,
        tmp_path / "store",
        retrieval_mode="auto",
        optimization_mode="off",
    )

    assert result.base_primer_skipped
    assert result.text == ""
    assert not result.local_retrieval_invoked


def test_optimized_evidence_is_shadowed_enforced_and_globally_disabled(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    store = tmp_path / "store"
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
    task = "Find where authentication token is validated"

    shadow = cached_task_primer(
        task,
        workspace,
        store,
        retrieval_mode="force",
        optimization_mode="shadow",
    )
    assert shadow.local_retrieval_invoked
    assert shadow.local_retrieval_packet_ready
    assert not shadow.local_retrieval_applied
    assert "Local retrieval evidence packet" not in shadow.text

    enforced = cached_task_primer(
        task,
        workspace,
        store,
        retrieval_mode="force",
        optimization_mode="enforce",
    )
    assert enforced.local_retrieval_cache_hit
    assert enforced.local_retrieval_applied
    assert "Local retrieval evidence packet" in enforced.text

    disabled = cached_task_primer(
        task,
        workspace,
        store,
        retrieval_mode="force",
        optimization_mode="off",
    )
    assert not disabled.local_retrieval_invoked
    assert not disabled.local_retrieval_applied
