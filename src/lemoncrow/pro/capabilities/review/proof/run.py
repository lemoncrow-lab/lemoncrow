"""Run the planned reproductions and the pull request's changed tests, and say what each run showed.

This step executes pull request code, so it holds nothing worth stealing: the child process gets an
allow-listed environment (``PATH``, a throwaway ``HOME`` and ``TMPDIR``, ``CI=1``, a locale) and never a model
key or a GitHub token, whatever this process holds. In bubblewrap the child also gets no network, no host home
and only the toolchain directories named with ``toolchain``; without bubblewrap it runs plainly in the
throwaway runner, and every run records which (``sandbox: bubblewrap`` or ``none``).

Each commit is materialized from the object store into its own scratch workspace. A reproduction runs once on
head and once on base; the pull request's added or changed test files run on head and then on base with the
head's test files copied over the base source (red-on-base). Nothing is ever called a pass without a recorded
run.
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from lemoncrow.pro.capabilities.review.differential import (
    TestCase,
    TestReport,
    diff_test_file,
    is_test_path,
    parse_report,
)
from lemoncrow.pro.capabilities.review.execution import REPORT_DIR, ExecutionSubject
from lemoncrow.pro.capabilities.review.proof import wire
from lemoncrow.pro.capabilities.review.proof.frameworks import Detection, Framework, GitTree, detect

SANDBOX_CHOICES: Final[tuple[str, ...]] = ("auto", "bubblewrap", "none")
DEFAULT_TIMEOUT: Final[int] = 300
MAX_TIMEOUT: Final[int] = 900
#: Changed test files run for red-on-base; past this the rest are counted and not run.
MAX_RED_FILES: Final[int] = 20
REPORTS: Final[str] = ".lemoncrow-proof-reports"
SANDBOX_ROOT: Final[str] = "/workspace"
_STANDARD_PATH: Final[str] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_OUTPUT_BYTES: Final[int] = 16384
#: A failure that is not the claimed bug: the test or the code under it did not load, compile or start.
_SETUP_PROBLEM: Final[re.Pattern[str]] = re.compile(
    r"^.*(?:SyntaxError|IndentationError|ModuleNotFoundError|ImportError|No module named|ERROR collecting|"
    r"fixture '[^']+' not found|Cannot find module|ERR_MODULE_NOT_FOUND|SyntaxError:|npm ERR!|"
    r"\[build failed\]|\[setup failed\]|cannot find package|undefined: \w+|error\[E\d+\]|could not compile|"
    r"command not found|^bwrap: ).*$",
    re.MULTILINE,
)

Execute = Callable[..., "subprocess.CompletedProcess[str]"]


def _default_execute(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    from lemoncrow.pro.capabilities.review.sandbox_limits import run_limited_bounded

    return run_limited_bounded(argv, **kwargs)


# --------------------------------------------------------------------------- #
# the sandbox
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Sandbox:
    """How this step runs pull request code: ``bubblewrap`` or ``none``, and why."""

    mode: str
    reason: str = ""
    #: ``False``: nothing may run (bubblewrap was required and does not work here).
    usable: bool = True


def probe_bubblewrap() -> str:
    """``""`` when an offline bubblewrap namespace starts here; otherwise why not."""

    from lemoncrow.pro.capabilities.review.hermetic import HermeticUnavailable, offline_bwrap_prefix

    try:
        command = [*offline_bwrap_prefix(), "--", "/bin/true"]
    except HermeticUnavailable:
        return "bubblewrap is not installed"
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=20, check=False, env={"PATH": _STANDARD_PATH}
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"bubblewrap could not start: {exc}"[:300]
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        return f"bubblewrap does not work here: {detail[-1] if detail else f'exit {done.returncode}'}"[:300]
    return ""


def resolve_sandbox(requested: str, *, require: bool, probe: Callable[[], str] = probe_bubblewrap) -> Sandbox:
    """The sandbox for *requested* (``auto``, ``bubblewrap`` or ``none``); *require*: no plain fallback."""

    if requested == "none":
        if require:
            return Sandbox("none", "a local proof run needs bubblewrap; --sandbox none is refused", usable=False)
        return Sandbox("none", "--sandbox none: ran plainly in the throwaway runner")
    problem = probe()
    if not problem:
        return Sandbox("bubblewrap", "offline bubblewrap namespace")
    if requested == "bubblewrap" or require:
        return Sandbox("bubblewrap", f"sandbox missing: {problem}", usable=False)
    return Sandbox("none", f"{problem}; ran plainly in the throwaway runner")


# --------------------------------------------------------------------------- #
# one command
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RunConfig:
    repo: Path
    head_sha: str
    base_sha: str
    sandbox: Sandbox
    #: Directories mounted read-only at the same path inside bubblewrap (interpreters, toolchains, a venv).
    toolchain: tuple[Path, ...] = ()
    #: Repository-relative dependency directories of the checkout (``node_modules``, ``.venv``) made visible in
    #: each workspace: read-only in bubblewrap, a symlink otherwise.
    deps: tuple[str, ...] = ()
    timeout: int = DEFAULT_TIMEOUT
    max_red_files: int = MAX_RED_FILES
    execute: Execute = _default_execute
    #: The host PATH a plain run uses (and the source of the toolchain entries in bubblewrap).
    host_path: str = field(default_factory=lambda: os.environ.get("PATH", _STANDARD_PATH))

    def sandbox_path(self) -> str:
        roots = [str(root).rstrip("/") for root in self.toolchain]
        entries = [
            entry
            for entry in self.host_path.split(os.pathsep)
            if entry and any(entry == root or entry.startswith(root + "/") for root in roots)
        ]
        return ":".join(dict.fromkeys([*entries, *_STANDARD_PATH.split(":")]))


@dataclass(slots=True)
class Outcome:
    record: dict[str, Any]
    report: TestReport | None = None
    report_text: str = ""

    @property
    def status(self) -> str:
        return str(self.record["status"])

    @property
    def output(self) -> str:
        return str(self.record["output_tail"])


def child_env(home: str, tmp: str, path: str) -> dict[str, str]:
    """The whole environment a pull request process gets: an allow-list, never this process's environment."""

    return {
        "PATH": path,
        "HOME": home,
        "TMPDIR": tmp,
        "CI": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
    }


