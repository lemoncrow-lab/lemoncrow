"""Alembic, Django and Rails migrations run for real only in an image the trusted base pins.

The driver itself needs a container (it seals itself as PID 1 with Linux ``prctl``); these tests hold what
decides whether it may run and what its report may claim.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from lemoncrow_client.environment import Toolchain

from lemoncrow.pro.capabilities.review import artifact_analyses, migration_execution
from lemoncrow.pro.capabilities.review.analysis_common import AnalysisSpec, SideRun, payload_problem
from lemoncrow.pro.environment import oci, runners
from lemoncrow.pro.environment.services import ServiceSet

IMAGE = "registry.example/tool@sha256:" + "a" * 64
DB_IMAGE = "registry.example/db@sha256:" + "b" * 64
PINNED = f'version = 1\n[toolchain]\nimage = "{IMAGE}"\n'
WITH_DB = (
    PINNED + f'[services.db]\nimage = "{DB_IMAGE}"\nready = "true"\nsql = "postgres"\n'
    '[services.db.env]\nPOSTGRES_HOST_AUTH_METHOD = "trust"\n'
)
ROW: dict[str, Any] = {"id": "al", "framework": "alembic", "dir": "migrations/versions"}
DB_KEYS = {"service": "db", "database_url": "postgresql://postgres@{host}/postgres"}


def _spec(**extra: Any) -> AnalysisSpec:
    errors: list[str] = []
    specs = migration_execution.parse_specs([{**ROW, **extra}], errors)
    assert not errors
    return specs[0]


def _attached(text: str | None, **extra: Any) -> AnalysisSpec:
    return migration_execution.attach_environment(_spec(**extra), lambda _path: text)


def _sealed(**extra: Any) -> dict[str, Any]:
    return {"exit": 0, "seal": {"started": True}, "engine": "sqlite", **extra}


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (None, "has no .lemoncrow/environment.toml"),
        ("", "has no .lemoncrow/environment.toml"),
        ("version = 1\n", "pinned by digest"),
        ('version = 1\n[toolchain]\nimage = "python:3.12"\n', "pinned by digest"),
        ("not toml [", "environment.toml"),
    ],
)
def test_a_framework_without_a_pinned_trusted_image_is_not_run(text: str | None, why: str) -> None:
    spec = _attached(text)
    assert spec.executable is False and why in spec.reason


def test_a_pinned_image_makes_the_run_executable_and_never_optional() -> None:
    spec = _attached(PINNED)
    assert spec.executable is True and spec.reason == ""
    values = migration_execution.runtime_values(spec)
    assert values["toolchain"] == Toolchain(IMAGE) and values["require_toolchain"] is True
    assert "services" not in values


def test_the_image_is_part_of_the_run_identity() -> None:
    other = PINNED.replace("a" * 64, "c" * 64)
    first, why = migration_execution.payload(_attached(PINNED), read_base=lambda _path: None)
    second, _ = migration_execution.payload(_attached(other), read_base=lambda _path: None)
    assert not why and first["image"] == IMAGE and first != second


def test_a_named_service_must_be_declared_and_pinned() -> None:
    assert _attached(PINNED, **DB_KEYS).executable is False
    unpinned = WITH_DB.replace(DB_IMAGE, "postgres:17")
    assert _attached(unpinned, **DB_KEYS).executable is False
    spec = _attached(WITH_DB, **DB_KEYS)
    services = migration_execution.runtime_values(spec)["services"]
    assert isinstance(services, ServiceSet)
    service = services.services[0]
    assert (service.name, service.image, service.sql) == ("db", DB_IMAGE, "postgres")
    assert service.env == (("POSTGRES_HOST_AUTH_METHOD", "trust"),)


@pytest.mark.parametrize(
    "extra",
    [
        {"service": "db"},
        {"database_url": "postgresql://x@{host}/y"},
        {"service": "db", "database_url": "postgresql://fixed/y"},
        {"service": "db", "database_url": "postgresql://{host}/y", "dialect": "sqlite"},
        {"root": "../elsewhere"},
        {"root": "other"},
        {"config": "../alembic.ini"},
    ],
)
def test_invalid_framework_rows_are_reported(extra: dict[str, str]) -> None:
    errors: list[str] = []
    assert migration_execution.parse_specs([{**ROW, **extra}], errors) == [] and errors


def test_django_names_its_settings_module() -> None:
    row = {"id": "dj", "framework": "django", "dir": "shop/migrations"}
    for settings in ({}, {"settings": "proj.settings; import os"}):
        errors: list[str] = []
        assert migration_execution.parse_specs([{**row, **settings}], errors) == [] and errors


def test_only_direct_migration_files_of_the_framework_count_as_base() -> None:
    spec = migration_execution.attach_base_files(
        _attached(PINNED),
        ["migrations/versions/1_a.py", "migrations/versions/__init__.py", "migrations/versions/sub/2.py", "x.py"],
    )
    assert spec.options["base_files"] == ["migrations/versions/1_a.py"]


def test_the_driver_fits_the_executor_and_names_nothing_undefined() -> None:
    spec = _attached(PINNED)
    document, why = migration_execution.payload(spec, read_base=lambda _path: None)
    assert not why and payload_problem(migration_execution.FRAMEWORK_DRIVER, spec, document) == ""
    tree = ast.parse(migration_execution.FRAMEWORK_DRIVER)
    defined = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.ClassDef):
            defined.add(node.name)
        if isinstance(node, ast.FunctionDef | ast.Lambda):
            arguments = node.args
            defined.update(item.arg for item in (*arguments.args, *arguments.kwonlyargs))
            defined.update(item.arg for item in (arguments.vararg, arguments.kwarg) if item is not None)
        if isinstance(node, ast.Import | ast.ImportFrom):
            defined.update((item.asname or item.name).split(".")[0] for item in node.names)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        if isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert used <= defined, used - defined


def test_framework_runs_use_their_own_driver() -> None:
    assert artifact_analyses._driver("migrations.execute", _spec()) == migration_execution.FRAMEWORK_DRIVER
    errors: list[str] = []
    plain = migration_execution.parse_specs([{"id": "sql", "dir": "db"}], errors)[0]
    assert artifact_analyses._driver("migrations.execute", plain) == migration_execution.DRIVER


@pytest.mark.parametrize(
    ("report", "code"),
    [
        (_sealed(), 1),  # the process and the report disagree
        ({"exit": 0, "seal": {"started": False}}, 0),  # written by something that never sealed
        ({"exit": 0}, 0),
        ({"exit": 1, "seal": {"started": True}}, 1),  # stage A never claims findings
    ],
)
def test_a_report_the_sealed_driver_did_not_write_is_refused(report: dict[str, Any], code: int) -> None:
    accepted, why = migration_execution.inspect_framework_report(report, code)
    assert accepted is None and why


@pytest.mark.parametrize(
    "extra",
    [{"failed": {"id": "2", "error": "x"}}, {"rollback": {"status": "failed"}}, {"rollback": {"status": "mismatch"}}],
)
def test_the_host_derives_the_failure_exit(extra: dict[str, Any]) -> None:
    accepted, why = migration_execution.inspect_framework_report(_sealed(**extra), 0)
    assert not why and accepted is not None and accepted["exit"] == 1


def test_a_tampered_run_is_never_a_pass() -> None:
    report = _sealed(
        seal={"started": True, "tampered": ["the build left background processes running after it exited"]},
        schema_before={},
        schema_after={},
        applied=["2"],
        failed=None,
        rollback={"status": "verified"},
        migrations=[],
        counts={"total": 1, "added": 1, "edited": 0},
        statements=[],
    )
    accepted, why = migration_execution.inspect_framework_report(report, 0)
    assert not why and accepted is not None and accepted["tampered"]
    evaluation = migration_execution.evaluate(
        _attached(PINNED), SideRun("new", "ok", report=accepted, exit_code=0), None
    )
    assert evaluation.status == "UNKNOWN"


def test_without_an_engine_the_run_is_unavailable_and_never_falls_back_to_the_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def no_engine() -> Any:
        raise oci.OciUnavailable("no container engine")

    monkeypatch.setattr(oci, "detect_runtime", no_engine)
    runtime = SimpleNamespace(
        id="rt",
        strings=lambda _key: ["python3", "-c", "raise SystemExit('the host ran this')"],
        values={"toolchain": Toolchain(IMAGE), "require_toolchain": True},
    )
    surface = SimpleNamespace(id="s", provider="p")
    result = runners.CommandRunner().run(SimpleNamespace(repo_root=tmp_path), runtime, surface, side="new")  # type: ignore[arg-type]
    assert result.status == "unavailable" and "no container engine" in result.summary
