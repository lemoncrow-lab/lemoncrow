from __future__ import annotations

from pathlib import Path

from lemoncrow.core.service.jobs import JOB_CONSOLIDATE_BLOCKS, JOB_OPTIMIZE
from lemoncrow.core.service.telemetry.schema import validate_event_props
from lemoncrow.core.service.worker import Worker
from lemoncrow.infra.runtime.servicectl_lifecycle import _servicectl_tick
from lemoncrow.pro.capabilities.optimization.policy import AutomationConfig


def test_worker_optimize_handler_uses_shared_runner(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _fake_cycle(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr("lemoncrow.pro.capabilities.optimization.run_optimization_cycle", _fake_cycle)
    worker = Worker(store=type("Store", (), {"root": tmp_path / ".lemoncrow"})())

    result = worker._dispatch[JOB_OPTIMIZE]({"days": 3, "host": "claude", "source": "servicectl"})

    assert result == {"ok": True}
    assert captured["days"] == 3
    assert captured["host"] == "claude"
    assert captured["open_pr"] is False
    assert captured["source"] == "servicectl"


def test_servicectl_tick_enqueues_optimize_only_once_per_interval(monkeypatch, tmp_path: Path) -> None:
    # The store is split: the job queue lives on the ``.jobs`` sub-store
    # (JobsStore), so servicectl reaches it via ``store.jobs.enqueue_job(...)``.
    class _JobsStore:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []

        def job_queue_health(self) -> dict[str, int]:
            active = sum(1 for job in self.records if job["status"] in {"pending", "running", "failed"})
            return {"pending": active, "running": 0, "failed": 0, "active": active}

        def list_jobs(self, *, job_type: str, limit: int) -> list[dict[str, object]]:
            return [job for job in self.records if job["job_type"] == job_type][:limit]

        def enqueue_job(self, job_type: str, payload: dict[str, object]) -> str:
            job_id = f"{job_type}-{len(self.records) + 1}"
            self.records.append({"id": job_id, "job_type": job_type, "status": "pending", "payload": payload})
            return job_id

    class _Store:
        def __init__(self, root: Path) -> None:
            self.root = root
            self.jobs = _JobsStore()

        def init(self) -> None:
            return None

    store = _Store(tmp_path / ".lemoncrow")

    class _Worker:
        def __init__(self, store) -> None:
            self.store = store

        def run_once(self):
            return None

    monkeypatch.setattr("lemoncrow.infra.storage.factory.create_store", lambda root: store)
    monkeypatch.setattr("lemoncrow.core.service.worker.Worker", _Worker)
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.optimization.load_automation_config",
        lambda root: AutomationConfig(enabled=True),
    )
    monkeypatch.setattr("lemoncrow.infra.runtime.servicectl_lifecycle._servicectl_refresh_host_status", lambda root: {})
    monkeypatch.setattr("lemoncrow.infra.runtime.servicectl_lifecycle._servicectl_import_sessions", lambda root: {})

    first = _servicectl_tick(
        tmp_path / ".lemoncrow",
        maintenance_interval_seconds=60,
        session_import_interval_seconds=-1,
    )
    second = _servicectl_tick(
        tmp_path / ".lemoncrow",
        maintenance_interval_seconds=60,
        session_import_interval_seconds=-1,
    )

    assert any(job["job_type"] == JOB_CONSOLIDATE_BLOCKS for job in store.jobs.records)
    assert sum(1 for job in store.jobs.records if job["job_type"] == JOB_OPTIMIZE) == 1
    assert any(job_id.startswith(JOB_OPTIMIZE) for job_id in first["enqueued_jobs"])
    assert not any(job_id.startswith(JOB_OPTIMIZE) for job_id in second["enqueued_jobs"])


def test_optimization_telemetry_schema_accepts_new_events() -> None:
    filtered, dropped = validate_event_props(
        "optimization_proposal_evaluated",
        {
            "source": "cli",
            "repo_id": "sha256:abc",
            "has_recommendation": True,
            "projected_tokens_saved": 1234,
            "projected_weekly_savings_usd": 4.5,
            "benchmark_evidence_present": True,
            "ni_passed": False,
            "open_pr_requested": True,
        },
    )
    assert dropped == []
    assert filtered is not None
    assert filtered["projected_tokens_saved"] == 1234
