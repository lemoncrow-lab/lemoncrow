"""Runtime side effects observed in planned sandboxed runs, diffed base vs head (Phase 9R R7 items 10, 14)."""

from __future__ import annotations

import json
import secrets
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.gateway.test_review_check_runs_api import HEADERS, RUN_TESTS, _review
from tests.gateway.test_review_hermetic import needs_bwrap


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


CONFIG = """\
check_recipes:
  - id: unit
    kind: test
    command: python3 run_tests.py {report}
    report: junit
    inputs: [run_tests.py]
"""

APP_BASE = """\
import sqlite3


def main():
    con = sqlite3.connect(":memory:")
    con.execute("SELECT 1").fetchall()
    return helper()


def helper():
    return 1
"""

# Head adds an outbound call (TEST-NET-3, blocked by the sandbox), a file write,
# a process spawn, three inserts after a schema mutation, and one more SELECT.
APP_HEAD = """\
import socket
import sqlite3
import subprocess


def main():
    try:
        socket.create_connection(("203.0.113.7", 443), timeout=2).close()
    except OSError:
        pass
    with open("out.txt", "w") as handle:
        handle.write("x")
    subprocess.run(["git", "--version"], check=False, capture_output=True)
    con = sqlite3.connect(":memory:")
    con.execute("SELECT 1").fetchall()
    con.execute("SELECT 1").fetchall()
    con.execute("CREATE TABLE t (id INTEGER)")
    for value in (1, 2, 3):
        con.execute("INSERT INTO t VALUES (?)", (value,))
    return helper()


def helper():
    return 1
"""

TEST_APP = "from app import main\n\n\ndef test_main():\n    assert main() == 1\n"


def _files(app: str = APP_BASE) -> dict[str, str]:
    return {
        "app.py": app,
        "tests/test_app.py": TEST_APP,
        "run_tests.py": RUN_TESTS,
        ".lemoncrow/review.yaml": CONFIG,
    }


def _observe(review: SimpleNamespace, entry: str = "side_effects.runtime", *, pick: bool = True) -> Any:
    return review.client.post(
        f"/api/reviews/{review.review_id}/runtime-observation/run",
        headers=HEADERS,
        json={"revision_id": review.revision_id, "entry": entry, "pick": pick},
    )


def _state(review: SimpleNamespace) -> dict[str, Any]:
    response = review.client.get(f"/api/reviews/{review.review_id}/runtime-observation", headers=HEADERS)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _side_effect_targets(review: SimpleNamespace) -> list[dict[str, Any]]:
    response = review.client.get(f"/api/reviews/{review.review_id}/behavior", headers=HEADERS)
    assert response.status_code == 200, response.text
    behavior = response.json()["behavior"] or {}
    return [
        row
        for row in behavior.get("targets") or []
        if row["kind"] == "side_effect" and row["producer"] == "lemoncrow.runtime_observation"
    ]


def _compute(review: SimpleNamespace) -> dict[str, int]:
    response = review.client.get(f"/api/reviews/{review.review_id}/run-plan", headers=HEADERS)
    assert response.status_code == 200, response.text
    out: dict[str, int] = {}
    for row in response.json()["compute"]["rows"]:
        if row["stage"] == "run":
            key = f"{row['entry_id']}@{row['executor']}"
            out[key] = out.get(key, 0) + int(row["runs"])
    return out


def _attempt_output(review: SimpleNamespace, side: str) -> str:
    [record] = [
        json.loads(review.store.read_evidence_artifact(item) or b"{}")
        for item in review.store.list_evidence(review.review_id)
        if item.source_ref.startswith("runtime-observation:")
        and json.loads(review.store.read_evidence_artifact(item) or b"{}").get("side") == side
    ]
    return str(record["output"])


EXPECTED_TITLES = {
    "Runtime network · connect 203.0.113.7:443",
    "Runtime file written · ./out.txt",
    "Runtime process spawned · git",
    "Runtime schema mutation · CREATE TABLE t (id INTEGER)",
    "Runtime database query · INSERT INTO t VALUES (?)",
    "Runtime database query · SELECT ?",
    "Runtime database query count · 1 -> 5",
}