def _bwrap_command(
    config: RunConfig, workspace: Path, argv: Sequence[str], cwd: str, env: Mapping[str, str]
) -> list[str]:
    from lemoncrow.pro.capabilities.review.hermetic import offline_bwrap_prefix

    command = offline_bwrap_prefix()
    for root in config.toolchain:
        command += ["--ro-bind", str(root), str(root)]
    command += ["--bind", str(workspace), SANDBOX_ROOT]
    for rel in config.deps:
        command += ["--ro-bind", str(config.repo / rel), f"{SANDBOX_ROOT}/{rel}"]
    for key, value in env.items():
        command += ["--setenv", key, value]
    command += ["--chdir", posixpath.normpath(posixpath.join(SANDBOX_ROOT, cwd)), "--", *argv]
    return command


def run_one(
    config: RunConfig,
    workspace: Path,
    framework: Framework,
    argv: Sequence[str],
    *,
    side: str,
    scratch: Path,
) -> Outcome:
    """Run *argv* in *workspace* once and record it."""

    reports = workspace / REPORTS
    shutil.rmtree(reports, ignore_errors=True)
    reports.mkdir(parents=True)
    sandboxed = config.sandbox.mode == "bubblewrap"
    inside = f"{SANDBOX_ROOT}/{REPORTS}" if sandboxed else str(reports)
    command = [part.replace(REPORT_DIR, inside) for part in argv]
    record: dict[str, Any] = {
        "side": side,
        "command": list(argv),
        "exit_code": None,
        "status": "not_run",
        "duration_ms": 0,
        "output_tail": "",
        "sandbox": config.sandbox.mode,
    }
    if not config.sandbox.usable:
        record["output_tail"] = config.sandbox.reason
        return Outcome(record)
    if sandboxed:
        env = child_env("/tmp", "/tmp", config.sandbox_path())
        full = _bwrap_command(config, workspace, command, framework.cwd, env)
        outer = {"PATH": _STANDARD_PATH}
        cwd: Path | None = None
    else:
        home, tmp = scratch / "home", scratch / "tmp"
        home.mkdir(exist_ok=True)
        tmp.mkdir(exist_ok=True)
        outer = child_env(str(home), str(tmp), config.host_path)
        full = command
        cwd = workspace / framework.cwd
    started = time.monotonic()
    try:
        done = config.execute(full, timeout=config.timeout, max_output_bytes=_OUTPUT_BYTES, cwd=cwd, env=outer)
    except subprocess.TimeoutExpired:
        record.update(status="timeout", output_tail=f"timed out after {config.timeout}s")
    except Exception as exc:  # the sandbox's own failures (capacity, a missing binary) are a run that did not happen
        record.update(status="error", output_tail=f"could not start: {type(exc).__name__}: {exc}"[:600])
    else:
        output = f"{done.stdout or ''}{done.stderr or ''}"
        record.update(
            exit_code=done.returncode,
            status="passed" if done.returncode == 0 else "failed",
            output_tail=output[-wire.MAX_OUTPUT_TAIL :],
        )
        if sandboxed and done.returncode != 0 and output.lstrip().startswith("bwrap:"):
            record["status"] = "error"
    record["duration_ms"] = int((time.monotonic() - started) * 1000)
    from lemoncrow.pro.capabilities.review.runtimes import read_report

    text, truncated = read_report(reports)
    report = parse_report(framework.report, text, truncated=truncated) if record["exit_code"] is not None else None
    shutil.rmtree(reports, ignore_errors=True)
    return Outcome(record, report, text[-wire.MAX_OUTPUT_TAIL :])


