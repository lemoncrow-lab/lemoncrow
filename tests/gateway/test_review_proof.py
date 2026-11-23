"""Proof (P2.1 reproductions, P2.2 red-on-base) on a tiny real repository, with a stub model.

The fixture has a base commit and a head commit that seeds an off-by-one bug in ``last_n``, fixes a bug in
``total`` (with a test that would catch it coming back), and adds a test that passes on base too. Tests run for
real (pytest in a subprocess); only the model is stubbed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.pro.capabilities.review import part_proof as pp
from lemoncrow.pro.capabilities.review import part_proof_ci as ci
from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage, StructuredExecution
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding
from lemoncrow.pro.capabilities.review.pr_bot.state import SUMMARY_MARKER, ReviewState, Usage, dump_state
from lemoncrow.pro.capabilities.review.proof import plan as plan_mod
from lemoncrow.pro.capabilities.review.proof import report as report_mod
from lemoncrow.pro.capabilities.review.proof import run as run_mod
from lemoncrow.pro.capabilities.review.proof import wire

#: Another job's tokens GitHub Actions may put in a job's environment (the identity token's request URL and bearer,
#: and the artifact-upload credential), and the keys a proof run must never hold.
ACTIONS_TOKENS = (
    "ACTIONS_ID_TOKEN_REQUEST_URL",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
    "GITHUB_TOKEN",
)
SECRETS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GH_TOKEN", *ACTIONS_TOKENS)
#: The environment GitHub Actions gives a job, as far as the sandbox decision reads it.
ACTIONS = {"GITHUB_ACTIONS": "true"}
NO_BWRAP = "bubblewrap is not installed"

BASE_LIB = """def last_n(items, n):
    return items[-n:] if n > 0 else []


def total(items):
    return sum(items[1:])


def is_even(value):
    return value % 2 == 0
"""
HEAD_LIB = """def last_n(items, n):
    return items[len(items) - n + 1:] if n > 0 else []


def total(items):
    return sum(items)


def is_even(value):
    return value % 2 == 0
"""
REPRO_LAST_N = """from mylib import last_n


def test_lemoncrow_proof_f1():
    assert last_n([1, 2, 3, 4], 2) == [3, 4]
"""
REPRO_TOTAL = """from mylib import total


def test_lemoncrow_proof_f2():
    assert total([1, 2, 3]) == 6
"""
REPRO_ENV = """import os


def test_lemoncrow_proof_env():
    seen = ",".join(sorted(os.environ))
    print("CHILD-ENV:" + seen)
    assert False, "CHILD-ENV:" + seen
