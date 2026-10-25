"""The pull request reviewer's security scan: Opengrep results as independent ``static_finding`` evidence.

The binary is never run here. Its output is a set of hand-written files under ``tests/fixtures/opengrep_results``
that follow Opengrep's documented JSON and SARIF shapes; where a whole review is exercised, the "binary" is a small
script that copies one of them to the path it is asked to write (so the real subprocess, argument and file plumbing
runs) and records how it was called.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from lemoncrow.pro.capabilities.review import ai_review
from lemoncrow.pro.capabilities.review.pr_bot import engine, opengrep_install, security
from lemoncrow.pro.capabilities.review.pr_bot.runner import PrReviewError
from lemoncrow.pro.capabilities.review.sources.github import resolve_pull_request
from lemoncrow.pro.capabilities.review.static_findings import investigator_static_context, static_security_lines
from tests.gateway.pr_bot_support import BASE, FakeModel, World, build_world

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "tests" / "fixtures" / "opengrep_results"
RULE_FIXTURES = ROOT / "tests" / "fixtures" / "opengrep"
VENDORED = ROOT / "src" / "lemoncrow" / "pro" / "capabilities" / "review" / "security_rules"

OS_RULE = "lemoncrow.python.os-command-dynamic"
HASH_RULE = "lemoncrow.python.weak-hash"
BUG = "the shell command is built from the caller's name"

HANDLERS_BASE = """import os
import subprocess


def legacy(cmd):
    subprocess.run(cmd, shell=True)


def ping(host):
    return host
"""
CLEANUP = f"""

def cleanup(name):
    os.system("rm -rf /tmp/" + name)  # BUG: {BUG}
    return name
"""
CLEANUP_QUIET = CLEANUP.replace(f"  # BUG: {BUG}", "")
HASHING = """import hashlib


def etag(data):
    return hashlib.md5(data).hexdigest()
