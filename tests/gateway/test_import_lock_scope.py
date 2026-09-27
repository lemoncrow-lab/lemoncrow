"""`lc import` must not hold the SQLite write lock across hosts (GH #43).

The whole multi-host loop used to run inside one ``store.history.batch_mode()``
transaction. SQLite's deferred ``BEGIN`` takes the write lock at the first
``record_raw_artifact`` and holds it until the final ``COMMIT``, so a *second*
concurrent ``lc import`` died with
``sqlite3.OperationalError: database is locked`` for the entire run.

These tests are deterministic, not timing-based: the "concurrent writer" runs
*inside* the second host's ``import_all`` (i.e. at the exact point in the run
where the old code was still holding the lock taken by the first host), on its
own connection with a 100 ms ``busy_timeout``. Either the first host's
transaction has committed by then -- lock released, row visible -- or it has
not, and the probe deterministically fails.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from lemoncrow.core.foundation.models import RawArtifact, Trace
from lemoncrow.gateway.cli import cli

_HOST_A = "claude"
_HOST_B = "codex"
_TRACE_A = "import-lock-scope-trace-a"
_TRACE_B = "import-lock-scope-trace-b"
_PROBE_TRACE = "import-lock-scope-concurrent-writer"

# One well-formed Claude JSONL line, so the reconstruction audit finds a turn.
_CLAUDE_JSONL = '{"type": "user", "message": {"role": "user", "content": "hi"}}\n'

#: Populated by :class:`_ImporterB` while ``lc import`` is mid-run.
PROBE: dict[str, Any] = {}


def _probe_as_a_second_writer(db_path: Path) -> None:
    """Stand in for a concurrent ``lc import``: own connection, short timeout.

    A real second process would use the store's 30 s ``busy_timeout``; 100 ms
    keeps the failing case fast and unambiguous (SQLITE_BUSY is raised, not
    waited out).
    """
    conn = sqlite3.connect(db_path, timeout=0.1)
    try:
        conn.execute("PRAGMA busy_timeout = 100")
        row = conn.execute("SELECT id FROM traces WHERE id = ?", (_TRACE_A,)).fetchone()
        PROBE["host_a_committed"] = row is not None
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO traces"
                " (id, agent, host, domain, status, task, workspace_path, created_at, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _PROBE_TRACE,
                    "probe",
                    "probe",
                    "testing",
                    "success",
                    "concurrent writer",
                    None,
                    "2026-01-01T00:00:00+00:00",
                    "{}",
                ),
            )
            conn.commit()
            PROBE["write_error"] = None
        except sqlite3.OperationalError as exc:
            conn.rollback()
            PROBE["write_error"] = str(exc)
    finally:
        conn.close()


class _ImporterA:
    """First host: records a trace plus its raw artifact, as real importers do."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def import_all(self, path: Path | None = None, *, force: bool = False) -> list[str]:
        artifact = RawArtifact(
            id=f"{_TRACE_A}-artifact",
            source=_HOST_A,
            source_session_id=_TRACE_A,
            kind="session",
            relative_path="a.jsonl",
            content_path=f"raw/{_HOST_A}/a.jsonl",
            sha256_original="0" * 64,
            sha256_redacted="0" * 64,
            byte_count_original=len(_CLAUDE_JSONL),
            byte_count_redacted=len(_CLAUDE_JSONL),
        )
        self.store.history.record_raw_artifact(artifact, _CLAUDE_JSONL)
        self.store.history.record_trace(
            Trace(
                id=_TRACE_A,
                agent=_HOST_A,
                domain="testing",
                task="host A import",
                status="success",
                host=_HOST_A,
                raw_artifact_ids=[artifact.id],
            )
        )
        return [_TRACE_A]


class _ImporterB:
    """Second host: probes for the lock *before* writing anything of its own."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def import_all(self, path: Path | None = None, *, force: bool = False) -> list[str]:
        _probe_as_a_second_writer(Path(self.store.history.db_path))
        self.store.history.record_trace(
            Trace(
                id=_TRACE_B,
                agent=_HOST_B,
                domain="testing",
                task="host B import",
                status="success",
                host=_HOST_B,
            )
        )
        return [_TRACE_B]


@pytest.fixture(autouse=True)
def _reset_probe() -> Iterator[None]:
    PROBE.clear()
    yield
    PROBE.clear()


@pytest.fixture
def _two_fake_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lemoncrow.gateway.hosts.session_parsers.registry.iter_importer_classes",
        lambda: [(_HOST_A, _ImporterA), (_HOST_B, _ImporterB)],
    )


def _invoke(root: Path, *args: str) -> Result:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(root), *args])


def _initialised_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tests.helpers import grant_oauth_pro

    grant_oauth_pro(monkeypatch)
    root = tmp_path / "store"
    result = _invoke(root, "init", "--no-index")
    assert result.exit_code == 0, result.output
    return root


@pytest.mark.usefixtures("_two_fake_hosts")
def test_import_releases_the_write_lock_between_hosts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _initialised_root(tmp_path, monkeypatch)

    result = _invoke(root, "import", "--json")

    assert result.exit_code == 0, result.output

    # (a) Host A's transaction committed before host B started importing.
    assert PROBE.get("host_a_committed") is True, (
        "host A's rows were still uncommitted while host B imported: " "the write transaction spans hosts"
    )
    # (b) A second writer was not blocked by the in-flight import.
    assert (
        PROBE.get("write_error") is None
    ), f"a concurrent writer was locked out mid-import: {PROBE.get('write_error')}"

    assert json.loads(result.stdout) == {_HOST_A: 1, _HOST_B: 1}

    # Every host's rows are durably committed once the command returns.
    with sqlite3.connect(root / "lemoncrow_history.db") as conn:
        ids = {row[0] for row in conn.execute("SELECT id FROM traces")}
    assert {_TRACE_A, _TRACE_B, _PROBE_TRACE} <= ids


@pytest.mark.usefixtures("_two_fake_hosts")
def test_import_human_output_and_audit_are_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Counters/audit survive moving the reconstruction pass out of the write txn."""
    root = _initialised_root(tmp_path, monkeypatch)

    result = _invoke(root, "import")

    assert result.exit_code == 0, result.output
    assert "imported 2 sessions" in result.stdout
    # Only host A recorded a raw artifact, so exactly one session reconstructs.
    assert "Audit: 1/2 sessions (50.0%) 100% reconstructable." in result.stdout


def test_one_failing_host_does_not_abort_the_others(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-host isolation: a raising importer must not stop later hosts."""

    class _Exploding:
        def __init__(self, store: Any) -> None:
            self.store = store

        def import_all(self, path: Path | None = None, *, force: bool = False) -> list[str]:
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "lemoncrow.gateway.hosts.session_parsers.registry.iter_importer_classes",
        lambda: [("opencode", _Exploding), (_HOST_A, _ImporterA), (_HOST_B, _ImporterB)],
    )
    root = _initialised_root(tmp_path, monkeypatch)

    result = _invoke(root, "import", "--json")

    assert result.exit_code == 0, result.output
    assert "FATAL: opencode importer raised" in result.stderr
    # The failing host contributes no count; the surviving hosts still import.
    assert json.loads(result.stdout) == {_HOST_A: 1, _HOST_B: 1}