"""


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def fixture_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "pyproject.toml", '[tool.pytest.ini_options]\naddopts = ""\n')
    _write(repo, "mylib.py", BASE_LIB)
    _write(
        repo,
        "tests/test_existing.py",
        "from mylib import is_even\n\n\ndef test_two_is_even():\n    assert is_even(2)\n",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "mylib.py", HEAD_LIB)
    _write(
        repo,
        "tests/test_total.py",
        "from mylib import total\n\n\ndef test_total_sums_every_item():\n    assert total([1, 2, 3]) == 6\n",
    )
    _write(
        repo, "tests/test_even.py", "from mylib import is_even\n\n\ndef test_four_is_even():\n    assert is_even(4)\n"
    )
    _write(repo, "lib/thing.rb", "def thing\n  1\nend\n")
    _write(repo, "spec/thing_spec.rb", "describe 'thing' do\n  it('is one') { expect(thing).to eq(1) }\nend\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, base, head


def _finding(identity: str, path: str, title: str, *, severity: str = "p1", thread: int = 0) -> BotFinding:
    return BotFinding(
        id=identity,
        fingerprint=f"fp-{identity}",
        severity=severity,
        category="correctness",
        state="new",
        path=path,
        line=2,
        end_line=2,
        title=title,
        explanation=title,
        thread_id=thread,
    )


def _state(head: str, base: str, *findings: BotFinding, spent: float = 0.0) -> ReviewState:
    return ReviewState(
        head_sha=head,
        base_sha=base,
        merge_base_sha=base,
        findings=findings,
        usage_total=Usage(calls=1, input_tokens=1, output_tokens=1, cost_usd=spent),
    )


FINDINGS = (
    _finding("F1", "mylib.py", "last_n returns one item too few", thread=101),
    _finding("F2", "mylib.py", "total skips the first item", thread=102),
    _finding("F3", "lib/thing.rb", "thing is off by one", severity="p0", thread=103),
    _finding("F4", "mylib.py", "naming", severity="p2"),
)


class StubModel:
    """A structured executor that answers from the finding in the prompt; counts its calls."""

    def __init__(self, answers: dict[str, tuple[str, str, str]]) -> None:
        self.answers = answers
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> StructuredExecution:
        self.calls.append(kwargs)
        blocks = json.loads(kwargs["prompt"])
        title = blocks["finding"]["value"]["title"]
        name, code, marker = self.answers[title]
        return StructuredExecution(
            result={"reproducible": True, "test_name": name, "test_code": code, "failure_marker": marker, "reason": ""},
            driver="openai",
            model="gpt-stub",
            usage=ModelCallUsage(calls=1, input_tokens=100, output_tokens=50, provider_cost=0.01, currency="USD"),
        )


ANSWERS = {
    "last_n returns one item too few": ("test_lemoncrow_proof_f1", REPRO_LAST_N, "[3, 4]"),
    "total skips the first item": ("test_lemoncrow_proof_f2", REPRO_TOTAL, ""),
}


def _plan(repo: Path, base: str, head: str, model: StubModel, **extra: Any) -> dict[str, Any]:
    return plan_mod.build_plan(
        plan_mod.PlanInputs(
            repo=repo,
            state=_state(head, base, *FINDINGS),
            head_sha=head,
            base_sha=base,
            repository="acme/widgets",
            pull_request=7,
            executor=model,
            route=lambda: ("", "openai"),
            **extra,
        )
    )


class Recorder:
    """The real bounded executor, recording each child's environment."""

    def __init__(self) -> None:
        self.envs: list[dict[str, str]] = []

    def __call__(self, argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.envs.append(dict(kwargs["env"]))
        return run_mod._default_execute(argv, **kwargs)


def _config(repo: Path, base: str, head: str, recorder: Recorder, sandbox: run_mod.Sandbox) -> run_mod.RunConfig:
    return run_mod.RunConfig(repo=repo, head_sha=head, base_sha=base, sandbox=sandbox, timeout=120, execute=recorder)


def _by(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {row[key]: row for row in rows}


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #


def test_plan_asks_once_per_open_p0_p1_finding_and_never_for_an_unsupported_language(fixture_repo) -> None:
    repo, base, head = fixture_repo
    model = StubModel(ANSWERS)
    plan = _plan(repo, base, head, model)

    rows = _by(plan["repros"], "finding_id")
    assert set(rows) == {"F1", "F2", "F3"}  # the P2 finding is not reproduced
    assert len(model.calls) == 2  # one model call per reproducible finding, none for the .rb one
    assert rows["F1"]["status"] == "planned"
    assert rows["F1"]["test_path"] == "tests/test_lemoncrow_proof_f1.py"
    assert rows["F1"]["framework"] == "pytest"
    assert "tests/test_lemoncrow_proof_f1.py" in rows["F1"]["command"]  # the base's command, not the model's
    assert rows["F1"]["thread_id"] == 101
    assert rows["F3"] == {**rows["F3"], "status": "skipped", "reason": "no test framework detected for .rb files"}
    assert plan["usage"]["calls"] == 2 and plan["usage"]["cost_usd"] == pytest.approx(0.02)
    # The prompt carries the finding as data and the head code; nothing chooses the command but the base.
    sent = json.loads(model.calls[0]["prompt"])
    assert sent["target"]["value"]["test_path"].startswith("tests/test_lemoncrow_proof_")
    assert "last_n" in sent["code"]["value"]["text"]


def test_plan_respects_the_pull_request_cost_cap(fixture_repo) -> None:
    repo, base, head = fixture_repo
    model = StubModel(ANSWERS)
    spent = plan_mod.build_plan(
        plan_mod.PlanInputs(
            repo=repo,
            state=_state(head, base, *FINDINGS, spent=3.0),
            head_sha=head,
            base_sha=base,
            max_usd=3.0,
            executor=model,
            route=lambda: ("", "openai"),
        )
    )
    assert model.calls == []
    assert all(row["status"] == "skipped" and "budget" in row["reason"] for row in spent["repros"])


def test_plan_refuses_a_summary_for_another_head(fixture_repo) -> None:
    repo, base, head = fixture_repo
    model = StubModel(ANSWERS)
    stale = plan_mod.build_plan(
        plan_mod.PlanInputs(
            repo=repo,
            state=_state(base, base, *FINDINGS),
            head_sha=head,
            base_sha=base,
            executor=model,
            route=lambda: ("", "openai"),
        )
    )
    assert stale["repros"] == [] and model.calls == []
    assert "not the head" in stale["notes"][0]


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def test_run_classifies_reproductions_and_red_on_base(fixture_repo, monkeypatch) -> None:
    repo, base, head = fixture_repo
    for name in SECRETS:
        monkeypatch.setenv(name, f"secret-{name.lower()}")
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    raw = wire.dump(plan)
    recorder = Recorder()
    results = run_mod.run_proof(
        _config(repo, base, head, recorder, run_mod.Sandbox("none", "test")), plan, plan_digest=wire.digest(raw)
    )

    repros = _by(results["repros"], "finding_id")
    assert repros["F1"]["outcome"] == "supported", repros["F1"]
    assert [run["side"] for run in repros["F1"]["runs"]] == ["head", "base"]
    assert repros["F1"]["runs"][0]["status"] == "failed" and repros["F1"]["runs"][1]["status"] == "passed"
    assert repros["F2"]["outcome"] == "contradicted", repros["F2"]
    assert repros["F3"]["outcome"] == "could_not_run"
    assert repros["F3"]["reason"] == "no test framework detected for .rb files"

    red = {(row["path"], row["test"]): row for row in results["red_on_base"]}
    assert red[("tests/test_total.py", "test_total_sums_every_item")]["outcome"] == "fails_on_base"
    assert red[("tests/test_even.py", "test_four_is_even")]["outcome"] == "passes_on_base"
    assert red[("spec/thing_spec.rb", "")]["outcome"] == "could_not_run"
    assert "no test framework detected for .rb files" in red[("spec/thing_spec.rb", "")]["reason"]
    # The repro files never land in the repository checkout.
    assert not list(repo.rglob("test_lemoncrow_proof_*"))

    # No model key or GitHub token reaches any child process.
    assert recorder.envs
    for env in recorder.envs:
        assert not set(SECRETS) & set(env), env
        assert set(env) <= {"PATH", "HOME", "TMPDIR", "CI", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE", "NO_COLOR"}
        assert not any(value.startswith("secret-") for value in env.values())


def test_the_child_process_sees_no_secret(fixture_repo, monkeypatch) -> None:
    repo, base, head = fixture_repo
    for name in SECRETS:
        monkeypatch.setenv(name, f"secret-{name.lower()}")
    answers = {"last_n returns one item too few": ("test_lemoncrow_proof_env", REPRO_ENV, "CHILD-ENV")}
    plan = plan_mod.build_plan(
        plan_mod.PlanInputs(
            repo=repo,
            state=_state(head, base, FINDINGS[0]),
            head_sha=head,
            base_sha=base,
            executor=StubModel(answers),
            route=lambda: ("", "openai"),
        )
    )
    results = run_mod.run_proof(
        _config(repo, base, head, Recorder(), run_mod.Sandbox("none", "test")), plan, plan_digest=""
    )
    head_run = results["repros"][0]["runs"][0]
    assert "CHILD-ENV:" in head_run["output_tail"] and "PATH" in head_run["output_tail"]
    for name in SECRETS:
        assert name not in head_run["output_tail"]
        assert f"secret-{name.lower()}" not in head_run["output_tail"]
    # It fails on head and on base alike: not caused by the change.
    assert results["repros"][0]["outcome"] == "inconclusive"


@pytest.mark.parametrize("mode", ["bubblewrap", "none"])
def test_no_actions_token_is_in_what_a_sandboxed_or_plain_run_is_given(fixture_repo, monkeypatch, mode: str) -> None:
    from lemoncrow.pro.capabilities.review import hermetic

    repo, base, head = fixture_repo
    # The identity-token request URL and bearer, the artifact-upload credential and the job token: all in this process.
    for name in SECRETS:
        monkeypatch.setenv(name, f"secret-{name.lower()}")
    monkeypatch.setattr(hermetic, "offline_bwrap_prefix", lambda **_: ["bwrap", "--clearenv", "--unshare-net"])
    started: list[tuple[list[str], dict[str, str]]] = []

    def execute(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        started.append((list(argv), dict(kwargs["env"])))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    plan = _plan(repo, base, head, StubModel(ANSWERS))
    config = run_mod.RunConfig(
        repo=repo, head_sha=head, base_sha=base, sandbox=run_mod.Sandbox(mode, "test"), timeout=120, execute=execute
    )
    run_mod.run_proof(config, plan, plan_digest="")
    assert started
    for argv, env in started:
        # Not in the process's own environment (``Popen`` gets exactly ``env``), ...
        assert not set(SECRETS) & set(env), env
        assert not any("secret-" in value for value in env.values())
        # ... and not handed in through the sandbox's command line (``--setenv NAME VALUE``) either.
        assert not any(name in part for name in SECRETS for part in argv), argv
        assert not any("secret-" in part for part in argv), argv
        if mode == "bubblewrap":
            assert argv[:2] == ["bwrap", "--clearenv"] and env == {"PATH": run_mod._STANDARD_PATH}


def test_a_missing_sandbox_is_could_not_run_and_never_runs(fixture_repo) -> None:
    repo, base, head = fixture_repo
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    sandbox = run_mod.resolve_sandbox("auto", require=True, probe=lambda: NO_BWRAP, environ={})
    assert not sandbox.usable
    recorder = Recorder()
    results = run_mod.run_proof(_config(repo, base, head, recorder, sandbox), plan, plan_digest="")
    assert recorder.envs == []  # nothing ran
    assert {row["outcome"] for row in results["repros"]} == {"could_not_run"}
    assert {row["outcome"] for row in results["red_on_base"]} == {"could_not_run"}
    assert "sandbox missing" in _by(results["repros"], "finding_id")["F1"]["reason"]
    # Off GitHub Actions and not --local, the same probe falls back to a plain run and says so.
    plain = run_mod.resolve_sandbox("auto", require=False, probe=lambda: NO_BWRAP, environ={})
    assert plain.mode == "none" and plain.usable and "throwaway runner" in plain.reason


@pytest.mark.parametrize("require", [False, True])
@pytest.mark.parametrize("requested", ["auto", "none", "bubblewrap"])
def test_under_actions_every_request_is_a_bubblewrap_run_or_no_run(requested: str, require: bool) -> None:
    asked: list[str] = []

    def works() -> str:
        asked.append(requested)
        return ""

    sandbox = run_mod.resolve_sandbox(requested, require=require, probe=works, environ=ACTIONS)
    assert (sandbox.mode, sandbox.usable) == ("bubblewrap", True)
    assert asked  # ``none`` does not skip the probe: it is asked for bubblewrap all the same
    # Without a working bubblewrap there is no plain fallback and no plain mode: the run is unusable.
    broken = run_mod.resolve_sandbox(requested, require=require, probe=lambda: NO_BWRAP, environ=ACTIONS)
    assert broken.mode == "bubblewrap" and not broken.usable
    assert "GitHub Actions" in broken.reason and NO_BWRAP in broken.reason


@pytest.mark.parametrize("environ", [{}, {"GITHUB_ACTIONS": "false"}, {"GITHUB_ACTIONS": ""}, {"CI": "true"}])
def test_outside_actions_the_flag_means_what_it_says(environ: dict[str, str]) -> None:
    def resolve(requested: str, *, require: bool = False, probe: str = NO_BWRAP) -> run_mod.Sandbox:
        return run_mod.resolve_sandbox(requested, require=require, probe=lambda: probe, environ=environ)

    assert resolve("none") == run_mod.Sandbox("none", "--sandbox none: ran plainly in the throwaway runner")
    refused = resolve("none", require=True)
    assert refused.mode == "none" and not refused.usable and "--sandbox none is refused" in refused.reason
    plain = resolve("auto")
    assert plain.mode == "none" and plain.usable and plain.reason == f"{NO_BWRAP}; ran plainly in the throwaway runner"
    needed = resolve("auto", require=True)
    assert needed.mode == "bubblewrap" and not needed.usable and needed.reason == f"sandbox missing: {NO_BWRAP}"
    missing = resolve("bubblewrap")
    assert missing.mode == "bubblewrap" and not missing.usable and "sandbox missing" in missing.reason
    assert resolve("auto", probe="") == run_mod.Sandbox("bubblewrap", "offline bubblewrap namespace")
    assert resolve("bubblewrap", probe="") == resolve("auto", probe="")


def test_actions_is_read_from_the_process_environment_unless_one_is_given(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert not wire.in_github_actions()
    assert run_mod.resolve_sandbox("none", require=False, probe=lambda: NO_BWRAP).mode == "none"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert wire.in_github_actions()
    assert not run_mod.resolve_sandbox("none", require=False, probe=lambda: NO_BWRAP).usable
    assert not wire.in_github_actions({})  # a given environment is the whole answer


def test_under_actions_a_plain_request_without_bubblewrap_runs_nothing_of_the_pull_request(fixture_repo) -> None:
    repo, base, head = fixture_repo
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    sandbox = run_mod.resolve_sandbox("none", require=False, probe=lambda: NO_BWRAP, environ=ACTIONS)
    recorder = Recorder()
    results = run_mod.run_proof(_config(repo, base, head, recorder, sandbox), plan, plan_digest="")
    assert recorder.envs == []  # no child process of the pull request's code, plain or not
    assert results["sandbox"]["mode"] == "bubblewrap" and "GitHub Actions" in results["sandbox"]["reason"]
    assert {row["outcome"] for row in results["repros"]} == {"could_not_run"}
    assert {row["outcome"] for row in results["red_on_base"]} == {"could_not_run"}
    assert "GitHub Actions requires bubblewrap" in _by(results["repros"], "finding_id")["F1"]["reason"]
    runs = [run for row in results["repros"] for run in row["runs"]] + results["red_runs"]
    assert (
        runs and {run["status"] for run in runs} == {"not_run"} and {run["sandbox"] for run in runs} == {"bubblewrap"}
    )
    assert all(NO_BWRAP in run["output_tail"] for run in runs)
    # Nothing ran, so there is no plain run for the report job to refuse: it reports every part as not run.
    assert report_mod.plain_run(results) == ""
    assert report_mod.summarize(plan, wire.dump(plan), results).conclusion == "neutral"


def test_cli_run_under_actions_ignores_sandbox_none_and_runs_nothing_without_bubblewrap(
    fixture_repo, tmp_path, monkeypatch
) -> None:
    from lemoncrow.gateway.cli.commands.review import review_cmd
    from lemoncrow.pro.capabilities.review import hermetic
    from lemoncrow.pro.environment import sandbox_limits

    repo, base, head = fixture_repo
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    plan_path, results_path = tmp_path / "plan.json", tmp_path / "results.json"
    wire.write(plan_path, plan)

    def no_bubblewrap(*_args: Any, **_kwargs: Any) -> Any:
        raise hermetic.HermeticUnavailable(NO_BWRAP)

    def started(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a process of the pull request's code was started")

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(hermetic, "offline_bwrap_prefix", no_bubblewrap)
    monkeypatch.setattr(sandbox_limits, "run_limited_bounded", started)
    ran = CliRunner().invoke(
        review_cmd,
        [
            "proof",
            "run",
            "--repo",
            str(repo),
            "--plan",
            str(plan_path),
            "--sandbox",
            "none",
            "--out",
            str(results_path),
        ],
        obj={"root": str(tmp_path / "lc-root")},
    )
    assert ran.exit_code == 0, ran.output  # the step wrote its file; what it says is that nothing ran
    assert "sandbox bubblewrap" in ran.output and "GitHub Actions requires bubblewrap" in ran.output
    document = json.loads(results_path.read_text(encoding="utf-8"))
    assert document["sandbox"]["mode"] == "bubblewrap"
    assert {row["outcome"] for row in document["repros"]} == {"could_not_run"}
    assert {row["outcome"] for row in document["red_on_base"]} == {"could_not_run"}


def test_a_proof_that_could_not_start_its_sandbox_reads_back_as_proof_sandbox_missing(fixture_repo) -> None:
    """The whole way a hosted Review learns it: run, render, parse the check's text, project onto the parts."""

    repo, base, head = fixture_repo
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    raw = wire.dump(plan)
    sandbox = run_mod.resolve_sandbox("auto", require=False, probe=lambda: NO_BWRAP, environ=ACTIONS)
    recorder = Recorder()
    results = run_mod.run_proof(_config(repo, base, head, recorder, sandbox), plan, plan_digest=wire.digest(raw))
    assert recorder.envs == []
    red = {(row["path"], row["reason"]) for row in results["red_on_base"]}
    assert ("tests/test_total.py", wire.RED_SANDBOX_MISSING) in red

    text = report_mod.check_summary(report_mod.summarize(plan, raw, results))[0]
    parsed = ci.parse_report(text)
    # The report is the Action's own and says bubblewrap, so hosted believes what it says: nothing ran.
    assert parsed is not None and parsed.sandboxed
    revision = SimpleNamespace(id="r1", head_sha=head, base_sha=base, merge_base_sha=base, range_mode="commit_range")
    parts = frozenset({"tests/test_total.py", "tests/test_even.py", "mylib.py"})
    shown = ci.outcomes_for(parsed, revision, parts)
    assert shown is not None
    missing = pp.PartOutcome(pp.COULDNT_RUN, "proof_sandbox_missing")
    # Every changed test the report names reads the one reason; a source file has no row, as for any report.
    assert shown == {"tests/test_total.py": missing, "tests/test_even.py": missing}
    assert pp.REASONS["proof_sandbox_missing"]
    assert "proof_sandbox_missing" not in pp.UNTRUSTED_CI_REASONS  # CI's own, believed word, not a check refused
    # The run also says it in one fixed place, for a part that has no row (a source file, a PR that changes no test).
    assert results["sandbox"]["usable"] is False and parsed.sandbox_missing
    usable = run_mod.run_proof(
        _config(repo, base, head, Recorder(), run_mod.Sandbox("none", "test")), plan, plan_digest=wire.digest(raw)
    )
    assert usable["sandbox"]["usable"] is True
    working = ci.parse_report(report_mod.check_summary(report_mod.summarize(plan, raw, usable))[0])
    assert working is not None and not working.sandbox_missing


def _dependency_change(fixture_repo: tuple[Path, str, str]) -> tuple[Path, str, str]:
    """``(repo, base, head)`` where head also bumps ``requirements-dev.txt`` and adds a test the base's dependencies fail."""

    repo, base, _head = fixture_repo
    _write(repo, "requirements-dev.txt", "pytest==9.9\nnewlib==2.0\n")
    _write(repo, "tests/test_newlib.py", "def test_needs_newlib():\n    assert False, 'newlib 2.0 is not installed'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bump")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def _run_with_manifests(
    repo: Path, base: str, head: str, plan: dict[str, Any], manifests: tuple[str, ...]
) -> dict[str, Any]:
    config = _config(repo, base, head, Recorder(), run_mod.Sandbox("none", "test"))
    config.deps_manifests = manifests
    return run_mod.run_proof(config, plan, plan_digest=wire.digest(wire.dump(plan)))


def test_a_changed_dependency_manifest_turns_a_head_failure_and_a_supported_reproduction_into_no_verdict(
    fixture_repo,
) -> None:
    repo, base, head = _dependency_change(fixture_repo)
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    changed = _run_with_manifests(repo, base, head, plan, ("requirements-dev.txt",))

    red = {(row["path"], row["test"]): row for row in changed["red_on_base"]}
    failing = red[("tests/test_newlib.py", "test_needs_newlib")]
    assert (failing["outcome"], failing["reason"]) == ("could_not_run", wire.RED_DEPS_CHANGED)
    # What passes on head is untouched: the downgrade is for a head failure only.
    assert red[("tests/test_total.py", "test_total_sums_every_item")]["outcome"] == "fails_on_base"
    repros = _by(changed["repros"], "finding_id")
    assert repros["F1"]["outcome"] == "inconclusive" and "requirements-dev.txt" in repros["F1"]["reason"]
    # Passing on head proves nothing either: the head's own dependencies were never installed.
    assert repros["F2"]["outcome"] == "inconclusive"
    assert "passes on head, but with the base's dependencies" in repros["F2"]["reason"]
    assert "requirements-dev.txt" in repros["F2"]["reason"]
    assert any("requirements-dev.txt" in note for note in changed["notes"])
    raw = wire.dump(plan)
    assert report_mod.summarize(plan, raw, changed).conclusion == "neutral"  # not the red check a `supported` gives

    # What a hosted Review reads back from the check's text: no "Fails", no "Supported", one named reason.
    # (The run here is plain, so say bubblewrap the way the Actions job's own results do.)
    sandboxed = _relabelled(changed, "bubblewrap")
    parsed = ci.parse_report(report_mod.check_summary(report_mod.summarize(plan, raw, sandboxed))[0])
    revision = SimpleNamespace(id="r1", head_sha=head, base_sha=base, merge_base_sha=base, range_mode="commit_range")
    shown = ci.outcomes_for(parsed, revision, frozenset({"tests/test_newlib.py", "tests/test_total.py"}))  # type: ignore[arg-type]
    assert shown is not None
    assert shown["tests/test_newlib.py"] == pp.PartOutcome(pp.COULDNT_RUN, "proof_deps_changed")
    assert shown["tests/test_total.py"].outcome == pp.PROVEN


def test_a_changed_dependency_manifest_never_lets_a_passing_reproduction_turn_the_check_green(fixture_repo) -> None:
    """The base's dependencies passed the reproduction; the head's were never installed, so nothing is contradicted."""

    repo, base, _head = fixture_repo
    _write(repo, "requirements-dev.txt", "pytest==9.9\nnewlib==2.0\n")
    # Without the other changed tests (one that cannot run, one the base also passes) the only thing between this
    # run and a green check is the reproduction.
    _git(repo, "rm", "-q", "tests/test_even.py", "spec/thing_spec.rb")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bump")
    head = _git(repo, "rev-parse", "HEAD")
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    plan["repros"] = [repro for repro in plan["repros"] if repro["finding_id"] == "F2"]
    raw = wire.dump(plan)

    unwatched = _run_with_manifests(repo, base, head, plan, ())
    assert _by(unwatched["repros"], "finding_id")["F2"]["outcome"] == "contradicted"
    assert [row["outcome"] for row in unwatched["red_on_base"]] == ["fails_on_base"]
    assert report_mod.summarize(plan, raw, unwatched).conclusion == "success"  # what the bump alone must not earn

    watched = _run_with_manifests(repo, base, head, plan, ("requirements-dev.txt",))
    assert _by(watched["repros"], "finding_id")["F2"]["outcome"] == "inconclusive"
    assert [row["outcome"] for row in watched["red_on_base"]] == ["fails_on_base"]  # both sides share the base's deps
    summary = report_mod.summarize(plan, raw, watched)
    assert summary.conclusion == "neutral"
    reply = report_mod.thread_reply(summary.findings[0], summary)
    assert "Contradicted" not in reply and "requirements-dev.txt" in reply


@pytest.mark.parametrize("manifests", [(), ("package-lock.json",), ("requirements.txt", "uv.lock")])
def test_a_manifest_the_pull_request_leaves_alone_downgrades_nothing(fixture_repo, manifests: tuple[str, ...]) -> None:
    repo, base, head = _dependency_change(fixture_repo)
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    unchanged = _run_with_manifests(repo, base, head, plan, manifests)

    red = {(row["path"], row["test"]): row for row in unchanged["red_on_base"]}
    failing = red[("tests/test_newlib.py", "test_needs_newlib")]
    assert (failing["outcome"], failing["reason"]) == ("could_not_run", wire.RED_HEAD_FAILED)  # a real failure
    assert _by(unchanged["repros"], "finding_id")["F1"]["outcome"] == "supported"
    assert not any("did not install" in note for note in unchanged["notes"])
    assert report_mod.summarize(plan, wire.dump(plan), unchanged).conclusion == "failure"


def test_changed_manifests_sees_every_way_a_file_is_touched(tmp_path: Path) -> None:
    repo = tmp_path / "manifests"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "requirements-dev.txt", "pytest==8.0\n")
    _write(repo, "package.json", '{"name": "a", "dependencies": {"left-pad": "1.0.0"}}\n')
    _write(repo, "old/name.txt", "".join(f"line {n} of a file that only moves\n" for n in range(30)))
    _write(repo, "untouched.txt", "same\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "requirements-dev.txt", "pytest==9.0\n")
    _git(repo, "rm", "-q", "package.json")
    (repo / "new").mkdir()
    _git(repo, "mv", "old/name.txt", "new/name.txt")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")

    names = ["requirements-dev.txt", "package.json", "old/name.txt", "new/name.txt", "untouched.txt", "missing.txt"]
    # modified, deleted, renamed away from, renamed into: touched. Same content and absent files: not.
    assert run_mod.changed_manifests(repo, base, head, names) == [
        "new/name.txt",
        "old/name.txt",
        "package.json",
        "requirements-dev.txt",
    ]
    assert run_mod.changed_manifests(repo, base, head, ["./requirements-dev.txt"]) == ["requirements-dev.txt"]
    assert run_mod.changed_manifests(repo, base, head, []) == []
    assert run_mod.changed_manifests(repo, base, base, names) == []
    # What the tests are chosen from is as before: files the change adds, modifies or renames into, never a deletion.
    assert sorted(run_mod.changed_paths(repo, base, head)) == ["new/name.txt", "requirements-dev.txt"]


def test_cli_run_takes_repeatable_deps_manifests_and_refuses_an_unsafe_or_missing_path(
    fixture_repo, tmp_path, monkeypatch
) -> None:
    from lemoncrow.gateway.cli.commands.review import review_cmd

    repo, base, head = _dependency_change(fixture_repo)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)  # off Actions, --sandbox none runs plainly
    obj = {"root": str(tmp_path / "lc-root")}
    common = ["proof", "run", "--repo", str(repo), "--base", base, "--head", head, "--sandbox", "none", "--json"]
    ran = CliRunner().invoke(
        review_cmd,
        [*common, "--deps-manifest", "pyproject.toml", "--deps-manifest", "requirements-dev.txt"],
        obj=obj,
    )
    assert ran.exit_code == 0, ran.output
    document = json.loads(ran.output)
    red = {row["path"]: row for row in document["red_on_base"]}
    assert red["tests/test_newlib.py"]["reason"] == wire.RED_DEPS_CHANGED
    only_other = CliRunner().invoke(review_cmd, [*common, "--deps-manifest", "pyproject.toml"], obj=obj)
    assert only_other.exit_code == 0, only_other.output
    red = {row["path"]: row for row in json.loads(only_other.output)["red_on_base"]}
    assert red["tests/test_newlib.py"]["reason"] == wire.RED_HEAD_FAILED
    for bad in ("../requirements-dev.txt", "/etc/passwd", "-x", "a b"):
        refused = CliRunner().invoke(review_cmd, [*common, "--deps-manifest", bad], obj=obj)
        assert refused.exit_code != 0 and "--deps-manifest" in refused.output, bad
    # A path that is in neither commit would never match a change: refused, not a guard that is silently off.
    # A directory is in the tree but is no file the install could have read: `cat-file -e` would accept it.
    for gone in ("requirements-dev.tx", "package-lock.json", "sub/requirements-dev.txt", "tests", "tests/"):
        refused = CliRunner().invoke(review_cmd, [*common, "--deps-manifest", gone], obj=obj)
        assert refused.exit_code != 0, gone
        assert "neither the base nor the head commit" in refused.output and gone in refused.output
        assert "relative to" in refused.output


def _stub(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture()
def programs(fixture_repo, tmp_path, monkeypatch) -> SimpleNamespace:
    """A system directory of working stand-ins, and look-alikes first on ``PATH`` that record being run."""

    system, fake = tmp_path / "system", tmp_path / "fake"
    system.mkdir()
    fake.mkdir()
    marker = tmp_path / "hijacked"
    real_git = shutil.which("git")
    assert real_git
    stubs = {"bwrap": "exit 0", "env": "exit 0", "systemd-run": "exit 0", "git": f'exec "{real_git}" "$@"'}
    for name, body in stubs.items():
        _stub(system / name, body)
    for name in (*stubs, "bubblewrap"):
        _stub(fake / name, f'echo "$0" >> "{marker}"; exit 1')
    from lemoncrow.pro.environment import sandbox_limits

    monkeypatch.setattr(wire, "SYSTEM_PATH", str(system))
    monkeypatch.setattr(sandbox_limits, "SYSTEM_PATH", str(system))
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    return SimpleNamespace(system=system, fake=fake, marker=marker)


def test_under_actions_no_program_the_proof_starts_outside_the_sandbox_is_found_on_path(
    fixture_repo, programs, monkeypatch
) -> None:
    from lemoncrow.gateway.cli.commands import review_proof_cli
    from lemoncrow.pro.environment import sandbox_limits

    repo, base, head = fixture_repo
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    plan = _plan(repo, base, head, StubModel(ANSWERS))  # the plan job's ``git diff``
    sandbox = run_mod.resolve_sandbox("auto", require=False)  # the real probe, from the environment
    assert sandbox == run_mod.Sandbox("bubblewrap", "offline bubblewrap namespace")
    started: list[list[str]] = []

    def execute(argv: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        started.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    config = run_mod.RunConfig(repo=repo, head_sha=head, base_sha=base, sandbox=sandbox, timeout=120, execute=execute)
    results = run_mod.run_proof(config, plan, plan_digest="")  # materialize, detect, changed_paths: all git
    assert started and {argv[0] for argv in started} == {str(programs.system / "bwrap")}
    assert {row["path"] for row in results["red_on_base"]} >= {"tests/test_total.py", "tests/test_even.py"}
    assert review_proof_cli._commit(repo, head, "head") == head  # the CLI's own ``git``
    sandbox_limits._cgroup_scope_runner.cache_clear()
    try:
        runner = sandbox_limits._cgroup_scope_runner()  # what stands in front of the sandbox
    finally:
        sandbox_limits._cgroup_scope_runner.cache_clear()
    assert runner[0] == str(programs.system / "env") and str(programs.system / "systemd-run") in runner
    assert not programs.marker.exists(), programs.marker.read_text(encoding="utf-8")  # no look-alike ever ran


def test_outside_actions_the_same_programs_are_still_found_on_path(fixture_repo, programs, monkeypatch) -> None:
    repo, base, head = fixture_repo
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert wire.system_path() is None and wire.system_executable("git") == "git"
    assert run_mod._bwrap_prefix()[0] == str(programs.fake / "bwrap")
    with pytest.raises(ValueError, match="git diff"):
        run_mod.changed_paths(repo, base, head)
    assert programs.marker.exists()  # the look-alike ``git`` is the one that ran


def test_a_program_missing_from_the_system_path_is_absolute_never_a_path_lookup(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(wire, "SYSTEM_PATH", str(tmp_path))  # an empty directory
    assert wire.system_executable("git", ACTIONS) == "/usr/bin/git"
    assert wire.system_executable("git", {}) == "git"
    assert wire.system_path(ACTIONS) == str(tmp_path) and wire.system_path({}) is None


@pytest.mark.skipif(sys.platform != "linux" or bool(run_mod.probe_bubblewrap()), reason="bubblewrap does not work here")
def test_run_in_bubblewrap_with_a_mounted_toolchain(fixture_repo) -> None:
    repo, base, head = fixture_repo
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    venv_bin = str(Path(sys.prefix) / "bin")
    config = run_mod.RunConfig(
        repo=repo,
        head_sha=head,
        base_sha=base,
        sandbox=run_mod.Sandbox("bubblewrap", "test"),
        toolchain=tuple(dict.fromkeys((Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()))),
        timeout=120,
        host_path=f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}",
    )
    results = run_mod.run_proof(config, plan, plan_digest="")
    repros = _by(results["repros"], "finding_id")
    assert repros["F1"]["outcome"] == "supported", repros["F1"]
    assert repros["F2"]["outcome"] == "contradicted", repros["F2"]
    assert {run["sandbox"] for run in repros["F1"]["runs"]} == {"bubblewrap"}


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


class FakeGitHub:
    def __init__(self) -> None:
        self.posts: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: Any, stdin: str | None = None) -> str:
        argv = list(args)
        if "--paginate" in argv:
            return "[[]]"
        body = json.loads(stdin or "{}")
        self.posts.append((argv, body))
        if argv[-3].endswith("/check-runs"):
            return json.dumps({"html_url": "https://github.com/acme/widgets/runs/1"})
        return json.dumps(
            {
                "id": 900 + len(self.posts),
                "body": body["body"],
                "user": {"login": "bot", "type": "Bot"},
                "html_url": "u",
            }
        )

    def check(self) -> dict[str, Any]:
        return next(body for argv, body in self.posts if argv[-3].endswith("/check-runs"))


def _post(plan: dict[str, Any], results: dict[str, Any] | None) -> tuple[FakeGitHub, dict[str, Any]]:
    github = FakeGitHub()
    summary = report_mod.summarize(plan, wire.dump(plan), results)
    posted = report_mod.post(summary, repository="acme/widgets", number=7, host="github.com", runner=github)
    return github, posted


def test_report_posts_failure_for_a_reproduced_bug_and_replies_on_each_thread(fixture_repo) -> None:
    repo, base, head = fixture_repo
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    results = run_mod.run_proof(
        _config(repo, base, head, Recorder(), run_mod.Sandbox("none", "bubblewrap is not installed")),
        plan,
        plan_digest=wire.digest(wire.dump(plan)),
    )
    github, posted = _post(plan, results)
    check = github.check()
    assert check["name"] == "LemonCrow Proof" and check["head_sha"] == head
    assert check["conclusion"] == posted["conclusion"] == "failure"
    assert check["output"]["title"] == "1 supported, 1 contradicted, 1 could not run, added tests: 1 of 3 fail on base"
    assert "Sandbox: `none`" in check["output"]["summary"]
    replies = {argv[3]: body["body"] for argv, body in github.posts if "/replies" in argv[3]}
    assert set(replies) == {f"repos/acme/widgets/pulls/7/comments/{thread}/replies" for thread in (101, 102, 103)}
    first = replies["repos/acme/widgets/pulls/7/comments/101/replies"]
    assert first.startswith(f"<!-- lemoncrow:proof F1 {head} -->")
    assert "**LemonCrow Proof: Supported**" in first and "assert last_n([1, 2, 3, 4], 2) == [3, 4]" in first


def test_report_is_neutral_when_nothing_could_run_never_success(fixture_repo) -> None:
    repo, base, head = fixture_repo
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    plan["repros"] = [row for row in plan["repros"] if row["finding_id"] == "F3"]  # the .rb finding alone
    raw = wire.dump(plan)
    results = {
        "schema": wire.RESULTS_SCHEMA,
        "head_sha": head,
        "base_sha": base,
        "plan_digest": wire.digest(raw),
        "sandbox": {"mode": "none", "reason": "test"},
        "repros": [{"finding_id": "F3", "outcome": "could_not_run", "reason": "no framework", "runs": []}],
        "red_on_base": [{"path": "spec/thing_spec.rb", "test": "", "outcome": "could_not_run", "reason": "x"}],
        "red_runs": [],
    }
    github, posted = _post(plan, results)
    assert github.check()["conclusion"] == posted["conclusion"] == "neutral"
    # And with no results at all (the run job died), still neutral.
    github, posted = _post(plan, None)
    assert github.check()["conclusion"] == "neutral"


def test_report_believes_no_outcome_without_a_matching_recorded_run(fixture_repo) -> None:
    repo, base, head = fixture_repo
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    raw = wire.dump(plan)
    forged_run = {"side": "head", "status": "failed", "exit_code": 1, "command": [], "output_tail": ""}
    results = {
        "schema": wire.RESULTS_SCHEMA,
        "head_sha": head,
        "plan_digest": wire.digest(raw),
        "repros": [
            {"finding_id": "F1", "outcome": "contradicted", "reason": "forged", "runs": [forged_run]},
            {"finding_id": "F2", "outcome": "contradicted", "reason": "forged", "runs": []},
        ],
        "red_on_base": [{"path": "tests/test_total.py", "test": "t", "outcome": "fails_on_base", "reason": "x"}],
        "red_runs": [],
    }
    summary = report_mod.summarize(plan, raw, results)
    outcomes = {item.plan["finding_id"]: item.outcome for item in summary.findings}
    assert outcomes == {"F1": "inconclusive", "F2": "could_not_run", "F3": "could_not_run"}
    assert summary.red[0]["outcome"] == "could_not_run"
    assert summary.conclusion == "neutral"
    # A results file for another plan is not used at all.
    other = report_mod.summarize(plan, raw, {**results, "plan_digest": "sha256:" + "0" * 64})
    assert {item.outcome for item in other.findings} == {"could_not_run"}


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"GITHUB_RUN_ID": "9000123", "GITHUB_RUN_ATTEMPT": "2"}, "9000123:2"),
        ({"GITHUB_RUN_ID": "9000123"}, ""),
        ({"GITHUB_RUN_ATTEMPT": "1"}, ""),
        ({"GITHUB_RUN_ID": "abc", "GITHUB_RUN_ATTEMPT": "1"}, ""),
        ({"GITHUB_RUN_ID": "12:3", "GITHUB_RUN_ATTEMPT": "1"}, ""),
        ({"GITHUB_RUN_ID": "123\n", "GITHUB_RUN_ATTEMPT": "1"}, ""),
        ({"GITHUB_RUN_ID": "1" * 20, "GITHUB_RUN_ATTEMPT": "1"}, ""),
        ({}, ""),
    ],
)
def test_the_check_names_its_workflow_run_from_the_actions_environment(env: dict[str, str], expected: str) -> None:
    assert report_mod.run_external_id(env) == expected


def test_the_report_file_holds_exactly_the_summary_the_check_carries(fixture_repo, tmp_path, monkeypatch) -> None:
    repo, base, head = fixture_repo
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    summary = report_mod.summarize(plan, wire.dump(plan), None)
    github, target = FakeGitHub(), tmp_path / "out" / "lemoncrow-proof-report.json"
    target.parent.mkdir()
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://token.example/oidc?api-version=2.0")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "request-bearer")
    asked: list[Any] = []

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        asked.append(request)
        return _Reply({"value": "header.claims.signature"})

    monkeypatch.setattr(report_mod.urllib.request, "urlopen", fake_urlopen)
    report_mod.post(
        summary,
        repository="acme/widgets",
        number=7,
        host="github.com",
        runner=github,
        external_id="9000123:2",
        report_file=target,
    )
    check = github.check()
    assert check["external_id"] == "9000123:2"
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document == {
        "schema": wire.REPORT_FILE_SCHEMA,
        "report": check["output"]["summary"],
        "token": "header.claims.signature",
    }
    assert document["report"] != ""
    # The token is asked for with the audience that binds the commit and this exact report, and goes nowhere else.
    [request] = asked
    audience = wire.report_audience(check["head_sha"], check["output"]["summary"])
    assert request.full_url == "https://token.example/oidc?api-version=2.0&audience=" + audience.replace(":", "%3A")
    assert request.get_header("Authorization") == "bearer request-bearer"
    assert "header.claims.signature" not in json.dumps(check) and "header.claims.signature" not in str(github.__dict__)
    # No run (a local dry run): no pointer is sent, and nothing else about the check changes.
    plain = FakeGitHub()
    report_mod.post(summary, repository="acme/widgets", number=7, host="github.com", runner=plain)
    assert "external_id" not in plain.check() and plain.check()["output"] == check["output"]


class _Reply:
    """What ``urlopen`` hands back: a context manager with a JSON body."""

    def __init__(self, document: object) -> None:
        self._body = json.dumps(document).encode()

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self._body[:limit] if limit >= 0 else self._body


def test_the_audience_binds_the_commit_and_the_exact_report() -> None:
    head, text = "a" * 40, "**report**\n"
    audience = wire.report_audience(head, text)
    assert audience.startswith(wire.AUDIENCE_PREFIX) and len(audience) == len(wire.AUDIENCE_PREFIX) + 64
    assert audience == wire.report_audience(head.upper(), text)  # the sha is compared in lower case
    assert audience != wire.report_audience("b" * 40, text) and audience != wire.report_audience(head, text + " ")


@pytest.mark.parametrize(
    ("environ", "why"),
    [
        ({}, "no GitHub identity token"),
        ({"ACTIONS_ID_TOKEN_REQUEST_URL": "https://t.example/x"}, "no GitHub identity token"),
        ({"ACTIONS_ID_TOKEN_REQUEST_TOKEN": "b"}, "no GitHub identity token"),
        ({"ACTIONS_ID_TOKEN_REQUEST_URL": "http://t.example/x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "b"}, "not https"),
    ],
)
def test_without_a_way_to_ask_for_a_token_the_report_goes_out_unattested(environ: dict[str, str], why: str) -> None:
    def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no request must be made")

    token, note = report_mod.identity_token(environ, "aud", urlopen=never)
    assert token == "" and why in note


@pytest.mark.parametrize("answer", [{}, {"value": ""}, {"value": 3}, [], "x"])
def test_an_answer_without_a_token_is_a_note_not_a_crash(answer: object) -> None:
    environ = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://t.example/x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "b"}
    token, note = report_mod.identity_token(environ, "aud", urlopen=lambda *_a, **_k: _Reply(answer))
    assert token == "" and "no token" in note


def test_a_failed_token_request_is_a_note_that_never_carries_the_bearer() -> None:
    environ = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://t.example/x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "s3cret"}

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("connection refused")

    token, note = report_mod.identity_token(environ, "aud", urlopen=refused)
    assert token == "" and "could not get" in note and "s3cret" not in note


def _recorded(
    *, top: object = "bubblewrap", repro: object = "bubblewrap", red: object = "bubblewrap"
) -> dict[str, Any]:
    """A results document that names its sandbox *top*, and *repro* and *red* on a run of each kind."""

    return {
        "schema": wire.RESULTS_SCHEMA,
        "sandbox": {"mode": top, "reason": ""},
        "repros": [{"finding_id": "F1", "runs": [{"side": "head", "sandbox": repro}]}],
        "red_runs": [{"side": "base", "sandbox": red}],
    }


@pytest.mark.parametrize(
    "document",
    [
        _recorded(top="none"),
        _recorded(repro="none"),
        _recorded(red="none"),
        _recorded(top="bubblewrap2"),
        _recorded(top=None),
        _recorded(repro=3),
        {"schema": wire.RESULTS_SCHEMA, "repros": [], "red_runs": []},
        {**_recorded(), "sandbox": "bubblewrap"},
    ],
)
def test_a_results_file_that_records_a_plain_or_unreadable_run_is_found(document: dict[str, Any]) -> None:
    assert "without a sandbox" in report_mod.plain_run(document)


def test_a_results_file_whose_every_run_was_sandboxed_is_not_refused() -> None:
    assert report_mod.plain_run(_recorded()) == ""
    assert report_mod.plain_run({**_recorded(), "repros": [], "red_runs": []}) == ""  # nothing ran, and says so


def test_a_withheld_report_is_written_without_a_token_and_none_is_asked_for(tmp_path) -> None:
    def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no request must be made")

    environ = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://t.example/x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "b"}
    payload = {"head_sha": "a" * 40, "output": {"summary": "the report"}}
    target = tmp_path / "report.json"
    note = report_mod.write_report(payload, target, environ=environ, urlopen=never, withhold="not attested: why")
    assert note == "not attested: why"
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "schema": wire.REPORT_FILE_SCHEMA,
        "report": "the report",
        "token": "",
    }


class SummaryGitHub(FakeGitHub):
    """The report's fake, with a LemonCrow summary on the pull request that a PATCH rewrites."""

    def __init__(self, body: str) -> None:
        super().__init__()
        self.body = body

    def __call__(self, args: Any, stdin: str | None = None) -> str:
        argv = list(args)
        if "--paginate" in argv and argv[-1].startswith("repos/acme/widgets/issues/7/comments"):
            row = {"id": 55, "body": self.body, "user": {"login": "bot", "type": "Bot"}, "html_url": "s"}
            return json.dumps([[row]])
        if argv[:3] == ["api", "--method", "PATCH"]:
            self.body = json.loads(stdin or "{}")["body"]
            return json.dumps({"id": 55, "body": self.body, "user": {"login": "bot", "type": "Bot"}})
        if argv[-3:-2] == ["repos/acme/widgets/check-runs"]:
            self.posts.append((argv, json.loads(stdin or "{}")))
            return json.dumps({"id": 4242, "html_url": "https://github.com/acme/widgets/runs/4242"})
        return super().__call__(args, stdin)


def test_report_writes_the_proof_line_and_the_plans_spend_into_the_summary_once(fixture_repo) -> None:
    from lemoncrow.pro.capabilities.review.pr_bot.render import proof_result_line
    from lemoncrow.pro.capabilities.review.pr_bot.state import parse_state

    repo, base, head = fixture_repo
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    raw = wire.dump(plan)
    results = run_mod.run_proof(
        _config(repo, base, head, Recorder(), run_mod.Sandbox("none", "bubblewrap is not installed")),
        plan,
        plan_digest=wire.digest(raw),
    )
    state = _state(head, base, *FINDINGS, spent=0.5)
    lead = "<h2>Changes requested</h2>\n\n**Reviewed 3 files** · A person should review this"
    github = SummaryGitHub(
        f"{SUMMARY_MARKER}\n<sub>x</sub>\n\n{lead}\n\n---\n\nrest\n\n---\n<sub>Independence: x<br>footer</sub>\n\n{dump_state(state)}"
    )
    summary = report_mod.summarize(plan, raw, results)
    for _ in range(2):  # a re-run report replaces the line and does not count the plan twice
        posted = report_mod.post(summary, repository="acme/widgets", number=7, host="github.com", runner=github)
        assert posted["summary"] == f"recorded on the summary for {head[:12]}" and not posted["errors"]
    line = (
        f"**Proof** on `{head[:7]}`: 1 supported, 1 contradicted, 0 inconclusive, 1 could not run; new tests: 1 of 3 "
        "fail on base · [LemonCrow Proof](https://github.com/acme/widgets/runs/4242)"
    )
    assert github.body.count("**Proof** on ") == 1
    assert f"{lead}\n\n{line}\n\n---" in github.body, "last on the first screen, before the rule"
    written = parse_state(github.body)
    assert written is not None and written.proof.startswith(f"{head[:12]}:s1:c1:i0:n1:rb1/3:p")
    assert written.proof_usage.calls == 2 and written.proof_usage.cost_usd == pytest.approx(0.02)
    assert written.spent.cost_usd == pytest.approx(0.52)  # what the budget reads
    # A summary of a newer head does not show a proof of an older one.
    assert proof_result_line(written.proof, "f" * 40, repo_url="https://github.com/acme/widgets") == ""


def test_the_next_review_keeps_the_proof_spend_and_shows_the_line_only_on_its_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.pr_bot.github import BotGitHub
    from lemoncrow.pro.capabilities.review.sources.github import PullRequestRef
    from tests.gateway.pr_bot_support import BASE, NUMBER, SLUG, build_world

    world = build_world(tmp_path, monkeypatch, BASE)
    first = world.origin.push({"src/plain.py": "VALUE = 2\n"})
    world.review()
    proof = report_mod.Summary(
        head_sha=first,
        base_sha="",
        sandbox="none",
        sandbox_reason="",
        findings=[],
        red=[{"path": "tests/test_plain.py", "test": "", "outcome": "fails_on_base", "reason": ""}],
        red_overflow=0,
        recorded=True,
        notes=[],
        plan_digest="ab" * 32,
        plan_usage={"calls": 1, "input_tokens": 10, "output_tokens": 10, "cost_usd": 0.4},
    )
    github = BotGitHub(PullRequestRef(*SLUG.split("/"), NUMBER), world.github)
    assert report_mod.record_on_summary(github, proof) == f"recorded on the summary for {first[:12]}"
    line = (
        f"**Proof** on `{first[:7]}`: 0 supported, 0 contradicted, 0 inconclusive, 0 could not run; new tests: 1 of 1 "
        f"fail on base · [LemonCrow Proof](https://github.com/{SLUG}/commit/{first}/checks)"
    )
    assert line in world.github.summary_comments()[0]["body"]
    world.origin.push({"src/plain.py": "VALUE = 3\n"})
    report = world.review()
    assert "**Proof** on " not in world.github.summary_comments()[0]["body"]
    assert report.state is not None and report.state.proof.startswith(f"{first[:12]}:s0")
    assert report.state.proof_usage.cost_usd == pytest.approx(0.4)


# --------------------------------------------------------------------------- #
# the CLI, end to end (plan -> run -> report --dry-run)
# --------------------------------------------------------------------------- #


def _fake_gh(pull: dict[str, Any], comments: list[dict[str, Any]], resolves: str = "") -> Any:
    """The two GitHub reads `proof plan --pr` makes, answered from memory (and `gh pr view N`, as *resolves*)."""

    def run(args: Any, stdin: str | None = None) -> str:
        args = list(args)
        if args[:2] == ["pr", "view"] and resolves:
            return json.dumps({"url": resolves})
        lowered = [arg.lower() for arg in args]  # GitHub paths are not case-sensitive
        if lowered == ["api", "repos/acme/widget/pulls/7"]:
            return json.dumps(pull)
        if "--slurp" in args and lowered[-1].startswith("repos/acme/widget/issues/7/comments"):
            return json.dumps([comments])
        raise AssertionError(f"unexpected gh call: {args}")

    return run


def _pr_plan(
    repo: Path,
    tmp_path: Path,
    monkeypatch,
    *,
    tip: str,
    named: str,
    head: str,
    extra: tuple[str, ...] = (),
    spec: str = "acme/widget#7",
    resolves: str = "",
    env: dict[str, str] | None = None,
) -> tuple[Any, Path]:
    """`proof plan --pr` against a GitHub whose base branch is at *tip* and whose summary names *named* as the base.

    Off GitHub Actions unless *env* says otherwise (this repository's own CI is Actions); *spec* is what --pr is given,
    and *resolves* what `gh pr view` answers for a bare number."""

    from lemoncrow.gateway.cli.commands import review_proof_cli
    from lemoncrow.gateway.cli.commands.review import review_cmd

    monkeypatch.setattr(plan_mod, "_default_executor", lambda: StubModel(ANSWERS))
    monkeypatch.setattr(plan_mod, "resolve_route", lambda: ("", "openai"))
    body = f"{SUMMARY_MARKER}\nReview\n\n{dump_state(_state(head, named, *FINDINGS))}\n"
    pull = {"state": "open", "head": {"sha": head}, "base": {"sha": tip, "ref": "main"}}
    # Any account GitHub types Bot can write the marked comment; the state in it is not signed.
    comment = {"id": 1, "body": body, "user": {"login": "echo-bot[bot]", "type": "Bot"}}
    monkeypatch.setattr(review_proof_cli, "_gh_runner", lambda _repo: _fake_gh(pull, [comment], resolves))
    for name in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_SERVER_URL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    out = tmp_path / "plan.json"
    result = CliRunner().invoke(
        review_cmd,
        ["proof", "plan", "--repo", str(repo), "--pr", spec, "--out", str(out), *extra],
        obj={"root": str(tmp_path / "lc-root")},
    )
    return result, out


def _forged_base(repo: Path, base: str, head: str) -> str:
    """A commit that only the pull request's branch holds (it is not in the base branch's history)."""

    return _git(repo, "commit-tree", f"{head}^{{tree}}", "-p", base, "-m", "from the pull request")


@pytest.mark.parametrize("moved", [False, True])
def test_plan_accepts_the_merge_base_the_base_branch_holds(fixture_repo, tmp_path, monkeypatch, moved: bool) -> None:
    repo, base, head = fixture_repo
    # The base branch is still at the merge base, or has moved on since the review (the merge base is behind its tip).
    result, out = _pr_plan(repo, tmp_path, monkeypatch, tip=head if moved else base, named=base, head=head)
    assert result.exit_code == 0, result.output
    assert wire.load(out, wire.PLAN_SCHEMA)[0]["base_sha"] == base


def test_plan_refuses_a_base_the_summary_names_that_is_not_on_the_base_branch(
    fixture_repo, tmp_path, monkeypatch
) -> None:
    """The install reads this commit before the sandbox starts, so a state that points it at the pull request's own
    branch (an echoing bot's comment) must stop the plan, not be installed from."""

    repo, base, head = fixture_repo
    forged = _forged_base(repo, base, head)
    result, out = _pr_plan(repo, tmp_path, monkeypatch, tip=base, named=forged, head=head)
    assert result.exit_code == 2
    assert f"the base commit {forged[:12]} is not in the history of the base branch main" in result.output
    assert "may not be LemonCrow's" in result.output
    assert not out.exists()
    # An explicit --base is held to the same rule: it is the commit the install reads.
    result, out = _pr_plan(repo, tmp_path, monkeypatch, tip=base, named=base, head=head, extra=("--base", forged))
    assert result.exit_code == 2 and "not in the history of the base branch main" in result.output
    assert not out.exists()


def test_plan_checks_the_clones_base_branch_when_a_push_moved_the_live_tip(fixture_repo, tmp_path, monkeypatch) -> None:
    repo, base, head = fixture_repo
    gone = "f" * 40  # the base branch's tip now, pushed after the checkout: not in this clone
    _git(repo, "remote", "add", "origin", "https://github.com/acme/widget.git")
    _git(repo, "update-ref", "refs/remotes/origin/main", head)
    result, _ = _pr_plan(repo, tmp_path, monkeypatch, tip=gone, named=base, head=head)
    assert result.exit_code == 0, result.output
    forged = _forged_base(repo, base, head)
    result, _ = _pr_plan(repo, tmp_path, monkeypatch, tip=gone, named=forged, head=head)
    assert result.exit_code == 2 and "not in the history of the base branch main" in result.output


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/widget",
        "https://github.com/Acme/Widget.git",
        "https://x-access-token:t0ken@github.com/acme/widget.git/",
        "git@github.com:acme/widget.git",
        "ssh://git@github.com/acme/widget.git",
        "ssh://git@github.com:22/acme/widget",
    ],
)
def test_plan_reads_the_clones_origin_when_it_is_the_base_repository(
    fixture_repo, tmp_path, monkeypatch, url: str
) -> None:
    repo, base, head = fixture_repo
    _git(repo, "remote", "add", "origin", url)
    _git(repo, "update-ref", "refs/remotes/origin/main", head)
    result, _ = _pr_plan(repo, tmp_path, monkeypatch, tip="f" * 40, named=base, head=head)
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/mallory/widget.git",  # the fork the head came from
        "https://github.com/acme/widget-fork.git",
        "https://github.com/acme/widget/extra",
        "https://ghe.example.com/acme/widget.git",  # the same name on another host
        "git@github.com:mallory/widget.git",
        str(Path("/srv/acme/widget")),
        "",
    ],
)
@pytest.mark.parametrize("live_tip_in_clone", [True, False])
def test_plan_does_not_trust_an_origin_that_is_not_the_base_repository(
    fixture_repo, tmp_path, monkeypatch, url: str, live_tip_in_clone: bool
) -> None:
    """A head checked out from the fork makes the fork ``origin``: its ``main`` is the author's to set, and a forged
    base on it must be refused whether or not GitHub's live tip is in the clone."""

    repo, base, head = fixture_repo
    forged = _forged_base(repo, base, head)
    if url:
        _git(repo, "remote", "add", "origin", url)
    _git(repo, "update-ref", "refs/remotes/origin/main", forged)
    tip = base if live_tip_in_clone else "f" * 40
    result, out = _pr_plan(repo, tmp_path, monkeypatch, tip=tip, named=forged, head=head)
    assert result.exit_code == 2 and not out.exists()
    assert ("not in the history of the base branch main" if live_tip_in_clone else "is not in this clone") in (
        result.output
    )
    if not live_tip_in_clone:
        assert "its branches are not consulted" in result.output


