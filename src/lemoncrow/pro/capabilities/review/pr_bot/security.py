"""Opengrep as independent evidence for the pull request reviewer (build-list item 10).

The reviewer runs the open-source Opengrep engine over the files a pull request changed, with a small vendored ruleset
(:data:`RULES_DIR`) and any rules the *trusted base* names, and turns what it reports into the ``static_finding``
evidence the existing severity rule already treats as independent (``ai_review._INDEPENDENT_EVIDENCE``):

* **Scope.** Only files of the change, read from the Snapshot (never the working tree, which on a ``pull_request``
  event is a merge commit), bounded in count, size and time. A result counts as a finding only when its line is a line
  the pull request added or changed; a result elsewhere in a changed file is context and is only counted.
* **Evidence.** Each file with a result becomes one ingested check row bound to that file, so the AI investigator can
  cite ``static:`` refs and a finding that cites one is ``verified`` (P0 when it is rated high). A file with an
  error-level result also becomes a ``security`` candidate (``ai_review.generate_candidates``).
* **Raw results.** A result no investigator cited shows as its own ``security`` finding: ERROR is P1, WARNING is P2,
  a result of low rule confidence is one step lower, and none is ever a P0 on its own. It carries the trailer
  ``Static check: <rule id>``.
* **Failure.** A scanner that is missing, fails, times out or prints something unreadable never fails the review: the
  receipt says ``security scan unavailable`` and the reason.

Nothing the pull request wrote chooses what is scanned for: the rules come from this package and from the merge base,
``nosem`` comments are ignored (``--disable-nosem``), and the scanner never receives the process environment.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final

import yaml

from lemoncrow.pro.capabilities.review.check_ingest import (
    MAX_MESSAGE_CHARS,
    CheckIngest,
    ImportedFinding,
    IngestAuthority,
    Truncation,
    finding_fingerprint,
    normalize_location,
    parse_sarif,
    record_check_ingest,
)
from lemoncrow.pro.capabilities.review.criticality import SEVERITY_RANK, FindingSeverity
from lemoncrow.pro.capabilities.review.pr_bot.findings import (
    MAX_EXPLANATION,
    MAX_TITLE,
    BotFinding,
    Located,
    trimmed,
)
from lemoncrow.pro.capabilities.review.pr_bot.settings import parse_file_settings
from lemoncrow.pro.capabilities.review.rules import path_matches
from lemoncrow.pro.capabilities.review.static_findings import finding_ref

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.pr_bot.engine import Captured
    from lemoncrow.pro.capabilities.review.pr_bot.state import ReviewState

TOOL: Final[str] = "Opengrep"
#: ``auto`` runs the scanner when one is available; ``off`` never does (the Action's ``security-scan`` input).
SCAN_MODES: Final[tuple[str, ...]] = ("auto", "off")
MODE_ENV: Final[str] = "LEMONCROW_SECURITY_SCAN"
#: The Opengrep binary the Action installed (empty when it could not); without it, ``opengrep`` on ``PATH``.
BIN_ENV: Final[str] = "LEMONCROW_OPENGREP_BIN"
#: Why the Action could not install it, for the receipt.
NOTE_ENV: Final[str] = "LEMONCROW_OPENGREP_NOTE"
TIMEOUT_ENV: Final[str] = "LEMONCROW_SECURITY_SCAN_TIMEOUT"
#: Wall-clock bound on one scan, seconds; the environment may lower or raise it within :data:`TIMEOUT_RANGE`.
DEFAULT_TIMEOUT_S: Final[int] = 120
TIMEOUT_RANGE: Final[tuple[int, int]] = (10, 600)
#: Bounds on what is scanned: changed files (first by path), bytes per file and in all.
MAX_FILES: Final[int] = 300
MAX_FILE_BYTES: Final[int] = 500_000
MAX_TOTAL_BYTES: Final[int] = 20_000_000
#: Results kept as evidence and findings (most severe first); the rest are counted.
MAX_RESULTS: Final[int] = 50
#: Rule files a repository may add, and the bytes of each.
MAX_RULE_FILES: Final[int] = 30
MAX_RULE_BYTES: Final[int] = 200_000
MAX_JSON_BYTES: Final[int] = 16 * 1024 * 1024
RULES_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "security_rules"
#: Environment variables the scanner may see; nothing else (no token, no model key) reaches it.
_ENV_ALLOW: Final[frozenset[str]] = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT"})
#: Paths never scanned: generated, vendored or locked files that only add noise.
_SKIP = re.compile(
    r"(^|/)(node_modules|vendor|dist|build|\.git|__pycache__|\.venv|site-packages)/"
    r"|\.min\.(js|css)$|\.lock$|(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|go\.sum|poetry\.lock|uv\.lock)$"
)
_SYMLINK_MODE: Final[int] = 0o120000
_CWE = re.compile(r"cwe[-_/ ]?0*(\d{1,5})", re.IGNORECASE)
_SENTENCE = re.compile(r"^(.{20,}?[.!?])(?:\s|$)")
_RULE_SUFFIXES: Final[tuple[str, ...]] = (".yaml", ".yml")


# --------------------------------------------------------------------------- #
# the trusted base's settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SecurityConfig:
    """The ``security:`` block of the trusted base's ``.lemoncrow/review.yaml``.

    ``rules`` are repository paths (files or directories of Opengrep rule YAML) read from the merge base and run
    *in addition to* the vendored ruleset, unless ``default_rules`` is false. ``notes`` say what was ignored.
    """

    rules: tuple[str, ...] = ()
    default_rules: bool = True
    notes: tuple[str, ...] = ()
    #: ``review.ignore`` of the same file: a file it names is out of the review, so it is not scanned either (a scan
    #: result on it would be a finding the repository asked not to get).
    ignore: tuple[str, ...] = ()


def scan_mode(environ: Mapping[str, str]) -> str:
    """``auto`` or ``off`` from ``LEMONCROW_SECURITY_SCAN`` (``auto`` when unset); anything else is an error."""

    value = str(environ.get(MODE_ENV) or "auto").strip().lower() or "auto"
    if value not in SCAN_MODES:
        raise ValueError(f"security-scan must be one of {', '.join(SCAN_MODES)}")
    return value


def _safe_relative(path: str) -> bool:
    pure = PurePosixPath(path)
    return bool(path) and not pure.is_absolute() and ".." not in pure.parts and "\\" not in path and "\0" not in path


def parse_security_block(content: str | None) -> SecurityConfig:
    """The ``security:`` block of a ``review.yaml`` text; defaults when absent, and a note for anything ignored."""

    if not content or not content.strip():
        return SecurityConfig()
    try:
        loaded = yaml.safe_load(content)
    except yaml.YAMLError:
        return SecurityConfig(notes=("the trusted base's review.yaml is not valid YAML; the vendored rules ran",))
    raw = loaded.get("security") if isinstance(loaded, Mapping) else None
    if raw is None:
        return SecurityConfig()
    if not isinstance(raw, Mapping):
        return SecurityConfig(notes=("security: must be a mapping; the vendored rules ran",))
    notes: list[str] = []
    unknown = sorted(str(key) for key in raw if key not in {"rules", "default_rules"})
    if unknown:
        notes.append(f"security: ignored unknown keys {', '.join(unknown)}")
    rows = raw.get("rules", [])
    rules: list[str] = []
    if not isinstance(rows, list):
        notes.append("security.rules must be a list of paths")
        rows = []
    for row in rows:
        text = row.strip().rstrip("/") if isinstance(row, str) else ""
        if not _safe_relative(text) or len(text) > 300:
            notes.append(f"security.rules: ignored {str(row)[:80]!r}, not a repository-relative path")
        elif text not in rules:
            rules.append(text)
    if len(rules) > MAX_RULE_FILES:
        notes.append(f"security.rules: only the first {MAX_RULE_FILES} paths are read")
        rules = rules[:MAX_RULE_FILES]
    flag = raw.get("default_rules", True)
    if not isinstance(flag, bool):
        notes.append("security.default_rules must be true or false; true was used")
        flag = True
    return SecurityConfig(rules=tuple(rules), default_rules=flag, notes=tuple(notes))


# --------------------------------------------------------------------------- #
# reading files at a git revision (the trusted base)
# --------------------------------------------------------------------------- #


def _git(root: Path, *args: str, timeout: float = 20) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=timeout, check=False)


def read_base_file(root: Path, sha: str, path: str, *, limit: int = MAX_RULE_BYTES) -> str | None:
    """*path* as it is at commit *sha*, or ``None`` when it does not exist there (or is over *limit* bytes)."""

    if not sha or not _safe_relative(path):
        return None
    done = _git(root, "show", f"{sha}:{path}")
    if done.returncode != 0 or len(done.stdout) > limit:
        return None
    return done.stdout.decode("utf-8", errors="replace")


def _base_rule_paths(root: Path, sha: str, path: str) -> list[str]:
    """The rule YAML files *path* names at *sha*: the file itself, or the YAML files under a directory."""

    if path.endswith(_RULE_SUFFIXES):
        return [path]
    listing = _git(root, "ls-tree", "-r", "--name-only", "-z", sha, "--", path)
    if listing.returncode != 0:
        return []
    names = [name for name in listing.stdout.decode("utf-8", errors="replace").split("\0") if name]
    return sorted(name for name in names if name.endswith(_RULE_SUFFIXES))


# --------------------------------------------------------------------------- #
# the scanner's result
# --------------------------------------------------------------------------- #


_LEVEL_SEVERITY: Final[Mapping[str, FindingSeverity]] = {"error": "p1", "warning": "p2"}
_ONE_LOWER: Final[Mapping[FindingSeverity, FindingSeverity]] = {"p1": "p2", "p2": "suggestion"}


def map_severity(level: str, confidence: str = "") -> FindingSeverity:
    """A rule's severity as a finding severity: ERROR is P1, WARNING is P2, everything else a suggestion.

    Deliberately not :func:`check_ingest.display_severity`, which keeps an imported error at P2 for the Reader. The
    reviewer promotes a scanner error one step further (P1) and no further: only an investigator's confirmation can
    make a P0. A rule that says its own ``confidence`` is LOW is one step lower.
    """

    base = _LEVEL_SEVERITY.get(level.strip().lower(), "suggestion")
    return _ONE_LOWER.get(base, base) if confidence.strip().upper() == "LOW" else base


@dataclass(frozen=True, slots=True)
class ParsedScan:
    """What one Opengrep output document held."""

    version: str
    findings: tuple[ImportedFinding, ...]
    #: Per-result titles from the rule's ``metadata.title``, by finding id.
    titles: Mapping[str, str] = field(default_factory=dict)
    #: Files or rules the engine could not handle (parse errors, timeouts); the scan went on without them.
    errors: int = 0
    #: Files the engine says it scanned (its own default ignore list skips test directories, vendored and minified
    #: files); ``None`` when the output does not say.
    scanned: int | None = None


def _bounded(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _cwes(values: Iterable[object]) -> tuple[int, ...]:
    found: set[int] = set()
    for value in values:
        found.update(int(match) for match in _CWE.findall(str(value)))
    return tuple(sorted(found))


def _finding_id(rule_id: str, path: str, start: int, end: int, message: str) -> str:
    document = json.dumps([TOOL, rule_id, path, start, end, message[:200]], separators=(",", ":"))
    return "sf_" + hashlib.sha256(document.encode("utf-8")).hexdigest()[:20]


def parse_opengrep(payload: bytes, *, snapshot_paths: Sequence[str] = ()) -> ParsedScan:
    """Opengrep's ``--json`` output (or a SARIF 2.1.0 document) as bounded, severity-mapped findings.

    Raises ``ValueError`` when the document is neither. Results the engine itself marks as ignored (``is_ignored``)
    are dropped. The matched source text (``extra.lines``) is used only inside the finding's fingerprint, so a secret
    on a line is never copied into evidence, a comment or the state.
    """

    if len(payload) > MAX_JSON_BYTES:
        raise ValueError("the scanner's output is over the size bound")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("the scanner's output is not valid JSON") from exc
    if not isinstance(document, Mapping):
        raise ValueError("the scanner's output is not a JSON object")
    if str(document.get("version") or "") == "2.1.0" and "runs" in document:
        return _from_sarif(payload, snapshot_paths)
    results = document.get("results")
    if not isinstance(results, list):
        raise ValueError("the scanner's output has no results list")
    errors = document.get("errors")
    paths = document.get("paths")
    scanned = paths.get("scanned") if isinstance(paths, Mapping) else None
    parsed: list[ImportedFinding] = []
    titles: dict[str, str] = {}
    for raw in results:
        item = _native_result(raw, snapshot_paths)
        if item is not None:
            parsed.append(item[0])
            titles[item[0].id] = item[1]
    return ParsedScan(
        version=_bounded(document.get("version"), 40),
        findings=_numbered(parsed),
        titles=titles,
        errors=len(errors) if isinstance(errors, list) else 0,
        scanned=len(scanned) if isinstance(scanned, list) else None,
    )


def _native_result(raw: object, snapshot_paths: Sequence[str]) -> tuple[ImportedFinding, str] | None:
    if not isinstance(raw, Mapping):
        return None
    extra = raw.get("extra")
    extra = extra if isinstance(extra, Mapping) else {}
    if extra.get("is_ignored") is True:
        return None
    start_block = raw.get("start")
    end_block = raw.get("end")
    start = int(start_block.get("line") or 0) if isinstance(start_block, Mapping) else 0
    end = int(end_block.get("line") or 0) if isinstance(end_block, Mapping) else 0
    rule_id = _bounded(raw.get("check_id"), 200)
    if not rule_id or start <= 0:
        return None
    metadata = extra.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    level = _bounded(extra.get("severity"), 32)
    path, in_snapshot = normalize_location(str(raw.get("path") or ""), snapshot_paths)
    raw_message = str(extra.get("message") or "")
    message = raw_message[:MAX_MESSAGE_CHARS]
    cwe = metadata.get("cwe")
    owasp = metadata.get("owasp")
    tags = [
        _bounded(tag, 64)
        for tag in (
            *(cwe if isinstance(cwe, list) else [cwe] if cwe else []),
            *(owasp if isinstance(owasp, list) else [owasp] if owasp else []),
        )
        if tag
    ][:16]
    end = max(start, end)
    finding = ImportedFinding(
        id=_finding_id(rule_id, path, start, end, message),
        tool=TOOL,
        rule_id=rule_id,
        severity=map_severity(level, str(metadata.get("confidence") or "")),
        level=level.lower(),
        path=path,
        start_line=start,
        end_line=end,
        message=message,
        message_truncated=len(raw_message) > MAX_MESSAGE_CHARS,
        tags=tuple(tags),
        cwe=_cwes((rule_id, *tags)),
        in_snapshot=in_snapshot,
        fingerprint=finding_fingerprint(TOOL, rule_id, path, message, str(extra.get("lines") or "")),
    )
    return finding, _bounded(metadata.get("title"), MAX_TITLE)


def _from_sarif(payload: bytes, snapshot_paths: Sequence[str]) -> ParsedScan:
    """Opengrep's ``--sarif`` output through the Reader's SARIF parser, with the reviewer's severity mapping."""

    imported = parse_sarif(payload, snapshot_paths=snapshot_paths)
    # Opengrep writes a rule's own confidence into its SARIF tags ("LOW CONFIDENCE").
    findings = tuple(
        replace(
            item,
            severity=map_severity(item.level, "LOW" if "LOW CONFIDENCE" in {tag.upper() for tag in item.tags} else ""),
        )
        for item in imported.findings
        if item.start_line > 0
    )
    return ParsedScan(version=imported.tool_version, findings=findings)


