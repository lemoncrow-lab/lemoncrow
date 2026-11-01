"""Proof (P2.1 reproductions, P2.2 red-on-base) on a tiny real repository, with a stub model.

The fixture has a base commit and a head commit that seeds an off-by-one bug in ``last_n``, fixes a bug in
``total`` (with a test that would catch it coming back), and adds a test that passes on base too. Tests run for
real (pytest in a subprocess); only the model is stubbed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.pro.capabilities.review.intelligence_runtime import ModelCallUsage, StructuredExecution
from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding
from lemoncrow.pro.capabilities.review.pr_bot.state import SUMMARY_MARKER, ReviewState, Usage, dump_state
from lemoncrow.pro.capabilities.review.proof import plan as plan_mod
from lemoncrow.pro.capabilities.review.proof import report as report_mod
from lemoncrow.pro.capabilities.review.proof import run as run_mod
from lemoncrow.pro.capabilities.review.proof import wire

SECRETS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_RUNTIME_TOKEN")

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


def test_a_missing_sandbox_is_could_not_run_and_never_runs(fixture_repo) -> None:
    repo, base, head = fixture_repo
    plan = _plan(repo, base, head, StubModel(ANSWERS))
    sandbox = run_mod.resolve_sandbox("auto", require=True, probe=lambda: "bubblewrap is not installed")
    assert not sandbox.usable
    recorder = Recorder()
    results = run_mod.run_proof(_config(repo, base, head, recorder, sandbox), plan, plan_digest="")
    assert recorder.envs == []  # nothing ran
    assert {row["outcome"] for row in results["repros"]} == {"could_not_run"}
    assert {row["outcome"] for row in results["red_on_base"]} == {"could_not_run"}
    assert "sandbox missing" in _by(results["repros"], "finding_id")["F1"]["reason"]
    # In CI (no --local) the same probe falls back to a plain run and says so.
    plain = run_mod.resolve_sandbox("auto", require=False, probe=lambda: "bubblewrap is not installed")
    assert plain.mode == "none" and plain.usable and "throwaway runner" in plain.reason


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
        f"{SUMMARY_MARKER}\n<sub>x</sub>\n\n{lead}\n\n<sub>Independence: x</sub>\n\n---\n\nrest\n\n{dump_state(state)}"
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
    assert f"{lead}\n\n{line}\n\n<sub>Independence: x</sub>\n\n---" in github.body
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


def test_cli_hands_off_through_files(fixture_repo, tmp_path, monkeypatch) -> None:
    from lemoncrow.gateway.cli.commands.review import review_cmd

    repo, base, head = fixture_repo
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