def _actions(repository: str = "acme/widget", server: str = "https://github.com") -> dict[str, str]:
    env = {"GITHUB_ACTIONS": "true", "GITHUB_SERVER_URL": server}
    return {**env, "GITHUB_REPOSITORY": repository} if repository else env


def test_plan_under_actions_refuses_a_bare_number_that_resolves_to_the_forks_pull_request(
    fixture_repo, tmp_path, monkeypatch
) -> None:
    """A head checked out from the fork makes the fork ``origin``, and `gh` resolves a bare number from the remotes:
    the pull request, its base branch and its summary are then the author's, and the origin check would agree."""

    repo, base, head = fixture_repo
    forged = _forged_base(repo, base, head)
    _git(repo, "remote", "add", "origin", "https://github.com/mallory/widget.git")
    _git(repo, "update-ref", "refs/remotes/origin/main", forged)
    result, out = _pr_plan(
        repo,
        tmp_path,
        monkeypatch,
        tip="f" * 40,
        named=forged,
        head=head,
        spec="7",
        resolves="https://github.com/mallory/widget/pull/7",
        env=_actions(),
    )
    assert result.exit_code == 2 and not out.exists()
    assert "resolves to mallory/widget on github.com, but this workflow runs in acme/widget" in result.output
    assert "--pr acme/widget#NUMBER" in result.output