def _numbered(found: Sequence[ImportedFinding]) -> tuple[ImportedFinding, ...]:
    """Results with the same fingerprint (the same rule on identical text of a file) told apart by order: ``#2``, ``#3``."""

    seen: dict[str, int] = {}
    out: list[ImportedFinding] = []
    for item in sorted(found, key=lambda row: (row.path, row.start_line, row.rule_id, row.id)):
        count = seen.get(item.fingerprint, 0) + 1
        seen[item.fingerprint] = count
        out.append(item if count == 1 else replace(item, fingerprint=f"{item.fingerprint}#{count}"))
    return tuple(out)


# --------------------------------------------------------------------------- #
# the diff: which lines count
# --------------------------------------------------------------------------- #

Ranges = tuple[tuple[int, int], ...]
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _added_lines(hunk: Mapping[str, Any]) -> Ranges:
    """The new-side lines one hunk added or changed, as inclusive ranges.

    Read from the hunk's own patch text when the capture kept it (exact: an added line is a ``+`` line); else from the
    packet's ``new_ranges``; else the hunk's whole right-side span, which only widens what counts.
    """

    header = _HUNK.match(str(hunk.get("header") or ""))
    patch = hunk.get("patch")
    if header is not None and isinstance(patch, str) and patch:
        number = int(header[1])
        added: list[int] = []
        for line in patch.split("\n"):
            if line.startswith("+"):
                added.append(number)
                number += 1
            elif line.startswith(" ") or line == "":
                number += 1
            # "-" (removed) and "\ No newline" lines have no new-side line
        return _merge_lines(added)
    ranges = hunk.get("new_ranges")
    if isinstance(ranges, list) and ranges:
        return tuple(
            (int(row[0]), int(row[1])) for row in ranges if isinstance(row, list) and len(row) == 2 and int(row[0]) > 0
        )
    if header is not None:
        first, count = int(header[1]), int(header[2]) if header[2] is not None else 1
        return ((first, first + count - 1),) if count > 0 else ()
    return ()