@needs_bwrap
def test_observed_runs_report_exact_side_effect_deltas_with_evidence(tmp_path: Path) -> None:
    review = _review(tmp_path, base=_files(), head={"app.py": APP_HEAD})
    # Auto does not escalate to the observed base-vs-head runs without a risk signal.
    assert _observe(review, pick=False).status_code == 409
    assert _compute(review) == {}

    response = _observe(review)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["plan_entry"]["origin"] == "manual" and payload["executions"] == 2
    [result] = payload["results"]
    assert result["diff"]["comparable"] is True and result["environment"]["base"] == result["environment"]["head"]
    assert {result["sides"][name]["observation"]["tier"] for name in ("base", "head")} == {"strace"}
    changes = {(row["category"], row["identity"], row["change"]) for row in result["diff"]["changes"]}
    assert changes == {
        ("network", "connect 203.0.113.7:443", "added"),
        ("file_write", "./out.txt", "added"),
        ("process", "git", "added"),
        ("schema_mutation", "CREATE TABLE t (id INTEGER)", "added"),
        ("db_query", "INSERT INTO t VALUES (?)", "added"),
        ("db_query", "SELECT ?", "changed"),
    }
    assert result["diff"]["query_count"] == {"base": 1, "head": 5}
    assert _compute(review) == {"side_effects.runtime@local": 2}

    targets = _side_effect_targets(review)
    assert {row["title"] for row in targets} == EXPECTED_TITLES
    network = next(row for row in targets if row["title"].startswith("Runtime network"))
    assert network["summary"].endswith("blocked by the sandbox and recorded.") and network["severity"] == "high"
    assert network["environment_fingerprint"].startswith("envfp:sha256:")
    detail = review.client.get(
        f"/api/reviews/{review.review_id}/behavior/targets/{network['behavior_key']}", headers=HEADERS
    ).json()["target"]["evidence"]
    refs = [ref["ref"] for ref in detail["refs"]]
    assert refs[0].startswith("artifact:") and all(
        ref.startswith("check:test:runtime-observation:") for ref in refs[1:]
    )
    assert detail["base"] is None and detail["head"]["summary"] == "connect 203.0.113.7:443 (blocked)"

    state = _state(review)
    entry = next(row for row in state["entries"] if row["id"] == "side_effects.runtime")
    assert entry["status"] == "run" and [row["recipe"]["id"] for row in state["side_effects"]] == ["unit"]
    assert len(state["limitations"]) == 2

    # A repeat request reuses both stored attempts: no new execution, no new compute.
    again = _observe(review)
    assert again.status_code == 200 and again.json()["executions"] == 0 and again.json()["reused"] == 2
    assert _compute(review) == {"side_effects.runtime@local": 2}


@needs_bwrap
def test_unchanged_behavior_yields_no_runtime_side_effect(tmp_path: Path) -> None:
    review = _review(tmp_path, base=_files(), head={"app.py": APP_BASE.replace("return 1", "return 1  # same")})
    response = _observe(review)
    assert response.status_code == 200, response.text
    [result] = response.json()["results"]
    assert result["diff"]["comparable"] is True and result["diff"]["changes"] == []
    assert result["diff"]["unchanged"] >= 1
    assert _side_effect_targets(review) == []


@needs_bwrap
def test_the_python_hook_tier_observes_the_same_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import runtime_observation

    # No syscall tracer in the sandbox runtime: the in-process audit-hook tier observes instead.
    monkeypatch.setattr(runtime_observation, "sandbox_strace", lambda: "")
    review = _review(tmp_path, base=_files(), head={"app.py": APP_HEAD})
    response = _observe(review)
    assert response.status_code == 200, response.text
    [result] = response.json()["results"]
    assert {result["sides"][name]["observation"]["tier"] for name in ("base", "head")} == {"hooks"}
    assert {row["title"] for row in _side_effect_targets(review)} == EXPECTED_TITLES