@pytest.mark.parametrize(
    ("spec", "resolves", "repository", "server"),
    [
        ("7", "https://github.com/acme/widget/pull/7", "acme/widget", "https://github.com"),
        ("7", "https://github.com/Acme/Widget/pull/7", "acme/widget", "https://github.com/"),
        ("acme/widget#7", "", "ACME/WIDGET", "https://GitHub.com"),
        ("7", "https://github.com/acme/widget/pull/7", "acme/widget", ""),
    ],
)
def test_plan_under_actions_accepts_the_workflows_own_repository(
    fixture_repo, tmp_path, monkeypatch, spec: str, resolves: str, repository: str, server: str
) -> None:
    repo, base, head = fixture_repo
    result, out = _pr_plan(
        repo, tmp_path, monkeypatch, tip=base, named=base, head=head, spec=spec, resolves=resolves,
        env=_actions(repository, server),
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert wire.load(out, wire.PLAN_SCHEMA)[0]["base_sha"] == base


@pytest.mark.parametrize(
    ("spec", "repository", "server", "says"),
    [
        ("acme/widget#7", "acme/other", "https://github.com", "this workflow runs in acme/other on github.com"),
        ("acme/widget#7", "mallory/widget", "https://github.com", "this workflow runs in mallory/widget"),
        ("https://ghe.example.com/acme/widget/pull/7", "acme/widget", "https://github.com", "runs in acme/widget"),
        ("acme/widget#7", "acme/widget", "https://ghe.example.com", "on ghe.example.com"),
    ],
)
def test_plan_under_actions_refuses_another_repository_or_host(
    fixture_repo, tmp_path, monkeypatch, spec: str, repository: str, server: str, says: str
) -> None:
    repo, base, head = fixture_repo
    result, out = _pr_plan(
        repo, tmp_path, monkeypatch, tip=base, named=base, head=head, spec=spec, env=_actions(repository, server)
    )
    assert result.exit_code == 2 and not out.exists()
    assert says in result.output


def test_plan_under_actions_without_github_repository_is_refused(fixture_repo, tmp_path, monkeypatch) -> None:
    repo, base, head = fixture_repo
    result, out = _pr_plan(repo, tmp_path, monkeypatch, tip=base, named=base, head=head, env=_actions(""))
    assert result.exit_code == 2 and not out.exists()
    assert "GITHUB_REPOSITORY is not set" in result.output


def test_plan_outside_actions_does_not_compare_repositories(fixture_repo, tmp_path, monkeypatch) -> None:
    repo, base, head = fixture_repo
    result, _ = _pr_plan(
        repo,
        tmp_path,
        monkeypatch,
        tip=base,
        named=base,
        head=head,
        env={"GITHUB_REPOSITORY": "someone/else", "GITHUB_SERVER_URL": "https://ghe.example.com"},
    )
    assert result.exit_code == 0, result.output


def test_plan_refuses_to_trust_a_base_when_the_base_branch_is_not_in_the_clone(
    fixture_repo, tmp_path, monkeypatch
) -> None:
    repo, base, head = fixture_repo
    result, out = _pr_plan(repo, tmp_path, monkeypatch, tip="f" * 40, named=base, head=head)
    assert result.exit_code == 2
    assert "the tip of the base branch main is not in this clone" in result.output
    assert "fetch-depth: 0" in result.output
    assert not out.exists()


def test_cli_hands_off_through_files(fixture_repo, tmp_path, monkeypatch) -> None:
    from lemoncrow.gateway.cli.commands.review import review_cmd

    repo, base, head = fixture_repo
    # ``--sandbox none`` runs plainly only off GitHub Actions (which this repository's own CI is).
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(plan_mod, "_default_executor", lambda: StubModel(ANSWERS))
    monkeypatch.setattr(plan_mod, "resolve_route", lambda: ("", "openai"))
    summary = tmp_path / "summary.md"
    summary.write_text(f"{SUMMARY_MARKER}\nReview\n\n{dump_state(_state(head, base, *FINDINGS))}\n", encoding="utf-8")
    plan_path, results_path = tmp_path / "plan.json", tmp_path / "results.json"
    obj = {"root": str(tmp_path / "lc-root")}
    runner = CliRunner()

    planned = runner.invoke(
        review_cmd,
        ["proof", "plan", "--repo", str(repo), "--state-file", str(summary), "--out", str(plan_path)],
        obj=obj,
    )
    assert planned.exit_code == 0, planned.output
    ran = runner.invoke(
        review_cmd,
        [
            "proof",
            "run",
            "--repo",
            str(repo),
            "--plan",
            str(plan_path),
            "--sandbox",
            "none",
            "--out",
            str(results_path),
        ],
        obj=obj,
    )
    assert ran.exit_code == 0, ran.output
    assert "Proof FAILURE" in ran.output and "supported" in ran.output
    reported = runner.invoke(
        review_cmd,
        ["proof", "report", "--plan", str(plan_path), "--results", str(results_path), "--dry-run"],
        obj=obj,
    )
    assert reported.exit_code == 0, reported.output
    posted = json.loads(reported.output)
    assert posted["check_run"]["conclusion"] == "failure"
    assert len(posted["replies"]) == 3
    assert "external_id" not in posted["check_run"]
    # In a workflow run the check names the run, and --report-file writes what the check carries.
    report_path = tmp_path / "lemoncrow-proof-report.json"
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_RUN_ID", "9000123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "3")
    again = runner.invoke(
        review_cmd,
        [
            "proof",
            "report",
            "--plan",
            str(plan_path),
            "--results",
            str(results_path),
            "--dry-run",
            "--report-file",
            str(report_path),
        ],
        obj=obj,
    )
    assert again.exit_code == 0, again.output
    named = json.loads(again.stdout)["check_run"]
    assert named["external_id"] == "9000123:3"
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["report"] == named["output"]["summary"] and written["schema"] == wire.REPORT_FILE_SCHEMA
    # No identity token to ask for (the job lacks id-token: write): the check is still posted, the file has none, and
    # the job says so on stderr; a hosted Review reads such a report as unattested.
    assert written["token"] == "" and "identity token" in again.stderr