def _merge_lines(lines: Sequence[int]) -> Ranges:
    out: list[tuple[int, int]] = []
    for line in sorted(set(lines)):
        if out and line == out[-1][1] + 1:
            out[-1] = (out[-1][0], line)
        else:
            out.append((line, line))
    return tuple(out)


def changed_lines(files: object) -> dict[str, Ranges]:
    """``{path: new-side changed line ranges}`` for the files of a stored packet that have text to scan.

    A deleted file, a binary, a symlink or a submodule pointer has none. A file with a diff the capture could not
    read (no hunks at all) is left out: nothing can be said about which of its lines changed.
    """

    out: dict[str, Ranges] = {}
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        return out
    for entry in files:
        if not isinstance(entry, Mapping):
            continue
        path = str(entry.get("path") or "")
        if (
            not path
            or entry.get("status") == "deleted"
            or entry.get("is_binary") is True
            or entry.get("submodule_pointer")
            or int(entry.get("new_mode") or 0) == _SYMLINK_MODE
        ):
            continue
        hunks = entry.get("hunks")
        ranges: list[tuple[int, int]] = []
        for hunk in hunks if isinstance(hunks, list) else ():
            if isinstance(hunk, Mapping):
                ranges.extend(_added_lines(hunk))
        if ranges:
            out[path] = tuple(sorted(set(ranges)))
    return out