PROBE = """\
import os
import socket


def main():
    seen = []
    for path in {paths!r}:
        try:
            with open(path, "rb") as handle:
                handle.read(1)
            seen.append("READ")
        except OSError:
            seen.append("DENIED")
    seen.append("VISIBLE" if os.path.exists({home!r}) else "HIDDEN")
    seen.append("ENV" if os.environ.get("R7_PROBE_SECRET") else "NOENV")
    for host in {hosts!r}:
        try:
            socket.create_connection((host, {port}), timeout=1).close()
            seen.append("REACHED")
        except OSError:
            seen.append("UNREACHABLE")
    print("PROBE " + " ".join(seen))
    return 1
"""


def _host_address() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("203.0.113.1", 9))  # no packet is sent for a UDP connect
        address = str(probe.getsockname()[0])
    except OSError:
        address = ""
    finally:
        probe.close()
    return "" if address.startswith("127.") else address


@needs_bwrap
def test_observed_runs_cannot_read_host_secrets_env_or_reach_a_host_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = secrets.token_hex(16)
    monkeypatch.setenv("R7_PROBE_SECRET", token)
    vault = tmp_path / "host-home"
    for rel in (".ssh/id_ed25519", ".aws/credentials", ".config/gcloud/credentials.db", "notes.txt"):
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(token, encoding="utf-8")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", 0))
    listener.listen(8)
    listener.settimeout(0.2)
    port = int(listener.getsockname()[1])
    accepted: list[object] = []
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            accepted.append(conn)
            conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    lan = _host_address()
    hosts = ["127.0.0.1", *([lan] if lan else [])]
    paths = [str(vault / rel) for rel in (".ssh/id_ed25519", ".aws/credentials", ".config/gcloud/credentials.db")]
    paths.append(str(Path.home() / ".ssh" / "known_hosts"))
    probe = PROBE.format(paths=paths, home=str(Path.home()), hosts=hosts, port=port)
    try:
        review = _review(tmp_path, base=_files(), head={"app.py": probe})
        response = _observe(review)
    finally:
        stop.set()
        thread.join(timeout=5)
        listener.close()
    assert response.status_code == 200, response.text
    expected = "PROBE " + " ".join(["DENIED"] * 4 + ["HIDDEN", "NOENV"] + ["UNREACHABLE"] * len(hosts))
    assert expected in _attempt_output(review, "new")
    assert accepted == []
    [result] = response.json()["results"]
    if lan:
        head = result["sides"]["head"]["observation"]
        blocked = [row for row in head["effects"] if row["identity"] == f"connect {lan}:{port}"]
        assert blocked and blocked[0]["outcome"] == "blocked"
    stored = b"".join(
        review.store.read_evidence_artifact(item) or b"" for item in review.store.list_evidence(review.review_id)
    )
    assert token.encode() not in stored


FLOW_BASE = {
    "api/app.py": "def main():\n    return 1\n",
    "core/engine.py": "def run():\n    return 1\n",
    "store/db.py": "def save():\n    return 1\n",
    "tests/test_app.py": "from api.app import main\n\n\ndef test_main():\n    assert main() == 1\n",
    "run_tests.py": RUN_TESTS,
    ".lemoncrow/review.yaml": CONFIG,
}
FLOW_HEAD: dict[str, str | None] = {
    # app -> engine and engine -> db are called; app -> db is referenced only by an uncalled function.
    "api/app.py": (
        "from core.engine import run\nfrom store.db import save\n\n\ndef main():\n    return run()\n\n\n"
        "def debug():\n    return save()\n"
    ),
    "core/engine.py": "from store.db import save\n\n\ndef run():\n    return save()\n",
    "store/db.py": "def save():\n    return 1  # head\n",
}