# --------------------------------------------------------------------------- #
# the report job under GitHub Actions: no attestation for a run that was not sandboxed
# --------------------------------------------------------------------------- #


def _relabelled(results: dict[str, Any], sandbox: str) -> dict[str, Any]:
    """*results* with every recorded sandbox (the document's and each run's) set to *sandbox*."""

    text = json.dumps(results)
    return json.loads(
        text.replace('"sandbox": "none"', f'"sandbox": "{sandbox}"').replace('"mode": "none"', f'"mode": "{sandbox}"')
    )


def _report_job(fixture_repo, tmp_path, monkeypatch, *, recorded: str, actions: bool, dry_run: bool = False) -> Any:
    """Run ``proof report`` on the results of a run that recorded *recorded*; ``(result, github, report file path)``."""

    from lemoncrow.gateway.cli.commands import review_proof_cli
    from lemoncrow.gateway.cli.commands.review import review_cmd

    repo, base, head = fixture_repo
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    plain = run_mod.run_proof(
        _config(repo, base, head, Recorder(), run_mod.Sandbox("none", "ran plainly")),
        plan,
        plan_digest=wire.digest(wire.dump(plan)),
    )
    results = _relabelled(plain, recorded)
    assert (report_mod.plain_run(results) != "") == (recorded == "none")
    plan_path, results_path, report_path = tmp_path / "plan.json", tmp_path / "results.json", tmp_path / "report.json"
    wire.write(plan_path, plan)
    wire.write(results_path, results)
    github = FakeGitHub()
    monkeypatch.setattr(review_proof_cli, "_gh_runner", lambda _repo: github)
    if actions:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    else:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://token.example/oidc?api-version=2.0")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "request-bearer")
    asked: list[Any] = []

    def minted(request: Any, timeout: float = 0) -> Any:
        asked.append(request)
        return _Reply({"value": "header.claims.signature"})

    monkeypatch.setattr(report_mod.urllib.request, "urlopen", minted)
    argv = [
        "proof",
        "report",
        "--plan",
        str(plan_path),
        "--results",
        str(results_path),
        "--report-file",
        str(report_path),
    ]
    result = CliRunner().invoke(
        review_cmd, [*argv, *(["--dry-run"] if dry_run else [])], obj={"root": str(tmp_path / "lc-root")}
    )
    return result, github, report_path, asked


