from __future__ import annotations

from lemoncrow.pro.capabilities.optimization.evidence_reuse import (
    VerificationReceipt,
    finalize_task_evidence,
    load_verified_evidence,
    stage_evidence_result,
)
from lemoncrow.pro.capabilities.owned_agent_session.primer_cache import cached_task_primer


def test_verified_read_evidence_reuses_only_while_all_fingerprints_match(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    root = tmp_path / "store"
    workspace.mkdir()
    source = workspace / "parser.py"
    source.write_text("def parse():\n    return 1\n", encoding="utf-8")
    (workspace / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    cached_task_primer("inspect parser", workspace, root)

    staged = stage_evidence_result(
        root,
        workspace,
        task="inspect parser",
        tool_name="read",
        args={"path": "parser.py:L1-L2"},
        result="parser.py:L1-L2\ndef parse():\n    return 1",
    )
    assert staged.staged
    finalized = finalize_task_evidence(
        root,
        workspace,
        task="inspect parser",
        receipt=VerificationReceipt(
            kind="read_only_completion",
            command="workspace fingerprint revalidation",
            ok=True,
            output_hash="abc",
        ),
    )
    assert finalized == 1

    reused = load_verified_evidence(root, workspace, task="inspect parser")
    assert reused.hit_count == 1
    assert "parser.py:L1-L2" in reused.text
    primer = cached_task_primer("inspect parser", workspace, root, optimization_mode="enforce")
    assert primer.evidence_hits == 1
    assert primer.evidence_applied
    assert "Verified cross-session evidence" in primer.text
    assert "read_only_completion receipt" in primer.text
    assert "workspace fingerprint revalidation" not in primer.text

    source.write_text("def parse():\n    return 2\n", encoding="utf-8")
    invalidated = load_verified_evidence(root, workspace, task="inspect parser")
    assert invalidated.hit_count == 0
    assert invalidated.invalidated_count == 1


def test_empty_lookup_and_finalize_skip_workspace_fingerprinting(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def forbidden(_workspace):
        raise AssertionError("no evidence rows must mean no workspace fingerprint")

    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.optimization.evidence_reuse._workspace_fingerprint",
        forbidden,
    )
    root = tmp_path / "store"
    assert load_verified_evidence(root, workspace, task="missing").hit_count == 0
    assert (
        finalize_task_evidence(
            root,
            workspace,
            task="missing",
            receipt=VerificationReceipt(kind="read_only_completion", command="verify", ok=True),
        )
        == 0
    )


def test_dependency_change_invalidates_verified_evidence(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    root = tmp_path / "store"
    workspace.mkdir()
    (workspace / "parser.py").write_text("value = 1\n", encoding="utf-8")
    lock = workspace / "uv.lock"
    lock.write_text("version = 1\n", encoding="utf-8")
    assert stage_evidence_result(
        root,
        workspace,
        task="inspect",
        tool_name="read",
        args={"path": "parser.py:L1"},
        result="parser.py:L1 value = 1",
    ).staged
    assert (
        finalize_task_evidence(
            root,
            workspace,
            task="inspect",
            receipt=VerificationReceipt(kind="test", command="uv run pytest -q", ok=True),
        )
        == 1
    )

    lock.write_text("version = 2\n", encoding="utf-8")
    reused = load_verified_evidence(root, workspace, task="inspect")
    assert reused.hit_count == 0
    assert reused.invalidated_count == 1


def test_mutation_and_shell_outputs_are_never_staged(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "parser.py").write_text("value = 1\n", encoding="utf-8")
    for tool_name in ("edit", "bash"):
        staged = stage_evidence_result(
            tmp_path / "store",
            workspace,
            task="fix",
            tool_name=tool_name,
            args={"path": "parser.py"},
            result="value = 1",
        )
        assert not staged.staged
