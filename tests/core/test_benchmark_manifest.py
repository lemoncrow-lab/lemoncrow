from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.benchmark_manifest import (
    build_codebench_manifest,
    build_runtime_attribution,
    build_terminalbench_manifest,
    runtime_policy_fingerprint,
)


def test_runtime_policy_fingerprint_is_stable() -> None:
    left = {"b": {"mode": "shadow"}, "a": {"version": "1"}}
    right = {"a": {"version": "1"}, "b": {"mode": "shadow"}}

    assert runtime_policy_fingerprint(left) == runtime_policy_fingerprint(right)
    assert runtime_policy_fingerprint(left) != runtime_policy_fingerprint({"a": {"version": "2"}})


def test_codebench_runtime_attribution_names_only_qualified_arms(tmp_path: Path) -> None:
    attribution = build_runtime_attribution(
        arms=["baseline", "lemoncrow-control", "lemoncrow-shadow"],
        cli_driver="claude",
    )

    assert attribution["qualification_state"] == "shadow_only"
    assert attribution["enforcement_qualified"] is False
    assert attribution["a3_available"] is False
    assert attribution["arms"]["baseline"]["role"] == "A0"
    assert attribution["arms"]["lemoncrow-control"]["role"] == "A1"
    assert attribution["arms"]["lemoncrow-control"]["runtime_policy"]["bounded_evidence_resolution"]["mode"] == "off"
    assert attribution["arms"]["lemoncrow-shadow"]["role"] == "A2"
    assert attribution["arms"]["lemoncrow-shadow"]["runtime_policy"]["bounded_evidence_resolution"]["mode"] == "shadow"
    assert attribution["arms"]["lemoncrow-shadow"]["host_control_tier"] == "H"

    manifest = build_codebench_manifest(
        tasks=[{"id": "t1"}],
        arms=["baseline", "lemoncrow-control", "lemoncrow-shadow"],
        reps=1,
        model="same-model",
        cli_driver="claude",
        timeout=60,
        jobs=1,
        parallel_scope="task",
        codebench_tasks_dir=tmp_path,
        bridge_command=None,
        runtime_attribution=attribution,
    )
    assert manifest["runtime_attribution"] == attribution
    assert manifest["protocol"]["matched_fields"]["model"] == "same-model"


def test_owned_driver_and_terminalbench_control_tiers_are_explicit(tmp_path: Path) -> None:
    owned = build_runtime_attribution(arms=["baseline", "lemoncrow-control"], cli_driver="lemoncrow-run")
    assert owned["arms"]["lemoncrow-control"]["host_control_tier"] == "O"

    manifest = build_terminalbench_manifest(
        task_ids=["hello"],
        modes=["off", "on"],
        rep=1,
        model="m",
        provider="p",
        dataset_meta={"name": "tb", "version": "1"},
        tasks_path=tmp_path,
    )
    attribution = manifest["runtime_attribution"]
    assert attribution["arms"]["off"]["role"] == "A0"
    assert attribution["arms"]["on"]["role"] == "A1"
    assert attribution["arms"]["on"]["host_control_tier"] == "S"


def test_a3_is_present_only_when_the_explicit_candidate_arm_is_requested() -> None:
    attribution = build_runtime_attribution(
        arms=["lemoncrow-control", "lemoncrow-shadow", "lemoncrow-candidate"],
        cli_driver="claude",
    )

    assert attribution["a3_available"] is True
    assert attribution["enforcement_qualified"] is False
    assert attribution["qualification_state"] == "candidate_experiment"
    candidate = attribution["arms"]["lemoncrow-candidate"]
    assert candidate["role"] == "A3"
    assert candidate["runtime_policy"]["bounded_evidence_resolution"]["mode"] == "experiment"
    assert candidate["runtime_policy"]["bounded_evidence_resolution"]["enforcement_qualified"] is False


def test_external_comparator_is_not_mislabeled_as_lemoncrow_runtime() -> None:
    comparator = {
        "other-tool": {
            "manifest_fingerprint": "abc123",
            "repo": "https://example.invalid/other-tool.git",
            "ref": "v1",
        }
    }
    attribution = build_runtime_attribution(
        arms=["baseline", "lemoncrow", "other-tool"],
        cli_driver="claude",
        comparators=comparator,
    )

    external = attribution["arms"]["other-tool"]
    assert external["role"] == "external"
    assert external["host_control_tier"] == "external"
    assert external["runtime_policy"] is None
    assert external["manifest_fingerprint"] == "abc123"
    assert attribution["comparators"] == comparator


def test_codebench_manifest_labels_candidate_as_lemoncrow_agent(tmp_path: Path) -> None:
    attribution = build_runtime_attribution(
        arms=["baseline", "lemoncrow-control", "lemoncrow-candidate"],
        cli_driver="claude",
    )
    manifest = build_codebench_manifest(
        tasks=[{"id": "task-1"}],
        arms=["baseline", "lemoncrow-control", "lemoncrow-candidate"],
        reps=1,
        model="claude-opus-4-8",
        cli_driver="claude",
        timeout=120,
        jobs=1,
        parallel_scope="task",
        codebench_tasks_dir=tmp_path,
        bridge_command=None,
        runtime_attribution=attribution,
    )

    assert manifest["protocol"]["arm_agents"]["lemoncrow-candidate"] == "lemoncrow:code"


def test_codebench_manifest_advertises_runtime_policy_proof_artifacts(tmp_path: Path) -> None:
    attribution = build_runtime_attribution(
        arms=["lemoncrow-control", "lemoncrow-candidate"],
        cli_driver="claude",
    )
    manifest = build_codebench_manifest(
        tasks=[{"id": "task-1"}],
        arms=["lemoncrow-control", "lemoncrow-candidate"],
        reps=1,
        model="sonnet",
        cli_driver="claude",
        timeout=120,
        jobs=1,
        parallel_scope="task",
        codebench_tasks_dir=tmp_path,
        bridge_command=None,
        runtime_attribution=attribution,
    )

    assert manifest["artifacts"]["runtime_policy_gate_json"] == "runtime-policy-gate.json"
    assert manifest["artifacts"]["runtime_policy_jsonl_glob"] == "*.runtime-policy.jsonl"