@pytest.mark.parametrize("dry_run", [False, True])
def test_the_report_job_under_actions_will_not_attest_a_run_that_was_not_sandboxed(
    fixture_repo, tmp_path, monkeypatch, dry_run: bool
) -> None:
    result, github, report_path, asked = _report_job(
        fixture_repo, tmp_path, monkeypatch, recorded="none", actions=True, dry_run=dry_run
    )
    # Not silent: the step fails, and says why and what to do.
    assert result.exit_code == 2
    assert "not attested" in result.stderr and "without a sandbox" in result.stderr
    assert "GitHub Actions" in result.stderr and "bubblewrap" in result.stderr
    # No token: it was never even asked for, and the report file (the artifact) carries none.
    assert asked == []
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["token"] == "" and document["report"]
    # Nothing the plain run claimed is in the report: the reproduced bug and the red-on-base rows are not believed.
    assert "| Supported |" not in document["report"] and "| Fails on base |" not in document["report"]
    if dry_run:
        check = json.loads(result.stdout)["check_run"]
        assert github.posts == []
    else:
        check = github.check()  # the neutral check is still posted, so the pull request shows the run did not count
    assert check["conclusion"] == "neutral" and check["output"]["summary"] == document["report"]
    assert "the results file was not used: the results file records a run without a sandbox" in check["output"]["text"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_the_report_job_under_actions_attests_a_run_that_was_sandboxed(
    fixture_repo, tmp_path, monkeypatch, dry_run: bool
) -> None:
    result, _github, report_path, asked = _report_job(
        fixture_repo, tmp_path, monkeypatch, recorded="bubblewrap", actions=True, dry_run=dry_run
    )
    assert result.exit_code == 0, result.output
    assert len(asked) == 1
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["token"] == "header.claims.signature" and "| Supported |" in document["report"]


def test_off_actions_the_report_job_is_unchanged_for_a_plain_run(fixture_repo, tmp_path, monkeypatch) -> None:
    result, github, report_path, asked = _report_job(
        fixture_repo, tmp_path, monkeypatch, recorded="none", actions=False
    )
    assert result.exit_code == 0, result.output
    assert len(asked) == 1 and json.loads(report_path.read_text(encoding="utf-8"))["token"] == "header.claims.signature"
    assert github.check()["conclusion"] == "failure" and "Sandbox: `none`" in github.check()["output"]["summary"]
