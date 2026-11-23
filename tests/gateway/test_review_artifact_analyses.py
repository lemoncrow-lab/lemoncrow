"""Artifact/infrastructure analysis drivers, specs and evaluation (Phase 9R R9), without a Review.

The drivers run here directly against deterministic fake tools placed on
``PATH`` (the driver seam); real sandboxed runs live in
``test_review_artifact_analyses_api.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import (
    compat_mixed_version,
    flag_matrix,
    infra_render,
    migration_execution,
    regenerate_compare,
    shipped_artifacts,
)
from lemoncrow.pro.capabilities.review.analysis_common import (
    INFRA_ENTRY,
    MIGRATIONS_ENTRY,
    SideRun,
    decode_report,
    driver_command,
    sealed_problem,
    side_state,
)
from lemoncrow.pro.capabilities.review.artifact_analyses import (
    ArtifactAnalysisProducer,
    apply_analysis_facts,
    build_analysis_catalog,
)
from lemoncrow.pro.capabilities.review.capability_catalog import (
    PRESET_SELECTIONS,
    STATIC_CATALOG,
    DetectedCapabilities,
    applicability,
)
from lemoncrow.pro.capabilities.review.run_evidence import RedactionPolicy, protect_text

DRIVERS = {
    "migrations": migration_execution.DRIVER,
    "regenerate": regenerate_compare.DRIVER,
    "shipped": shipped_artifacts.DRIVER,
    "infra": infra_render.DRIVER,
    "compat": compat_mixed_version.DRIVER,
    "flags": flag_matrix.DRIVER,
}
CREDENTIAL_ENV = {
    "AWS_ACCESS_KEY_ID": "host-aws-id",
    "AWS_SECRET_ACCESS_KEY": "host-aws-secret",
    "AWS_SESSION_TOKEN": "host-aws-session",
    "AWS_PROFILE": "prod",
    "GOOGLE_APPLICATION_CREDENTIALS": "/host/gcp.json",
    "GOOGLE_CREDENTIALS": "host-gcp",
    "ARM_CLIENT_SECRET": "host-azure",
    "TF_VAR_db_password": "host-tf-var",
    "TF_TOKEN_app_terraform_io": "host-tfc-token",
    "TF_CLI_ARGS": "-auto-approve",
    "TF_CLI_ARGS_plan": "-destroy",
    "KUBECONFIG": "/host/kubeconfig",
    "HELM_REGISTRY_CONFIG": "/host/helm.json",
    "GITHUB_TOKEN": "host-gh",
}


def _fake(bin_dir: Path, name: str, body: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _drive(
    tmp_path: Path, driver: str, payload: dict[str, Any], cwd: Path, *, path: str, env: dict[str, str] | None = None
) -> tuple[int, dict[str, Any]]:
    argv, problem = driver_command(driver, {"timeout": 60, "limit": 170000, **payload})
    assert not problem
    reports = tmp_path / f"reports-{len(list(tmp_path.glob('reports-*')))}"
    reports.mkdir()
    work = tmp_path / f"work-{reports.name}"
    proc = subprocess.run(
        [sys.executable, *argv[1:]],
        cwd=cwd,
        env={
            **(env or {}),
            "PATH": path,
            "LEMONCROW_REPORT_DIR": str(reports),
            "LEMONCROW_ANALYSIS_WORK": str(work),
            "HOME": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    report, why = decode_report((reports / "report").read_text())
    assert report is not None, (why, proc.stdout, proc.stderr)
    return (proc.returncode, report)


FAKE_TERRAFORM = '\nimport json, os, sys\nlog = {log!r}\nwith open(log, "a") as handle:\n    handle.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")\nargs = [a for a in sys.argv[1:] if not a.startswith("-chdir=")]\nstep = args[0]\nif step == "version":\n    print(json.dumps({{"terraform_version": "1.9.0"}}))\nelif step == "init":\n    print("Terraform has been successfully initialized!")\nelif step == "plan":\n    out = next(a.split("=", 1)[1] for a in args if a.startswith("-out="))\n    open(out, "w").write("plan")\n    print("Plan: 1 to add, 0 to change, 0 to destroy.")\nelif step == "show":\n    size = {size!r}\n    print(json.dumps({{"terraform_version": "1.9.0", "resource_changes": [{{\n        "address": "aws_instance.web", "type": "aws_instance", "mode": "managed",\n        "provider_name": "registry.terraform.io/hashicorp/aws",\n        "change": {{"actions": ["create"], "after": {{"instance_type": size}}}}}}]}}))\nelse:\n    sys.exit(97)\n'


def _terraform_tree(root: Path, size: str) -> None:
    (root / "infra").mkdir(parents=True, exist_ok=True)
    (root / "infra" / "main.tf").write_text(
        f'provider "aws" {{\n  region = "us-east-1"\n}}\n\nresource "aws_instance" "web" {{\n  instance_type = "{size}"\n}}\n'
    )


def test_terraform_driver_runs_only_the_plan_table_with_no_credentials(tmp_path: Path) -> None:
    log = tmp_path / "terraform.log"
    runs: dict[str, SideRun] = {}
    for side, size in (("old", "t3.micro"), ("new", "m5.large")):
        tools = tmp_path / f"bin-{side}"
        _fake(tools, "terraform", FAKE_TERRAFORM.format(log=str(log), size=size))
        tree = tmp_path / f"tree-{side}"
        _terraform_tree(tree, size)
        code, report = _drive(
            tmp_path,
            infra_render.DRIVER,
            {"tool": "terraform", "dir": "infra", "var_files": []},
            tree,
            path=f"{tools}:/usr/bin:/bin",
            env=CREDENTIAL_ENV,
        )
        assert code == 0, report
        runs[side] = SideRun(side, side_state(code, report), report=report, exit_code=code, heavy=report.pop("heavy"))
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    steps = [next(arg for arg in call["argv"] if not arg.startswith("-chdir=")) for call in calls]
    assert steps == ["version", "init", "plan", "show"] * 2
    for call in calls:
        assert not {"apply", "destroy", "-auto-approve", "-destroy"} & set(call["argv"])
        leaked = set(call["env"]) & set(CREDENTIAL_ENV)
        assert not leaked, leaked
        assert not [key for key in call["env"] if key.startswith(("AWS_", "TF_VAR_", "TF_TOKEN", "ARM_", "GOOGLE_"))]
        assert call["env"]["TF_INPUT"] == "0" and call["env"]["TF_CLI_CONFIG_FILE"].endswith("/terraformrc")
        rc = Path(call["env"]["TF_CLI_CONFIG_FILE"]).read_text()
        assert "filesystem_mirror" in rc and "direct" not in rc and ("network_mirror" not in rc)
    override = (tmp_path / "work-reports-1" / "src" / "infra" / "zz_lemoncrow_override.tf").read_text()
    assert 'backend "local"' in override and "skip_credentials_validation = true" in override
    assert runs["new"].report["mock_providers"] == ["aws"]
    spec = infra_render.discover_specs(["infra/main.tf"], lambda _path: '  provider "aws" {}\n')[0]
    evaluation = infra_render.evaluate(spec, runs["new"], runs["old"])
    assert evaluation.status == "PASS" and evaluation.summary["applied"] is False
    cost = evaluation.summary["cost"]
    assert cost["label"] == "estimate" and cost["monthly_delta"] == pytest.approx(70.1 - 7.6)
    assert [row.rule for row in evaluation.findings if row.rule == "cost_estimate"]


def test_terraform_driver_reports_a_missing_binary_as_unavailable(tmp_path: Path) -> None:
    _terraform_tree(tmp_path / "tree", "t3.micro")
    code, report = _drive(
        tmp_path,
        infra_render.DRIVER,
        {"tool": "terraform", "dir": "infra"},
        tmp_path / "tree",
        path=str(tmp_path / "empty"),
    )
    assert code == 2 and side_state(code, report) == "unavailable"
    spec = infra_render.discover_specs(["infra/main.tf"], lambda _path: 'resource "aws_instance" "web" {}\n')[0]
    evaluation = infra_render.evaluate(spec, SideRun("new", "unavailable", report["reason"]), None)
    assert evaluation.status == "NOT_RUN" and "Install terraform" in evaluation.setup_hint


FAKE_RENDER = '\nimport json, os, sys\nwith open({log!r}, "a") as handle:\n    handle.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")\nprint(\'\'\'apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  namespace: shop\nspec:\n  replicas: {replicas}\n---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: db\ndata:\n  password: {password}\n\'\'\')\n'


@pytest.mark.parametrize("tool", ["helm", "kustomize"])
def test_render_driver_diffs_manifests_masks_secrets_and_strips_cluster_credentials(tmp_path: Path, tool: str) -> None:
    log = tmp_path / "render.log"
    runs: dict[str, SideRun] = {}
    secret = "c3VwZXItc2VjcmV0LXZhbHVl"
    for side, replicas in (("old", 1), ("new", 3)):
        tools = tmp_path / f"bin-{side}"
        _fake(tools, tool, FAKE_RENDER.format(log=str(log), replicas=replicas, password=secret))
        tree = tmp_path / f"tree-{side}"
        (tree / "deploy").mkdir(parents=True)
        (tree / "deploy" / ("Chart.yaml" if tool == "helm" else "kustomization.yaml")).write_text("name: demo\n")
        code, report = _drive(
            tmp_path,
            infra_render.DRIVER,
            {"tool": tool, "dir": "deploy"},
            tree,
            path=f"{tools}:/usr/bin:/bin",
            env=CREDENTIAL_ENV,
        )
        assert code == 0, report
        heavy = report.pop("heavy")
        _resources, masked, problem = infra_render.rendered_resources(heavy["rendered.yaml"])
        assert not problem and secret not in masked
        runs[side] = SideRun(side, "ok", report=report, exit_code=0, heavy={"rendered.yaml": masked})
    for call in (json.loads(line) for line in log.read_text().splitlines()):
        assert not set(call["env"]) & set(CREDENTIAL_ENV)
    spec = infra_render.discover_specs(
        [f"deploy/{('Chart.yaml' if tool == 'helm' else 'kustomization.yaml')}"], lambda _p: ""
    )[0]
    evaluation = infra_render.evaluate(spec, runs["new"], runs["old"])
    assert evaluation.status == "PASS"
    assert [row.title for row in evaluation.findings] == ["Rendered resource changed · apps/v1/Deployment/shop/web"]
    assert "replicas: 3" in evaluation.heavy["rendered.diff"] and secret not in json.dumps(evaluation.heavy)
    assert evaluation.summary["masked_secrets"] == 1
    assert any("a change to only a Secret's values is not shown" in reason for reason in evaluation.reasons)


def test_artifact_analysis_safety_contract() -> None:
    """case: test_a_secret_value_change_is_not_a_delta_and_the_render_says_so"""
    spec = infra_render.discover_specs(["deploy/kustomization.yaml"], lambda _p: "")[0]
    secret = "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\ndata:\n{rows}"

    def side(name: str, rows: str) -> SideRun:
        _resources, masked, problem = infra_render.rendered_resources(secret.format(rows=rows))
        assert not problem
        return SideRun(name, "ok", report={}, exit_code=0, heavy={"rendered.yaml": masked})

    evaluation = infra_render.evaluate(spec, side("new", "  password: bmV3\n"), side("old", "  password: b2xk\n"))
    assert evaluation.status == "PASS" and evaluation.findings == []
    stated = [reason for reason in evaluation.reasons if "Secret's values is not shown" in reason]
    assert stated and "v1/Secret/db" in stated[0]
    assert "bmV3" not in json.dumps(evaluation.heavy) and "b2xk" not in json.dumps(evaluation.heavy)
    evaluation = infra_render.evaluate(
        spec, side("new", "  password: bmV3\n  token: dG9r\n"), side("old", "  password: b2xk\n")
    )
    assert [row.title for row in evaluation.findings] == ["Rendered resource changed · v1/Secret/db"]
    assert "Secret values are masked, so a change to a value is not shown" in evaluation.findings[0].summary
    "case: test_unparseable_render_output_is_unknown_never_an_empty_pass"
    from lemoncrow.pro.capabilities.review.artifact_analyses import _sanitize_heavy

    spec = infra_render.discover_specs(["deploy/kustomization.yaml"], lambda _p: "")[0]
    heavy = _sanitize_heavy(spec, {"rendered.yaml": "kind: Secret\ndata: {password: c2VjcmV0\n  - ["})
    assert "rendered.yaml" not in heavy and heavy["rendered.invalid"]
    evaluation = infra_render.evaluate(spec, SideRun("new", "ok", heavy=heavy), None)
    assert evaluation.status == "UNKNOWN"
    "case: test_the_terraform_command_builder_cannot_construct_apply_or_destroy"
    spec = infra_render.parse_specs(
        [{"id": "net", "tool": "terraform", "dir": "infra", "var_files": ["prod.tfvars"]}], []
    )[0]
    commands = infra_render.terraform_plan_commands(spec, workdir="/w", plan_file="/w/plan")
    assert [command[2] for command in commands] == ["version", "init", "plan", "show"]
    assert "-var-file=/w/prod.tfvars" in commands[2] and "-refresh=false" in commands[2]
    flat = {part.split("=", 1)[0] for command in commands for part in command}
    assert not flat & infra_render.FORBIDDEN_TERRAFORM
    for step in ("apply", "destroy", "import", "state", "refresh", "console", "login"):
        with pytest.raises(ValueError):
            infra_render.terraform_argv(step, workdir="/w", plan_file="/w/plan")
    for bad in ("-auto-approve", "-destroy", "../creds.tfvars", "/etc/passwd"):
        with pytest.raises(ValueError):
            infra_render.terraform_argv("plan", workdir="/w", plan_file="/w/plan", var_files=[bad])
    with pytest.raises(ValueError):
        infra_render.terraform_argv("show", workdir="/w", plan_file="-destroy")
    assert "TF_STEPS = {" in infra_render.DRIVER and '"-auto-approve"' in infra_render.DRIVER
    assert "apply" not in {step for step in infra_render.TERRAFORM_STEPS}
    "case: test_adversarial_infra_config_is_refused"
    for row in [
        {"id": "x", "tool": "terraform", "dir": "infra", "command": "terraform apply -auto-approve"},
        {"id": "x", "tool": "terraform", "dir": "infra", "args": ["apply"]},
        {"id": "x", "tool": "terraform", "dir": "infra", "steps": ["init", "apply"]},
        {"id": "x", "tool": "terraform", "dir": "infra", "extra_args": ["-destroy"]},
        {"id": "x", "tool": "terraform", "dir": "infra", "env": {"AWS_ACCESS_KEY_ID": "x"}},
        {"id": "x", "tool": "terraform", "dir": "infra", "script": "terraform destroy"},
        {"id": "x", "tool": "terraform apply", "dir": "infra"},
        {"id": "x", "tool": "terraform", "dir": "-chdir=/"},
        {"id": "x", "tool": "terraform", "dir": "infra", "var_files": ["-auto-approve"]},
        {"id": "x", "tool": "terraform", "dir": "../outside"},
        {"id": "x", "tool": "helm", "dir": "chart", "release": "--post-renderer=/bin/sh"},
    ]:
        errors: list[str] = []
        assert infra_render.parse_specs([row], errors) == []
        assert errors
    "case: test_command_keys_name_the_never_apply_rule"
    errors: list[str] = []
    infra_render.parse_specs([{"id": "x", "tool": "terraform", "dir": "infra", "command": "terraform apply"}], errors)
    assert "never applies" in errors[0]
    "case: test_driver_sources_survive_redaction_and_carry_no_credential_literal"
    for name, driver in DRIVERS.items():
        assert protect_text(driver, RedactionPolicy())[0] == driver, name
        argv = driver_command(driver, {})[0]
        assert argv[:3] == ("python3", "-I", "-c"), name
        assert len(argv[4]) < 2000


def test_artifact_analysis_detection_contract() -> None:
    """case: test_detection_comes_from_the_trusted_base_and_nothing_to_act_on_drops_the_fact"""
    files = {
        "db/migrations/0001_init.sql": "CREATE TABLE t (id INTEGER);",
        "db/migrations/nested/0002_more.sql": "",
        "pyproject.toml": '[project]\nname = "x"\n',
        "tests/fixtures/pkg/package.json": '{"name": "fixture"}',
        "infra/main.tf": 'resource "terraform_data" "x" {}\n',
        "modules/net/main.tf": 'resource "terraform_data" "y" {}\n',
        "docs/migrations/notes.md": "",
    }
    catalog = build_analysis_catalog("", sorted(files), _read(files))
    ids = {(spec.entry_id, spec.id) for spec in catalog.specs}
    assert ids == {
        ("migrations.execute", "db-migrations"),
        ("artifacts.shipped", "python"),
        ("infra.render", "terraform-infra"),
        ("compat.mixed_version", "persistence-db-migrations"),
    }
    migrations = catalog.get(MIGRATIONS_ENTRY, "db-migrations")
    assert migrations is not None and migrations.options["base_files"] == [
        "db/migrations/0001_init.sql",
        "db/migrations/nested/0002_more.sql",
    ]
    detected = DetectedCapabilities(facts=frozenset({"migrations", "infrastructure", "package_artifact", "generators"}))
    empty = apply_analysis_facts(detected, build_analysis_catalog("", ["docs/migrations/notes.md"], _read({})))
    assert empty.facts == frozenset()
    assert applicability(STATIC_CATALOG["migrations.execute"], empty) == ("inapplicable", "no migrations")
    kept = apply_analysis_facts(DetectedCapabilities(), catalog)
    assert kept.facts == {"migrations", "package_artifact", "infrastructure", "compat_surface"}
    provisional = DetectedCapabilities(facts=frozenset({"migrations"}), base_state="provisional")
    assert apply_analysis_facts(provisional, catalog) is provisional
    "case: test_configured_analyses_replace_discovery_and_report_errors"
    config = "analyses:\n  migrations:\n    - {id: core, dir: sql, dialect: sqlite, fixture: sql/schema.sql}\n  infra: []\n  regenerate:\n    - {id: api, command: make gen, outputs: ['gen/*.ts'], generator: [Makefile]}\n    - {id: bad, command: make gen}\n  shipped:\n    - {id: web, builder: npm, root: web}\n  surprise: []\n"
    files = {"infra/main.tf": 'resource "terraform_data" "x" {}\n', "sql/0001.sql": "", "sql/schema.sql": ""}
    catalog = build_analysis_catalog(config, sorted(files), _read(files))
    assert {(spec.entry_id, spec.id) for spec in catalog.specs} == {
        ("migrations.execute", "core"),
        ("artifacts.regenerate", "api"),
        ("artifacts.shipped", "web"),
    }
    assert catalog.for_entry(INFRA_ENTRY) == ()
    assert any("surprise" in error for error in catalog.errors)
    assert any("analyses.regenerate[1].outputs" in error for error in catalog.errors)
    regenerate = catalog.get("artifacts.regenerate", "api")
    assert regenerate is not None and regenerate.trusted_inputs == ("Makefile",)
    core = catalog.get(MIGRATIONS_ENTRY, "core")
    assert core is not None and core.trusted_inputs == ("sql/schema.sql",)
    "case: test_regenerate_has_a_catalog_entry_preset_and_detection_fact"
    entry = STATIC_CATALOG["artifacts.regenerate"]
    assert (entry.family, entry.cost_class, entry.degrade_tier, entry.requires, entry.executor) == (
        "deep_runtime",
        "medium",
        "deep_extra",
        "generators",
        "artifact_analyses",
    )
    for entry_id in ("migrations.execute", "artifacts.shipped", "infra.render"):
        assert STATIC_CATALOG[entry_id].executor == "artifact_analyses" and STATIC_CATALOG[entry_id].setup_hint
    deep = {row.id: row for row in PRESET_SELECTIONS["deep"]}
    assert deep["artifacts.regenerate"].sides == "base+head"
    assert "artifacts.regenerate" not in {row.id for row in PRESET_SELECTIONS["thorough"]}


def test_artifact_analysis_result_contract() -> None:
    """case: test_migration_rules_flag_destructive_and_lock_heavy_statements"""
    spec = migration_execution.parse_specs([{"id": "db", "dir": "db"}], [])[0]
    schema = {"table:users": {"kind": "table", "name": "users", "columns": [{"name": "id", "type": "INTEGER"}]}}
    up = "ALTER TABLE users ALTER COLUMN name TYPE varchar(20);\nALTER TABLE users ALTER COLUMN email SET NOT NULL;\nCREATE INDEX CONCURRENTLY users_a ON users(a);\nCREATE INDEX users_b ON users(b);\nALTER TABLE users ADD CONSTRAINT fk FOREIGN KEY (org) REFERENCES orgs(id);\nVACUUM FULL users;\nDELETE FROM sessions;\nDROP TABLE legacy;\nTRUNCATE audit;\nALTER TABLE users RENAME COLUMN a TO b;\n"
    report = {
        "dialect": "postgres",
        "schema_before": schema,
        "schema_after": schema,
        "migrations": [{"id": "0002", "paths": ["0002.up.sql", "0002.down.sql"], "state": "added", "up": up}],
    }
    rules = {finding.rule: finding for finding in migration_execution.classify(spec, report)}
    assert set(rules) >= {
        "type_change",
        "set_not_null",
        "non_concurrent_index",
        "foreign_key_validation",
        "table_rewrite",
        "delete_all_rows",
        "drop_table",
        "truncate",
        "rename",
    }
    assert rules["non_concurrent_index"].subject.endswith("users")
    assert rules["drop_table"].critical and rules["drop_table"].paths == ("db/0002.up.sql",)
    narrowing = migration_execution.classify(
        spec,
        {
            "dialect": "postgres",
            "schema_before": {"table:t": {"kind": "table", "name": "t", "columns": [{"name": "c", "type": "BIGINT"}]}},
            "schema_after": {"table:t": {"kind": "table", "name": "t", "columns": [{"name": "c", "type": "INTEGER"}]}},
            "migrations": [],
        },
    )
    assert [finding.rule for finding in narrowing] == ["type_narrowing"]
    rebuilt = migration_execution.classify(
        spec,
        {
            "dialect": "sqlite",
            "schema_before": {"table:t": {"kind": "table", "name": "t", "columns": [{"name": "c", "type": "TEXT"}]}},
            "schema_after": {
                "table:t": {"kind": "table", "name": "t", "columns": [{"name": "c", "type": "TEXT", "notnull": True}]}
            },
            "migrations": [],
        },
    )
    assert [(finding.rule, finding.subject.rsplit("::", 1)[-1]) for finding in rebuilt] == [
        ("not_null_without_default", "t.c")
    ]
    "case: test_engine_limits_are_unknown_but_real_failures_fail"
    spec = migration_execution.parse_specs([{"id": "db", "dir": "db"}], [])[0]

    def head(failed: dict[str, Any], dialect: str) -> SideRun:
        report = {"dialect": dialect, "schema_before": {}, "schema_after": {}, "migrations": [], "failed": failed}
        return SideRun("new", "failed", report=report, exit_code=1)

    syntax = {"id": "0002", "error": 'near "TYPE": syntax error'}
    assert migration_execution.evaluate(spec, head(syntax, "postgres"), None).status == "UNKNOWN"
    assert migration_execution.evaluate(spec, head(syntax, "sqlite"), None).status == "FAIL"
    missing = {"id": "0002", "error": "no such table: customers"}
    assert migration_execution.evaluate(spec, head(missing, "postgres"), None).status == "FAIL"
    "case: test_the_producer_turns_records_into_typed_behavior_deltas"
    import hashlib
    from types import SimpleNamespace

    from lemoncrow.pro.capabilities.review.analysis_common import ANALYSIS_MEDIA_TYPE

    def record(entry: str, rule: str) -> bytes:
        return json.dumps(
            {
                "entry_id": entry,
                "spec": {"id": "s", "title": "Spec"},
                "executor_class": "managed_sandbox",
                "environment": {"base": "env:a", "head": "env:a"},
                "attempts": [{"side": "new", "state": "ok", "evidence_ref": "check:test:x"}],
                "findings": [
                    {
                        "rule": rule,
                        "subject": f"s::{rule}",
                        "title": f"T {rule}",
                        "summary": "S",
                        "severity": "high",
                        "critical": True,
                        "change": "added",
                        "paths": ["a"],
                        "facts": [["K", "V"]],
                        "head": {"status": "x", "digest": "d", "summary": "s"},
                    }
                ],
            }
        ).encode()

    payloads = {
        f"ev-{entry}": record(entry, rule)
        for entry, rule in (
            ("migrations.execute", "drop_column"),
            ("artifacts.shipped", "secret"),
            ("infra.render", "resource_added"),
        )
    }
    evidence = [
        SimpleNamespace(
            id=key,
            mime_type=ANALYSIS_MEDIA_TYPE,
            revision_id="rev",
            execution_intent="current",
            created_at="2026-10-04",
            content_hash=hashlib.sha256(value).hexdigest(),
        )
        for key, value in payloads.items()
    ]
    context = SimpleNamespace(
        revision=SimpleNamespace(id="rev"), evidence=evidence, read_artifact=lambda item: payloads[item.id]
    )
    production = ArtifactAnalysisProducer().produce(context)
    kinds = {delta.target_ids[0]: delta.kind for delta in production.deltas}
    assert kinds == {"migrations.execute": "migration", "artifacts.shipped": "artifact", "infra.render": "infra"}
    assert all(delta.executor_class == "managed_sandbox" and delta.critical for delta in production.deltas)
    assert all(delta.evidence[0].ref.startswith("artifact:ev-") for delta in production.deltas)
    assert {scope.run_ref for scope in production.executed} == {"check:test:x"}
    "case: test_reports_are_decoded_fail_closed"
    good = json.dumps({"a": 1}).encode().hex()
    assert decode_report(good)[0] == {"a": 1}
    for bad in ("", "zz", good[:-1], "7b" * 3, json.dumps([1]).encode().hex()):
        assert decode_report(bad)[0] is None
    assert side_state(0, {}) == "ok" and side_state(1, {}) == "findings" and (side_state(2, {}) == "unavailable")
    assert side_state(3, {}) == "error" and side_state(4, {}) == "failed" and (side_state(None, None) == "error")


def _credentials() -> dict[str, str]:
    return {
        "aws.txt": "key = " + "AKIA" + "ABCDEFGHIJKLMNOP",
        "gh.txt": "token " + "ghp_" + "a" * 36,
        "pat.txt": "github_" + "pat_" + "b" * 30,
        "slack.txt": "xox" + "b-" + "1234567890-abcdef",
        "google.txt": "AI" + "za" + "c" * 35,
        "anthropic.txt": "sk-" + "ant-" + "d" * 30,
        "openai.txt": "sk-" + "e" * 40,
        "key.txt": "-----BEGIN " + "RSA PRIV" + "ATE KEY-----\nabc\n",
        "config.py": "client_secret = '" + "f" * 24 + "'",
    }


def test_the_artifact_scanner_reports_paths_and_rules_never_values(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "pkg").mkdir(parents=True)
    credentials = _credentials()
    for name, text in credentials.items():
        (project / "pkg" / name).write_text(text + "\n")
    (project / "pkg" / ".env").write_text("X=1\n")
    command = [sys.executable, "-c", "import shutil; shutil.make_archive('dist/pkg', 'zip', '.', 'pkg')"]
    code, report = _drive(
        tmp_path,
        shipped_artifacts.DRIVER,
        {"builder": "command", "command": command, "artifacts": ["dist/*.zip"]},
        project,
        path=f"{Path(sys.executable).parent}:/usr/bin:/bin",
    )
    assert code == 0, report
    assert "secrets" not in report and report["seal"]["started"] is True
    assert [row["path"] for row in report["outputs"]] == ["dist/pkg.zip"]
    inspected, why = shipped_artifacts.inspect_outputs(report, code)
    assert inspected is not None, why
    assert inspected["exit"] == 1
    assert "outputs" not in inspected
    report = inspected
    found = {(row["path"], row["rule"]) for row in report["secrets"]}
    assert found >= {
        ("pkg/aws.txt", "aws-access-key-id"),
        ("pkg/gh.txt", "github-token"),
        ("pkg/pat.txt", "github-fine-grained-token"),
        ("pkg/slack.txt", "slack-token"),
        ("pkg/google.txt", "google-api-key"),
        ("pkg/anthropic.txt", "anthropic-api-key"),
        ("pkg/openai.txt", "openai-api-key"),
        ("pkg/key.txt", "private-key"),
        ("pkg/config.py", "credential-assignment"),
        ("pkg/.env", "dotenv-file"),
    }
    rendered = json.dumps(report)
    for text in credentials.values():
        value = text.split()[-1].strip("'")
        assert value not in rendered


@pytest.mark.parametrize("planted", ["read-only-file", "directory"])
def test_a_report_planted_by_the_build_is_replaced_and_a_forged_exit_is_never_trusted(
    tmp_path: Path, planted: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    reports = tmp_path / "reports-0"
    forged = json.dumps({"v": 1, "exit": 1, "seal": {"v": 1, "started": True, "tampered": []}, "forged": 1})
    lock = (
        "os.chmod(target, 0o444)\n"
        if planted == "read-only-file"
        else "os.rename(target, target + '.x'); os.mkdir(target); os.rename(target + '.x', target + '/x')\n"
    )
    plant = (
        f"import os, pathlib\ntarget = {str(reports / 'report')!r}\npathlib.Path(target).write_text({forged.encode().hex()!r})\n"
        + lock
        + "os.chmod(os.path.dirname(target), 0o555)\npathlib.Path('dist').mkdir()\npathlib.Path('dist/notes.txt').write_text('built')\n"
    )
    code, report = _drive(
        tmp_path,
        shipped_artifacts.DRIVER,
        {"builder": "command", "command": [sys.executable, "-c", plant], "artifacts": ["dist/*"]},
        project,
        path=f"{Path(sys.executable).parent}:/usr/bin:/bin",
    )
    assert code == 0 and "forged" not in report, report
    assert report["build"]["exit"] == 0 and [row["path"] for row in report["outputs"]] == ["dist/notes.txt"]
    assert sealed_problem(report, code) == ""
    assert "never reports exit 1" in sealed_problem(json.loads(forged), 1)
    assert "but its report claims 0" in sealed_problem(report, 120)


def test_a_process_the_build_leaves_behind_is_killed_before_the_copy_out_and_taints_the_run(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    pid_file = tmp_path / "daemon.pid"
    linger = (
        f"import os, pathlib, time\npathlib.Path('dist').mkdir()\npathlib.Path('dist/notes.txt').write_text('built')\nif os.fork() == 0:\n    os.setsid()\n    pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n    time.sleep(60)\n    os._exit(0)\nwhile not os.path.exists("
        + repr(str(pid_file))
        + "):\n    time.sleep(0.01)\n"
    )
    code, report = _drive(
        tmp_path,
        shipped_artifacts.DRIVER,
        {"builder": "command", "command": [sys.executable, "-c", linger], "artifacts": ["dist/*"]},
        project,
        path=f"{Path(sys.executable).parent}:/usr/bin:/bin",
    )
    assert code == 0, report
    assert report["seal"]["tampered"] == ["the build left background processes running after it exited"]
    daemon = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(daemon, 0)
    inspected, _why = shipped_artifacts.inspect_outputs(report, code)
    assert inspected is not None and "background processes" in inspected["tampered"]


def test_a_build_that_swaps_its_working_directory_copies_nothing_out(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    swap = "import os\nhere = os.getcwd()\nos.rename(here, here + '.old')\nos.makedirs(here + '/dist')\nopen(here + '/dist/notes.txt', 'w').write('clean')\n"
    code, report = _drive(
        tmp_path,
        shipped_artifacts.DRIVER,
        {"builder": "command", "command": [sys.executable, "-c", swap], "artifacts": ["dist/*"]},
        project,
        path=f"{Path(sys.executable).parent}:/usr/bin:/bin",
    )
    assert code == 3 and "replaced its working directory" in report["reason"], report
    assert "outputs" not in report


def test_the_sandbox_runs_the_same_stage_b_as_the_host(tmp_path: Path) -> None:
    import inspect as pyinspect
    import io
    import zipfile

    from lemoncrow.pro.capabilities.review import artifact_inspection
    from lemoncrow.pro.capabilities.review.analysis_common import embedded_source

    shared = embedded_source(pyinspect.getsource(artifact_inspection))
    assert "def inspect_files(" in shipped_artifacts.DRIVER and "def compare_regenerated(" in regenerate_compare.DRIVER
    assert shared.split("\ndef ", 1)[1] in shipped_artifacts.DRIVER
    sandbox: dict[str, Any] = {}
    exec(compile(shared, "<embedded>", "exec"), sandbox)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("demo/__init__.py", "__all__ = ['alpha']\n")
        archive.writestr("demo/.env", "X=1\n")
        for name, text in _credentials().items():
            archive.writestr(f"demo/{name}", text + "\n")
    files = [("demo-1.0-py3-none-any.whl", buffer.getvalue()), ("notes.txt", b"plain\n")]
    host = artifact_inspection.inspect_files(files, "python", 2)
    assert host["secrets"] and host["api"]
    assert sandbox["inspect_files"](files, "python", 2) == host
    committed = {"a.py": "A = 1\n", "b.py": "B = 1\n"}
    before = {path: hashlib.sha256(text.encode()).hexdigest() for path, text in committed.items()}
    regenerated = {"a.py": b"A = 2\n", "b.py": b"B = 1\n", "c.py": b"C = 1\n"}
    rows = artifact_inspection.compare_regenerated(before, regenerated, {}, committed.get, 10)
    assert sandbox["compare_regenerated"](before, regenerated, {}, committed.get, 10) == rows
    assert [row["state"] for row in rows[0]] == ["differs", "unchanged", "uncommitted"]


def _read(files: dict[str, str]) -> Any:
    return files.get


def test_host_env_never_reaches_a_driver_tool(tmp_path: Path) -> None:
    tools = tmp_path / "bin"
    dump = tmp_path / "env.json"
    _fake(tools, "terraform", f"import json, os\nopen({str(dump)!r}, 'w').write(json.dumps(dict(os.environ)))\n")
    (tmp_path / "tree").mkdir()
    _drive(
        tmp_path,
        infra_render.DRIVER,
        {"tool": "terraform", "dir": "."},
        tmp_path / "tree",
        path=f"{tools}:/usr/bin:/bin",
        env={**CREDENTIAL_ENV, "SSH_AUTH_SOCK": "/tmp/agent", "NPM_TOKEN": "x", **dict(os.environ)},
    )
    seen = set(json.loads(dump.read_text()))
    assert seen <= {
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "SOURCE_DATE_EPOCH",
        "VIRTUAL_ENV",
        "PYTHONDONTWRITEBYTECODE",
        "HOME",
        "CI",
        "NO_COLOR",
        "TF_CLI_CONFIG_FILE",
        "TF_DATA_DIR",
        "TF_IN_AUTOMATION",
        "TF_INPUT",
        "CHECKPOINT_DISABLE",
        "PWD",
        "SHLVL",
        "_",
        "LC_CTYPE",
    }, seen


# --------------------------------------------------------------------------- #
# RC12-C: the executor command bound is derived, proved with headroom, and refused at plan time
# --------------------------------------------------------------------------- #

#: What each driver leaves of the hosted worker's 32,768-character command (30,000 here), rounded down.
#: A driver that grows past its floor fails this table: shrink it or move inputs out of the command.
HEADROOM_FLOOR = {
    "migrations": 14_000,
    "regenerate": 4_500,
    "shipped": 3_500,
    "infra": 14_500,
    "compat": 14_000,
    "flags": 12_500,
}


def test_every_driver_leaves_its_documented_command_headroom() -> None:
    from lemoncrow.pro.capabilities.review.analysis_common import MAX_COMMAND_CHARS, payload_headroom

    for name, driver in DRIVERS.items():
        room = payload_headroom(driver)
        assert room >= HEADROOM_FLOOR[name], (name, room)
        # The headroom is exactly what driver_command still accepts: one more character is refused.
        argv, problem = driver_command(driver, {"x": "a" * (room - len('{"x":""}'))})
        assert not problem and sum(len(item) for item in argv) == MAX_COMMAND_CHARS
        _argv, problem = driver_command(driver, {"x": "a" * (room - len('{"x":""}') + 1)})
        assert problem.startswith("Command too large")


def _config_rows(command_chars: int) -> list[dict[str, Any]]:
    # Two 2000-character arguments at most each, so the command is spread over arguments.
    parts, left = [], command_chars
    while left > 0:
        parts.append("a" * min(left, 1999))
        left -= 1999
    return [{"id": "gen", "builder": "command", "root": ".", "command": parts, "artifacts": ["dist/*.tar"]}]


def _largest_accepted(fits: Any) -> int:
    low, high = 1, 20_000
    assert fits(low) and not fits(high)
    while low + 1 < high:
        mid = (low + high) // 2
        low, high = (mid, high) if fits(mid) else (low, mid)
    return low


def test_a_configured_shipped_command_is_accepted_exactly_up_to_the_bound_and_never_fails_at_run_time() -> None:
    from lemoncrow.pro.capabilities.review.analysis_common import LOCAL_REPORT_LIMIT, MAX_TIMEOUT, PAYLOAD_RESERVE
    from lemoncrow.pro.capabilities.review.artifact_analyses import fit_command

    def fitted(chars: int) -> Any:
        specs = shipped_artifacts.parse_specs(_config_rows(chars), [])
        assert len(specs) == 1
        return fit_command(specs[0])

    bound = _largest_accepted(lambda chars: fitted(chars).executable)
    accepted, refused = fitted(bound), fitted(bound + 1)
    assert accepted.executable and not refused.executable
    assert refused.reason.startswith("Command too large") and "analyses.shipped" in refused.setup_hint
    # Every run-time envelope value at its worst case still fits: parse-time acceptance means run-time success.
    envelope = {"entry": accepted.entry_id, "spec": accepted.id, "root": accepted.root}
    envelope |= {"timeout": MAX_TIMEOUT, "limit": LOCAL_REPORT_LIMIT}
    document = {**shipped_artifacts.payload(accepted), **envelope}
    argv, problem = driver_command(shipped_artifacts.DRIVER, document)
    assert not problem and sum(len(item) for item in argv) <= 30_000
    # The bound is real: past it (and the reserve kept for what a run adds) the run-time command is refused too.
    far = fitted(bound + PAYLOAD_RESERVE + 64)
    assert not far.executable
    assert driver_command(shipped_artifacts.DRIVER, {**shipped_artifacts.payload(far), **envelope})[1].startswith(
        "Command too large"
    )


def test_a_command_too_large_analysis_plans_not_available_and_reserves_no_budget() -> None:
    from lemoncrow.pro.capabilities.review.capability_catalog import DetectedCapabilities
    from lemoncrow.pro.capabilities.review.run_plan import (
        PlanInputs,
        SettingOrigin,
        build_run_plan,
        parse_run_settings,
        resolve_run_settings,
    )

    def plan_for(command_chars: int) -> Any:
        config = "analyses:\n  shipped:\n    - " + json.dumps(_config_rows(command_chars)[0]) + "\n"
        catalog = build_analysis_catalog(config, ["pyproject.toml"], lambda _path: "")
        detected = apply_analysis_facts(DetectedCapabilities(facts=frozenset(), base_state="present"), catalog)
        repo = SettingOrigin("trusted_base_repository", "base revision .lemoncrow/review.yaml")
        settings = resolve_run_settings([parse_run_settings({"effort": "deep"}, origin=repo)], default_run_on="local")
        inputs = PlanInputs(
            review_id="r",
            revision_id="v",
            trigger="open",
            settings=settings,
            detected=detected,
            changed_paths=("pyproject.toml",),
            allowed_run_on=("local",),
            allowed_run_on_by_class={"runtime": ("local",), "ai": ("local",)},
        )
        return detected, build_run_plan(inputs)

    _detected, fits = plan_for(100)
    assert fits.entry("artifacts.shipped").status == "run"
    detected, too_large = plan_for(6000)
    row = too_large.entry("artifacts.shipped")
    assert row is not None and row.status == "not_available"
    assert row.reason.startswith("Command too large") and "analyses.shipped" in row.reason
    # No budget is reserved for work that cannot run.
    shipped_minutes = fits.entry("artifacts.shipped").estimated_minutes
    assert shipped_minutes > 0
    assert too_large.totals["minutes"] == pytest.approx(fits.totals["minutes"] - shipped_minutes)
    # The reason survives the detection round trip that freezes it with the plan.
    assert DetectedCapabilities.from_wire(detected.to_wire()).unavailable == detected.unavailable


@pytest.mark.parametrize("entry", ["migrations", "compat"])
def test_many_base_migration_files_become_not_available_with_the_typed_reason_not_a_run_time_failure(
    entry: str,
) -> None:
    from lemoncrow.pro.capabilities.review.analysis_common import LOCAL_REPORT_LIMIT
    from lemoncrow.pro.capabilities.review.artifact_analyses import fit_command

    def build(count: int) -> Any:
        files = {f"db/migrations/{index:05d}_{'x' * 40}.sql": "CREATE TABLE t (id INTEGER);" for index in range(count)}
        catalog = build_analysis_catalog("", sorted(files), _read(files))
        want = MIGRATIONS_ENTRY if entry == "migrations" else "compat.mixed_version"
        spec = next(row for row in catalog.specs if row.entry_id == want)
        return spec, files

    def fits(count: int) -> bool:
        return build(count)[0].executable

    # Below the old 600-file cap, but past what the driver command can carry.
    bound = _largest_accepted_files(fits)
    assert 100 < bound < 600
    ok, files = build(bound)
    over, _ = build(bound + 1)
    assert ok.executable and not over.executable
    assert over.reason.startswith("Command too large") and "migration" in over.setup_hint.lower()
    # What was accepted really fits, with the real digests and the worst-case envelope.
    module = migration_execution if entry == "migrations" else compat_mixed_version
    driver = migration_execution.DRIVER if entry == "migrations" else compat_mixed_version.DRIVER
    payload, problem = module.payload(ok, read_base=files.get)
    assert not problem
    document = {
        **payload,
        "entry": ok.entry_id,
        "spec": ok.id,
        "root": ok.root,
        "timeout": 900,
        "limit": LOCAL_REPORT_LIMIT,
    }
    assert not driver_command(driver, document)[1]
    assert fit_command(ok).executable


def _largest_accepted_files(fits: Any) -> int:
    low, high = 1, 600
    assert fits(low) and not fits(high)
    while low + 1 < high:
        mid = (low + high) // 2
        low, high = (mid, high) if fits(mid) else (low, mid)
    return low


def _flag_spec(command_chars: int, *, template: str, on: str, off: str) -> Any:
    parts, left = [], command_chars
    while left > 0:
        parts.append("a" * min(left, 1999))
        left -= 1999
    errors: list[str] = []
    row = {"id": "flags", "command": ["pytest", *parts], "env": template, "on": on, "off": off}
    specs = flag_matrix.parse_specs([row], errors)
    assert not errors and len(specs) == 1, errors
    return specs[0]


@pytest.mark.parametrize(
    ("template", "on", "off"),
    [
        ("FEATURE_{FLAG}", "1", "0"),
        ("F" * 121 + "_{FLAG}", "o" * 64, "f" * 64),  # the longest template the parser accepts
        ("X_{flag}", "\U0001f600" * 16, '"' * 64),  # values that escape at their widest
    ],
)
def test_a_flag_matrix_spec_the_plan_accepts_always_fits_at_run_time(template: str, on: str, off: str) -> None:
    """Plan-time acceptance guarantees the run-time command fits, whatever flags the Change touches."""

    from lemoncrow.pro.capabilities.review.analysis_common import LOCAL_REPORT_LIMIT
    from lemoncrow.pro.capabilities.review.artifact_analyses import fit_command

    bound = _largest_accepted(
        lambda chars: fit_command(_flag_spec(chars, template=template, on=on, off=off)).executable
    )
    spec = _flag_spec(bound, template=template, on=on, off=off)
    assert not fit_command(_flag_spec(bound + 1, template=template, on=on, off=off)).executable
    worst_names = {
        "ascii": [chr(ord("a") + i) * 100 for i in range(4)],
        "bmp": [chr(0x4E00 + i) * 100 for i in range(4)],
        "astral": [chr(0x20000 + i) * 100 for i in range(4)],
        "mixed": ["a" * 100, chr(0x20000) * 100, "b.c:d-" * 16, chr(0x4E00) * 100],
    }
    for label, names in worst_names.items():
        head = "\n".join(f'is_enabled("{name}")' for name in names)
        prepared = flag_matrix.prepare(
            spec,
            changed_paths=["app.py"],
            read_side=lambda _path, side, text=head: text if side == "new" else "",
            depth="deep",
        )
        assert len(prepared.states) - len(prepared.limited) == 8, label  # every state of the deepest matrix
        document = {
            **flag_matrix.payload(spec, prepared, budget_seconds=spec.timeout - 0.05),
            "entry": spec.entry_id,
            "spec": spec.id,
            "root": spec.root,
            "timeout": spec.timeout,
            "limit": LOCAL_REPORT_LIMIT,
        }
        assert not driver_command(flag_matrix.DRIVER, document)[1], label


def test_flag_values_too_wide_for_the_worst_case_matrix_are_not_available_at_plan_time() -> None:
    from lemoncrow.pro.capabilities.review.artifact_analyses import fit_command

    errors: list[str] = []
    row = {"id": "wide", "command": ["pytest"], "env": "X_{flag}", "on": "\U0001f600" * 64, "off": "0"}
    (spec,) = flag_matrix.parse_specs([row], errors)
    fitted = fit_command(spec)
    assert not fitted.executable and fitted.reason.startswith("Command too large")
    assert "analyses.flags" in fitted.setup_hint


def test_a_flag_env_template_is_bounded_at_parse_time() -> None:
    errors: list[str] = []
    rows = [{"id": "ok", "command": "pytest", "env": "F" * 121 + "_{FLAG}"}]
    rows.append({"id": "long", "command": "pytest", "env": "F" * 122 + "_{FLAG}"})
    specs = flag_matrix.parse_specs(rows, errors)
    assert [spec.id for spec in specs] == ["ok"]
    assert len(errors) == 1 and "at most 128 characters" in errors[0]


def test_a_flag_name_past_the_reserved_size_is_listed_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # The inventory never names a flag past 100 characters; should it ever, the state is not run.
    spec = _flag_spec(10, template="FEATURE_{FLAG}", on="1", off="0")
    touched = {"ab": ("read", ("app.py",)), "x" * 101: ("introduced", ("app.py",))}
    monkeypatch.setattr(flag_matrix, "touched_flags", lambda _paths, _read: dict(touched))
    prepared = flag_matrix.prepare(spec, changed_paths=["app.py"], read_side=lambda _p, _s: None, depth="deep")
    assert sorted(prepared.limited) == ["x" * 101 + "=off", "x" * 101 + "=on"]
    assert all("longer than 100 characters" in reason for reason in prepared.limited.values())
    assert [state["id"] for state in flag_matrix.payload(spec, prepared, budget_seconds=None)["states"]] == [
        "ab=on",
        "ab=off",
    ]


# --------------------------------------------------------------------------- #
# RC12-C: helm and kustomize against the real binaries (driver seam; skipped when absent)
#
# The sandbox-level runs live in test_review_artifact_analyses_api.py and need the binaries in the sandbox's
# system directories; the release gate's runner requires them (REVIEW_SANDBOX_TOOLS in the private release gate).
# These run the same driver against whatever real binary is on PATH, so a chart or kustomization the tools
# really render is proved here without a sandbox.
# --------------------------------------------------------------------------- #

_HELM = shutil.which("helm")
_KUSTOMIZE = shutil.which("kustomize") or shutil.which("kubectl")


def _chart(root: Path, replicas: int, *, secret: str = "s3cr3t-value") -> None:
    (root / "deploy" / "templates").mkdir(parents=True)
    (root / "deploy" / "Chart.yaml").write_text("apiVersion: v2\nname: shop\nversion: 0.1.0\n")
    (root / "deploy" / "values.yaml").write_text(f"replicas: {replicas}\nsecret: {secret}\n")
    (root / "deploy" / "templates" / "web.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n  namespace: {{ .Release.Namespace }}\n"
        "spec:\n  replicas: {{ .Values.replicas }}\n"
        "---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: db\ndata:\n  password: {{ .Values.secret | b64enc }}\n"
    )


def _kustomization(root: Path, replicas: int) -> None:
    (root / "deploy").mkdir(parents=True)
    (root / "deploy" / "kustomization.yaml").write_text("resources:\n  - web.yaml\n")
    (root / "deploy" / "web.yaml").write_text(
        f"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  replicas: {replicas}\n"
    )


def _real_runs(tmp_path: Path, tool: str, build: Any) -> dict[str, SideRun]:
    runs: dict[str, SideRun] = {}
    for side, replicas in (("old", 1), ("new", 3)):
        tree = tmp_path / f"real-{side}"
        build(tree, replicas)
        code, report = _drive(
            tmp_path,
            infra_render.DRIVER,
            {"tool": tool, "dir": "deploy", "release": "shop", "namespace": "prod"},
            tree,
            path=os.environ["PATH"],
            env=CREDENTIAL_ENV,
        )
        assert code == 0, report
        heavy = report.pop("heavy")
        _resources, masked, problem = infra_render.rendered_resources(heavy["rendered.yaml"])
        assert not problem
        runs[side] = SideRun(side, "ok", report=report, exit_code=0, heavy={"rendered.yaml": masked})  # type: ignore[arg-type]
    return runs


@pytest.mark.skipif(_HELM is None, reason="helm is not installed; the release runner provides it")
def test_helm_renders_a_real_chart_and_the_diff_masks_the_secret(tmp_path: Path) -> None:
    runs = _real_runs(tmp_path, "helm", _chart)
    spec = infra_render.discover_specs(["deploy/Chart.yaml"], lambda _p: "")[0]
    evaluation = infra_render.evaluate(spec, runs["new"], runs["old"])
    assert evaluation.status == "PASS"
    assert [row.title for row in evaluation.findings] == ["Rendered resource changed · apps/v1/Deployment/prod/web"]
    assert "replicas: 3" in evaluation.heavy["rendered.diff"]
    assert "s3cr3t-value" not in json.dumps(evaluation.heavy) and "czNjcjN0LXZhbHVl" not in json.dumps(evaluation.heavy)


@pytest.mark.skipif(_HELM is None, reason="helm is not installed; the release runner provides it")
def test_a_chart_helm_cannot_render_is_a_failed_run_not_an_empty_pass(tmp_path: Path) -> None:
    tree = tmp_path / "broken"
    (tree / "deploy" / "templates").mkdir(parents=True)
    (tree / "deploy" / "Chart.yaml").write_text("apiVersion: v2\nname: shop\nversion: 0.1.0\n")
    (tree / "deploy" / "templates" / "web.yaml").write_text("kind: {{ .Values.missing.deeper }\n")
    code, report = _drive(
        tmp_path, infra_render.DRIVER, {"tool": "helm", "dir": "deploy"}, tree, path=os.environ["PATH"]
    )
    assert code == 4 and "rendering failed" in report["reason"]


@pytest.mark.skipif(_KUSTOMIZE is None, reason="kustomize/kubectl is not installed; the release runner provides it")
def test_kustomize_builds_a_real_kustomization_and_diffs_it(tmp_path: Path) -> None:
    runs = _real_runs(tmp_path, "kustomize", _kustomization)
    spec = infra_render.discover_specs(["deploy/kustomization.yaml"], lambda _p: "")[0]
    evaluation = infra_render.evaluate(spec, runs["new"], runs["old"])
    assert evaluation.status == "PASS"
    assert [row.title for row in evaluation.findings] == ["Rendered resource changed · apps/v1/Deployment/web"]
    assert "replicas: 3" in evaluation.heavy["rendered.diff"]


@pytest.mark.skipif(_KUSTOMIZE is None, reason="kustomize/kubectl is not installed; the release runner provides it")
def test_a_remote_base_kustomize_cannot_fetch_is_unavailable_never_a_pass(tmp_path: Path) -> None:
    tree = tmp_path / "remote"
    (tree / "deploy").mkdir(parents=True)
    (tree / "deploy" / "kustomization.yaml").write_text("resources:\n  - https://invalid.example/base?ref=main\n")
    code, report = _drive(
        tmp_path,
        infra_render.DRIVER,
        {"tool": "kustomize", "dir": "deploy"},
        tree,
        path=os.environ["PATH"],
        env={"HTTPS_PROXY": "http://127.0.0.1:9"},
    )
    assert code in {2, 4}, report


# --------------------------------------------------------------------------- #
# RC12-C: frameworks and images this build cannot execute are explicitly not available, with the way out
# --------------------------------------------------------------------------- #


def test_alembic_django_rails_and_container_images_are_not_available_with_a_setup_reason() -> None:
    files = {
        "svc/alembic/versions/0001_init.py": "revision = '0001'\n",
        "web/app/migrations/0001_initial.py": "from django.db import migrations\n",
        "rails/db/migrate/20240101000000_create_users.rb": "class CreateUsers < ActiveRecord::Migration[7.0]; end\n",
        "Dockerfile": "FROM scratch\n",
    }
    catalog = build_analysis_catalog("", sorted(files), _read(files))
    by_tool = {spec.tool: spec for spec in catalog.specs if spec.entry_id in {MIGRATIONS_ENTRY, "artifacts.shipped"}}
    assert set(by_tool) == {"alembic", "django", "rails", "container"}
    for tool, spec in by_tool.items():
        assert not spec.executable and spec.reason and spec.setup_hint, tool
    assert (
        "plain SQL migrations" in by_tool["alembic"].setup_hint and "alembic" in by_tool["alembic"].setup_hint.lower()
    )
    assert "sqlmigrate" in by_tool["django"].setup_hint
    assert "plain SQL migrations" in by_tool["rails"].setup_hint
    assert "Docker/BuildKit" in by_tool["container"].setup_hint and "command builder" in by_tool["container"].setup_hint


def test_a_configured_container_builder_or_framework_is_not_executable_either() -> None:
    config = (
        "analyses:\n"
        "  shipped:\n    - {id: image, builder: container, root: .}\n"
        "  migrations:\n    - {id: orm, dir: app, framework: alembic}\n"
    )
    catalog = build_analysis_catalog(config, ["Dockerfile", "app/alembic/versions/0001.py"], _read({}))
    assert not catalog.errors
    rows = {spec.id: spec for spec in catalog.specs}
    assert not rows["image"].executable and "Docker/BuildKit" in rows["image"].setup_hint
    assert not rows["orm"].executable