"""
SAFE_CLEANUP = '\n\ndef cleanup(name):\n    return subprocess.run(["rm", "-rf", name])\n'


def fixture(name: str) -> bytes:
    return (RESULTS / name).read_bytes()


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def test_native_output_becomes_severity_mapped_findings_without_copying_matched_text() -> None:
    parsed = security.parse_opengrep(
        fixture("native_full.json"), snapshot_paths=("app/handlers.py", "app/hashing.py", "app/settings.txt")
    )
    assert parsed.version == "1.32.0" and parsed.errors == 1 and parsed.scanned == 3
    by_rule = {item.rule_id: item for item in parsed.findings}
    # The engine's own `is_ignored` result is dropped; the rest are kept.
    assert set(by_rule) == {
        OS_RULE,
        "lemoncrow.python.subprocess-shell-true",
        HASH_RULE,
        "acme.style.todo-comment",
        "acme.crypto.maybe-bad",
        "lemoncrow.secrets.cloud-or-vcs-token",
    }
    # ERROR -> P1 (never the Reader's P2, never P0), WARNING -> P2, INFO -> suggestion, a LOW-confidence rule one lower.
    assert by_rule[OS_RULE].severity == "p1"
    assert by_rule[HASH_RULE].severity == "p2"
    assert by_rule["acme.style.todo-comment"].severity == "suggestion"
    assert by_rule["acme.crypto.maybe-bad"].severity == "p2"
    assert by_rule[OS_RULE].path == "app/handlers.py", "a leading ./ is normalised against the Snapshot's paths"
    assert (by_rule[OS_RULE].start_line, by_rule[OS_RULE].end_line) == (14, 14)
    assert by_rule[OS_RULE].cwe == (78,) and by_rule["acme.crypto.maybe-bad"].cwe == (327,)
    # The matched source line (here a made-up key) is hashed into the fingerprint and kept nowhere else.
    wire = json.dumps([item.to_wire() for item in parsed.findings])
    assert "AKIAABCDEFGHIJKLMNOP" not in wire and "rm -rf" not in wire
    assert all(item.fingerprint.startswith("sfp_") for item in parsed.findings)


def test_sarif_output_is_read_with_the_same_severity_mapping() -> None:
    parsed = security.parse_opengrep(fixture("sarif.json"), snapshot_paths=("app/handlers.py", "app/hashing.py"))
    assert parsed.version == "1.32.0"
    by_rule = {item.rule_id: item for item in parsed.findings}
    assert by_rule[OS_RULE].severity == "p1" and (by_rule[OS_RULE].path, by_rule[OS_RULE].start_line) == (
        "app/handlers.py",
        14,
    )
    assert by_rule[HASH_RULE].severity == "p2"
    assert by_rule["acme.crypto.maybe-bad"].severity == "p2", "Opengrep's `LOW CONFIDENCE` tag lowers an error one step"


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"[]", b'{"version": "1.32.0"}', b'{"results": "nope"}', b"\xff\xfe"],
)
def test_output_that_is_not_opengrep_json_is_refused(payload: bytes) -> None:
    with pytest.raises(ValueError):
        security.parse_opengrep(payload)


def test_identical_results_in_one_file_get_distinct_fingerprints() -> None:
    row = {
        "check_id": OS_RULE,
        "path": "a.py",
        "start": {"line": 1},
        "end": {"line": 1},
        "extra": {"message": "m", "severity": "ERROR", "lines": "os.system(x)", "metadata": {}},
    }
    second = {**row, "start": {"line": 9}, "end": {"line": 9}}
    parsed = security.parse_opengrep(json.dumps({"version": "1", "results": [row, second]}).encode())
    assert len({item.fingerprint for item in parsed.findings}) == 2
    assert len({item.id for item in parsed.findings}) == 2


def test_severity_mapping_is_conservative() -> None:
    assert [security.map_severity(level) for level in ("ERROR", "error", "WARNING", "INFO", "", "EXPERIMENT")] == [
        "p1",
        "p1",
        "p2",
        "suggestion",
        "suggestion",
        "suggestion",
    ]
    assert security.map_severity("ERROR", "low") == "p2" and security.map_severity("WARNING", "LOW") == "suggestion"
    assert security.map_severity("ERROR", "HIGH") == "p1"
    assert "p0" not in {security.map_severity(level, "HIGH") for level in ("ERROR", "WARNING", "INFO", "CRITICAL")}


# --------------------------------------------------------------------------- #
# the diff: which lines count
# --------------------------------------------------------------------------- #


def test_only_lines_the_pull_request_added_or_changed_count() -> None:
    patch = "\n".join([" context", "+added one", "+added two", " context", "-removed", " context", "+added three"])
    files = [
        {"path": "a.py", "status": "modified", "hunks": [{"header": "@@ -1,5 +10,6 @@", "patch": patch}]},
        # No patch text kept: the packet's own changed ranges are used.
        {"path": "b.py", "status": "added", "hunks": [{"header": "@@ -0,0 +1,3 @@", "new_ranges": [[1, 3]]}]},
        # Neither: the hunk's whole right-side span (only ever wider).
        {"path": "c.py", "status": "modified", "hunks": [{"header": "@@ -4,2 +4,3 @@"}]},
        {"path": "gone.py", "status": "deleted", "hunks": [{"header": "@@ -1 +0,0 @@", "new_ranges": [[0, 1]]}]},
        {"path": "image.png", "status": "added", "is_binary": True, "hunks": []},
        {"path": "link", "status": "added", "new_mode": 0o120000, "hunks": [{"header": "@@ -0,0 +1 @@"}]},
        {"path": "empty.txt", "status": "added", "hunks": []},
    ]
    ranges = security.changed_lines(files)
    assert ranges == {"a.py": ((11, 12), (15, 15)), "b.py": ((1, 3),), "c.py": ((4, 6),)}


def test_a_result_counts_when_any_of_its_lines_changed() -> None:
    def finding(first: int, last: int) -> Any:
        row = {
            "check_id": "r",
            "path": "a.py",
            "start": {"line": first},
            "end": {"line": last},
            "extra": {"message": "m", "severity": "ERROR"},
        }
        return security.parse_opengrep(json.dumps({"version": "1", "results": [row]}).encode()).findings[0]

    ranges = ((10, 12), (20, 20))
    assert security.place_in_diff(finding(11, 11), ranges) == (11, 11)
    assert security.place_in_diff(finding(8, 11), ranges) == (10, 11), "the comment sits on the changed part"
    assert security.place_in_diff(finding(12, 19), ranges) == (12, 12)
    assert security.place_in_diff(finding(13, 19), ranges) is None, "context between hunks"
    assert security.place_in_diff(finding(1, 9), ranges) is None
    assert security.place_in_diff(finding(11, 11), ()) is None


# --------------------------------------------------------------------------- #
# the trusted base's settings
# --------------------------------------------------------------------------- #


def test_the_security_block_reads_rules_and_the_default_switch() -> None:
    assert security.parse_security_block(None) == security.SecurityConfig()
    assert security.parse_security_block("rules: []\n") == security.SecurityConfig()
    block = security.parse_security_block(
        "security:\n  rules: [rules/security, .semgrep/extra.yml]\n  default_rules: false\n"
    )
    assert block.rules == ("rules/security", ".semgrep/extra.yml") and block.default_rules is False and not block.notes


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("security:\n  rules: ['../outside.yaml']\n", "not a repository-relative path"),
        ("security:\n  rules: ['/etc/rules.yaml']\n", "not a repository-relative path"),
        ("security:\n  rules: rules/a.yaml\n", "must be a list"),
        ("security:\n  default_rules: maybe\n", "true or false"),
        ("security:\n  mode: strict\n", "unknown keys mode"),
        ("security: yes\n", "must be a mapping"),
        ("security: [unclosed\n", "not valid YAML"),
    ],
)
def test_a_bad_security_block_is_ignored_with_a_note_and_never_raises(text: str, fragment: str) -> None:
    block = security.parse_security_block(text)
    assert any(fragment in note for note in block.notes), block.notes
    assert block.default_rules is True and block.rules == (), "a bad block never turns the vendored rules off"


def test_the_mode_is_auto_or_off_and_nothing_else() -> None:
    assert security.scan_mode({}) == "auto" and security.scan_mode({security.MODE_ENV: " OFF "}) == "off"
    assert security.scan_mode({security.MODE_ENV: ""}) == "auto"
    with pytest.raises(ValueError, match="auto, off"):
        security.scan_mode({security.MODE_ENV: "always"})


# --------------------------------------------------------------------------- #
# the scan on a real captured pull request (a fake runner stands in for the binary)
# --------------------------------------------------------------------------- #


class FakeRunner:
    """A ``security.Runner``: records how the scanner was called and writes the output file it was asked to."""

    def __init__(
        self, output: bytes | None = None, *, returncode: int = 0, stderr: str = "", error: Exception | None = None
    ):
        self.output = output
        self.returncode = returncode
        self.stderr = stderr
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, args: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float
    ) -> security.ProcessResult:
        configs = [Path(args[index + 1]) for index, value in enumerate(args) if value == "--config"]
        self.calls.append(
            {
                "args": list(args),
                "cwd": cwd,
                "env": dict(env),
                "timeout": timeout,
                "files": sorted(str(p.relative_to(cwd)) for p in cwd.rglob("*") if p.is_file()),
                "contents": {str(p.relative_to(cwd)): p.read_text() for p in cwd.rglob("*") if p.is_file()},
                "rules": {config.name: sorted(f.name for f in config.rglob("*") if f.is_file()) for config in configs},
                "rule_text": {
                    f"{config.name}/{f.name}": f.read_text() for config in configs for f in config.rglob("*.yaml")
                },
            }
        )
        if self.error is not None:
            raise self.error
        out = next(Path(arg.split("=", 1)[1]) for arg in args if arg.startswith("--json-output="))
        if self.output is not None:
            out.write_bytes(self.output)
        return security.ProcessResult(self.returncode, self.stderr)


def stub_binary(tmp_path: Path) -> str:
    path = tmp_path / "bin" / "opengrep"
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def pr_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra_base: str) -> World:
    world = build_world(tmp_path, monkeypatch, {**BASE, "app/handlers.py": HANDLERS_BASE, **extra_base})
    world.origin.push({"app/handlers.py": HANDLERS_BASE + CLEANUP, "app/hashing.py": HASHING})
    return world


def capture(world: World, tmp_path: Path) -> engine.Captured:
    pull = resolve_pull_request(world.origin.clone, "7", runner=world.github)
    scratch = tmp_path / "store"
    scratch.mkdir()
    return engine.capture_pull_request(world.origin.clone, pull, scratch)


def environ_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GH_TOKEN": "ghs_not_for_the_scanner",
        "OPENAI_API_KEY": "sk-not-for-the-scanner",
        "GITHUB_ACTIONS": "true",
        security.BIN_ENV: stub_binary(tmp_path),
        **extra,
    }


def test_the_scanner_sees_only_the_changed_files_as_they_are_at_the_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = pr_world(tmp_path, monkeypatch)
    # The checkout the job runs in is not the head: a stray edit there must not be what is scanned.
    (world.origin.clone / "app" / "handlers.py").write_text("print('working tree, not the head')\n")
    captured = capture(world, tmp_path)
    runner = FakeRunner(fixture("pr_scan.json"))
    scan = security.scan_pull_request(captured, environ=environ_for(tmp_path), runner=runner)
    assert scan.ran and scan.version == "1.32.0"
    (call,) = runner.calls
    assert call["files"] == ["app/handlers.py", "app/hashing.py"], "untouched files are not scanned"
    assert (
        "rm -rf /tmp/" in call["contents"]["app/handlers.py"]
        and "working tree" not in call["contents"]["app/handlers.py"]
    )
    # Bounded: a time limit, a per-file size limit, a per-rule timeout; and suppression comments in the PR do not hide anything.
    args = call["args"]
    assert call["timeout"] == security.DEFAULT_TIMEOUT_S
    for flag in ("--timeout=5", "--disable-nosem", "--no-rewrite-rule-ids", "--disable-version-check", "--quiet"):
        assert flag in args
    assert f"--max-target-bytes={security.MAX_FILE_BYTES}" in args and args[-1] == "."
    # The scanner does not get the job's secrets.
    assert (
        "GH_TOKEN" not in call["env"] and "OPENAI_API_KEY" not in call["env"] and set(call["env"]) <= {"PATH", "HOME"}
    )
    assert call["rules"] == {"default": sorted(f.name for f in VENDORED.glob("*.yaml"))}


def test_results_outside_the_changed_lines_are_context_and_only_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = pr_world(tmp_path, monkeypatch)
    scan = security.scan_pull_request(
        capture(world, tmp_path), environ=environ_for(tmp_path), runner=FakeRunner(fixture("pr_scan.json"))
    )
    assert scan.total == 3 and scan.outside == 1, "the old subprocess call on an untouched line"
    assert [(row.finding.rule_id, row.line) for row in scan.results] == [(OS_RULE, 14), (HASH_RULE, 5)]
    assert "elsewhere in those files, not reported" in scan.receipt()
    assert scan.to_wire()["elsewhere"] == 1 and scan.to_wire()["on_changed_lines"] == 2


def test_results_are_recorded_as_static_findings_bound_to_their_own_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = pr_world(tmp_path, monkeypatch)
    captured = capture(world, tmp_path)
    scan = security.scan_pull_request(
        captured, environ=environ_for(tmp_path), runner=FakeRunner(fixture("pr_scan.json"))
    )
    store, revision = captured.store, captured.revision
    refs, stale = investigator_static_context(store, revision, {"app/handlers.py"})
    assert [ref for ref, _label, path in refs] == [
        row.ref for row in scan.results if row.finding.path == "app/handlers.py"
    ]
    ref, label, path = refs[0]
    assert path == "app/handlers.py" and ref.startswith("static:") and "Opengrep" in label and OS_RULE in label
    assert "P1" in label and "app/handlers.py:14" in label and stale == []
    # Another file's targets are not offered it, and a file with only a warning is not a security candidate.
    assert not [r for r, _l, p in investigator_static_context(store, revision, {"src/auth.py"})[0]]
    assert static_security_lines(store, revision) == {"app/handlers.py": ((14, 14),)}
    # One evidence row per file, each bound to that file: its artifact/check refs reach only that file's targets.
    rows = [item for item in store.list_evidence(revision.review_id) if item.title.startswith("Opengrep:")]
    assert sorted(item.path for item in rows) == ["app/handlers.py", "app/hashing.py"]
    assert all(item.verification_status == "PASS" and item.provider_revision == revision.head_sha for item in rows)


def test_only_an_error_level_result_makes_a_security_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = pr_world(tmp_path, monkeypatch)
    captured = capture(world, tmp_path)
    targets = engine.review_context(captured).targets
    assert not [
        c for c in ai_review.generate_candidates(targets) if c.category == "security"
    ], "nothing in the files looks it"
    security.scan_pull_request(captured, environ=environ_for(tmp_path), runner=FakeRunner(fixture("pr_scan.json")))
    lines = static_security_lines(captured.store, captured.revision)
    assert lines == {"app/handlers.py": ((14, 14),)}
    found = [c for c in ai_review.generate_candidates(targets, security_lines=lines) if c.category == "security"]
    # One candidate, for the target that holds line 14 (the function), not for the other target of the same file.
    (candidate,) = found
    holding = [t for t in targets if t.target_id in candidate.target_ids]
    assert [t.path for t in holding] == ["app/handlers.py"]
    assert any(span.side == "new" and span.start_line <= 14 <= span.end_line for span in holding[0].spans)
    assert len([t for t in targets if t.path == "app/handlers.py"]) > 1
    assert "deterministic scanner" in candidate.rationale[0]
    # Without paths (the Reader, or a review with no scanner) the candidates are exactly what they were.
    assert [c for c in ai_review.generate_candidates(targets) if c.category == "security"] == []


def test_the_result_bound_keeps_the_most_severe_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = pr_world(tmp_path, monkeypatch)
    rows = [
        {
            "check_id": f"acme.rule-{number}",
            "path": "app/handlers.py",
            "start": {"line": 14},
            "end": {"line": 14},
            "extra": {"message": f"m{number}", "severity": "WARNING" if number % 2 else "ERROR", "lines": str(number)},
        }
        for number in range(security.MAX_RESULTS + 10)
    ]
    output = json.dumps({"version": "1.32.0", "results": rows, "errors": []}).encode()
    scan = security.scan_pull_request(
        capture(world, tmp_path), environ=environ_for(tmp_path), runner=FakeRunner(output)
    )
    assert len(scan.results) == security.MAX_RESULTS and scan.dropped == 10
    assert {row.finding.severity for row in scan.results[: security.MAX_RESULTS // 2]} == {"p1"}
    assert "not kept" in scan.receipt()


def test_rules_come_from_the_merge_base_and_never_from_the_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_yaml = "security:\n  rules: [security/rules, security/one.yaml, security/missing]\n  default_rules: false\n"
    base_rule = "rules:\n  - id: acme.base-rule\n    languages: [python]\n    severity: ERROR\n    message: m\n    pattern: bad()\n"
    world = build_world(
        tmp_path,
        monkeypatch,
        {
            **BASE,
            "app/handlers.py": HANDLERS_BASE,
            ".lemoncrow/review.yaml": review_yaml,
            "security/rules/base.yaml": base_rule,
            "security/rules/notes.txt": "not a rule file",
            "security/one.yaml": base_rule.replace("acme.base-rule", "acme.one"),
        },
    )
    # The pull request tries to replace the config and a rule: neither takes effect before it is merged.
    world.origin.push(
        {
            "app/handlers.py": HANDLERS_BASE + CLEANUP,
            ".lemoncrow/review.yaml": "security:\n  rules: []\n",
            "security/rules/base.yaml": "rules: []\n",
            "security/rules/head-only.yaml": "rules:\n  - id: acme.head-only\n",
        }
    )
    runner = FakeRunner(fixture("clean.json"))
    scan = security.scan_pull_request(capture(world, tmp_path), environ=environ_for(tmp_path), runner=runner)
    assert scan.ran
    (call,) = runner.calls
    assert list(call["rules"]) == ["repo"], "default_rules: false leaves only the repository's rules"
    texts = " ".join(call["rule_text"].values())
    assert "acme.base-rule" in texts and "acme.one" in texts and "head-only" not in texts
    assert any("security/missing was not found on the base branch" in note for note in scan.notes)
    assert scan.rules == 2


def test_the_repository_rules_run_in_addition_to_the_vendored_ones_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(
        tmp_path,
        monkeypatch,
        {
            **BASE,
            "app/handlers.py": HANDLERS_BASE,
            ".lemoncrow/review.yaml": "security:\n  rules: [security/extra.yaml]\n",
            "security/extra.yaml": "rules:\n  - id: acme.extra\n    languages: [python]\n    severity: ERROR\n    message: m\n    pattern: bad()\n",
        },
    )
    world.origin.push({"app/handlers.py": HANDLERS_BASE + CLEANUP})
    runner = FakeRunner(fixture("clean.json"))
    scan = security.scan_pull_request(capture(world, tmp_path), environ=environ_for(tmp_path), runner=runner)
    assert sorted(runner.calls[0]["rules"]) == ["default", "repo"]
    assert scan.rules == len(load_rules()) + 1


def test_a_change_with_no_text_to_scan_runs_nothing_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(tmp_path, monkeypatch, {**BASE, "docs/old.txt": "gone soon\n"})
    (world.origin.author / "logo.bin").write_bytes(b"\x00\x01\x02")
    world.origin.push({"docs/old.txt": None})  # type: ignore[dict-item]
    runner = FakeRunner(fixture("clean.json"))
    scan = security.scan_pull_request(capture(world, tmp_path), environ=environ_for(tmp_path), runner=runner)
    assert scan.ran and scan.files_scanned == 0 and runner.calls == []
    assert scan.receipt() == "Security scan: no changed file with text to scan"


# --------------------------------------------------------------------------- #
# failure is never fatal
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("runner", "fragment"),
    [
        (
            FakeRunner(None, returncode=2, stderr="\nfatal: invalid rule\nmore"),
            "exited with code 2: fatal: invalid rule",
        ),
        # A rejected rule file exits non-zero yet writes a document with no results: never read as a clean scan.
        (
            FakeRunner(fixture("invalid_config.json"), returncode=7),
            "exited with code 7: One of these properties is missing: 'match', 'pattern'",
        ),
        (FakeRunner(b"this is not json"), "output unreadable"),
        (FakeRunner(b'{"version": "1.32.0"}'), "output unreadable"),
        (FakeRunner(error=TimeoutError("slow")), f"timed out after {security.DEFAULT_TIMEOUT_S}s"),
        (FakeRunner(error=FileNotFoundError("gone")), "could not start"),
    ],
)
def test_a_scanner_that_fails_leaves_a_receipt_line_and_no_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: FakeRunner, fragment: str
) -> None:
    world = pr_world(tmp_path, monkeypatch)
    captured = capture(world, tmp_path)
    scan = security.scan_pull_request(captured, environ=environ_for(tmp_path), runner=runner)
    assert scan.unavailable and fragment in scan.reason
    assert scan.receipt().startswith("Not run: security scan unavailable (") and not scan.results
    assert static_security_lines(captured.store, captured.revision) == {}


def test_a_crash_inside_the_scan_is_contained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = pr_world(tmp_path, monkeypatch)
    captured = capture(world, tmp_path)

    class Boom:
        def __call__(self, *_args: Any) -> Any:
            raise RuntimeError("unexpected")

    scan = security.scan_pull_request(captured, environ=environ_for(tmp_path), runner=Boom())  # type: ignore[arg-type]
    assert scan.unavailable and "RuntimeError: unexpected" in scan.reason


def test_a_missing_scanner_is_unavailable_in_actions_and_silently_skipped_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = capture(pr_world(tmp_path, monkeypatch), tmp_path)
    stub_binary(tmp_path)
    nothing = {"PATH": str(tmp_path / "empty"), "GITHUB_ACTIONS": "true"}
    in_actions = security.scan_pull_request(captured, environ=nothing, runner=FakeRunner())
    assert in_actions.unavailable and "opengrep is not installed" in in_actions.reason
    # The Action says why the install failed, and that is what the receipt carries.
    told = security.scan_pull_request(
        captured,
        environ={**nothing, security.NOTE_ENV: "checksum mismatch for opengrep_manylinux_x86"},
        runner=FakeRunner(),
    )
    assert "checksum mismatch" in told.reason and told.receipt().startswith("Not run: security scan unavailable")
    local = security.scan_pull_request(captured, environ={"PATH": str(tmp_path / "empty")}, runner=FakeRunner())
    assert (
        local.status == "skipped" and local.receipt() == "" and local.to_wire()["reason"] == "opengrep is not installed"
    )
    # An Opengrep named on PATH is used locally.
    found = security.scan_pull_request(
        captured, environ={"PATH": str(tmp_path / "bin")}, runner=FakeRunner(fixture("clean.json"))
    )
    assert found.ran
    # A binary the environment names but that cannot be executed is unavailable, even off Actions.
    broken = security.scan_pull_request(
        captured, environ={security.BIN_ENV: str(tmp_path / "nope"), "GITHUB_ACTIONS": "true"}, runner=FakeRunner()
    )
    assert broken.unavailable and "is not an executable file" in broken.reason


def test_security_scan_off_runs_nothing_and_changes_no_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = capture(pr_world(tmp_path, monkeypatch), tmp_path)
    runner = FakeRunner(fixture("pr_scan.json"))
    scan = security.scan_pull_request(
        captured, environ=environ_for(tmp_path, **{security.MODE_ENV: "off"}), runner=runner
    )
    assert scan.status == "off" and not scan.results and runner.calls == [] and scan.receipt() == ""
    assert static_security_lines(captured.store, captured.revision) == {}
    assert scan.to_wire()["status"] == "off"


# --------------------------------------------------------------------------- #
# whole reviews: a recorded result, a script standing in for the binary
# --------------------------------------------------------------------------- #

FAKE_BINARY = """#!{python}
import json, os, pathlib, shutil, sys

