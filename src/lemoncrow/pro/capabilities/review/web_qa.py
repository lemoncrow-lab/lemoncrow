"""Web QA: replay a saved browser scenario in a real Chromium (``lc review qa run``).

The scenario comes from the ``browser_checks`` section of the trusted base's
``.lemoncrow/review.yaml`` (see :mod:`browser_checks`): a plain-language
``outcome`` and ordered, deterministic ``steps``. No model is involved. The
steps are replayed against a preview URL that serves one exact head SHA, by
Node Playwright (the machine's installed one, or a pinned version installed on
demand) driven through the small ``web_qa_runner.js`` script.

Each run produces one immutable report with exactly one outcome:

* ``pass`` -- every step ran and every expectation held;
* ``fail`` -- an expectation failed (a user-visible regression), or the page
  logged console or uncaught errors the scenario does not allow (the same rule
  as Review's browser checks);
* ``warning`` -- every expectation held and no error was logged, but a step
  was slower than the threshold;
* ``could_not_run`` -- setup, a missing secret, a browser that would not
  install or start, an unreachable or untrusted target, a bot wall or a
  timeout. It never carries a verdict.

Page text is untrusted data. Nothing read from the page is executed or chooses
an action; the runner follows the saved steps and no others, and a
navigation to an origin the base configuration does not trust is blocked.
Secrets named as ``${secrets.NAME}`` in a ``fill`` value are read from the
environment, handed to the runner only, and never printed or stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets as _secrets
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lemoncrow.pro.capabilities.review.browser_checks import (
    BrowserPolicy,
    BrowserScenario,
    validate_browser_target,
    viewport_key,
)

QA_REPORT_SCHEMA = "lemoncrow.web-qa.report/v1"
QA_CHECK_NAME = "LemonCrow QA"
QA_PROVIDER = "lemoncrow-qa"
OUTCOMES = ("pass", "fail", "warning", "could_not_run")
DEFAULT_STEP_TIMEOUT_MS = 10_000
DEFAULT_NAVIGATION_TIMEOUT_MS = 30_000
DEFAULT_SLOW_STEP_MS = 5_000
DEFAULT_RUN_TIMEOUT_S = 180.0
DEFAULT_VIEWPORT = "1280x720"
SECRET_ENV_PREFIX = "LEMONCROW_QA_SECRET_"
RUNNER_SCRIPT = Path(__file__).with_name("web_qa_runner.js")
_HEAD_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PLAYWRIGHT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_PASSTHROUGH_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "PLAYWRIGHT_BROWSERS_PATH", "XDG_CACHE_HOME")
_SEVERITY = {"pass": 0, "warning": 1, "could_not_run": 2, "fail": 3}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def validate_head_sha(value: str) -> str:
    sha = value.strip().lower()
    if not _HEAD_SHA.fullmatch(sha):
        raise ValueError(
            "the head SHA must be a full 40- or 64-character commit id, the exact commit the preview serves"
        )
    return sha


# --------------------------------------------------------------------------- #
# Playwright
# --------------------------------------------------------------------------- #


def _has_playwright(directory: Path) -> bool:
    return (directory / "node_modules" / "playwright" / "package.json").is_file()


def resolve_playwright_dir(explicit: Path | None = None, *, search: Sequence[Path] = ()) -> Path | None:
    """A directory whose ``node_modules`` holds ``playwright``, or ``None``.

    Order: *explicit*, ``LEMONCROW_PLAYWRIGHT_DIR``, the *search* directories,
    then the global npm root. Nothing is installed here.
    """

    if explicit is not None:
        return explicit if _has_playwright(explicit) else None
    candidates = [Path(item) for item in (os.environ.get("LEMONCROW_PLAYWRIGHT_DIR", ""),) if item]
    candidates.extend(search)
    for candidate in candidates:
        if _has_playwright(candidate):
            return candidate
    npm = shutil.which("npm")
    if npm:
        try:
            root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if root.returncode == 0 and root.stdout.strip():
            global_dir = Path(root.stdout.strip()).parent
            if _has_playwright(global_dir):
                return global_dir
    return None


def install_playwright(version: str, target: Path, *, with_deps: bool = False, timeout_s: float = 900.0) -> str:
    """Install pinned Playwright *version* and its Chromium under *target*.

    Returns ``""`` on success, else the reason (a ``could_not_run`` cause).
    """

    if not _PLAYWRIGHT_VERSION.fullmatch(version):
        return f"Playwright version must be an exact x.y.z pin, not {version!r}"
    npm, node = shutil.which("npm"), shutil.which("node")
    if not npm or not node:
        return "Node.js (node and npm) is not installed on this machine"
    target.mkdir(parents=True, exist_ok=True)
    installed = target / "node_modules" / "playwright" / "package.json"
    current = ""
    if installed.is_file():
        try:
            current = str(json.loads(installed.read_text(encoding="utf-8")).get("version") or "")
        except (OSError, ValueError):
            current = ""
    commands: list[list[str]] = []
    if current != version:
        commands.append(
            [npm, "install", "--no-save", "--no-audit", "--no-fund", "--prefix", str(target), f"playwright@{version}"]
        )
    cli = target / "node_modules" / "playwright" / "cli.js"
    commands.append([node, str(cli), "install", *(["--with-deps"] if with_deps else []), "chromium"])
    for command in commands:
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"browser install failed: {exc}"
        if done.returncode != 0:
            tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
            return f"browser install failed ({' '.join(command[1:3])}): {tail[0][:300]}"
    return ""


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def _allowed_origins(base_url: str, trusted_origins: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for origin in (base_url, *trusted_origins):
        parsed = urlparse(origin)
        host = str(parsed.hostname or "").lower()
        wildcard = host.startswith("*.")
        host = host[2:] if wildcard else host
        if ":" in host:
            host = f"[{host}]"  # WHATWG URL.hostname keeps IPv6 brackets
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        rows.append({"scheme": parsed.scheme, "host": host, "port": port, "wildcard": wildcard})
    return rows


def _redact(value: Any, hidden: Sequence[str]) -> Any:
    if isinstance(value, str):
        for secret in hidden:
            value = value.replace(secret, "***")
        return value
    if isinstance(value, list):
        return [_redact(item, hidden) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, hidden) for key, item in value.items()}
    return value


def _run_node(
    node: str, spec: Mapping[str, Any], *, env: Mapping[str, str], timeout_s: float
) -> tuple[dict[str, Any] | None, str]:
    """Run the runner script; ``(result, "")`` or ``(None, could_not_run reason)``."""

    with tempfile.TemporaryDirectory(prefix="lemoncrow-qa-") as scratch:
        spec_path = Path(scratch) / "spec.json"
        result_path = Path(scratch) / "result.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        try:
            process = subprocess.Popen(
                [node, str(RUNNER_SCRIPT), str(spec_path), str(result_path)],
                cwd=scratch,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return None, f"the browser runner could not start: {exc}"
        try:
            _out, err = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # Chromium runs in the runner's process group: stop all of it.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            return None, f"timeout: the scenario did not finish within {timeout_s:.0f}s"
        try:
            decoded = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or [f"exit {process.returncode}"]
            return None, f"the browser runner wrote no result: {tail[0][:300]}"
        return (decoded, "") if isinstance(decoded, dict) else (None, "the browser runner wrote a malformed result")


def _step_spec(scenario: BrowserScenario, origin: str, base_path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for step in scenario.steps:
        row: dict[str, Any] = {**step.to_wire(), "description": step.describe()}
        if step.action == "goto":
            row["url"] = f"{origin}{base_path}{step.path}"
        elif step.action == "expect_url":
            row["expected"] = f"{base_path}{step.path}"
        rows.append(row)
    return rows


def classify_run(
    raw: Mapping[str, Any],
    scenario: BrowserScenario,
    *,
    slow_step_ms: int = DEFAULT_SLOW_STEP_MS,
) -> tuple[str, list[str], str]:
    """``(outcome, reasons, could_not_run category)`` from the runner's raw result."""

    setup = raw.get("setup_error")
    if isinstance(setup, Mapping):
        return (
            "could_not_run",
            [str(setup.get("message") or "the browser could not be set up")],
            str(setup.get("category") or "setup"),
        )
    rows = [row for row in raw.get("steps") or () if isinstance(row, Mapping)]
    steps = scenario.steps
    for row, step in zip(rows, steps, strict=False):
        status = str(row.get("status") or "")
        where = f"step {row.get('index')} ({step.describe()})"
        if status == "could_not_run":
            return "could_not_run", [f"{where}: {row.get('error') or 'could not run'}"], str(row.get("category") or "")
        if status == "failed":
            return "fail", [f"{where} failed: {row.get('error') or 'expectation failed'}"], ""
    if len(rows) != len(steps) or any(str(row.get("status")) != "passed" for row in rows):
        return "could_not_run", ["the browser runner stopped before every step ran"], "runner"
    # One rule with Review's browser checks (``browser_checks``): a console or uncaught page error is a failure unless the
    # scenario allows them. Only a slow step is a warning.
    errors: list[str] = []
    if not scenario.allow_console_errors:
        console = [row for row in raw.get("console") or () if isinstance(row, Mapping)]
        page_errors = [row for row in raw.get("page_errors") or () if isinstance(row, Mapping)]
        if console:
            errors.append(f"{len(console)} console error(s), first: {str(console[0].get('text') or '')[:200]}")
        if page_errors:
            errors.append(f"{len(page_errors)} uncaught page error(s), first: {page_errors[0].get('message')}")
    warnings: list[str] = []
    for row, step in zip(rows, steps, strict=True):
        duration = int(row.get("duration_ms") or 0)
        if duration > slow_step_ms:
            warnings.append(
                f"step {row.get('index')} ({step.describe()}) took {duration} ms, past the {slow_step_ms} ms threshold"
            )
    if errors:
        return "fail", errors + warnings, ""
    if warnings:
        return "warning", warnings, ""
    return "pass", [f"{len(steps)} step(s) passed"], ""


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seal(report: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Write ``report.json`` once, digest it and make the run directory read-only."""

    body = {key: value for key, value in report.items() if key != "digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    report["digest"] = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    path = out_dir / "report.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    for item in out_dir.rglob("*"):
        if item.is_file():
            item.chmod(0o444)
    return report


def run_scenario(
    scenario: BrowserScenario,
    *,
    policy: BrowserPolicy,
    base_url: str,
    head_sha: str,
    out_dir: Path,
    playwright_dir: Path | None,
    viewport: str = "",
    env: Mapping[str, str] | None = None,
    config_ref: str = "",
    step_timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
    slow_step_ms: int = DEFAULT_SLOW_STEP_MS,
    timeout_s: float = DEFAULT_RUN_TIMEOUT_S,
    setup_error: str = "",
) -> dict[str, Any]:
    """Replay *scenario* against *base_url* and write its sealed report to *out_dir*.

    *setup_error* (e.g. a failed Playwright install) makes the run
    ``could_not_run`` before anything is launched. Raises ``ValueError`` only
    for caller errors: a malformed head SHA, a scenario without steps, or an
    *out_dir* that already holds files.
    """

    sha = validate_head_sha(head_sha)
    if not scenario.steps:
        raise ValueError(f"browser scenario {scenario.id} has no saved steps to run")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError(f"{out_dir} already holds a report; a QA report is written once")
    environment = dict(os.environ if env is None else env)
    key = viewport_key(viewport or DEFAULT_VIEWPORT)
    width, height = (int(part) for part in key.split("x", 1))
    started_at, started = _now(), time.monotonic()
    report: dict[str, Any] = {
        "schema": QA_REPORT_SCHEMA,
        "run_id": f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{sha[:7]}-{_secrets.token_hex(3)}",
        "scenario": scenario.id,
        "title": scenario.title,
        "user_outcome": scenario.outcome,
        "route": scenario.route,
        "head_sha": sha,
        "base_url": base_url.strip(),
        "config_ref": config_ref,
        "policy_source": policy.source,
        "viewport": {"width": width, "height": height, "configured": bool(viewport)},
        "started_at": started_at,
        "steps": [
            {"index": index + 1, "action": step.action, "description": step.describe(), "status": "skipped"}
            for index, step in enumerate(scenario.steps)
        ],
        "screenshots": [],
        "console_errors": [],
        "page_errors": [],
        "page_url": "",
        "browser": {"name": "chromium", "version": "", "playwright": ""},
        "target_trusted": False,
    }

    def finish(outcome: str, reasons: list[str], category: str = "") -> dict[str, Any]:
        report["outcome"] = outcome
        # A run that could not run has no verdict: it says nothing about the change.
        report["verdict"] = None if outcome == "could_not_run" else outcome
        report["could_not_run_category"] = category if outcome == "could_not_run" else ""
        report["reasons"] = reasons
        report["finished_at"] = _now()
        report["duration_ms"] = int((time.monotonic() - started) * 1000)
        out_dir.mkdir(parents=True, exist_ok=True)
        return _seal(report, out_dir)

    try:
        target = validate_browser_target(base_url, policy.trusted_origins)
    except ValueError as exc:
        return finish("could_not_run", [str(exc)], "untrusted_origin")
    parsed = urlparse(target)
    if not target or parsed.query or parsed.fragment:
        return finish("could_not_run", ["the preview URL must be an http(s) URL with no query or fragment"], "setup")
    report["target_trusted"] = True
    missing = [name for name in scenario.secrets() if not environment.get(name)]
    if missing:
        # Names only; a value is never read into the report.
        return finish("could_not_run", [f"missing secret(s): {', '.join(missing)}"], "missing_secret")
    if setup_error:
        return finish("could_not_run", [setup_error], "browser_install")
    node = shutil.which("node", path=environment.get("PATH"))
    if not node:
        return finish("could_not_run", ["Node.js is not installed, so Playwright cannot run"], "setup")
    if playwright_dir is None:
        return finish(
            "could_not_run",
            ["Playwright is not installed: pass --playwright-dir, or --playwright-version to install a pinned one"],
            "setup",
        )
    shots = out_dir / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    spec = {
        "playwright_dir": str(playwright_dir),
        "allowed_origins": _allowed_origins(origin, policy.trusted_origins),
        "viewport": {"width": width, "height": height},
        "step_timeout_ms": step_timeout_ms,
        "navigation_timeout_ms": max(step_timeout_ms, DEFAULT_NAVIGATION_TIMEOUT_MS),
        "screenshot_dir": str(shots),
        "secret_env_prefix": SECRET_ENV_PREFIX,
        "steps": _step_spec(scenario, origin, parsed.path.rstrip("/")),
    }
    hidden = [environment[name] for name in scenario.secrets()]
    child_env = {name: environment[name] for name in _PASSTHROUGH_ENV if environment.get(name)}
    child_env.update({SECRET_ENV_PREFIX + name: environment[name] for name in scenario.secrets()})
    raw, failure = _run_node(node, spec, env=child_env, timeout_s=timeout_s)
    if raw is None:
        return finish(
            "could_not_run", [_redact(failure, hidden)], "timeout" if failure.startswith("timeout") else "runner"
        )
    raw = _redact(raw, hidden)
    report["browser"] = {
        "name": "chromium",
        "version": str(raw.get("browser_version") or ""),
        "playwright": str(raw.get("playwright_version") or ""),
    }
    report["page_url"] = str(raw.get("final_url") or "")
    navigation = raw.get("navigation")
    report["navigation"] = dict(navigation) if isinstance(navigation, Mapping) else {}
    report["console_errors"] = [dict(row) for row in raw.get("console") or () if isinstance(row, Mapping)]
    report["page_errors"] = [dict(row) for row in raw.get("page_errors") or () if isinstance(row, Mapping)]
    for row, ran in zip(report["steps"], raw.get("steps") or (), strict=False):
        if isinstance(ran, Mapping):
            row.update(
                status=str(ran.get("status") or "skipped"),
                started_at=str(ran.get("started_at") or ""),
                duration_ms=int(ran.get("duration_ms") or 0),
                error=str(ran.get("error") or ""),
                http_status=ran.get("http_status"),
                slow=int(ran.get("duration_ms") or 0) > slow_step_ms,
            )
    for shot in raw.get("screenshots") or ():
        path = Path(str(shot.get("file") or "")) if isinstance(shot, Mapping) else Path()
        if path.is_file() and path.resolve().parent == shots.resolve():
            report["screenshots"].append(
                {
                    "name": str(shot.get("name") or path.stem),
                    "file": str(path.relative_to(out_dir)),
                    "sha256": _file_digest(path),
                    "bytes": path.stat().st_size,
                }
            )
    outcome, reasons, category = classify_run(raw, scenario, slow_step_ms=slow_step_ms)
    return finish(outcome, reasons, category)


def overall_outcome(reports: Sequence[Mapping[str, Any]]) -> str:
    """Worst outcome of several runs: fail > could_not_run > warning > pass."""

    if not reports:
        return "could_not_run"
    return max((str(item.get("outcome") or "could_not_run") for item in reports), key=lambda o: _SEVERITY.get(o, 2))


def exit_code(outcome: str) -> int:
    return {"pass": 0, "warning": 0, "fail": 1}.get(outcome, 2)


def prune_runs(root: Path, *, keep: int = 50) -> None:
    """Keep the newest *keep* run directories under *root* (QA report retention)."""

    if not root.is_dir():
        return
    runs = sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name, reverse=True)
    for stale in runs[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Review binding
# --------------------------------------------------------------------------- #


def snapshot_for_sha(revisions: Sequence[Mapping[str, Any]], head_sha: str) -> Mapping[str, Any] | None:
    """The newest Snapshot whose head (or commit) is exactly *head_sha*."""

    sha = head_sha.strip().lower()
    matches = [
        row
        for row in revisions
        if sha
        and sha in {str(row.get("head_sha") or "").lower(), str(row.get("commit_sha") or "").lower()}
        and not row.get("dirty")
    ]
    return max(matches, key=lambda row: int(row.get("revision_number") or 0), default=None)


def review_run_body(
    report: Mapping[str, Any],
    scenario: BrowserScenario,
    *,
    revision_id: str,
    source: str = "test",
    screenshot_evidence_id: str = "",
) -> dict[str, Any]:
    """The ``POST /reviews/{id}/browser-checks`` body for one sealed report.

    Review recomputes the verdict from these inspectable checks with
    ``normalize_browser_run``; ``could_not_run`` is reported as NOT_RUN with
    its reason and no checks, so it can never read as a verdict.
    """

    browser = report.get("browser") or {}
    body: dict[str, Any] = {
        "revision_id": revision_id,
        "scenario": scenario.id,
        "route": scenario.route,
        "side": "new",
        "title": scenario.title,
        "provider": QA_PROVIDER,
        "source": source,
        "run_ref": f"qa:{report.get('run_id')} {report.get('digest')}",
        "started_at": report.get("started_at") or "",
        "finished_at": report.get("finished_at") or "",
        "environment": {
            "browser": "chromium",
            "browser_version": str(browser.get("version") or ""),
            "playwright": str(browser.get("playwright") or ""),
            "head_sha": str(report.get("head_sha") or ""),
            "runner": "lc review qa run",
            "qa_outcome": str(report.get("outcome") or ""),
        },
    }
    if report.get("target_trusted"):
        body["target_url"] = str(report.get("base_url") or "")
    viewport = report.get("viewport") or {}
    if viewport.get("configured"):
        # Only a configured viewport names the row; otherwise the run fills the scenario's default slot.
        body["viewport"] = {"width": viewport.get("width"), "height": viewport.get("height")}
    reasons = [str(item) for item in report.get("reasons") or ()]
    if report.get("outcome") == "could_not_run":
        body["status"] = "NOT_RUN"
        body["not_run_reason"] = ("could not run: " + "; ".join(reasons))[:500]
        return body
    steps = list(report.get("steps") or ())
    body["completed"] = all(row.get("status") != "skipped" for row in steps)
    navigation = report.get("navigation") or {}
    if navigation.get("status") is not None:
        body["navigation"] = {"status": navigation.get("status"), "final_url": str(report.get("page_url") or "")}
    body["assertions"] = [
        {
            "name": f"step {row.get('index')}: {row.get('description')}",
            "status": {"passed": "PASS", "failed": "FAIL"}.get(str(row.get("status")), "NOT_RUN"),
            "expected": str(row.get("description") or ""),
            "actual": str(row.get("error") or row.get("status") or ""),
            "detail": f"{row.get('duration_ms', 0)} ms",
        }
        for row in steps
    ]
    body["console"] = [
        {"level": "error", "text": str(row.get("text") or ""), "location": str(row.get("location") or "")}
        for row in report.get("console_errors") or ()
    ]
    body["runtime_errors"] = [
        {"message": str(row.get("message") or ""), "stack": str(row.get("stack") or "")}
        for row in report.get("page_errors") or ()
    ]
    observations = [f"User outcome: {scenario.outcome}"] if scenario.outcome else []
    if report.get("outcome") == "warning":
        observations.extend(f"QA warning: {item}" for item in reasons)
    body["observations"] = observations
    if screenshot_evidence_id:
        body["artifacts"] = {"screenshot": screenshot_evidence_id}
    return body


# --------------------------------------------------------------------------- #
# GitHub check run
# --------------------------------------------------------------------------- #

_CONCLUSIONS = {"pass": "success", "warning": "neutral", "fail": "failure", "could_not_run": "neutral"}
_LABELS = {"pass": "PASS", "warning": "WARNING", "fail": "FAIL", "could_not_run": "COULD NOT RUN"}


def github_check_payload(
    reports: Sequence[Mapping[str, Any]], *, head_sha: str, details_url: str = ""
) -> dict[str, Any]:
    """``POST /repos/{owner}/{repo}/check-runs`` body; ``could_not_run`` is ``neutral``, never ``success``."""

    overall = overall_outcome(reports)
    counts: dict[str, int] = {}
    for item in reports:
        outcome = str(item.get("outcome") or "could_not_run")
        counts[outcome] = counts.get(outcome, 0) + 1
    title = ", ".join(f"{counts[name]} {_LABELS[name].lower()}" for name in OUTCOMES if counts.get(name)) or (
        "no scenario ran"
    )
    first = reports[0] if reports else {}
    browser = first.get("browser") or {}
    lines = [
        f"**{_LABELS.get(overall, overall)}** on `{head_sha[:12]}` at {first.get('base_url') or 'no preview URL'}",
        "",
        f"Chromium {browser.get('version') or 'not started'}; saved steps from {first.get('policy_source') or 'the trusted base'}.",
        "",
        "| Scenario | Outcome | Why |",
        "| --- | --- | --- |",
    ]
    text: list[str] = []
    for item in reports:
        why = "; ".join(str(reason) for reason in item.get("reasons") or ())
        name = str(item.get("title") or item.get("scenario") or "")
        lines.append(f"| {name} | {_LABELS.get(str(item.get('outcome')), '')} | {why.replace('|', '/')[:300]} |")
        text.append(f"### {name}")
        if item.get("user_outcome"):
            text.append(f"User outcome: {item['user_outcome']}")
        text.append("")
        for step in item.get("steps") or ():
            error = f" -- {step.get('error')}" if step.get("error") else ""
            text.append(
                f"{step.get('index')}. `{step.get('status')}` {step.get('description')} "
                f"({step.get('duration_ms', 0)} ms){error}"
            )
        text.append("")
    output = {"title": title[:250], "summary": "\n".join(lines)[:65_000], "text": "\n".join(text)[:65_000]}
    payload: dict[str, Any] = {
        "name": QA_CHECK_NAME,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": _CONCLUSIONS.get(overall, "neutral"),
        "completed_at": _now(),
        "output": output,
    }
    if details_url:
        payload["details_url"] = details_url
    return payload


def post_github_check(payload: Mapping[str, Any], *, repository: str, gh: str = "gh") -> str:
    """Create the check run with ``gh api``; returns its URL. Raises ``RuntimeError`` on failure."""

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RuntimeError("GITHUB_REPOSITORY must look like owner/repo")
    try:
        done = subprocess.run(
            [gh, "api", "-X", "POST", f"repos/{repository}/check-runs", "--input", "-"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"gh api could not run: {exc}") from exc
    if done.returncode != 0:
        raise RuntimeError(f"gh api refused the check run: {(done.stderr or done.stdout).strip()[:300]}")
    try:
        return str(json.loads(done.stdout).get("html_url") or "")
    except ValueError:
        return ""


__all__ = [
    "OUTCOMES",
    "QA_CHECK_NAME",
    "QA_REPORT_SCHEMA",
    "classify_run",
    "exit_code",
    "github_check_payload",
    "install_playwright",
    "overall_outcome",
    "post_github_check",
    "prune_runs",
    "resolve_playwright_dir",
    "review_run_body",
    "run_scenario",
    "snapshot_for_sha",
    "validate_head_sha",
]
