"""P3p exact signed job admission and local operator consent, never auto claim."""

from __future__ import annotations

import io
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from lemoncrow_client.review_agent_task_cli import _signed_task, run_manual_agent_task
from lemoncrow_client.review_worker_client import ReviewWorkerClient, WorkerBinding, validate_lease
from test_review_worker_client import NOW, MemoryTransport, sign

pytest_plugins = ["test_review_worker_client"]


def valid_lease(authority: tuple[WorkerBinding, Ed25519PrivateKey], **scope_changes: Any) -> Any:
    binding, private = authority
    scope = {
        "trigger": "manual",
        "task_id": "author-1",
        "task_kind": "investigate",
        "max_model_calls": 1,
        "max_runtime_ms": 60_000,
        "external_source_approved": True,
    }
    scope.update(scope_changes)
    return validate_lease(
        sign(
            private,
            spec_overrides={
                "execution_class": "review_intelligence",
                "capability": "review_intelligence:agent_task",
                "requires_repository_execution": False,
                "scope": scope,
            },
            document_overrides={
                "assignment": {
                    "model_route_id": "attested-route",
                    "driver": "openai",
                    "model": "safe-model-1",
                    "configuration_generation": "route-generation-1",
                    "model_data_locus": "external",
                    "provider_identity": "openai",
                }
            },
        ),
        binding,
        now=NOW,
        expected_class="review_intelligence",
    )


def test_manual_task_from_signed_lease_only(authority: tuple[WorkerBinding, Ed25519PrivateKey]) -> None:
    lease = valid_lease(authority)
    task = _signed_task(lease)
    assert task.task_id == "author-1" and task.max_model_calls == 1
    assert task.repo_id == "repo-1" and task.policy_sha256 == lease.policy_digest
    assert task.deadline_unix_ms < lease.deadline_at * 1000


@pytest.mark.parametrize(
    "scope,needle",
    [
        ({"trigger": "auto"}, "approval"),
        ({"external_source_approved": False}, "approval"),
        ({"max_model_calls": 2}, "one call"),
        ({"task_kind": "verify_recipe"}, "invalid"),
        ({"task_id": "../injected"}, "identifier"),
    ],
)
def test_signed_wrong_task_authority_refused(
    authority: tuple[WorkerBinding, Ed25519PrivateKey],
    scope: dict[str, Any],
    needle: str,
) -> None:
    with pytest.raises(ValueError):
        _signed_task(valid_lease(authority, **scope))


def test_exact_claim_refuses_wrong_or_unapproved_job(
    authority: tuple[WorkerBinding, Ed25519PrivateKey],
) -> None:
    binding, private = authority
    envelope = sign(
        private,
        spec_overrides={
            "execution_class": "review_intelligence",
            "capability": "review_intelligence:agent_task",
            "requires_repository_execution": False,
        },
    )
    wire = MemoryTransport(
        {
            "job": {
                "job_id": "job-1",
                "review_id": "review-1",
                "revision_id": "revision-1",
                "execution_class": "review_intelligence",
            },
            "envelope": envelope,
        }
    )
    client = ReviewWorkerClient(binding, transport=wire, clock=lambda: NOW)
    assert (
        client.claim(execution_class="review_intelligence", model_route_id="attested-route", exact_job_id="job-1")
        is not None
    )
    assert wire.calls[-1][1]["exact_job_id"] == "job-1"
    with pytest.raises(ValueError, match="not the explicitly requested"):
        client.claim(execution_class="review_intelligence", model_route_id="attested-route", exact_job_id="job-other")
    with pytest.raises(ValueError, match="exact job"):
        client.claim(execution_class="runtime_validation", exact_job_id="job-1")
    assert len(wire.calls) == 2
    with pytest.raises(ValueError):
        client.claim(execution_class="review_intelligence", model_route_id="attested-route", exact_job_id="../bad")
    assert len(wire.calls) == 2