here = pathlib.Path(__file__).resolve().parent
args = sys.argv[1:]
cwd = pathlib.Path(os.getcwd())
with (here / "calls.jsonl").open("a") as handle:
    handle.write(
        json.dumps(
            {{
                "args": args,
                "files": sorted(str(p.relative_to(cwd)) for p in cwd.rglob("*") if p.is_file()),
                "env": sorted(os.environ),
            }}
        )
        + "\\n"
    )
mode = (here / "mode").read_text().strip()
out = next(a.split("=", 1)[1] for a in args if a.startswith("--json-output="))
if mode == "fail":
    print("fatal: could not load the rules", file=sys.stderr)
    sys.exit(2)
if mode == "garbage":
    pathlib.Path(out).write_text("not json")
    sys.exit(0)
shutil.copy(here / mode, out)
"""


class Scanner:
    """The stand-in binary: ``mode`` is a fixture file to answer with, ``fail`` or ``garbage``."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "opengrep"
        self.path.write_text(FAKE_BINARY.format(python=sys.executable))
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)
        for source in RESULTS.glob("*.json"):
            (directory / source.name).write_bytes(source.read_bytes())
        self.answer("pr_scan.json")

    def answer(self, mode: str) -> None:
        (self.directory / "mode").write_text(mode)

    @property
    def calls(self) -> list[dict[str, Any]]:
        log = self.directory / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