@needs_bwrap
def test_flow_steps_cite_observed_traces_and_uncalled_relationships_stay_inferred(tmp_path: Path) -> None:
    review = _review(tmp_path, base=FLOW_BASE, head=FLOW_HEAD)
    before = review.client.get(f"/api/reviews/{review.review_id}/map?projection=architecture_delta", headers=HEADERS)
    assert before.status_code == 200, before.text
    flow = before.json()["architecture_delta"]["flow"]
    assert flow["applicable"] is True, flow
    assert {step["grounding"] for step in flow["steps"]} == {"inferred"}
    assert flow["observed"]["available"] is False

    assert _observe(review).status_code == 200
    after = review.client.get(f"/api/reviews/{review.review_id}/map?projection=architecture_delta", headers=HEADERS)
    flow = after.json()["architecture_delta"]["flow"]
    steps = {(step["from"], step["to"]): step for step in flow["steps"]}
    called = steps[("api/app.py", "core/engine.py")]
    assert called["grounding"] == "observed"
    assert called["trace"]["caller_fn"] == "main" and called["trace"]["callee_fn"] == "run"
    assert called["trace"]["side"] == "head" and called["trace"]["evidence_ref"].startswith(
        "check:test:runtime-observation:"
    )
    assert steps[("core/engine.py", "store/db.py")]["grounding"] == "observed"
    uncalled = steps[("api/app.py", "store/db.py")]
    assert uncalled["grounding"] == "inferred" and uncalled["trace"] is None
    observed = flow["observed"]
    assert observed["comparable"] is True and observed["grounded_steps"] == 2 and observed["inferred_steps"] == 1
    added = {(row["from"], row["from_fn"], row["to"], row["to_fn"]) for row in observed["added"]}
    assert {("api/app.py", "main", "core/engine.py", "run"), ("core/engine.py", "run", "store/db.py", "save")} <= added


# --------------------------------------------------------------------------- #
# planned execution only (deterministic runner at the registry seam)
# --------------------------------------------------------------------------- #


class _Observed:
    """A ``command`` runner returning a typed execution and a scripted observation per side."""

    runner_id = "command"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def setup_candidates(self, _repo_root: Path) -> tuple[()]:
        return ()

    def run(self, context: Any, runtime: Any, surface: Any, *, side: str) -> Any:
        from lemoncrow.pro.capabilities.review.execution import ExecutionSubject, StageOutcome
        from lemoncrow.pro.capabilities.review.runtime_observation import ObservedEffect, RuntimeObservation
        from lemoncrow.pro.capabilities.review.runtimes import SurfaceRunResult
        from tests.gateway.test_review_execution_contract import _result

        self.calls.append((runtime.id, side))
        effects = [ObservedEffect("db_query", "SELECT ?", count=1)]
        if side == "new":
            effects.append(ObservedEffect("process", "curl"))
        execution = _result(
            subject=ExecutionSubject(
                review_id=context.revision.review_id, revision_id=context.revision.id, side=side, snapshot_identity="t"
            ),
            stages=(StageOutcome("run", "passed", exit_code=0, duration_ms=1000),),
        ).to_wire()
        observation = RuntimeObservation(side=side, tier="hooks", effects=tuple(effects), query_count=1)
        return SurfaceRunResult(
            surface.id,
            surface.provider,
            side,
            "passed",
            "observed",
            self.runner_id,
            runtime.id,
            exit_code=0,
            duration_ms=1000,
            data={"execution": execution, "observation": observation.to_wire()},
        )


def _scripted(
    tmp_path: Path,
    config: str = CONFIG,
    *,
    tests: bool = True,
    shared_store: tuple[Any, Path] | None = None,
) -> tuple[SimpleNamespace, _Observed]:
    from lemoncrow.pro.capabilities.review.runtimes import RunnerRegistry

    runner = _Observed()
    files = {**_files(), ".lemoncrow/review.yaml": config}
    if not tests:
        files = {"app.py": APP_BASE, ".lemoncrow/review.yaml": config}
    review = _review(
        tmp_path,
        base=files,
        head={"app.py": APP_HEAD},
        shared_store=shared_store,
        runner_registry=RunnerRegistry((runner,)),
    )
    return review, runner