def test_local_consent_and_price_validation_occurs_before_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lemoncrow_client import review_agent_task_cli as cli

    invoked: list[str] = []
    monkeypatch.setattr(cli, "load_config", lambda **_kw: invoked.append("config") or None)
    basic = [
        "job-1",
        "--route",
        "route-1",
        "--provider",
        "openai",
        "--model",
        "safe-model-1",
        "--generation",
        "g-1",
        "--input-price",
        "1000000",
        "--output-price",
        "2000000",
        "--max-cost",
        "10000",
    ]
    output = io.StringIO()
    assert run_manual_agent_task(basic, sink=output, read_secret=lambda _p: "never") == 2
    assert not invoked and "--allow-external-source" in output.getvalue()
    output = io.StringIO()
    assert run_manual_agent_task([*basic, "--allow-external-source", "--max-cost", "-3"], sink=output) == 1
    assert not invoked and "refused" in output.getvalue().lower()


_ROUTE_ARGS = [
    "--route",
    "route-1",
    "--provider",
    "openai",
    "--model",
    "safe-model-1",
    "--generation",
    "g-1",
    "--input-price",
    "1000000",
    "--output-price",
    "2000000",
    "--max-cost",
    "500000",
    "--allow-external-source",
]


def _unattended_lease(authority: tuple[WorkerBinding, Ed25519PrivateKey], *, cap: Any = 250_000, **scope: Any) -> str:
    _binding, private = authority
    base = {
        "trigger": "manual",
        "task_id": "author-1",
        "task_kind": "investigate",
        "max_model_calls": 1,
        "max_runtime_ms": 60_000,
        "external_source_approved": True,
    }
    base.update(scope)
    return sign(
        private,
        spec_overrides={
            "execution_class": "review_intelligence",
            "capability": "review_intelligence:agent_task",
            "requires_repository_execution": False,
            "scope": base,
        },
        document_overrides={
            "assignment": {
                "model_route_id": "route-1",
                "driver": "openai",
                "model": "safe-model-1",
                "configuration_generation": "g-1",
                "model_data_locus": "external",
                "provider_identity": "openai",
                "unattended": {"run_id": "run-1", "max_task_cost_micros": cap},
            }
        },
    )


def _job() -> dict[str, str]:
    return {"job_id": "job-1", "review_id": "review-1", "revision_id": "revision-1"}


def test_unattended_claim_sends_task_kinds_and_requires_the_signed_cap(
    authority: tuple[WorkerBinding, Ed25519PrivateKey],
) -> None:
    binding, _ = authority
    wire = MemoryTransport({"job": _job(), "envelope": _unattended_lease(authority)})
    client = ReviewWorkerClient(binding, transport=wire, clock=lambda: NOW)
    lease, refusal = client.claim_unattended(model_route_id="route-1", task_kinds=("investigate",))
    assert lease is not None and refusal == ""
    body = wire.calls[-1][1]
    assert body["unattended"] == {"task_kinds": ["investigate"]} and "exact_job_id" not in body
    # A lease without a server-signed budget, or for an unrequested task kind, is refused.
    for envelope in (
        sign(
            authority[1],
            spec_overrides={
                "execution_class": "review_intelligence",
                "capability": "review_intelligence:agent_task",
                "requires_repository_execution": False,
            },
        ),
        _unattended_lease(authority, cap=0),
        _unattended_lease(authority, cap="250000"),
        _unattended_lease(authority, task_kind="propose_fix"),
    ):
        refusing = ReviewWorkerClient(
            binding, transport=MemoryTransport({"job": _job(), "envelope": envelope}), clock=lambda: NOW
        )
        with pytest.raises(ValueError):
            refusing.claim_unattended(model_route_id="route-1", task_kinds=("investigate",))
    off = ReviewWorkerClient(
        binding, transport=MemoryTransport({"job": None, "unattended_refusal": "device_off"}), clock=lambda: NOW
    )
    assert off.claim_unattended(model_route_id="route-1", task_kinds=("investigate",)) == (None, "device_off")
    with pytest.raises(ValueError):
        off.claim_unattended(model_route_id="route-1", task_kinds=("verify_recipe",))


def test_unattended_start_refuses_before_any_claim_without_local_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_client import review_agent_task_cli as cli

    monkeypatch.setattr(cli, "load_config", lambda **_: pytest.fail("must refuse before config/claim"))
    out = io.StringIO()
    assert cli.run_unattended_agent_tasks(_ROUTE_ARGS[:-1], sink=out, read_secret=lambda _p: "never") == 2
    assert "--allow-external-source" in out.getvalue()
    assert cli.run_unattended_agent_tasks([*_ROUTE_ARGS, "--poll", "1"], sink=io.StringIO()) == 2
    assert cli.run_unattended_agent_tasks([], sink=io.StringIO()) == 2