class ScanModel(FakeModel):
    """The fake investigator, which can also cite the scanner's ``static_finding`` evidence for a finding it confirms."""

    def __init__(self) -> None:
        super().__init__()
        self.cite_static: set[str] = set()

    def __call__(self, **kwargs: Any) -> Any:
        execution = super().__call__(**kwargs)
        result = execution.result
        if isinstance(result, dict) and result.get("status") == "confirmed" and result.get("title") in self.cite_static:
            registry = self.evidence_seen[-1]["evidence"]
            result["evidence_refs"] = [
                *result["evidence_refs"],
                *(
                    row["ref"]
                    for row in registry
                    if row["kind"] == "static_finding" and set(row["target_ids"]) & set(result["target_ids"])
                ),
            ]
        return execution


def scanning_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, push: bool = True
) -> tuple[World, Scanner, ScanModel]:
    world = build_world(tmp_path, monkeypatch, {**BASE, "app/handlers.py": HANDLERS_BASE})
    scanner = Scanner(tmp_path / "scanner")
    world.model = ScanModel()
    monkeypatch.setenv("LEMONCROW_SECURITY_SCAN", "auto")
    monkeypatch.setenv("LEMONCROW_OPENGREP_BIN", str(scanner.path))
    if push:
        world.origin.push({"app/handlers.py": HANDLERS_BASE + CLEANUP, "app/hashing.py": HASHING})
    assert isinstance(world.model, ScanModel)
    return world, scanner, world.model


def raw(report: Any) -> list[Any]:
    return [finding for finding in report.findings if finding.scan]