# --------------------------------------------------------------------------- #
# workspaces
# --------------------------------------------------------------------------- #


def _materialize(config: RunConfig, sha: str, parent: Path) -> Path:
    from lemoncrow.pro.capabilities.review.reproduce import materialize_snapshot

    workspace = parent / "ws"
    materialize_snapshot(config.repo, ExecutionSubject(snapshot_identity=sha), workspace)
    if config.sandbox.mode != "bubblewrap":
        for rel in config.deps:
            link = workspace / rel
            if not link.exists() and not link.is_symlink():
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(config.repo / rel, target_is_directory=True)
    return workspace


def _put(workspace: Path, rel: str, text: str) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    target.write_text(text, encoding="utf-8")


def _setup_problem(*texts: str) -> str:
    for text in texts:
        found = _SETUP_PROBLEM.search(text or "")
        if found:
            return found.group(0).strip()[:200]
    return ""


def _short(outcome: Outcome) -> str:
    lines = [line for line in outcome.output.strip().splitlines() if line.strip()]
    return lines[-1][:200] if lines else outcome.status


def _leaf(case_id: str) -> str:
    return case_id.split("::")[-1].split("[")[0].strip()


def _named(report: TestReport | None, name: str) -> list[TestCase]:
    if report is None or report.error:
        return []
    return [case for case in report.cases if _leaf(case.id) == name or case.id.endswith(" " + name)]


# --------------------------------------------------------------------------- #
# reproductions
# --------------------------------------------------------------------------- #


def classify_repro(
    head: Outcome, base: Outcome | None, *, name: str, marker: str, report_format: str
) -> tuple[str, str]:
    """``(outcome, reason)`` for one reproduction from its head and base runs."""

    if head.status not in {"passed", "failed"}:
        return "could_not_run", f"the head run did not complete ({head.status}): {_short(head)}"
    target = _named(head.report, name)
    if head.status == "passed":
        if report_format and not any(case.status == "passed" for case in target):
            return "inconclusive", "the head run passed, but the reproduction was not among the tests it ran"
        return "contradicted", "the reproduction passes on head: the claimed failure does not happen"
    problem = _setup_problem(head.output, head.report_text) if not any(c.status == "failed" for c in target) else ""
    if problem:
        return "inconclusive", f"fails on head for an unrelated reason: {problem}"
    if report_format and not any(case.status == "failed" for case in target):
        return "inconclusive", "fails on head, but the reproduction itself is not what failed"
    if marker and marker not in head.output and marker not in head.report_text:
        return "inconclusive", f"fails on head, but not with the claimed failure ({marker!r} is not in the output)"
    if base is None or base.status not in {"passed", "failed"}:
        why = f" ({base.status}): {_short(base)}" if base is not None else ""
        return "could_not_run", f"fails on head as claimed; the base run did not complete{why}"
    if base.status == "passed":
        return "supported", "fails on head as claimed and passes on base"
    base_target = _named(base.report, name)
    new_path = _setup_problem(base.output, base.report_text)
    if new_path and not any(case.status == "failed" for case in base_target):
        return "supported", f"fails on head as claimed; on base the code path does not exist yet ({new_path})"
    return "inconclusive", "fails on base too: the failure is not caused by this change"