def test_runtime_observation_runs_only_as_planned_and_records_compute(tmp_path: Path) -> None:
    from lemoncrow.pro.capabilities.review.store import ReviewStore

    store_root = tmp_path / "shared-store"
    shared_store = (ReviewStore(store_root), store_root)
    review, runner = _scripted(tmp_path / "auto", shared_store=shared_store)
    unknown = _observe(review, "side_effects.everything")
    assert unknown.status_code == 422
    skipped = _observe(review, pick=False)
    assert skipped.status_code == 409 and "not in the effective run plan" in skipped.json()["detail"]
    # A non-web change: the rendering entries are inapplicable, even when picked.
    for entry in ("visual.dom_a11y", "visual.matrix"):
        refused = _observe(review, entry)
        assert refused.status_code == 409 and "not in the effective run plan" in refused.json()["detail"]
    assert runner.calls == [] and _compute(review) == {}
    picked = _observe(review)
    assert picked.status_code == 200, picked.text
    assert picked.json()["plan_entry"]["status"] == "run" and picked.json()["plan_entry"]["origin"] == "manual"
    assert runner.calls == [("observe:unit", "old"), ("observe:unit", "new")]
    assert _compute(review) == {"side_effects.runtime@local": 2}
    [target] = _side_effect_targets(review)
    assert target["title"] == "Runtime process spawned · curl"

    off, off_runner = _scripted(
        tmp_path / "off",
        CONFIG + "review:\n  capabilities:\n    deep_runtime: off\n",
        shared_store=shared_store,
    )
    refused = _observe(off)
    assert refused.status_code == 409 and "family turned off" in refused.json()["detail"]
    assert off_runner.calls == [] and _compute(off) == {}

    # No test recipe (configured or discoverable): the entry is inapplicable.
    bare, bare_runner = _scripted(tmp_path / "bare", "review: {}\n", tests=False, shared_store=shared_store)
    inapplicable = _observe(bare)
    assert inapplicable.status_code == 409 and "not in the effective run plan" in inapplicable.json()["detail"]
    assert bare_runner.calls == [] and _compute(bare) == {}

    limited, limited_runner = _scripted(
        tmp_path / "limited",
        CONFIG + "review:\n  budget:\n    per_review_minutes: 1\n",
        shared_store=shared_store,
    )
    capped = _observe(limited)
    assert capped.status_code == 409 and "Limited by budget" in capped.json()["detail"]
    assert limited_runner.calls == [] and _compute(limited) == {}


def test_a_run_needs_an_exact_snapshot(tmp_path: Path) -> None:
    review, runner = _scripted(tmp_path)
    missing = review.client.post(
        f"/api/reviews/{review.review_id}/runtime-observation/run",
        headers=HEADERS,
        json={"entry": "side_effects.runtime", "pick": True},
    )
    assert missing.status_code == 400 and runner.calls == []


def test_incomplete_runtime_observation_blocks_live_readiness_even_without_runtime_deltas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.behavior import BehaviorProduction
    from lemoncrow.pro.capabilities.review.runtime_observation import RuntimeObservationProducer

    def incomplete(_self: RuntimeObservationProducer, _context: Any) -> BehaviorProduction:
        return BehaviorProduction(degraded=("runtime observation incomplete: unit has truncated base; re-run",))

    monkeypatch.setattr(RuntimeObservationProducer, "produce", incomplete)
    review = _review(tmp_path, base=_files(), head={"app.py": APP_HEAD})
    response = review.client.get(f"/api/reviews/{review.review_id}/readiness", headers=HEADERS)
    assert response.status_code == 200, response.text
    condition = next(row for row in response.json()["conditions"] if row["id"] == "behavior-changes-judged")
    assert condition["state"] == "unknown"
    assert condition["reason"] == "incomplete_runtime_observation"
    assert "re-run" in condition["detail"]