def test_a_finding_that_cites_a_matching_static_result_is_verified_and_a_p0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _scanner, model = scanning_world(tmp_path, monkeypatch)
    model.severity[BUG] = "high"
    model.cite_static.add(BUG)
    report = world.review(fail_on="p0")
    (finding,) = [f for f in report.findings if not f.scan]
    assert finding.category == "security" and finding.severity == "p0" and finding.verified
    assert finding.static_check == OS_RULE and finding.path == "app/handlers.py"
    # The investigator was offered the scanner's result as evidence, bound to that file's target.
    offered = [row for seen in model.evidence_seen for row in seen["evidence"] if row["kind"] == "static_finding"]
    assert offered and OS_RULE in offered[0]["label"] and "app/handlers.py:14" in offered[0]["label"]
    assert [call["category"] for call in model.calls] == ["security"] and model.calls[0]["path"] == "app/handlers.py"
    # It is one finding, not also a raw one; the warning stays a held-back P2 of its own.
    assert [(f.severity, f.static_check) for f in raw(report)] == [("p2", HASH_RULE)]
    inline = [body for body in report.inline_bodies if "Static check:" in body]
    assert len(inline) == 1 and f"Static check: {OS_RULE}" in inline[0] and "P0 · must fix" in inline[0]
    assert report.verdict is not None and report.verdict.state == "must_fix" and report.status is not None
    assert report.status[0] == "failure"


def test_with_the_scan_off_the_same_model_rating_cannot_be_a_p0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _scanner, model = scanning_world(tmp_path, monkeypatch)
    monkeypatch.setenv("LEMONCROW_SECURITY_SCAN", "off")
    model.severity[BUG] = "high"
    model.cite_static.add(BUG)
    report = world.review()
    # No static evidence exists to cite and no security candidate asks for one: nothing here can be a P0.
    assert not [f for f in report.findings if f.severity == "p0"]
    assert all(row["kind"] != "static_finding" for seen in model.evidence_seen for row in seen["evidence"])


def test_a_model_finding_that_does_not_cite_the_scanner_is_not_verified_by_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _scanner, model = scanning_world(tmp_path, monkeypatch)
    model.severity[BUG] = "high"  # high, but it cites only its own code
    report = world.review()
    ai = [f for f in report.findings if not f.scan and f.category == "security"]
    assert [(f.severity, f.verified) for f in ai] == [("p1", False)]
    # The scanner's result is still shown, as its own finding (the investigator did not cite it).
    assert [(f.severity, f.static_check) for f in raw(report) if f.path == "app/handlers.py"] == [("p1", OS_RULE)]


def test_a_raw_result_nobody_confirmed_is_a_p1_security_finding_that_can_never_fail_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _scanner, model = scanning_world(tmp_path, monkeypatch, push=False)
    world.origin.push({"app/handlers.py": HANDLERS_BASE + CLEANUP_QUIET, "app/hashing.py": HASHING})
    report = world.review(fail_on="p0")
    findings = {f.static_check: f for f in raw(report)}
    assert set(findings) == {OS_RULE, HASH_RULE}
    top = findings[OS_RULE]
    assert (top.severity, top.category, top.state, top.scan, top.path, top.line) == (
        "p1",
        "security",
        "new",
        True,
        "app/handlers.py",
        14,
    )
    assert top.title == "Command injection: os.system or os.popen with a dynamic command"
    assert top.target_ids and top.anchor == "cited" and top.anchor_digest and not top.verified and not top.impact
    assert findings[HASH_RULE].severity == "p2"
    # Shown inline with the flag and the trailer, in the summary with the flag, and the warning held back.
    (comment,) = [body for body in report.inline_bodies]
    assert comment.startswith("**P1 · should fix · security** — Command injection")
    assert f"Static check: {OS_RULE}" in comment and "LemonCrow security scan (Opengrep)" in comment
    assert "LemonCrow AI review" not in comment
    assert re.search(r"^- \*\*P1\*\* .*`app/handlers.py:14` · new · security$", report.summary, re.MULTILINE)
    assert "1 low-priority finding held back" in report.summary
    # Never a P0 on its own, so it cannot fail the status whatever fail-on says.
    assert report.verdict is not None and report.verdict.state != "must_fix" and report.verdict.p1 == 1
    assert report.status is not None and report.status[0] == "success" and report.exit_code == 0
    posted = world.github.inline()
    assert len(posted) == 1 and "Static check: " + OS_RULE in posted[0]["body"]
    # The record the person running `--json` gets.
    wire = report.to_wire()
    assert wire["security"]["status"] == "ran" and wire["security"]["on_changed_lines"] == 2
    assert wire["security"]["elsewhere"] == 1 and wire["security"]["tool"] == "Opengrep"
    assert any(row.get("scan") and row.get("static_check") == OS_RULE for row in wire["findings"])
    # The investigator was asked, with the scanner's result as evidence, and did not confirm it.
    assert model.calls and all(call["category"] == "security" for call in model.calls)