def place_in_diff(finding: ImportedFinding, ranges: Ranges) -> tuple[int, int] | None:
    """The lines of *finding* that sit on changed lines, as ``(first, last)`` of its first overlap; ``None`` for none.

    A result that spans several lines counts when any of them changed; the comment goes on the changed part of it.
    """

    for low, high in ranges:
        first, last = max(finding.start_line, low), min(finding.end_line, high)
        if first <= last:
            return first, last
    return None


# --------------------------------------------------------------------------- #
# a scan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ScanResult:
    """One result on a changed line, as evidence (``ref``) and as the lines its comment would sit on."""

    finding: ImportedFinding
    ref: str
    line: int
    end_line: int
    #: ``finding_replacement.lines_digest`` of the lines, so a later push can tell they changed.
    digest: str
    title: str

    @property
    def check_id(self) -> str:
        return self.finding.rule_id

    @property
    def tracked_id(self) -> str:
        """The id of the finding this result becomes. Stable while the matched text and the rule are."""

        return "sec-" + hashlib.sha256(self.finding.fingerprint.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class SecurityScan:
    """What the security scan did.

    ``status``: ``off`` (``security-scan: off``), ``skipped`` (no scanner on this machine and nobody asked for one: the
    local CLI), ``unavailable`` (a scanner was expected or was started and could not produce results: the receipt says
    so and why) or ``ran``.
    """

    status: str
    reason: str = ""
    version: str = ""
    rules: int = 0
    files_scanned: int = 0
    files_changed: int = 0
    #: Results the scanner reported, those on changed lines (``results`` holds them), and the rest, which are context.
    total: int = 0
    outside: int = 0
    #: Results on changed lines that were not kept, over :data:`MAX_RESULTS` or an informational severity.
    dropped: int = 0
    errors: int = 0
    results: tuple[ScanResult, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ran(self) -> bool:
        return self.status == "ran"

    @property
    def unavailable(self) -> bool:
        return self.status == "unavailable"

    def by_ref(self) -> dict[str, ScanResult]:
        return {row.ref: row for row in self.results}

    def receipt(self, *, reported: int = 0, confirmed: int = 0) -> str:
        """The line "What we checked" shows. ``reported``/``confirmed`` are the results shown alone and merged into
        an investigator's finding."""

        if self.status in {"off", "skipped"}:
            return ""  # nothing was asked for, so the receipt does not change for a repository that never wanted a scan
        if self.status == "unavailable":
            return f"Not run: security scan unavailable ({self.reason or 'the scanner could not be run'})"
        if self.files_scanned == 0 and not self.version:
            return "Security scan: no changed file with text to scan"
        scanned = f"{self.files_scanned} of {self.files_changed} changed files" if self.files_changed else "no files"
        version = f" {self.version}" if self.version else ""
        parts = [f"{len(self.results)} on changed lines"]
        if self.results:
            parts.append(f"{reported} reported, {confirmed} confirmed by the review")
        if self.outside:
            parts.append(f"{self.outside} elsewhere in those files, not reported")
        if self.dropped:
            parts.append(f"{self.dropped} not kept (informational or over the {MAX_RESULTS}-result bound)")
        text = f"Security scan: {TOOL}{version}, {self.rules} rules, {scanned}; " + "; ".join(parts)
        if self.errors:
            text += f"; {self.errors} file or rule error(s) in the scanner"
        return text

    def to_wire(self) -> dict[str, Any]:
        """The ``--json`` record of the scan: always present, so a consumer can tell "skipped" from "clean"."""

        return {
            "status": self.status,
            "reason": self.reason,
            "tool": TOOL if self.status in {"ran", "unavailable"} else "",
            "version": self.version,
            "rules": self.rules,
            "files_scanned": self.files_scanned,
            "files_changed": self.files_changed,
            "results": self.total,
            "on_changed_lines": len(self.results),
            "elsewhere": self.outside,
            "not_kept": self.dropped,
            "errors": self.errors,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stderr: str = ""


#: ``runner(args, cwd, env, timeout)``; raises ``TimeoutError`` when the process has to be killed, ``OSError`` when it
#: cannot start. Tests pass a fake that writes the ``--json-output`` file.
Runner = Callable[[Sequence[str], Path, Mapping[str, str], float], ProcessResult]


def run_process(args: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float) -> ProcessResult:
    """Run the scanner in its own process group so a timeout kills the engine's workers too."""

    process = subprocess.Popen(
        list(args),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            process.kill()
        process.communicate()
        raise TimeoutError(f"timed out after {int(timeout)}s") from exc
    return ProcessResult(process.returncode, (err or b"")[-4000:].decode("utf-8", errors="replace"))


def scanner_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment the scanner runs in: a short allow-list, so no token or key can reach it."""

    return {key: value for key, value in environ.items() if key in _ENV_ALLOW}


def resolve_binary(environ: Mapping[str, str]) -> tuple[str, str]:
    """``(path, "")`` of the scanner to run, or ``("", why)``."""

    named = str(environ.get(BIN_ENV) or "").strip()
    if named:
        if os.path.isfile(named) and os.access(named, os.X_OK):
            return named, ""
        return "", f"{named} is not an executable file"
    found = shutil.which("opengrep", path=environ.get("PATH"))
    return (found, "") if found else ("", "opengrep is not installed")


def _timeout(environ: Mapping[str, str]) -> int:
    try:
        value = int(str(environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_S))
    except ValueError:
        value = DEFAULT_TIMEOUT_S
    return max(TIMEOUT_RANGE[0], min(TIMEOUT_RANGE[1], value))


def _decodable(text: str) -> bool:
    return "\0" not in text


def scan_pull_request(
    captured: Captured,
    *,
    environ: Mapping[str, str],
    say: Callable[[str], None] = lambda _message: None,
    runner: Runner = run_process,
) -> SecurityScan:
    """Scan the pull request's changed lines and record the results as ``static_finding`` evidence in its Snapshot.

    Never raises: whatever goes wrong becomes ``status="unavailable"`` with a short reason, and the review goes on.
    ``ValueError`` is raised only for an invalid ``security-scan`` value, which is the caller's input to fix.
    """

    mode = scan_mode(environ)
    if mode == "off":
        return SecurityScan("off")
    if captured.pull is None:
        # A local range has no merge base to read the trusted settings and rules from: only a pull request is scanned.
        return SecurityScan("skipped", reason="not a pull request")
    binary, missing = resolve_binary(environ)
    if not binary:
        # In Actions the install step was expected to provide one: say so. On a laptop nobody asked: stay silent.
        if str(environ.get("GITHUB_ACTIONS") or "") == "true":
            why = str(environ.get(NOTE_ENV) or "").strip() or missing
            return SecurityScan("unavailable", reason=_bounded(why, 200))
        return SecurityScan("skipped", reason=missing)
    say("Running the security scan")
    try:
        with tempfile.TemporaryDirectory(prefix="lemoncrow-security-") as scratch:
            return _scan(captured, binary, Path(scratch), environ, runner)
    except Exception as exc:
        return SecurityScan("unavailable", reason=_bounded(f"{type(exc).__name__}: {exc}", 200))


def _scan(captured: Captured, binary: str, scratch: Path, environ: Mapping[str, str], runner: Runner) -> SecurityScan:
    from lemoncrow.pro.capabilities.review.ask import exact_revision_sources
    from lemoncrow.pro.capabilities.review.finding_replacement import lines_digest
    from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

    store, revision = captured.store, captured.revision
    packet = read_packet_json(store, revision)
    if packet is None:
        return SecurityScan("unavailable", reason="the change could not be read back from the Snapshot")
    ranges = changed_lines(packet.get("files"))
    config = _base_config(captured)
    wanted = sorted(
        path for path in ranges if _safe_relative(path) and not _SKIP.search(path) and not _ignored(config, path)
    )
    files_changed = len(wanted)
    sources = exact_revision_sources(store, revision, paths=wanted[:MAX_FILES], side="new")
    root = scratch / "files"
    root.mkdir()
    staged: dict[str, str] = {}
    total_bytes = 0
    for path in wanted[:MAX_FILES]:
        text = sources.get(path)
        if text is None or not _decodable(text):
            continue
        size = len(text.encode("utf-8"))
        if size > MAX_FILE_BYTES or total_bytes + size > MAX_TOTAL_BYTES:
            continue
        total_bytes += size
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")
        staged[path] = text
    notes: list[str] = []
    if len(wanted) > MAX_FILES:
        notes.append(f"scanned the first {MAX_FILES} of {len(wanted)} changed files")
    if not staged:
        return SecurityScan("ran", files_changed=files_changed, notes=tuple(notes))

    rule_files, rule_notes = _stage_rules(captured, config, scratch / "rules")
    notes.extend((*config.notes, *rule_notes))
    if not rule_files:
        return SecurityScan("unavailable", reason="no security rules to run", notes=tuple(notes))
    out = scratch / "result.json"
    args = [
        binary,
        "scan",
        *(part for rules in rule_files for part in ("--config", str(rules))),
        "--json",
        f"--json-output={out}",
        "--quiet",
        "--disable-version-check",
        "--disable-nosem",
        "--no-rewrite-rule-ids",
        "--timeout=5",
        "--timeout-threshold=3",
        f"--max-target-bytes={MAX_FILE_BYTES}",
        "--jobs=2",
        ".",
    ]
    limit = _timeout(environ)
    try:
        done = runner(args, root, scanner_env(environ), float(limit))
    except TimeoutError:
        return SecurityScan("unavailable", reason=f"Opengrep timed out after {limit}s", notes=tuple(notes))
    except OSError as exc:
        return SecurityScan("unavailable", reason=_bounded(f"Opengrep could not start: {exc}", 200), notes=tuple(notes))
    try:
        payload = out.read_bytes()
    except OSError:
        payload = b""
    if done.returncode != 0:
        # A rejected rule file exits non-zero but still writes a document: with no results and the reason in `errors`.
        # That is not a clean scan, so it is never read as one.
        why = f"Opengrep exited with code {done.returncode}"
        detail = _failure_detail(payload) or next((line for line in done.stderr.splitlines() if line.strip()), "")
        return SecurityScan(
            "unavailable", reason=why + (f": {_bounded(detail, 140)}" if detail else ""), notes=tuple(notes)
        )
    try:
        parsed = parse_opengrep(payload, snapshot_paths=tuple(staged))
    except ValueError as exc:
        return SecurityScan(
            "unavailable", reason=_bounded(f"Opengrep output unreadable: {exc}", 200), notes=tuple(notes)
        )

    on_lines: list[tuple[ImportedFinding, int, int]] = []
    outside = 0
    for item in parsed.findings:
        spot = place_in_diff(item, ranges.get(item.path, ())) if item.path in staged else None
        if spot is None:
            outside += 1
        else:
            on_lines.append((item, *spot))
    # Most severe first, then by place; informational results are context, never findings.
    on_lines.sort(key=lambda row: (-SEVERITY_RANK[row[0].severity], row[0].path, row[1], row[0].rule_id))
    kept = [row for row in on_lines if row[0].severity != "suggestion"][:MAX_RESULTS]
    rows: list[ScanResult] = []
    for hit, begin, finish in kept:
        lines = staged[hit.path].splitlines()[begin - 1 : finish]
        rows.append(
            ScanResult(
                finding=hit,
                ref="",
                line=begin,
                end_line=finish,
                digest=lines_digest(lines) if lines else "",
                title=parsed.titles.get(hit.id, ""),
            )
        )
    rows = _record(store, revision, rows, parsed.version)
    return SecurityScan(
        "ran",
        version=parsed.version,
        rules=_count_rules(rule_files),
        files_scanned=len(staged) if parsed.scanned is None else min(parsed.scanned, len(staged)),
        files_changed=files_changed,
        total=len(parsed.findings),
        outside=outside,
        dropped=len(on_lines) - len(rows),
        errors=parsed.errors,
        results=tuple(rows),
        notes=tuple(notes),
    )


def _failure_detail(payload: bytes) -> str:
    """The first error the engine wrote into its output document, if it wrote one."""

    try:
        document = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return ""
    errors = document.get("errors") if isinstance(document, Mapping) else None
    for row in errors if isinstance(errors, list) else ():
        if isinstance(row, Mapping):
            text = row.get("long_msg") or row.get("message") or row.get("short_msg")
            if text:
                return str(text)
    return ""


def _merge_base(captured: Captured) -> str:
    """The pull request's merge base, where the trusted settings and rules are read from; ``""`` for a local range."""

    return captured.pull.merge_base_sha if captured.pull is not None else ""


def _base_config(captured: Captured) -> SecurityConfig:
    text = read_base_file(captured.repo_root, _merge_base(captured), ".lemoncrow/review.yaml", limit=1_000_000)
    config = parse_security_block(text)
    ignore = parse_file_settings(text or "").ignore
    return replace(config, ignore=ignore) if ignore else config


def _ignored(config: SecurityConfig, path: str) -> bool:
    try:
        return bool(config.ignore) and path_matches(path, config.ignore)
    except ValueError:
        return False  # an odd path is never a reason to leave a result out


def _stage_rules(captured: Captured, config: SecurityConfig, target: Path) -> tuple[list[Path], list[str]]:
    """Copy the rules to run into *target*: the vendored set, then the trusted base's. Returns ``(paths, notes)``."""

    notes: list[str] = []
    paths: list[Path] = []
    target.mkdir()
    if config.default_rules:
        default = target / "default"
        shutil.copytree(RULES_DIR, default, ignore=shutil.ignore_patterns("*.md"))
        paths.append(default)
    repo = target / "repo"
    staged = 0
    total = 0
    for entry in config.rules:
        names = _base_rule_paths(captured.repo_root, _merge_base(captured), entry)
        if not names:
            notes.append(f"security.rules: {entry} was not found on the base branch")
        for name in names:
            if staged >= MAX_RULE_FILES:
                notes.append(f"security.rules: only the first {MAX_RULE_FILES} rule files are read")
                break
            text = read_base_file(captured.repo_root, _merge_base(captured), name)
            if text is None:
                notes.append(f"security.rules: {name} is missing or over {MAX_RULE_BYTES // 1000} KB")
                continue
            total += len(text)
            staged += 1
            repo.mkdir(exist_ok=True)
            (repo / f"{staged:02d}-{hashlib.sha256(name.encode()).hexdigest()[:8]}.yaml").write_text(
                text, encoding="utf-8"
            )
    if staged:
        paths.append(repo)
    return paths, notes


def _count_rules(paths: Sequence[Path]) -> int:
    count = 0
    for root in paths:
        for file in sorted(root.rglob("*")):
            if file.suffix not in _RULE_SUFFIXES:
                continue
            try:
                loaded = yaml.safe_load(file.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            rules = loaded.get("rules") if isinstance(loaded, Mapping) else None
            count += len(rules) if isinstance(rules, list) else 0
    return count


def _record(store: Any, revision: Any, rows: Sequence[ScanResult], version: str) -> list[ScanResult]:
    """Ingest the results as one ``sast`` check row per file, bound to that file; return them with their refs.

    A row bound to its file means the evidence refs an investigator may cite reach that file's targets only. It is
    recorded as a completed scan (``PASS``): its findings are the evidence, and a ``FAIL`` would also raise a
    ``verification`` candidate that investigates the scan itself rather than the code.
    """

    by_path: dict[str, list[ScanResult]] = {}
    for row in rows:
        by_path.setdefault(row.finding.path, []).append(row)
    out: list[ScanResult] = []
    authority = IngestAuthority(attestation_class="", source="external")
    for path, group in sorted(by_path.items()):
        findings = tuple(row.finding for row in group)
        evidence, _created = record_check_ingest(
            store,
            revision,
            CheckIngest(
                tool=TOOL,
                tool_version=version,
                category="sast",
                status="PASS",
                status_basis="the scan completed; its results are the static findings of this row",
                head_sha=revision.head_sha,
                title=f"{TOOL}: {path}",
                stable_key=f"opengrep:{path}",
                findings=findings,
                truncation=Truncation(total=len(findings), kept=len(findings)),
            ),
            authority,
            path=path,
        )
        out.extend(replace(row, ref=finding_ref(evidence.id, row.finding.id)) for row in group)
    out.sort(key=lambda row: (-SEVERITY_RANK[row.finding.severity], row.finding.path, row.line, row.finding.rule_id))
    return out


# --------------------------------------------------------------------------- #
# findings: what the investigators cited, what is left, what became of earlier ones
# --------------------------------------------------------------------------- #


def cited_results(findings: Sequence[Any], scan: SecurityScan) -> dict[str, tuple[ScanResult, ...]]:
    """``{confirmed finding id: the scan results its evidence refs cited}`` for the findings that cited any."""

    known = scan.by_ref()
    out: dict[str, tuple[ScanResult, ...]] = {}
    for item in findings:
        hits = tuple(known[ref] for ref in getattr(item, "evidence_refs", ()) if ref in known)
        if hits:
            out[item.id] = hits
    return out


def static_checks(cited: Mapping[str, Sequence[ScanResult]]) -> dict[str, str]:
    """``{confirmed finding id: "rule-a, rule-b"}``: the trailer text for findings that cited scanner evidence."""

    return {key: ", ".join(dict.fromkeys(row.check_id for row in rows))[:200] for key, rows in cited.items()}


def shown_counts(scan: SecurityScan, cited: Mapping[str, Sequence[ScanResult]]) -> dict[str, int]:
    """``reported`` (results shown as findings of their own) and ``confirmed`` (merged into an investigator's finding)."""

    confirmed_ids = {row.tracked_id for rows in cited.values() for row in rows}
    confirmed = sum(1 for row in scan.results if row.tracked_id in confirmed_ids)
    return {"reported": len(scan.results) - confirmed, "confirmed": confirmed}


def without_scan_findings(previous: ReviewState | None) -> ReviewState | None:
    """*previous* minus the raw scanner findings, which :func:`merge` owns; everything else is the AI path's."""

    if previous is None or not any(item.scan for item in previous.findings):
        return previous
    return replace(previous, findings=tuple(item for item in previous.findings if not item.scan))


def _targets_at(
    path: str, line: int, locations: Mapping[str, Located], spans: Mapping[str, Sequence[Any]]
) -> tuple[str, ...]:
    """Targets of *path* whose new-side changed spans hold *line* (else every target of the file)."""

    ofpath = [target for target, where in locations.items() if where.path == path]
    holding = [
        target
        for target in ofpath
        if any(side == "new" and first <= line <= last for side, first, last in spans.get(target, ()))
    ]
    return tuple((holding or ofpath)[:16])


def raw_finding(
    row: ScanResult,
    *,
    head: str,
    locations: Mapping[str, Located],
    spans: Mapping[str, Sequence[Any]],
) -> BotFinding:
    """A scanner result nobody confirmed as a tracked finding: ``security``, P1 for an error, P2 for a warning."""

    finding = row.finding
    targets = _targets_at(finding.path, row.line, locations, spans)
    sentence = _SENTENCE.match(finding.message)
    title = row.title or (sentence[1] if sentence else finding.message)
    return BotFinding(
        id=row.tracked_id,
        fingerprint="fp-" + hashlib.sha256(("static\0" + finding.fingerprint).encode("utf-8")).hexdigest()[:16],
        severity=finding.severity if finding.severity in {"p1", "p2"} else "p2",
        category="security",
        state="new",
        path=finding.path,
        line=row.line,
        end_line=row.end_line,
        side="new",
        target_ids=targets,
        title=trimmed(title, MAX_TITLE),
        explanation=trimmed(finding.message, MAX_EXPLANATION),
        first_seen=head,
        last_seen=head,
        anchor="cited",
        target_line=row.line,
        anchor_digest=row.digest,
        static_check=finding.rule_id,
        scan=True,
    )


#: Earlier raw findings still alive between reviews: open, or closed as "no longer reported" with their thread left open.
_LIVE: Final[frozenset[str]] = frozenset({"new", "carried", "unreported"})


def _on_thread(old: BotFinding) -> dict[str, Any]:
    """The fields a finding keeps from the thread an earlier one already has: where the thread is, and that it exists."""

    return {
        "first_seen": old.first_seen,
        "thread_id": old.thread_id,
        "thread_url": old.thread_url,
        "placement": old.placement,
        **(
            {"path": old.path, "line": old.line, "end_line": old.end_line, "side": old.side, "anchor": old.anchor}
            if old.thread_id
            else {}
        ),
    }


def merge(
    previous: ReviewState | None,
    reconciled: Sequence[BotFinding],
    scan: SecurityScan,
    *,
    cited: Mapping[str, Sequence[ScanResult]],
    head: str,
    current_paths: Collection[str],
    locations: Mapping[str, Located],
    spans: Mapping[str, Sequence[Any]],
    intact: Collection[str] = (),
) -> list[BotFinding]:
    """The review's findings with the scanner's results folded in.

    *reconciled* is what the AI path decided (``reconcile.reconcile``, run without the earlier raw findings).

    * A result an investigator's finding cited is that finding's evidence and appears nowhere else. When it already
      had a thread as a raw finding, the investigator's finding takes the thread over (same id, same place).
    * Any other result is a raw ``security`` finding. One seen before is carried on its thread; a new one is new.
    * An earlier raw finding the scan no longer reports is ``fixed`` when its lines changed, ``unreported`` ("not
      confirmed fixed") when they are still there word for word, and ``stale`` when its file left the change. When the
      scan did not run it is carried, unverified: absence proves nothing.

    lc-debt: a raw finding whose result an investigator's *carried* finding now cites (not a new one) is dropped
    without a reply, so its thread stays open; upgrade: reply "now tracked as ..." on it.
    """

    earlier = {item.id: item for item in (previous.findings if previous is not None else ()) if item.scan}
    cited_ids = {row.tracked_id for rows in cited.values() for row in rows}
    kept_intact = set(intact)
    out = list(reconciled)
    taken: set[str] = set()

    for index, item in enumerate(out):
        if item.state != "new":
            continue
        for row in cited.get(item.id, ()):
            old = earlier.get(row.tracked_id)
            if old is not None and old.state in _LIVE and old.id not in taken:
                taken.add(old.id)
                out[index] = item.changed(state="carried", id=old.id, **_on_thread(old))
                break

    if scan.ran:
        for row in scan.results:
            if row.tracked_id in cited_ids:
                continue
            fresh = raw_finding(row, head=head, locations=locations, spans=spans)
            old = earlier.get(row.tracked_id)
            if old is not None and old.state in _LIVE and old.id not in taken:
                taken.add(old.id)
                fresh = fresh.changed(state="carried", **_on_thread(old))
            out.append(fresh)

    for old in earlier.values():
        if old.id in taken or old.state not in _LIVE or old.id in cited_ids:
            continue  # closed ones are not carried between reviews, like every other finding
        if old.path not in current_paths:
            if old.state != "unreported":  # an unreported one whose code left the change has nothing more to say
                out.append(old.changed(state="stale", resolved_in=head, last_seen=head))
        elif old.state == "unreported":
            out.append(old)
        elif not scan.ran:
            out.append(old.changed(state="carried", last_seen=head, unverified=True))
        elif old.id in kept_intact or not old.anchor_digest:
            out.append(old.changed(state="unreported", resolved_in=head, last_seen=head))
        else:
            out.append(old.changed(state="fixed", resolved_in=head, last_seen=head))
    return out