def test_unattended_start_runs_under_the_signed_cap_and_exits_when_stopped(
    monkeypatch: pytest.MonkeyPatch,
    authority: tuple[WorkerBinding, Ed25519PrivateKey],
    tmp_path: Any,
) -> None:
    from types import SimpleNamespace

    from lemoncrow_client import review_agent_task_cli as cli

    binding, _ = authority
    leases = [validate_lease(_unattended_lease(authority), binding, now=NOW, expected_class="review_intelligence")]
    replies: list[tuple[Any, str]] = [
        (leases[0], ""),
        (leases[0], ""),
        (None, "daily_cost_cap"),
        (None, ""),
        (None, "stopped"),
    ]
    caps: list[int] = []
    failed: list[str] = []
    sleeps: list[float] = []

    class Transport:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def close(self) -> None:
            pass

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def claim_unattended(self, *, model_route_id: str, task_kinds: tuple[str, ...]) -> Any:
            assert model_route_id == "route-1" and task_kinds == ("investigate",)
            return replies.pop(0)

        def fail(self, lease: Any, *, reason: str, retryable: bool) -> None:
            assert retryable is False
            failed.append(reason)

    def submit(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["explicit_external_source_approval"] is True
        caps.append(args[4].route.max_cost_micros)
        if len(caps) == 2:
            raise cli.ModelBrokerRefused("over cap", may_have_charged=False)
        return SimpleNamespace(job_id=args[1].job_id, observation=SimpleNamespace(estimated_cost_micros=1200))

    monkeypatch.setattr(
        cli,
        "load_config",
        lambda **_: SimpleNamespace(hosted=True, review_state_dir=tmp_path / "review", request_timeout_s=3.0),
    )
    monkeypatch.setattr(cli, "_paired_binding", lambda _: binding)
    monkeypatch.setattr(cli, "HttpTransport", Transport)
    monkeypatch.setattr(cli, "check_machine_keys", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "ReviewWorkerClient", Client)
    monkeypatch.setattr(cli, "submit_approved_model_investigation", submit)
    out = io.StringIO()
    code = cli.run_unattended_agent_tasks(
        _ROUTE_ARGS, sink=out, read_secret=lambda _: "explicit-secret-with-length-12345", sleep=sleeps.append
    )
    assert code == 0 and not replies
    # The smaller of the local --max-cost (500000) and the signed per-task cap (250000) bounds every call.
    assert caps == [250_000, 250_000]
    assert failed == ["execution_failed"]
    assert sleeps == [30.0, 30.0]
    text = out.getvalue()
    assert "spending limit" in text and "stopped by an administrator" in text
    assert "explicit-secret" not in text


def _batch_args(job_ids: list[str], *, total: int | None = None) -> list[str]:
    args = [
        *job_ids,
        "--route",
        "route-1",
        "--provider",
        "openai",
        "--model",
        "safe-model-1",
        "--generation",
        "g-1",
        "--input-price",
        "1000000",
        "--output-price",
        "2000000",
        "--max-cost",
        "10000",
        "--allow-external-source",
    ]
    if total is not None:
        args.extend(["--max-total-cost", str(total)])
    return args


@pytest.mark.parametrize(
    "jobs,total",
    [
        (["job-1", "job-2"], None),
        (["job-1", "job-2"], 19999),
        (["job-1", "job-1"], 20000),
        (["job-1", "../invalid"], 20000),
        ([f"job-{i}" for i in range(21)], 210000),
    ],
)
def test_batch_rejects_implicit_or_unbounded_spend_before_claim(
    monkeypatch: pytest.MonkeyPatch, jobs: list[str], total: int | None
) -> None:
    from lemoncrow_client import review_agent_task_cli as cli

    monkeypatch.setattr(cli, "load_config", lambda **_: pytest.fail("batch must refuse before config/claim"))
    out = io.StringIO()
    assert cli.run_manual_agent_task(_batch_args(jobs, total=total), sink=out) == 2
    assert "No job claimed" in out.getvalue() or "no claims" in out.getvalue()


def test_batch_claims_only_preapproved_exact_jobs_and_separately_bounds_model_calls(
    monkeypatch: pytest.MonkeyPatch,
    authority: tuple[WorkerBinding, Ed25519PrivateKey],
    tmp_path: Any,
) -> None:
    from types import SimpleNamespace

    from lemoncrow_client import review_agent_task_cli as cli

    binding, _private = authority
    claims: list[str] = []
    calls: list[str] = []
    secret_reads: list[str] = []
    route: list[Any] = []

    class Transport:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def close(self) -> None:
            pass

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def claim(self, *, execution_class: str, model_route_id: str, exact_job_id: str) -> Any:
            assert execution_class == "review_intelligence" and model_route_id == "route-1"
            claims.append(exact_job_id)
            return valid_lease(authority)

    monkeypatch.setattr(
        cli,
        "load_config",
        lambda **_: SimpleNamespace(
            hosted=True,
            review_state_dir=tmp_path / "review",
            request_timeout_s=3.0,
        ),
    )
    monkeypatch.setattr(cli, "_paired_binding", lambda config: binding)
    monkeypatch.setattr(cli, "HttpTransport", Transport)
    monkeypatch.setattr(cli, "check_machine_keys", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "ReviewWorkerClient", Client)

    def submit(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["explicit_external_source_approval"] is True
        assert kwargs["api_key"] == "explicit-secret-with-length-12345"
        assert args[3].max_model_calls == 1
        assert args[4].route.max_cost_micros == 10000
        route.append(args[4])
        calls.append(args[1].job_id)
        return SimpleNamespace(job_id=args[1].job_id)

    monkeypatch.setattr(cli, "submit_approved_model_investigation", submit)
    out = io.StringIO()
    exit_code = cli.run_manual_agent_task(
        _batch_args(["job-a", "job-b"], total=20000),
        sink=out,
        read_secret=lambda prompt: secret_reads.append(prompt) or "explicit-secret-with-length-12345",
    )
    assert exit_code == 0
    assert claims == ["job-a", "job-b"]
    assert len(calls) == 2 and len(route) == 2 and route[0] is not route[1]
    assert len(secret_reads) == 1
    assert "explicit-secret" not in out.getvalue()
    assert out.getvalue().count("author_agent_unverified") == 2


def test_batch_halts_on_unknown_billing_failure_without_claiming_later_tasks(
    monkeypatch: pytest.MonkeyPatch,
    authority: tuple[WorkerBinding, Ed25519PrivateKey],
    tmp_path: Any,
) -> None:
    from types import SimpleNamespace

    from lemoncrow_client import review_agent_task_cli as cli

    binding, _ = authority
    claims: list[str] = []
    calls = 0

    class Transport:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def close(self) -> None:
            pass

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def claim(self, *, execution_class: str, model_route_id: str, exact_job_id: str) -> Any:
            claims.append(exact_job_id)
            return valid_lease(authority)

    def submit(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise cli.ModelBrokerRefused("unknown billing", may_have_charged=True)
        return SimpleNamespace(job_id=args[1].job_id)

    monkeypatch.setattr(
        cli,
        "load_config",
        lambda **_: SimpleNamespace(
            hosted=True,
            review_state_dir=tmp_path / "review",
            request_timeout_s=3.0,
        ),
    )
    monkeypatch.setattr(cli, "_paired_binding", lambda _: binding)
    monkeypatch.setattr(cli, "HttpTransport", Transport)
    monkeypatch.setattr(cli, "check_machine_keys", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "ReviewWorkerClient", Client)
    monkeypatch.setattr(cli, "submit_approved_model_investigation", submit)
    out = io.StringIO()
    assert (
        cli.run_manual_agent_task(
            _batch_args(["job-1", "job-2", "job-3"], total=30000),
            sink=out,
            read_secret=lambda _: "explicit-secret-with-length-12345",
        )
        == 1
    )
    assert claims == ["job-1", "job-2"]
    assert calls == 2
    assert "do not retry" in out.getvalue()
