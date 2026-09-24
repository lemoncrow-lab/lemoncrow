from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

import lemoncrow.core.capabilities.benchmark_protocol as BENCH_PROTOCOL
from lemoncrow.core.capabilities.benchmark_gate import (
    evaluate_codebench_comparison_gates,
    evaluate_codebench_gate,
    evaluate_runtime_policy_gate,
)
from lemoncrow.core.capabilities.benchmark_manifest import (
    build_codebench_manifest,
    build_runtime_attribution,
)
from lemoncrow.core.capabilities.benchmark_protocol import (
    codebench_protocol_commands,
    codebench_protocol_summary,
    load_codebench_protocol,
    verify_codebench_manifest,
    verify_codebench_protocol_hosts,
    verify_codebench_publication,
    verify_codebench_run,
)


def _catalog(repo: str, ref: str) -> list[dict[str, object]]:
    return [
        {
            "id": "task-a",
            "language": "python",
            "task_dir": "task-a",
            "source": ["repo", repo, ref],
        }
    ]


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, list[dict[str, object]]]:
    ref = "a" * 40
    repo = "https://example.invalid/repo"
    task_source = tmp_path / "tasks-root"
    prompt_dir = task_source / "tasks" / "task-a"
    prompt_dir.mkdir(parents=True)
    prompt = prompt_dir / "prompt.md"
    prompt.write_text("find the thing\n", encoding="utf-8")
    digest = hashlib.sha256(prompt.read_bytes()).hexdigest()

    competitor = tmp_path / "competitor.json"
    competitor.write_text(
        json.dumps(
            {
                "name": "other-tool",
                "repo": "https://example.invalid/tool",
                "ref": "b" * 40,
                "mcp": {"command": "tool", "args": ["mcp"]},
            }
        ),
        encoding="utf-8",
    )
    competitor_sha256 = hashlib.sha256(competitor.read_bytes()).hexdigest()
    protocol = tmp_path / "protocol.json"
    protocol.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "proof-v1",
                "frozen_at": "2026-09-23",
                "tasks": [
                    {
                        "id": "task-a",
                        "language": "python",
                        "repo": repo,
                        "ref": ref,
                        "prompt_sha256": digest,
                    }
                ],
                "runs": [
                    {
                        "id": "qualify",
                        "kind": "qualification",
                        "cli_driver": "claude",
                        "model": "claude-opus-4-8",
                        "arms": [
                            "baseline",
                            "lemoncrow-control",
                            "lemoncrow-shadow",
                            "lemoncrow-candidate",
                        ],
                        "reps": 5,
                        "timeout": 1800,
                        "jobs": 1,
                        "parallel_scope": "task",
                        "mode": "cost",
                        "require_pass": True,
                        "judge": True,
                        "judge_model": "claude-opus-4-8",
                        "cli_version": "claude-test",
                        "judge_cli_version": "claude-test",
                    },
                    {
                        "id": "generalize",
                        "kind": "generalization",
                        "cli_driver": "codex",
                        "model": "gpt-5-codex",
                        "arms": ["baseline", "lemoncrow-control", "lemoncrow-candidate"],
                        "reps": 3,
                        "timeout": 1800,
                        "jobs": 1,
                        "parallel_scope": "task",
                        "mode": "cost",
                        "require_pass": True,
                        "judge": True,
                        "judge_model": "claude-opus-4-8",
                        "cli_version": "codex-test",
                        "judge_cli_version": "claude-test",
                    },
                    {
                        "id": "external",
                        "kind": "external",
                        "cli_driver": "claude",
                        "model": "claude-opus-4-8",
                        "arms": ["baseline", "lemoncrow"],
                        "reps": 5,
                        "timeout": 1800,
                        "jobs": 1,
                        "parallel_scope": "task",
                        "mode": "cost",
                        "require_pass": False,
                        "judge": True,
                        "judge_model": "claude-opus-4-8",
                        "cli_version": "claude-test",
                        "judge_cli_version": "claude-test",
                        "competitors": [
                            {
                                "path": "competitor.json",
                                "sha256": competitor_sha256,
                            }
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return protocol, task_source, _catalog(repo, ref)


def _write_complete_qualification_run(
    run_dir: Path,
    *,
    protocol,
    task_source: Path,
) -> None:
    run = next(item for item in protocol.runs if item.id == "qualify")
    task = protocol.tasks[0]
    manifest = build_codebench_manifest(
        tasks=[
            {
                "id": task.id,
                "language": task.language,
                "task_dir": task.id,
                "source": ["repo", task.repo, task.ref],
                "prompt_sha256": task.prompt_sha256,
            }
        ],
        arms=list(run.arms),
        reps=run.reps,
        model=run.model,
        cli_driver=run.cli_driver,
        timeout=run.timeout,
        jobs=run.jobs,
        parallel_scope=run.parallel_scope,
        codebench_tasks_dir=task_source,
        bridge_command=None,
        mode=run.mode,
        judge=run.judge,
        judge_model=run.judge_model,
        cli_version=run.cli_version,
        judge_cli_version=run.judge_cli_version,
        runtime_attribution=build_runtime_attribution(
            arms=list(run.arms),
            cli_driver=run.cli_driver,
        ),
    )
    run_dir.mkdir(parents=True)
    (run_dir / "benchmark-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    costs = {
        "baseline": 2.0,
        "lemoncrow-control": 1.7,
        "lemoncrow-shadow": 1.6,
        "lemoncrow-candidate": 1.4,
    }
    rows = []
    for rep in range(run.reps):
        for arm in run.arms:
            rows.append(
                {
                    "task": task.id,
                    "arm": arm,
                    "rep": rep,
                    "ok": True,
                    "valid": True,
                    "correct": True,
                    "cost_usd": costs[arm],
                    "runtime_policy_events": 1 if arm in {"lemoncrow-control", "lemoncrow-candidate"} else 0,
                    "runtime_policy_experiment_events": 1 if arm == "lemoncrow-candidate" else 0,
                    "runtime_policy_expansions": 1 if arm == "lemoncrow-candidate" and rep == 0 else 0,
                }
            )
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    with (run_dir / "pairwise_quality.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "task",
                "rep",
                "baseline_arm",
                "candidate_arm",
                "judged",
                "candidate_at_least_baseline",
                "quality_adjusted_saved_usd",
            ],
        )
        writer.writeheader()
        for rep in range(run.reps):
            for candidate in ("lemoncrow-control", "lemoncrow-shadow", "lemoncrow-candidate"):
                writer.writerow(
                    {
                        "task": task.id,
                        "rep": rep,
                        "baseline_arm": "baseline",
                        "candidate_arm": candidate,
                        "judged": "True",
                        "candidate_at_least_baseline": "True",
                        "quality_adjusted_saved_usd": "0.1",
                    }
                )
            writer.writerow(
                {
                    "task": task.id,
                    "rep": rep,
                    "baseline_arm": "lemoncrow-control",
                    "candidate_arm": "lemoncrow-candidate",
                    "judged": "True",
                    "candidate_at_least_baseline": "True",
                    "quality_adjusted_saved_usd": "0.1",
                }
            )

    for name in ("results.csv", "summary.csv", "report.txt"):
        (run_dir / name).write_text("fixture\n", encoding="utf-8")

    benchmark_gate = evaluate_codebench_gate(
        run_dir,
        baseline_arm="baseline",
        candidate_arm="lemoncrow-control",
        mode="cost",
    )
    comparison_gate = evaluate_codebench_comparison_gates(
        run_dir,
        baseline_arm="baseline",
        candidate_arms=["lemoncrow-control", "lemoncrow-shadow", "lemoncrow-candidate"],
        mode="cost",
    )
    runtime_gate = evaluate_runtime_policy_gate(
        run_dir,
        control_arm="lemoncrow-control",
        candidate_arm="lemoncrow-candidate",
    )
    (run_dir / "benchmark-gate.json").write_text(json.dumps(benchmark_gate), encoding="utf-8")
    (run_dir / "benchmark-comparison-gates.json").write_text(json.dumps(comparison_gate), encoding="utf-8")
    (run_dir / "runtime-policy-gate.json").write_text(json.dumps(runtime_gate), encoding="utf-8")

    evidence = {
        "suite": "codebench",
        "commit_under_test": {"commit": "abc123", "dirty": False},
        "artifacts": {
            "results_jsonl": {"path": str(run_dir / "results.jsonl"), "exists": True},
            "results_csv": {"path": str(run_dir / "results.csv"), "exists": True},
            "summary_csv": {"path": str(run_dir / "summary.csv"), "exists": True},
            "pairwise_quality_csv": {"path": str(run_dir / "pairwise_quality.csv"), "exists": True},
            "report_txt": {"path": str(run_dir / "report.txt"), "exists": True},
        },
    }
    (run_dir / "benchmark-evidence.json").write_text(json.dumps(evidence), encoding="utf-8")


def test_frozen_protocol_validates_and_renders_zero_spend_commands(tmp_path: Path) -> None:
    path, task_source, catalog = _write_fixture(tmp_path)

    protocol = load_codebench_protocol(path, task_catalog=catalog, task_source_dir=task_source)
    commands = codebench_protocol_commands(protocol, task_source_dir=task_source)
    output_commands = codebench_protocol_commands(
        protocol,
        task_source_dir=task_source,
        output_root=tmp_path / "bundle",
    )

    assert protocol.id == "proof-v1"
    assert len(protocol.fingerprint) == 64
    assert [run.id for run in protocol.runs] == ["qualify", "generalize", "external"]
    external = dict(commands)["external"]
    summary = codebench_protocol_summary(protocol)
    assert summary["total_agent_rows"] == 44
    assert summary["pairwise_judge_comparisons"] == 39
    assert external[:3] == ("lc", "benchmark", "codebench")
    assert "--competitor" in external
    assert "--require-pass" not in external
    assert "--task-source-dir" in external
    qualify_out = dict(output_commands)["qualify"]
    assert qualify_out[qualify_out.index("--out") + 1] == str((tmp_path / "bundle" / "qualify").resolve())


def test_completed_manifest_is_verified_against_frozen_run(tmp_path: Path) -> None:
    path, task_source, catalog = _write_fixture(tmp_path)
    protocol = load_codebench_protocol(path, task_catalog=catalog, task_source_dir=task_source)
    task = protocol.tasks[0]
    manifest = build_codebench_manifest(
        tasks=[
            {
                "id": task.id,
                "language": task.language,
                "task_dir": task.id,
                "source": ["repo", task.repo, task.ref],
                "prompt_sha256": task.prompt_sha256,
            }
        ],
        arms=["baseline", "lemoncrow-control", "lemoncrow-shadow", "lemoncrow-candidate"],
        reps=5,
        model="claude-opus-4-8",
        cli_driver="claude",
        timeout=1800,
        jobs=1,
        parallel_scope="task",
        codebench_tasks_dir=task_source,
        bridge_command=None,
        mode="cost",
        judge=True,
        judge_model="claude-opus-4-8",
        cli_version="claude-test",
        judge_cli_version="claude-test",
        runtime_attribution=build_runtime_attribution(
            arms=["baseline", "lemoncrow-control", "lemoncrow-shadow", "lemoncrow-candidate"],
            cli_driver="claude",
        ),
    )

    verification = verify_codebench_manifest(protocol, run_id="qualify", manifest=manifest)

    assert verification["passed"] is True
    assert verification["reasons"] == []

    manifest["protocol"]["matched_fields"]["model"] = "claude-other"
    verification = verify_codebench_manifest(protocol, run_id="qualify", manifest=manifest)
    assert verification["passed"] is False
    assert any("model differs" in reason for reason in verification["reasons"])


def test_external_manifest_verification_includes_competitor_bytes(tmp_path: Path) -> None:
    path, task_source, catalog = _write_fixture(tmp_path)
    protocol = load_codebench_protocol(path, task_catalog=catalog, task_source_dir=task_source)
    task = protocol.tasks[0]
    competitor = protocol.runs[2].competitors[0]
    attribution = build_runtime_attribution(
        arms=["baseline", "lemoncrow", competitor.name],
        cli_driver="claude",
        comparators={
            competitor.name: {
                "repo": competitor.repo,
                "ref": competitor.ref,
                "manifest_sha256": competitor.sha256,
                "manifest_fingerprint": "test",
            }
        },
    )
    manifest = build_codebench_manifest(
        tasks=[
            {
                "id": task.id,
                "language": task.language,
                "task_dir": task.id,
                "source": ["repo", task.repo, task.ref],
                "prompt_sha256": task.prompt_sha256,
            }
        ],
        arms=["baseline", "lemoncrow", competitor.name],
        reps=5,
        model="claude-opus-4-8",
        cli_driver="claude",
        timeout=1800,
        jobs=1,
        parallel_scope="task",
        codebench_tasks_dir=task_source,
        bridge_command=None,
        mode="cost",
        judge=True,
        judge_model="claude-opus-4-8",
        cli_version="claude-test",
        judge_cli_version="claude-test",
        runtime_attribution=attribution,
    )

    verification = verify_codebench_manifest(protocol, run_id="external", manifest=manifest)

    assert verification["passed"] is True
    manifest["runtime_attribution"]["comparators"][competitor.name]["manifest_sha256"] = "0" * 64
    verification = verify_codebench_manifest(protocol, run_id="external", manifest=manifest)
    assert verification["passed"] is False
    assert any("manifest hash differs" in reason for reason in verification["reasons"])


def test_complete_qualification_run_verification_recomputes_gates(tmp_path: Path) -> None:
    path, task_source, catalog = _write_fixture(tmp_path)
    protocol = load_codebench_protocol(path, task_catalog=catalog, task_source_dir=task_source)
    run_dir = tmp_path / "run"
    _write_complete_qualification_run(run_dir, protocol=protocol, task_source=task_source)

    verification = verify_codebench_run(protocol, run_id="qualify", run_dir=run_dir)

    assert verification["passed"] is True
    assert verification["details"]["expected_rows"] == 20
    assert verification["details"]["observed_rows"] == 20
    assert verification["details"]["runtime_policy_gate_passed"] is True


def test_complete_run_verification_rejects_stale_green_gate(tmp_path: Path) -> None:
    path, task_source, catalog = _write_fixture(tmp_path)
    protocol = load_codebench_protocol(path, task_catalog=catalog, task_source_dir=task_source)
    run_dir = tmp_path / "run"
    _write_complete_qualification_run(run_dir, protocol=protocol, task_source=task_source)

    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["arm"] == "lemoncrow-control":
            row["cost_usd"] = 3.0
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    verification = verify_codebench_run(protocol, run_id="qualify", run_dir=run_dir)

    assert verification["passed"] is False
    assert any("stored benchmark gate is stale" in reason for reason in verification["reasons"])
    assert any("benchmark quality/cost gate did not pass" in reason for reason in verification["reasons"])


def test_complete_run_verification_rejects_missing_or_dirty_evidence(tmp_path: Path) -> None:
    path, task_source, catalog = _write_fixture(tmp_path)
    protocol = load_codebench_protocol(path, task_catalog=catalog, task_source_dir=task_source)
    run_dir = tmp_path / "run"
    _write_complete_qualification_run(run_dir, protocol=protocol, task_source=task_source)

    evidence = json.loads((run_dir / "benchmark-evidence.json").read_text(encoding="utf-8"))
    evidence["commit_under_test"]["dirty"] = True
    (run_dir / "results.csv").unlink()
    (run_dir / "benchmark-evidence.json").write_text(json.dumps(evidence), encoding="utf-8")

    verification = verify_codebench_run(protocol, run_id="qualify", run_dir=run_dir)

    assert verification["passed"] is False
    assert any("checkout was dirty" in reason for reason in verification["reasons"])
    assert any(
        "required benchmark artifact is missing on disk: results_csv" in reason for reason in verification["reasons"]
    )


def test_protocol_host_preflight_requires_frozen_cli_versions(tmp_path: Path) -> None:
    path, task_source, catalog = _write_fixture(tmp_path)
    protocol = load_codebench_protocol(path, task_catalog=catalog, task_source_dir=task_source)

    passing = verify_codebench_protocol_hosts(
        protocol,
        current_versions={"claude": "claude-test", "codex": "codex-test"},
    )
    assert passing["passed"] is True

    failing = verify_codebench_protocol_hosts(
        protocol,
        current_versions={"claude": "claude-test", "codex": "codex-new"},
    )
    assert failing["passed"] is False
    assert any("codex version differs" in reason for reason in failing["reasons"])


def test_publication_verification_requires_all_runs_on_same_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, task_source, catalog = _write_fixture(tmp_path)
    protocol = load_codebench_protocol(path, task_catalog=catalog, task_source_dir=task_source)
    run_root = tmp_path / "bundle"
    for run in protocol.runs:
        (run_root / run.id).mkdir(parents=True)

    monkeypatch.setattr(
        BENCH_PROTOCOL,
        "verify_codebench_protocol_hosts",
        lambda protocol, current_versions: {
            "passed": True,
            "reasons": [],
            "current_versions": dict(current_versions),
            "checks": [],
        },
    )

    commits = {run.id: "same-commit" for run in protocol.runs}

    def _verify(protocol, *, run_id: str, run_dir: Path):
        return {
            "protocol_id": protocol.id,
            "protocol_fingerprint": protocol.fingerprint,
            "run_id": run_id,
            "passed": True,
            "reasons": [],
            "details": {"commit_under_test": {"commit": commits[run_id], "dirty": False}},
        }

    monkeypatch.setattr(BENCH_PROTOCOL, "verify_codebench_run", _verify)

    verification = verify_codebench_publication(
        protocol,
        run_root=run_root,
        current_versions={"claude": "x", "codex": "y"},
    )

    assert verification["passed"] is True
    assert verification["commit_under_test"] == "same-commit"
    assert set(verification["runs"]) == {run.id for run in protocol.runs}

    commits["external"] = "different-commit"
    verification = verify_codebench_publication(
        protocol,
        run_root=run_root,
        current_versions={"claude": "x", "codex": "y"},
    )
    assert verification["passed"] is False
    assert any("do not test the same LemonCrow commit" in reason for reason in verification["reasons"])


def test_protocol_rejects_prompt_drift_before_spend(tmp_path: Path) -> None:
    path, task_source, catalog = _write_fixture(tmp_path)
    (task_source / "tasks" / "task-a" / "prompt.md").write_text("changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="prompt drift"):
        load_codebench_protocol(path, task_catalog=catalog, task_source_dir=task_source)


def test_protocol_rejects_mutable_task_or_competitor_refs(tmp_path: Path) -> None:
    path, task_source, catalog = _write_fixture(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["tasks"][0]["ref"] = "main"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="40-char repository commit"):
        load_codebench_protocol(path, task_catalog=catalog, task_source_dir=task_source)

    path, task_source, catalog = _write_fixture(tmp_path / "drift")
    competitor = path.parent / "competitor.json"
    competitor.write_text(competitor.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="competitor manifest drift"):
        load_codebench_protocol(path, task_catalog=catalog, task_source_dir=task_source)

    path, task_source, catalog = _write_fixture(tmp_path / "mutable")
    competitor = path.parent / "competitor.json"
    competitor_raw = json.loads(competitor.read_text(encoding="utf-8"))
    competitor_raw["ref"] = "v1.0.0"
    competitor.write_text(json.dumps(competitor_raw), encoding="utf-8")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["runs"][2]["competitors"][0]["sha256"] = hashlib.sha256(competitor.read_bytes()).hexdigest()
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="immutable 40-char commit SHA"):
        load_codebench_protocol(path, task_catalog=catalog, task_source_dir=task_source)


def test_protocol_rejects_unfair_or_underpowered_runs(tmp_path: Path) -> None:
    path, task_source, catalog = _write_fixture(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["runs"][0]["parallel_scope"] = "arm"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="parallel_scope='task'"):
        load_codebench_protocol(path, task_catalog=catalog, task_source_dir=task_source)

    path, task_source, catalog = _write_fixture(tmp_path / "underpowered")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["runs"][0]["reps"] = 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="reps must be >= 5"):
        load_codebench_protocol(path, task_catalog=catalog, task_source_dir=task_source)