def _plan_framework(repro: Mapping[str, Any]) -> Framework | None:
    command = repro.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        return None
    cwd = wire.text(repro.get("cwd"), 300) or "."
    if cwd != "." and not wire.SAFE_PATH.match(cwd):
        return None
    return Framework(
        wire.text(repro.get("framework"), 40),
        wire.text(repro.get("language"), 20),
        tuple(command),
        report=wire.text(repro.get("report"), 20),
        cwd=cwd,
    )


def run_repros(
    config: RunConfig, plan: Mapping[str, Any], head_ws: Path, base_ws: Path, base_tree: GitTree, scratch: Path
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    base_paths = set(base_tree.paths())
    for repro in wire.rows(plan.get("repros")):
        finding_id = wire.text(repro.get("finding_id"), 80)
        row: dict[str, Any] = {"finding_id": finding_id, "outcome": "could_not_run", "reason": "", "runs": []}
        results.append(row)
        if repro.get("status") != "planned":
            row["reason"] = wire.text(repro.get("reason")) or "no reproduction was planned"
            continue
        framework = _plan_framework(repro)
        test_path = wire.text(repro.get("test_path"), 300)
        name = wire.text(repro.get("test_name"), 80)
        code = repro.get("test_code")
        if framework is None or not wire.SAFE_PATH.match(test_path) or not isinstance(code, str) or not name:
            row["reason"] = "the planned reproduction is malformed"
            continue
        if test_path in base_paths or (head_ws / test_path).exists():
            row["reason"] = f"{test_path} already exists; the reproduction would overwrite a repository file"
            continue
        runs: list[Outcome] = []
        for side, workspace in (("head", head_ws), ("base", base_ws)):
            _put(workspace, test_path, code)
            try:
                # The plan's command is concrete: the plan step built it from the base's framework.
                runs.append(run_one(config, workspace, framework, framework.template, side=side, scratch=scratch))
            finally:
                (workspace / test_path).unlink(missing_ok=True)
            if side == "head" and runs[0].status != "failed":
                break  # base only matters when head fails
        outcome, reason = classify_repro(
            runs[0],
            runs[1] if len(runs) > 1 else None,
            name=name,
            marker=wire.text(repro.get("failure_marker"), 200),
            report_format=framework.report,
        )
        row.update(outcome=outcome, reason=reason, runs=[run.record for run in runs])
    return results


# --------------------------------------------------------------------------- #
# red-on-base
# --------------------------------------------------------------------------- #


def changed_paths(repo: Path, base: str, head: str) -> list[str]:
    """Paths the change adds, modifies or renames into (deleted files are not run)."""

    done = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-status", "-z", "-M", base, head],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if done.returncode != 0:
        raise ValueError(f"git diff {base[:12]}..{head[:12]} failed: {done.stderr.decode(errors='replace').strip()}")
    parts = [item.decode("utf-8", "replace") for item in done.stdout.split(b"\0")]
    out: list[str] = []
    index = 0
    while index < len(parts) and parts[index]:
        status = parts[index]
        if status[:1] in {"R", "C"}:
            out.append(parts[index + 2])
            index += 3
            continue
        if status[:1] in {"A", "M"}:
            out.append(parts[index + 1])
        index += 2
    return out


#: A file named like a test in any language, so a test no detected framework can run is still said.
_TESTISH: Final[re.Pattern[str]] = re.compile(r"(^|/)test_[^/]+\.\w+$|_(test|spec)\.\w+$|\.(test|spec)\.\w+$")

_DEFINITIONS: Final[Mapping[str, re.Pattern[str]]] = {
    "python": re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(", re.MULTILINE),
    "js": re.compile(r"\b(?:it|test)(?:\.\w+)?\(\s*(['\"`])(.+?)\1"),
    "go": re.compile(r"^func\s+(Test\w+)\s*\(", re.MULTILINE),
    "rust": re.compile(r"#\[test\]\s*(?:#\[[^\]]*\]\s*)*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", re.MULTILINE),
}


def defined_tests(language: str, text: str) -> list[str]:
    pattern = _DEFINITIONS[language]
    return list(dict.fromkeys(match.group(match.lastindex or 1) for match in pattern.finditer(text)))


def _file_cases(report: TestReport, framework: Framework, path: str, names: Iterable[str]) -> dict[str, list[TestCase]]:
    """The report's cases for each test *name* defined in *path*."""

    relative = framework.relative(path)
    module = posixpath.splitext(relative)[0].replace("/", ".")
    wanted = set(names)

    def in_file(case: TestCase) -> bool:
        if case.file:
            return case.file in {path, relative} or case.file.endswith("/" + relative)
        prefix = case.id.split("::", 1)[0]
        if framework.language == "python" and "::" in case.id:
            return prefix == module or prefix.startswith(module + ".")
        return True  # go and cargo name a package or binary: the defined names decide

    found: dict[str, list[TestCase]] = {name: [] for name in wanted}
    for case in report.cases:
        if not in_file(case):
            continue
        leaf = _leaf(case.id)
        for name in wanted:
            if leaf == name or case.id.endswith(" " + name):
                found[name].append(case)
    return found


def _status(cases: Sequence[TestCase]) -> str:
    statuses = {case.status for case in cases}
    if not statuses:
        return "missing"
    if "failed" in statuses:
        return "failed"
    return "passed" if "passed" in statuses else "skipped"


def red_verdict(head: str, base: str, *, base_run: Outcome) -> tuple[str, str]:
    """``(outcome, reason)`` for one changed test from its head status and base status."""

    if head != "passed":
        return "could_not_run", (
            "it does not pass on head, so there is no green baseline" if head == "failed" else f"it is {head} on head"
        )
    if base == "failed":
        return "fails_on_base", "fails on base and passes on head: it can catch the change being undone"
    if base == "passed":
        return "passes_on_base", "passes on base too: it may not test this change"
    if base == "missing" and base_run.status == "failed":
        problem = _setup_problem(base_run.output, base_run.report_text) or _short(base_run)
        return "fails_on_base", f"cannot load or compile on base ({problem}): it depends on this change"
    return "could_not_run", f"it is {base} in the base run"


def run_red_on_base(
    config: RunConfig,
    detection: Detection,
    head_ws: Path,
    base_ws: Path,
    head_tree: GitTree,
    base_tree: GitTree,
    scratch: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """``(per-test outcomes, runs, files not run past the cap)``."""

    changed = changed_paths(config.repo, config.base_sha, config.head_sha)
    outcomes: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    groups: dict[str, list[str]] = {}
    frameworks: dict[str, Framework] = {}
    chosen = 0
    overflow = 0
    for path in changed:
        framework = detection.for_path(path)
        if framework is None:
            if _TESTISH.search(path):
                suffix = posixpath.splitext(path)[1] or path
                outcomes.append(
                    {
                        "path": path,
                        "test": "",
                        "outcome": "could_not_run",
                        "reason": f"no test framework detected for {suffix} files",
                    }
                )
            continue
        if not framework.runnable(path):
            continue
        if chosen >= config.max_red_files:
            overflow += 1
            continue
        chosen += 1
        groups.setdefault(framework.language, []).append(path)
        frameworks[framework.language] = framework
    if not groups:
        return outcomes, runs, overflow
    # The head's test files and test support files (conftest, fixtures, helpers) go over the base source.
    support = [path for path in changed if is_test_path(path) or any(path in group for group in groups.values())]
    for path in support:
        source = head_ws / path
        if source.is_file() and not source.is_symlink():
            _put(base_ws, path, source.read_text(encoding="utf-8", errors="replace"))
    for language, files in groups.items():
        framework = frameworks[language]
        # Without a report the exit status is all there is, so each file runs alone.
        batches = [files] if framework.report else [[path] for path in files]
        for batch in batches:
            head_run = run_one(config, head_ws, framework, framework.argv(batch), side="head", scratch=scratch)
            base_run = run_one(config, base_ws, framework, framework.argv(batch), side="base", scratch=scratch)
            runs.extend((head_run.record, base_run.record))
            for path in batch:
                outcomes.extend(_red_for_file(framework, path, head_run, base_run, head_tree, base_tree))
    return outcomes, runs, overflow


def _red_for_file(
    framework: Framework, path: str, head_run: Outcome, base_run: Outcome, head_tree: GitTree, base_tree: GitTree
) -> list[dict[str, Any]]:
    def row(test: str, outcome: str, reason: str) -> dict[str, Any]:
        return {"path": path, "test": test, "outcome": outcome, "reason": reason}

    if head_run.status not in {"passed", "failed"}:
        return [row("", "could_not_run", f"the head run did not complete ({head_run.status}): {_short(head_run)}")]
    if base_run.status not in {"passed", "failed"}:
        return [row("", "could_not_run", f"the base run did not complete ({base_run.status}): {_short(base_run)}")]
    if not framework.report:
        head = "passed" if head_run.status == "passed" else "failed"
        base = "passed" if base_run.status == "passed" else "failed"
        return [row("", *red_verdict(head, base, base_run=base_run))]
    head_text = head_tree.read(path) or ""
    change = diff_test_file(path, base_tree.read(path), head_text)
    defined = defined_tests(framework.language, head_text)
    names = defined if change.whole else [name for name in defined if name in change.touched]
    if not names:
        return []  # the file changed, but no test definition in it did
    if head_run.report is None or head_run.report.error:
        why = head_run.report.error if head_run.report is not None else "no report"
        return [row("", "could_not_run", f"the head run's report could not be read: {why}")]
    head_cases = _file_cases(head_run.report, framework, path, names)
    base_report = base_run.report if base_run.report is not None and not base_run.report.error else TestReport("")
    base_cases = _file_cases(base_report, framework, path, names)
    return [
        row(name, *red_verdict(_status(head_cases[name]), _status(base_cases[name]), base_run=base_run))
        for name in names
    ]


# --------------------------------------------------------------------------- #
# the step
# --------------------------------------------------------------------------- #


def run_proof(config: RunConfig, plan: Mapping[str, Any] | None, *, plan_digest: str = "") -> dict[str, Any]:
    """Run everything and return the results document (``wire.RESULTS_SCHEMA``)."""

    notes: list[str] = []
    document: dict[str, Any] = {
        "schema": wire.RESULTS_SCHEMA,
        "head_sha": config.head_sha,
        "base_sha": config.base_sha,
        "plan_digest": plan_digest,
        "sandbox": {"mode": config.sandbox.mode, "reason": config.sandbox.reason},
        "repros": [],
        "red_on_base": [],
        "red_runs": [],
        "red_overflow": 0,
        "notes": notes,
    }
    base_tree = GitTree(config.repo, config.base_sha)
    head_tree = GitTree(config.repo, config.head_sha)
    detection = detect(base_tree)
    notes.extend(detection.notes)
    scratch = Path(tempfile.mkdtemp(prefix="lemoncrow-proof-"))
    try:
        head_ws = _materialize(config, config.head_sha, scratch / "head")
        base_ws = _materialize(config, config.base_sha, scratch / "base")
        if plan is not None:
            document["repros"] = run_repros(config, plan, head_ws, base_ws, base_tree, scratch)
        red, runs, overflow = run_red_on_base(config, detection, head_ws, base_ws, head_tree, base_tree, scratch)
        document.update(red_on_base=red, red_runs=runs, red_overflow=overflow)
        if overflow:
            notes.append(f"{overflow} more changed test file(s) were not run (the cap is {config.max_red_files})")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return document