def test_the_receipt_names_what_the_scan_did(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, scanner, _model = scanning_world(tmp_path, monkeypatch, push=False)
    scanner.answer("clean.json")
    world.origin.push(
        {"app/handlers.py": HANDLERS_BASE + SAFE_CLEANUP.replace("subprocess", "os"), "app/hashing.py": HASHING}
    )
    report = world.review()
    assert not report.findings
    receipt = re.search(r"- Security scan: .*", report.summary)
    assert receipt is not None
    assert (
        "Opengrep 1.32.0" in receipt[0]
        and f"{len(load_rules())} rules" in receipt[0]
        and "2 of 2 changed files" in receipt[0]
    )
    assert "0 on changed lines" in receipt[0]


def test_a_scanner_that_fails_does_not_fail_the_review_and_the_summary_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, scanner, _model = scanning_world(tmp_path, monkeypatch, push=False)
    scanner.answer("fail")
    world.origin.push({"app/handlers.py": HANDLERS_BASE + CLEANUP_QUIET, "app/hashing.py": HASHING})
    report = world.review(fail_on="p0")
    assert not report.skipped and report.exit_code == 0 and report.state is not None and report.state.complete
    assert report.status is not None and report.status[0] == "success"
    assert not raw(report)
    assert "security scan unavailable" in report.summary
    assert (
        "Not run: security scan unavailable (Opengrep exited with code 2: fatal: could not load the rules)"
        in report.summary
    )
    assert any(note.startswith("security scan unavailable: Opengrep exited") for note in report.notes)
    assert report.to_wire()["security"]["status"] == "unavailable"
    # Garbage output is the same.
    scanner.answer("garbage")
    world.origin.push({"src/plain.py": "VALUE = 3\n"})
    again = world.review()
    assert not again.skipped and "security scan unavailable (Opengrep output unreadable" in again.summary


def test_a_missing_binary_in_actions_says_why_and_still_reviews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _scanner, _model = scanning_world(tmp_path, monkeypatch)
    monkeypatch.setenv("LEMONCROW_OPENGREP_BIN", "")
    monkeypatch.setenv(
        "LEMONCROW_OPENGREP_NOTE", "checksum mismatch for opengrep_manylinux_x86: the download was not installed"
    )
    monkeypatch.setattr(security.shutil, "which", lambda *_a, **_k: None)
    report = world.review()
    assert not report.skipped and report.state is not None and report.state.complete
    assert "security scan unavailable" in report.summary and "checksum mismatch" in report.summary


def test_off_never_runs_the_binary_and_leaves_the_summary_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, scanner, model = scanning_world(tmp_path, monkeypatch)
    monkeypatch.setenv("LEMONCROW_SECURITY_SCAN", "off")
    report = world.review()
    assert scanner.calls == []
    assert not raw(report) and "Security scan" not in report.summary and "security scan" not in report.summary
    assert report.to_wire()["security"]["status"] == "off"
    assert all(row["kind"] != "static_finding" for seen in model.evidence_seen for row in seen["evidence"])


def test_a_local_run_without_opengrep_skips_silently_and_says_so_in_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _scanner, _model = scanning_world(tmp_path, monkeypatch)
    monkeypatch.delenv("LEMONCROW_OPENGREP_BIN")
    monkeypatch.setattr(security.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.delenv("GITHUB_ACTIONS")
    report = world.review(publish=False)
    assert not report.skipped
    wire = report.to_wire()["security"]
    assert wire["status"] == "skipped" and wire["reason"] == "opengrep is not installed"
    assert "security scan" not in report.summary.lower()
    assert not [
        note for note in report.notes if "security" in note
    ], "nothing is printed for a scanner nobody asked for"


def test_an_invalid_security_scan_value_is_an_error_not_a_silent_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, scanner, _model = scanning_world(tmp_path, monkeypatch)
    monkeypatch.setenv("LEMONCROW_SECURITY_SCAN", "sometimes")
    with pytest.raises(PrReviewError, match="security-scan must be one of auto, off"):
        world.review()
    assert scanner.calls == []


def test_a_later_push_carries_a_still_present_result_on_its_thread_and_closes_a_fixed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, scanner, _model = scanning_world(tmp_path, monkeypatch, push=False)
    world.origin.push({"app/handlers.py": HANDLERS_BASE + CLEANUP_QUIET, "app/hashing.py": HASHING})
    first = world.review()
    (opened,) = [f for f in raw(first) if f.static_check == OS_RULE]
    assert opened.state == "new" and opened.thread_id > 0 and len(world.github.inline()) == 1

    # An unrelated push: the same code, the same result at a shifted line. Same finding, same thread, no new comment.
    world.origin.push({"src/plain.py": "VALUE = 3\n"})
    second = world.review()
    (carried,) = [f for f in raw(second) if f.static_check == OS_RULE]
    assert (carried.id, carried.thread_id, carried.state) == (opened.id, opened.thread_id, "carried")
    assert len(world.github.inline()) == 1

    # The scanner cannot run on the next push: absence proves nothing, so the finding stays, unverified.
    scanner.answer("fail")
    world.origin.push({"src/plain.py": "VALUE = 4\n"})
    third = world.review()
    (stays,) = [f for f in raw(third) if f.static_check == OS_RULE]
    assert stays.state == "carried" and stays.unverified and stays.thread_id == opened.thread_id
    assert "security scan unavailable" in third.summary

    # The code is fixed and the scanner no longer reports it: closed on its thread, deterministically.
    scanner.answer("clean.json")
    fixed_code = HANDLERS_BASE + CLEANUP_QUIET.replace('os.system("rm -rf /tmp/" + name)', 'os.remove("/tmp/" + name)')
    world.origin.push({"app/handlers.py": fixed_code})
    fourth = world.review()
    (closed,) = [f for f in fourth.findings if f.id == opened.id]
    assert closed.state == "fixed" and closed.resolved_in
    assert any(
        reply["in_reply_to_id"] == opened.thread_id and "Fixed in" in reply["body"]
        for reply in world.github.bot_replies()
    )
    assert not [f for f in fourth.findings if f.is_open and f.scan and f.static_check == OS_RULE]


def test_a_result_the_investigator_later_confirms_takes_over_its_threads_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _scanner, model = scanning_world(tmp_path, monkeypatch, push=False)
    model.forget.add(BUG)  # the first time, the investigator does not report it
    world.origin.push({"app/handlers.py": HANDLERS_BASE + CLEANUP, "app/hashing.py": HASHING})
    first = world.review()
    (opened,) = [f for f in raw(first) if f.static_check == OS_RULE]
    assert opened.severity == "p1" and opened.thread_id > 0

    # The function changes (so it is investigated again) but the flagged line does not; now the model confirms it,
    # citing the scanner. That finding inherits the raw one's id and thread: one defect, one thread, no new comment.
    model.forget.clear()
    model.severity[BUG] = "high"
    model.cite_static.add(BUG)
    world.origin.push({"app/handlers.py": HANDLERS_BASE + CLEANUP.replace("return name", "return name.strip()")})
    second = world.review()
    same = [f for f in second.findings if f.id == opened.id]
    assert len(same) == 1 and same[0].thread_id == opened.thread_id and not same[0].scan
    assert same[0].severity == "p0" and same[0].static_check == OS_RULE and same[0].verified
    assert not [f for f in raw(second) if f.static_check == OS_RULE]
    assert len(world.github.inline()) == 1, "no second comment about the same defect"


def test_a_result_for_a_file_that_left_the_change_goes_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, _scanner, _model = scanning_world(tmp_path, monkeypatch, push=False)
    world.origin.push({"app/handlers.py": HANDLERS_BASE + CLEANUP_QUIET, "app/hashing.py": HASHING})
    first = world.review()
    (weak,) = [f for f in raw(first) if f.static_check == HASH_RULE]
    assert weak.state == "new"
    world.origin.push({"app/hashing.py": None})  # type: ignore[dict-item]
    second = world.review()
    assert [f.state for f in second.findings if f.id == weak.id] == ["stale"]


# --------------------------------------------------------------------------- #
# the installer: pins, checksums, the cache
# --------------------------------------------------------------------------- #


def test_every_pinned_checksum_is_a_real_sha256_or_the_marked_todo() -> None:
    """R11, as for SHA pins: never invent the digest; say it is owed. A TODO is allowed, a made-up value is not."""

    assert re.fullmatch(r"v\d+\.\d+\.\d+", opengrep_install.VERSION), "pin a release tag, never a branch or `latest`"
    assert opengrep_install.RELEASE_URL.startswith("https://github.com/opengrep/opengrep/releases/download/")
    assert opengrep_install.PINS, "at least one platform is pinned"
    seen: set[str] = set()
    for (system, machine), pin in opengrep_install.PINS.items():
        assert system in {"linux", "darwin"} and machine in {"x86_64", "aarch64", "arm64"}
        assert re.fullmatch(r"opengrep_(manylinux|osx)_(x86|aarch64|arm64)", pin.asset), pin.asset
        assert (
            opengrep_install.is_sha256(pin.sha256) or pin.sha256 == opengrep_install.CHECKSUM_TODO
        ), f"{pin.asset}: {pin.sha256!r} is neither 64 lowercase hex characters nor {opengrep_install.CHECKSUM_TODO}"
        assert pin.asset not in seen, "one asset per platform"
        seen.add(pin.asset)
    digests = [pin.sha256 for pin in opengrep_install.PINS.values() if opengrep_install.is_sha256(pin.sha256)]
    assert len(digests) == len(set(digests)), "two platforms cannot share a binary"


@pytest.mark.parametrize(
    "value",
    ["", "TODO", "todo(checksum)", "a" * 63, "a" * 65, "A" * 64, "g" * 64, " " + "a" * 63, "sha256:" + "a" * 57],
)
def test_only_a_lowercase_64_hex_digest_counts_as_a_checksum(value: str) -> None:
    assert not opengrep_install.is_sha256(value)
    assert opengrep_install.is_sha256("0123456789abcdef" * 4)


def test_the_installer_is_standard_library_only_and_not_tied_to_the_package() -> None:
    source = Path(opengrep_install.__file__).read_text(encoding="utf-8")
    imported = set(re.findall(r"^(?:from|import) (\w+)", source, re.MULTILINE))
    assert "lemoncrow" not in imported and "yaml" not in imported and "requests" not in imported


def test_platforms_are_matched_the_way_releases_are_named() -> None:
    key = opengrep_install.platform_key
    assert key("Linux", "x86_64") == ("linux", "x86_64") and key("Linux", "amd64") == ("linux", "x86_64")
    assert key("Linux", "arm64") == ("linux", "aarch64") and key("Linux", "aarch64") == ("linux", "aarch64")
    assert key("Darwin", "arm64") == ("darwin", "arm64") and key("Darwin", "x86_64") == ("darwin", "x86_64")
    pin, why = opengrep_install.pin_for("Windows", "AMD64")
    assert pin is None and "no Opengrep release is pinned for windows x86_64" in why
    pin, why = opengrep_install.pin_for("Linux", "x86_64")
    assert pin is not None and pin.asset == "opengrep_manylinux_x86" and why == ""


SCRIPT = b"#!/bin/sh\necho 1.32.0\n"


def patched_pin(monkeypatch: pytest.MonkeyPatch, sha256: str) -> None:
    monkeypatch.setitem(
        opengrep_install.PINS, ("linux", "x86_64"), opengrep_install.Pin("opengrep_manylinux_x86", sha256)
    )


def test_a_checksum_marked_todo_is_never_installed_or_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patched_pin(monkeypatch, opengrep_install.CHECKSUM_TODO)
    fetched: list[str] = []
    result = opengrep_install.install(
        tmp_path, system="Linux", machine="x86_64", fetch=lambda url, dest: fetched.append(url)
    )
    assert result.path == "" and "TODO(checksum)" in result.note and fetched == []
    assert not any(tmp_path.rglob("opengrep"))


def test_a_download_that_does_not_match_its_checksum_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patched_pin(monkeypatch, hashlib.sha256(SCRIPT).hexdigest())

    def tampered(_url: str, destination: Path) -> None:
        destination.write_bytes(SCRIPT + b"# a different binary\n")

    result = opengrep_install.install(tmp_path, system="Linux", machine="x86_64", fetch=tampered)
    assert result.path == "" and "checksum mismatch" in result.note
    assert not (tmp_path / opengrep_install.VERSION / "opengrep").exists()
    assert not list(tmp_path.rglob("download-*")), "the partial download is cleaned up"


def test_a_failed_download_is_a_note_not_an_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patched_pin(monkeypatch, hashlib.sha256(SCRIPT).hexdigest())

    def refused(_url: str, _destination: Path) -> None:
        raise OSError("network is down")

    result = opengrep_install.install(tmp_path, system="Linux", machine="x86_64", fetch=refused)
    assert result.path == "" and result.note == "the download failed (OSError)"


def test_a_verified_download_is_installed_once_and_then_served_from_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patched_pin(monkeypatch, hashlib.sha256(SCRIPT).hexdigest())
    urls: list[str] = []

    def release(url: str, destination: Path) -> None:
        urls.append(url)
        destination.write_bytes(SCRIPT)

    first = opengrep_install.install(tmp_path, system="Linux", machine="x86_64", fetch=release, env={})
    assert first.path == str(tmp_path / opengrep_install.VERSION / "opengrep") and first.downloaded and not first.note
    assert urls == [
        f"https://github.com/opengrep/opengrep/releases/download/{opengrep_install.VERSION}/opengrep_manylinux_x86"
    ]
    assert Path(first.path).stat().st_mode & stat.S_IXUSR
    again = opengrep_install.install(tmp_path, system="Linux", machine="x86_64", fetch=release, env={})
    assert again.path == first.path and not again.downloaded and len(urls) == 1, "a cache hit downloads nothing"


def test_the_downloaded_binary_is_first_run_without_the_jobs_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patched_pin(monkeypatch, hashlib.sha256(SCRIPT).hexdigest())
    seen: list[dict[str, str]] = []
    real = opengrep_install.smoke_test

    def spy(binary: Path, env: Mapping[str, str]) -> str:
        seen.append(dict(env))
        return real(binary, env)

    monkeypatch.setattr(opengrep_install, "smoke_test", spy)

    def release(_url: str, destination: Path) -> None:
        destination.write_bytes(SCRIPT)

    environment = {"PATH": "/usr/bin:/bin", "ANTHROPIC_API_KEY": "sk-secret", "GH_TOKEN": "ghs_secret"}
    result = opengrep_install.install(tmp_path, system="Linux", machine="x86_64", fetch=release, env=environment)
    assert result.path and seen == [{"PATH": "/usr/bin:/bin"}]


def test_a_cache_that_no_longer_matches_the_pin_is_replaced_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patched_pin(monkeypatch, hashlib.sha256(SCRIPT).hexdigest())
    target = tmp_path / opengrep_install.VERSION / "opengrep"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"#!/bin/sh\necho poisoned\n")
    target.chmod(0o755)
    calls: list[str] = []

    def release(url: str, destination: Path) -> None:
        calls.append(url)
        destination.write_bytes(SCRIPT)

    result = opengrep_install.install(tmp_path, system="Linux", machine="x86_64", fetch=release, env={})
    assert result.path == str(target) and len(calls) == 1 and target.read_bytes() == SCRIPT


def test_a_binary_that_does_not_report_the_pinned_version_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = b"#!/bin/sh\necho 0.0.1\n"
    patched_pin(monkeypatch, hashlib.sha256(wrong).hexdigest())

    def release(_url: str, destination: Path) -> None:
        destination.write_bytes(wrong)

    result = opengrep_install.install(tmp_path, system="Linux", machine="x86_64", fetch=release, env={})
    assert result.path == "" and "did not report" in result.note


def test_the_pin_and_install_commands_write_action_outputs_and_always_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "temp"))
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    assert opengrep_install.main(["pin"]) == 0
    lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert lines["dir"] == str(tmp_path / "temp" / "lemoncrow-opengrep")
    pin = opengrep_install.PINS[("linux", "x86_64")]
    assert lines["key"] == f"opengrep-{opengrep_install.VERSION}-{pin.sha256[:16]}-{pin.asset}"

    monkeypatch.setattr(opengrep_install, "install", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    output.write_text("")
    assert opengrep_install.main(["install"]) == 0
    lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert lines["bin"] == "" and lines["note"] == "the install failed (RuntimeError)"
    assert "::warning" in capsys.readouterr().out
    assert opengrep_install.main(["unknown"]) == 0


def test_a_platform_without_a_pin_gets_no_cache_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    assert opengrep_install.main(["pin"]) == 0
    assert "key=\n" in output.read_text()


# --------------------------------------------------------------------------- #
# the vendored ruleset
# --------------------------------------------------------------------------- #

RULE_FILES = sorted(VENDORED.glob("*.yaml"))


def load_rules() -> list[tuple[Path, Mapping[str, Any]]]:
    return [(path, rule) for path in RULE_FILES for rule in yaml.safe_load(path.read_text(encoding="utf-8"))["rules"]]


def test_the_vendored_ruleset_is_small_original_and_carries_its_licence() -> None:
    rules = load_rules()
    assert 20 <= len(rules) <= 60, "a small, high-signal set"
    ids = [rule["id"] for _path, rule in rules]
    assert len(ids) == len(set(ids)), "rule ids are unique"
    for path in RULE_FILES:
        head = path.read_text(encoding="utf-8")[:900]
        assert (
            "SPDX-License-Identifier: Apache-2.0" in head and "Original rules written for LemonCrow" in head
        ), path.name
    readme = " ".join((VENDORED / "README.md").read_text(encoding="utf-8").split())
    for fact in ("Semgrep Rules License", "Commons Clause", "Apache-2.0", "copied from, or adapted from"):
        assert fact in readme, fact


def test_the_documented_rule_count_is_the_real_one() -> None:
    count = len(load_rules())
    guide = (ROOT / "docs" / "setup" / "github-action.md").read_text(encoding="utf-8")
    status = (ROOT / "docs" / "status" / "capabilities.toml").read_text(encoding="utf-8")
    assert f"**{count} original rules**" in guide, f"the guide must say there are {count} original rules"
    assert f"The default ruleset is {count} original Apache-2.0 rules" in status


def test_every_rule_is_well_formed_and_maps_to_a_conservative_severity() -> None:
    for path, rule in load_rules():
        name = f"{path.name}:{rule.get('id')}"
        assert re.fullmatch(r"lemoncrow\.[a-z]+\.[a-z0-9-]+", rule["id"]), name
        assert rule["id"].split(".")[1] == path.stem, name
        assert rule["severity"] in {"ERROR", "WARNING"}, f"{name}: INFO is never a finding and CRITICAL does not exist"
        assert len(" ".join(rule["message"].split())) >= 60, f"{name}: say what to change"
        meta = rule["metadata"]
        assert meta["category"] == "security" and meta["confidence"] in {"HIGH", "MEDIUM", "LOW"}, name
        assert all(re.match(r"CWE-\d+: ", cwe) for cwe in meta["cwe"]) and meta["cwe"], name
        assert 10 <= len(meta["title"]) <= 80 and not meta["title"].endswith("."), name
        assert set(rule["languages"]) <= {"python", "javascript", "typescript", "go", "java", "regex"}, name
        # Whatever the rule says, the reviewer never turns a result into more than a P1.
        assert security.map_severity(rule["severity"], meta["confidence"]) in {"p1", "p2"}, name


def test_a_rule_message_never_repeats_the_matched_text() -> None:
    """A report must not copy a secret: messages are fixed text, with no metavariable interpolation."""

    for path, rule in load_rules():
        assert not re.search(r"\$[A-Z_]+", rule["message"]), f"{path.name}:{rule['id']} interpolates matched text"


def test_every_rule_has_a_line_it_must_report_and_one_it_must_not() -> None:
    must: dict[str, int] = {}
    must_not: dict[str, int] = {}
    for fixture_file in RULE_FIXTURES.iterdir():
        for line in fixture_file.read_text(encoding="utf-8").splitlines():
            found = re.search(r"\b(ruleid|ok): (lemoncrow\.[\w.-]+)", line)
            if found:
                bucket = must if found[1] == "ruleid" else must_not
                bucket[found[2]] = bucket.get(found[2], 0) + 1
    ids = {rule["id"] for _path, rule in load_rules()}
    assert set(must) <= ids and set(must_not) <= ids, "a fixture names a rule that does not exist"
    assert ids - set(must) == set(), f"no `ruleid:` line for {sorted(ids - set(must))}"
    assert ids - set(must_not) == set(), f"no `ok:` line for {sorted(ids - set(must_not))}"
    for path in RULE_FILES:
        assert list(RULE_FIXTURES.glob(f"{path.stem}.*")), f"no fixture file named {path.stem}.*"


def test_the_default_rules_are_reachable_from_an_installed_copy_of_the_package() -> None:
    assert security.RULES_DIR == VENDORED and (VENDORED / "python.yaml").is_file()
    # Staging copies the YAML and leaves the documentation behind, so the scanner is handed rules and nothing else.
    assert not [path for path in security.RULES_DIR.iterdir() if path.suffix not in {".yaml", ".md"}]


# --------------------------------------------------------------------------- #
# with the noise controls, the impact label and the risks examined
# --------------------------------------------------------------------------- #

IGNORE_HANDLERS = "review:\n  ignore:\n    - app/handlers.py\n"
STRICT = "review:\n  strictness: 3\n"


def test_a_file_the_review_ignores_is_not_scanned_and_gets_no_scan_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = pr_world(tmp_path, monkeypatch, **{".lemoncrow/review.yaml": IGNORE_HANDLERS})
    runner = FakeRunner(fixture("pr_scan.json"))
    scan = security.scan_pull_request(capture(world, tmp_path), environ=environ_for(tmp_path), runner=runner)
    (call,) = runner.calls
    assert call["files"] == ["app/hashing.py"], "review.ignore leaves the file out of the scan, as of the review"
    assert scan.ran and {row.finding.path for row in scan.results} == {"app/hashing.py"}
    assert scan.files_changed == 1


def test_a_scan_result_below_the_threshold_is_held_back_and_the_risks_examined_still_name_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(
        tmp_path, monkeypatch, {**BASE, "app/handlers.py": HANDLERS_BASE, ".lemoncrow/review.yaml": STRICT}
    )
    scanner = Scanner(tmp_path / "scanner")
    world.model = ScanModel()
    monkeypatch.setenv("LEMONCROW_SECURITY_SCAN", "auto")
    monkeypatch.setenv("LEMONCROW_OPENGREP_BIN", str(scanner.path))
    world.origin.push({"app/handlers.py": HANDLERS_BASE + CLEANUP, "app/hashing.py": HASHING})
    report = world.review()
    (comment,) = world.github.summary_comments()
    lines = str(comment["body"]).split("\n")
    body = "\n".join(lines)
    assert report.verdict is not None and (report.verdict.p1, report.verdict.p2, report.verdict.held) == (2, 0, 1)
    assert "2 should-fix · 1 held back" in lines[1]
    # The warning is a P2 below strictness 3: listed once, with the flag that says it is the scanner's, never inline.
    held = body.split("held back below strictness 3</summary>", 1)[1].split("</details>", 1)[0]
    assert "Weak hash: MD5 or SHA-1" in held and "`app/hashing.py:5` · new · security" in held
    assert not [text for text in report.inline_bodies if "Weak hash" in text]
    # The scanner's error-level result is a risk signal of the change, found without a model, and sets the impact label.
    assert lines[3].startswith("**Medium impact** · security-sensitive code")
    assert "<summary>Risks examined: 1 of 1 · 1 found</summary>" in body and "2 open findings" in body
    assert body.index("held back below strictness 3") < body.index("Risks examined")
    prompt = body.split("Fix with your agent", 1)[1].split("</details>", 1)[0]
    assert "Weak hash" not in prompt, "a held-back result is not in the fix prompt"
