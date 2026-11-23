"""Shared model of the artifact and infrastructure analyses (Phase 9R items 15, 31, 32, 33).

Four run-plan entries execute sandboxed analyses defined by the trusted base:

* ``migrations.execute`` -- migrations up/down against a schema fixture
  (``migration_execution.py``);
* ``artifacts.regenerate`` -- regenerate and compare generated files
  (``regenerate_compare.py``);
* ``artifacts.shipped`` -- diff shipped artifacts (``shipped_artifacts.py``);
* ``infra.render`` -- helm/kustomize render and credential-free terraform plan
  (``infra_render.py``).

Each analysis is one :class:`AnalysisSpec` (configured under ``analyses:`` in
the trusted-base ``.lemoncrow/review.yaml`` or discovered from the trusted base)
and one stdlib-only Python driver that runs inside the R4 hermetic runner as
``python3 -c <driver> <payload>``. The driver never trusts its environment:
every tool it starts gets an allowlisted environment (no provider or registry
credentials), works on a ``/tmp`` copy (the Snapshot stays untouched), and
writes one JSON report, hex-encoded so the runner's line-oriented redaction can
never corrupt its structure; the host decodes it and redacts every string
before anything is stored.
"""

from __future__ import annotations

import ast
import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Final, Literal

MIGRATIONS_ENTRY: Final[str] = "migrations.execute"
REGENERATE_ENTRY: Final[str] = "artifacts.regenerate"
SHIPPED_ENTRY: Final[str] = "artifacts.shipped"
INFRA_ENTRY: Final[str] = "infra.render"
#: Phase 9R item 41 (``compat_mixed_version.py``) and item 44 (``flag_matrix.py``).
COMPAT_ENTRY: Final[str] = "compat.mixed_version"
FLAG_MATRIX_ENTRY: Final[str] = "behavior.flag_matrix"
ANALYSIS_ENTRIES: Final[tuple[str, ...]] = (
    MIGRATIONS_ENTRY,
    REGENERATE_ENTRY,
    SHIPPED_ENTRY,
    INFRA_ENTRY,
    COMPAT_ENTRY,
    FLAG_MATRIX_ENTRY,
)
ANALYSIS_EXECUTOR: Final[str] = "artifact_analyses"
ANALYSIS_MEDIA_TYPE: Final[str] = "application/vnd.lemoncrow.artifact-analysis+json;version=1"
ANALYSIS_PRODUCER_ID: Final[str] = "lemoncrow.artifact_analyses"
ANALYSIS_PROVIDER: Final[str] = "analysis"
#: Detection fact each entry's applicability requires (``capability_catalog``).
ENTRY_FACT: Final[Mapping[str, str]] = {
    MIGRATIONS_ENTRY: "migrations",
    REGENERATE_ENTRY: "generators",
    SHIPPED_ENTRY: "package_artifact",
    INFRA_ENTRY: "infrastructure",
    COMPAT_ENTRY: "compat_surface",
    FLAG_MATRIX_ENTRY: "flag_overrides",
}
#: ``analyses.<key>`` in the trusted-base review config.
CONFIG_KEYS: Final[Mapping[str, str]] = {
    MIGRATIONS_ENTRY: "migrations",
    REGENERATE_ENTRY: "regenerate",
    SHIPPED_ENTRY: "shipped",
    INFRA_ENTRY: "infra",
    COMPAT_ENTRY: "compatibility",
    FLAG_MATRIX_ENTRY: "flags",
}
#: Behavior kind of each entry's deltas (R2 contract).
ENTRY_KIND: Final[Mapping[str, str]] = {
    MIGRATIONS_ENTRY: "migration",
    REGENERATE_ENTRY: "artifact",
    SHIPPED_ENTRY: "artifact",
    INFRA_ENTRY: "infra",
    COMPAT_ENTRY: "compatibility",
    FLAG_MATRIX_ENTRY: "test_outcome",
}

# Driver exit codes (the report's ``exit`` repeats it).
EXIT_OK: Final[int] = 0
EXIT_FINDINGS: Final[int] = 1
EXIT_UNAVAILABLE: Final[int] = 2
EXIT_DRIVER_ERROR: Final[int] = 3
EXIT_TOOL_FAILED: Final[int] = 4
#: The exits a sealed stage A reports (findings come from stage B on the host).
_SEALED_EXITS: Final[frozenset[int]] = frozenset({EXIT_OK, EXIT_UNAVAILABLE, EXIT_DRIVER_ERROR, EXIT_TOOL_FAILED})

#: The hosted worker accepts at most 32,768 command characters in total.
# lc-debt: a driver's inputs travel inside the executor command, so each driver has a derived ceiling
# (payload_headroom: about 3.8k characters for the shipped-artifact driver). An analysis past it is refused at
# plan time with the typed "Command too large" reason. Upgrade: carry large inputs as an overlay file, as the
# compatibility statements already do, and keep only identities in the command.
MAX_COMMAND_CHARS: Final[int] = 30_000
#: JSON characters of one report; hex doubles it, under the worker's 384 KiB report cap.
REPORT_LIMIT: Final[int] = 170_000
#: The local runner reads reports up to 4 MiB (``runtimes._MAX_REPORT_BYTES``): hex of 1.9M characters fits.
LOCAL_REPORT_LIMIT: Final[int] = 1_900_000
#: Raw bytes a sealed driver may copy out of a report of *limit* JSON characters (base64 + metadata).
COPY_OUT_OVERHEAD: Final[int] = 40_000
DEFAULT_TIMEOUT: Final[int] = 300
MAX_TIMEOUT: Final[int] = 900
MAX_SPECS_PER_ENTRY: Final[int] = 16

Verdict = Literal["PASS", "FAIL", "UNKNOWN", "NOT_RUN"]
Side = Literal["old", "new"]
Severity = Literal["low", "medium", "high"]
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def sha256_hex(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def text_sha16(text: str) -> str:
    """The 16-hex content key drivers compute for files (``sha(path)[:16]``)."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def valid_spec_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def relative_path(value: object, *, allow_root: bool = True) -> str | None:
    """A normalized repo-relative path, or ``None`` for anything that could escape or read as a flag."""

    text = str(value if value is not None else ".").strip() or "."
    if text.startswith("-") or "\\" in text or "\0" in text or "\n" in text:
        return None
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        return None
    normalized = path.as_posix().strip("/")
    if normalized in {"", "."}:
        return "." if allow_root else None
    if any(part.startswith("-") for part in PurePosixPath(normalized).parts):
        return None
    return normalized


def join_path(root: str, rel: str) -> str:
    return rel if root in {"", "."} else f"{root.rstrip('/')}/{rel}" if rel not in {"", "."} else root


def glob_regex(pattern: str) -> re.Pattern[str]:
    """``**``-aware glob over ``/`` paths (the drivers use the same translation)."""

    out, index = "", 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out, index = out + "(?:.*/)?", index + 3
        elif pattern.startswith("**", index):
            out, index = out + ".*", index + 2
        elif pattern[index] == "*":
            out, index = out + "[^/]*", index + 1
        elif pattern[index] == "?":
            out, index = out + "[^/]", index + 1
        else:
            out, index = out + re.escape(pattern[index]), index + 1
    return re.compile(out + "$")


@dataclass(frozen=True, slots=True)
class AnalysisSpec:
    """One trusted-base analysis of one entry (a migration directory, a package, a chart ...).

    ``options`` are validated, JSON-safe, tool-specific values; a head-side
    edit of the review config never reaches them. ``executable`` is false for
    a framework detected in the trusted base that this build cannot run
    (``reason``/``setup_hint`` say why and how to enable it).
    """

    entry_id: str
    id: str
    tool: str
    root: str = "."
    title: str = ""
    origin: Literal["configured", "discovered"] = "discovered"
    source: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)
    executable: bool = True
    reason: str = ""
    setup_hint: str = ""
    timeout: int = DEFAULT_TIMEOUT
    #: Trusted-base files the analysis takes from the base side (generators, fixtures).
    trusted_inputs: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "id": self.id,
            "tool": self.tool,
            "root": self.root,
            "options": dict(self.options),
            "executable": self.executable,
            "timeout": self.timeout,
            "trusted_inputs": list(self.trusted_inputs),
        }

    @property
    def digest(self) -> str:
        return "analysis:sha256:" + sha256_hex(self.document())

    def to_wire(self) -> dict[str, Any]:
        return {
            **self.document(),
            "title": self.title or self.id,
            "origin": self.origin,
            "source": self.source,
            "reason": self.reason,
            "setup_hint": self.setup_hint,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class AnalysisCatalog:
    """Every analysis the trusted base defines for one Snapshot."""

    specs: tuple[AnalysisSpec, ...] = ()
    errors: tuple[str, ...] = ()
    base_state: Literal["present", "absent", "provisional"] = "present"
    configured_entries: frozenset[str] = frozenset()

    def for_entry(self, entry_id: str) -> tuple[AnalysisSpec, ...]:
        return tuple(spec for spec in self.specs if spec.entry_id == entry_id)

    def get(self, entry_id: str, spec_id: str) -> AnalysisSpec | None:
        return next((spec for spec in self.for_entry(entry_id) if spec.id == spec_id), None)

    def facts(self) -> dict[str, str]:
        """Detection fact -> why, for each entry that has something to act on.

        A spec whose command can never fit the executor (``Command too large``, decided at plan time) does
        not count: an entry with only such specs plans ``not_available`` with that reason
        (:meth:`unavailable`), never ``run`` with budget reserved for work that cannot start. Other
        non-executable specs (a framework this build cannot execute) still count: their run reports
        ``NOT_RUN`` with how to set them up.
        """

        out: dict[str, str] = {}
        for spec in self.specs:
            if spec.executable or not spec.reason.startswith(COMMAND_TOO_LARGE):
                fact = ENTRY_FACT[spec.entry_id]
                out.setdefault(fact, f"{spec.entry_id} {spec.id} ({spec.tool}, {spec.source or spec.origin})")
        return out

    def unavailable(self) -> dict[str, str]:
        """Detection fact -> the typed reason, for each entry whose every spec is ``Command too large``."""

        runnable = self.facts()
        out: dict[str, str] = {}
        for spec in self.specs:
            fact = ENTRY_FACT[spec.entry_id]
            if fact in runnable or fact in out:
                continue
            # The typed reason leads (``Command too large: ...``), then which spec and how to fix it.
            reason = (spec.reason or "not executable in this build").rstrip(".")
            hint = f" {spec.setup_hint}" if spec.setup_hint else ""
            out[fact] = f"{reason} ({spec.entry_id} {spec.id}, {spec.source or spec.origin}).{hint}"
        return out

    def to_wire(self) -> dict[str, Any]:
        return {
            "specs": [spec.to_wire() for spec in self.specs],
            "errors": list(self.errors),
            "base_state": self.base_state,
        }


@dataclass(frozen=True, slots=True)
class Observation:
    """One side of a finding, as the behavior contract shows it."""

    status: str = ""
    summary: str = ""
    digest: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"status": self.status, "summary": self.summary, "digest": self.digest}


@dataclass(frozen=True, slots=True)
class Finding:
    """One observed difference worth a reviewer's attention (becomes one behavior delta)."""

    rule: str
    category: str
    subject: str
    title: str
    summary: str
    severity: Severity = "medium"
    critical: bool = False
    change: Literal["added", "changed", "removed"] = "changed"
    paths: tuple[str, ...] = ()
    facts: tuple[tuple[str, str], ...] = ()
    base: Observation | None = None
    head: Observation | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "category": self.category,
            "subject": self.subject,
            "title": self.title,
            "summary": self.summary,
            "severity": self.severity,
            "critical": self.critical,
            "change": self.change,
            "paths": list(self.paths),
            "facts": [list(item) for item in self.facts],
            "base": None if self.base is None else self.base.to_wire(),
            "head": None if self.head is None else self.head.to_wire(),
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> Finding:
        def observation(value: object) -> Observation | None:
            if not isinstance(value, Mapping):
                return None
            return Observation(
                str(value.get("status") or ""), str(value.get("summary") or ""), str(value.get("digest") or "")
            )

        severity = str(raw.get("severity") or "medium")
        change = str(raw.get("change") or "changed")
        return cls(
            rule=str(raw.get("rule") or ""),
            category=str(raw.get("category") or ""),
            subject=str(raw.get("subject") or ""),
            title=str(raw.get("title") or ""),
            summary=str(raw.get("summary") or ""),
            severity=severity if severity in {"low", "medium", "high"} else "medium",  # type: ignore[arg-type]
            critical=raw.get("critical") is True,
            change=change if change in {"added", "changed", "removed"} else "changed",  # type: ignore[arg-type]
            paths=tuple(str(item) for item in raw.get("paths") or () if isinstance(item, str))[:20],
            facts=tuple(
                (str(item[0]), str(item[1]))
                for item in raw.get("facts") or ()
                if isinstance(item, Sequence) and not isinstance(item, str) and len(item) == 2
            )[:12],
            base=observation(raw.get("base")),
            head=observation(raw.get("head")),
        )


@dataclass(slots=True)
class SideRun:
    """One side's attempt of one spec, as the host parsed it (fresh or reused)."""

    side: Side
    state: Literal["ok", "findings", "unavailable", "failed", "error", "not_run"]
    reason: str = ""
    report: Mapping[str, Any] = field(default_factory=dict)
    exit_code: int | None = None
    evidence_id: str = ""
    evidence_ref: str = ""
    duration_ms: int = 0
    reused: bool = False
    environment: str = ""
    executor_class: str = ""
    #: Decoded, redacted heavy text of a fresh run (name -> text); empty when reused.
    heavy: dict[str, str] = field(default_factory=dict)

    @property
    def ran(self) -> bool:
        return self.state in {"ok", "findings", "failed"}

    def to_wire(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "state": self.state,
            "reason": self.reason,
            "exit_code": self.exit_code,
            "evidence_id": self.evidence_id,
            "evidence_ref": self.evidence_ref,
            "duration_ms": self.duration_ms,
            "reused": self.reused,
            "environment": self.environment,
            "executor_class": self.executor_class,
        }


@dataclass(slots=True)
class Evaluation:
    """What one spec's runs show: a verdict, findings, a bounded summary and heavy text to retain."""

    status: Verdict
    reasons: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    #: Name -> (redacted) text stored as raw run artifacts with expiry.
    heavy: dict[str, str] = field(default_factory=dict)
    setup_hint: str = ""


def side_state(exit_code: int | None, report: Mapping[str, Any] | None) -> str:
    if report is None:
        return "error"
    code = report.get("exit", exit_code)
    return {
        EXIT_OK: "ok",
        EXIT_FINDINGS: "findings",
        EXIT_UNAVAILABLE: "unavailable",
        EXIT_TOOL_FAILED: "failed",
    }.get(code if isinstance(code, int) else -1, "error")


def decode_report(text: str) -> tuple[dict[str, Any] | None, str]:
    """The driver report (hex of UTF-8 JSON); ``(None, why)`` when it is missing or altered."""

    raw = (text or "").strip()
    if not raw:
        return None, "the analysis driver wrote no report"
    try:
        decoded = json.loads(bytes.fromhex(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, ("the analysis report is unreadable (truncated, or altered by a configured redaction pattern)")
    if not isinstance(decoded, dict):
        return None, "the analysis report is not an object"
    return decoded, ""


def sealed_problem(report: Mapping[str, Any], exit_code: int | None) -> str:
    """Why a sealed (two-stage) driver's report is not accepted, or ``""``.

    The driver only exits through ``finish``, which replaces whatever is at the
    report path with its own report and then exits with its ``exit`` (or with
    120, which no report may claim, when it cannot): a process that was killed
    or crashed after reviewed code wrote a report of its own fails this check.
    Stage A never reports findings (stage B derives them), so ``exit`` 1 is
    never accepted either.
    """

    code = report.get("exit")
    if not isinstance(code, int) or isinstance(code, bool) or code != exit_code:
        return (
            f"the analysis process exited {exit_code} but its report claims {code}; "
            "the report is not trusted (reviewed code may have written it)"
        )
    if code not in _SEALED_EXITS:
        return (
            f"a sealed build never reports exit {code}; the report is not trusted (reviewed code may have written it)"
        )
    seal = report.get("seal")
    if code in {EXIT_OK, EXIT_TOOL_FAILED} and not (isinstance(seal, Mapping) and seal.get("started") is True):
        return "the report does not come from a sealed build; it is not trusted"
    return ""


def sealed_inspection_problem(report: Mapping[str, Any]) -> str:
    """Why stage-B results the sealed driver derived inside the sandbox are not trusted, or ``""``.

    The driver inspects in the sandbox only after the kill sweep left no process
    of the build alive and its trees kept their identity (else it exits 3). Any
    interference the seal recorded voids the result: UNKNOWN, never PASS.
    """

    seal = report.get("seal")
    if not isinstance(seal, Mapping) or seal.get("started") is not True or not isinstance(seal.get("tampered"), list):
        return "the in-sandbox inspection does not come from a sealed build; it is not trusted"
    tampered = seal_tampered(report)
    if tampered:
        return (
            f"the build interfered with the analysis ({tampered}); its in-sandbox inspection is not trusted, "
            "so this result cannot pass"
        )
    return ""


def inspection_sides(head: SideRun, base: SideRun | None) -> dict[str, str]:
    """Where each side's stage B ran: ``host`` (copied-out bytes) or ``sealed_in_sandbox``."""

    return {
        name: str(run.report["inspection"])
        for name, run in (("head", head), ("base", base))
        if run is not None and run.report.get("inspection")
    }


def seal_tampered(report: Mapping[str, Any]) -> str:
    """The sealed build's tamper reasons (signals to the driver, processes outliving a step)."""

    seal = report.get("seal")
    reasons = seal.get("tampered") if isinstance(seal, Mapping) else None
    if not isinstance(reasons, list):
        return ""
    return "; ".join(str(item)[:300] for item in reasons[:4])


def decode_outputs(report: Mapping[str, Any], *, limit: int = 2000) -> tuple[dict[str, bytes], str]:
    """The files a sealed build copied out (path -> bytes), each checked against its size and digest."""

    rows = report.get("outputs")
    if not isinstance(rows, list):
        return {}, "the sealed build copied out no file list"
    out: dict[str, bytes] = {}
    for row in rows[:limit]:
        if not isinstance(row, Mapping):
            return {}, "the sealed build copied out a malformed row"
        path = str(row.get("path") or "")
        if relative_path(path, allow_root=False) != path or path in out:
            return {}, f"the sealed build copied out an unsafe path {path[:200]!r}"
        try:
            data = base64.b64decode(str(row.get("b64") or ""), validate=True)
        except (ValueError, binascii.Error):
            return {}, f"the copied-out file {path} is not valid base64"
        if len(data) != row.get("size") or hashlib.sha256(data).hexdigest() != row.get("sha256"):
            return {}, f"the copied-out file {path} does not match its size and digest"
        out[path] = data
    return out, ""


def omitted_outputs(report: Mapping[str, Any]) -> dict[str, str]:
    """Files the sealed build could not copy out: path -> why (with the size when known)."""

    out: dict[str, str] = {}
    for row in report.get("omitted") or ():
        if isinstance(row, Mapping) and row.get("path"):
            size = row.get("size")
            why = str(row.get("why") or "not copied out")[:200]
            out[str(row["path"])[:1024]] = f"{why} ({size} bytes)" if isinstance(size, int) and size else why
    return out


#: The typed prefix of the reason an analysis whose command cannot fit the executor carries, at plan
#: time (a non-executable spec) and at run time (a ``not_run`` side): never a silent drop or a truncation.
COMMAND_TOO_LARGE: Final[str] = "Command too large"
#: Characters kept free for what a run adds to its payload after planning (a budget, a longer state list).
PAYLOAD_RESERVE: Final[int] = 256
_COMMAND_WORDS: Final[tuple[str, ...]] = ("python3", "-I", "-c")


def payload_headroom(driver: str) -> int:
    """Characters of the executor command left for a payload after ``python3 -I -c <driver>``."""

    return MAX_COMMAND_CHARS - sum(len(item) for item in (*_COMMAND_WORDS, driver.strip()))


def driver_command(driver: str, payload: Mapping[str, Any]) -> tuple[tuple[str, ...], str]:
    """``python3 -I -c <driver> <payload>``; ``((), why)`` when it exceeds the hosted command limit.

    ``-I`` (isolated): the working directory is the reviewed tree, and without it
    a committed ``json.py`` would shadow the standard library inside the driver.
    """

    encoded = json.dumps(dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    argv = (*_COMMAND_WORDS, driver.strip(), encoded)
    total = sum(len(item) for item in argv)
    if total > MAX_COMMAND_CHARS:
        return (), (
            f"{COMMAND_TOO_LARGE}: the analysis command is {total} characters, over the {MAX_COMMAND_CHARS}-character "
            f"executor limit ({payload_headroom(driver)} are left for the inputs after the driver); narrow the analysis"
        )
    return argv, ""


def trusted_environment(read_base: Any) -> tuple[Any, str]:
    """The trusted base's ``.lemoncrow/environment.toml``: ``(definition or None, why it cannot be used)``."""

    from lemoncrow_client.environment import ENVIRONMENT_FILE, DefinitionError, parse_environment

    text = read_base(ENVIRONMENT_FILE)
    if not (text or "").strip():
        return None, f"the trusted base has no {ENVIRONMENT_FILE}"
    try:
        return parse_environment(text, source=ENVIRONMENT_FILE), ""
    except DefinitionError as exc:
        return None, str(exc)[:400]


def payload_problem(driver: str, spec: AnalysisSpec, payload: Mapping[str, Any], *, reserve: int = 0) -> str:
    """Why *spec*'s command cannot fit the executor at its worst case, or ``""``.

    The document is the run's own (payload plus entry, spec, root, timeout and the report limit at their
    largest) padded by :data:`PAYLOAD_RESERVE` and *reserve*: acceptance here means the run-time command fits.
    """

    document = {
        **payload,
        "entry": spec.entry_id,
        "spec": spec.id,
        "root": spec.root,
        "timeout": spec.timeout,
        "limit": LOCAL_REPORT_LIMIT,
        "reserve": "x" * (PAYLOAD_RESERVE + reserve),
    }
    return driver_command(driver, document)[1]


# --------------------------------------------------------------------------- #
# driver prelude and trailer (stdlib only; runs inside the sandbox)
# --------------------------------------------------------------------------- #

#: Shared driver helpers. Text here is stored with every execution record and
#: must survive redaction unchanged (a test pins ``protect_text(driver) == driver``).
DRIVER_PRELUDE: Final[str] = r"""
import difflib, hashlib, json, os, re, shutil, stat, subprocess, sys, time
P = json.loads(sys.argv[1])
R = {"v": 1, "entry": str(P.get("entry") or ""), "spec": str(P.get("spec") or ""), "notes": [], "heavy": {}}
LIMIT = int(P.get("limit") or 170000)
TIMEOUT = max(5, int(P.get("timeout") or 300))
STARTED = time.monotonic()
# The whole revision is the workspace (so a run reproduces from its commit); the spec's
# trusted-base root is a directory inside it, never outside.
TOP = os.path.realpath(os.getcwd())
CWD = os.path.realpath(os.path.join(TOP, str(P.get("root") or ".")))
if CWD != TOP and not CWD.startswith(TOP + os.sep):
    CWD = os.path.join(TOP, ".lemoncrow-invalid-root")
# This sandbox's private /tmp; Seatbelt has none, so its runner or a driver test moves it.
WORK = os.environ.get("LEMONCROW_ANALYSIS_WORK") or "/tmp/lc-analysis"
KEEP = ("PATH", "LANG", "LC_ALL", "TZ", "SOURCE_DATE_EPOCH", "VIRTUAL_ENV", "PYTHONDONTWRITEBYTECODE")
os.makedirs(WORK + "/home", exist_ok=True)


def env(extra=None):
    # Allowlist only: no provider, registry or cloud credential can reach a tool.
    out = {k: v for k, v in os.environ.items() if k in KEEP}
    out.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    out.update({"HOME": WORK + "/home", "CI": "1", "LANG": "C.UTF-8", "NO_COLOR": "1"})
    out.update(extra or {})
    return out


def which(name):
    return shutil.which(name, path=env()["PATH"])


def run(argv, cwd, extra=None, split=False, timeout=None):
    left = TIMEOUT - (time.monotonic() - STARTED)
    if left <= 1:
        return -9, "", "the analysis ran out of time"
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=env(extra), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if split else subprocess.STDOUT, timeout=min(timeout or left, left),
        )
    except FileNotFoundError:
        return None, "", argv[0] + " is not installed in the sandbox"
    except subprocess.TimeoutExpired:
        return -9, "", "timed out: " + " ".join(argv[:2])
    out = proc.stdout.decode("utf-8", "replace")
    return proc.returncode, out, proc.stderr.decode("utf-8", "replace") if split else out


def sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def text(path, limit=1 << 20):
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data[:limit].decode("utf-8", "replace")


def rel(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def globre(pattern):
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + "$")


def copy_root(limit=268435456):
    # The revision is read-only on hosted executors and never modified locally: work on a copy.
    total = 0
    for base, dirs, files in os.walk(CWD):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
        if total > limit:
            finish(2, "the analysed tree is larger than the sandbox copy limit")
    dst = WORK + "/src"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(CWD, dst, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    return dst


FINISH_HOOKS = []


def write_report(folder, payload):
    # Reviewed code may have left something at the report path (a forged report made
    # read-only, a link, a directory): replace it. No process of it is alive by now.
    try:
        os.chmod(folder, stat.S_IMODE(os.lstat(folder).st_mode) | 0o700)
    except OSError:
        pass
    path = os.path.join(folder, "report")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    if info is not None and stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    elif info is not None:
        os.unlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as handle:
        handle.write(payload)


def finish(code, reason=""):
    for hook in FINISH_HOOKS:
        hook()
    R["exit"] = code
    if reason:
        R["reason"] = str(reason)[:2000]
    data = json.dumps(R, sort_keys=True, separators=(",", ":"), default=str)
    heavy = R.get("heavy") or {}
    while len(data) > LIMIT and any(heavy.values()):
        name = max(heavy, key=lambda key: len(heavy[key]))
        keep = max(0, len(heavy[name]) - (len(data) - LIMIT) - 256)
        heavy[name] = heavy[name][:keep] if keep < len(heavy[name]) - 1 else ""
        if name not in R.setdefault("truncated", []):
            R["truncated"].append(name)
        data = json.dumps(R, sort_keys=True, separators=(",", ":"), default=str)
    if len(data) > LIMIT:
        code = 3
        data = json.dumps({"v": 1, "exit": 3, "entry": R["entry"], "spec": R["spec"],
                           "reason": "the analysis report exceeded its size limit"})
    folder = os.environ.get("LEMONCROW_REPORT_DIR")
    if folder:
        try:
            write_report(folder, data.encode("utf-8").hex())
        except OSError:
            # The report path could not be reclaimed: exit with a status no report may
            # claim, so the host trusts whatever is there as nothing.
            sys.stdout.write("analysis report could not be written\n")
            sys.stdout.flush()
            os._exit(120)
    sys.stdout.write("analysis exit %d: %s\n" % (code, (str(reason) or "ok")[:300]))
    sys.stdout.flush()
    sys.exit(code)
"""

DRIVER_TRAILER: Final[str] = r"""
try:
    main()
except SystemExit:
    raise
except Exception as exc:
    finish(3, "analysis driver error: %s: %s" % (type(exc).__name__, exc))
finish(0)
"""


#: Stage A of a two-stage analysis (shipped artifacts, regenerate-and-compare).
#: Reviewed code runs only in ``sealed_run`` children. The driver makes itself
#: undumpable (no ptrace, no /proc/<pid>/mem, with Yama ptrace_scope >= 1 for
#: its ancestors) and the subreaper of everything they start, records every
#: signal sent to it (SIGSTOP cannot be caught, but the SIGCONT that resumes it
#: can), and after each step kills every descendant still alive. A descendant
#: alive or exiting after the step's main process marks the run tampered.
#: Only then are the outputs copied out (regular files, bounded, validated
#: names, inside the build tree); the host inspects those bytes (stage B), and
#: accepts the report only when the process exit status equals its ``exit``.
SEAL_PRELUDE: Final[str] = r"""
import base64, ctypes, signal, stat
SEAL = {"v": 1, "signals": [], "lingering": 0, "steps": 0, "started": False}


def _tamper(signum, frame):
    SEAL["signals"].append(int(signum))


def seal_start():
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        isolated = libc.prctl(4, 0, 0, 0, 0) == 0 and libc.prctl(36, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        isolated = False
    if not isolated:
        finish(2, "the sandbox cannot isolate the build's processes (prctl is unavailable)")
    # Yama guards this process's ancestors. As PID 1 of its own PID namespace (a one-shot container) it has none
    # that a descendant can see, and it is undumpable with no CAP_SYS_PTRACE in the container, so Yama adds nothing.
    try:
        with open("/proc/sys/kernel/yama/ptrace_scope") as handle:
            scope = int(handle.read().strip() or "0")
    except (OSError, ValueError):
        scope = 0
    if scope < 1 and os.getpid() != 1:
        finish(2, "the sandbox kernel allows ptrace between the build's processes (Yama ptrace_scope < 1)")
    if not os.path.isdir("/proc/self/task"):
        finish(2, "the sandbox has no /proc to account for the build's processes")
    for sig in signal.valid_signals():
        if sig in (signal.SIGKILL, signal.SIGSTOP, signal.SIGCHLD):
            continue
        try:
            signal.signal(sig, _tamper)
        except (OSError, ValueError, RuntimeError):
            pass
    SEAL["started"] = True
    FINISH_HOOKS.append(seal_stamp)


def _parents():
    out = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "rb") as handle:
                fields = handle.read().rsplit(b")", 1)[1].split()
        except (OSError, IndexError):
            continue
        out[int(name)] = (int(fields[1]), fields[0].decode("ascii", "replace"))
    return out


def _descendants():
    rows, me, found = _parents(), os.getpid(), set()
    changed = True
    while changed:
        changed = False
        for pid, (parent, state) in rows.items():
            if pid not in found and (parent == me or parent in found):
                found.add(pid)
                changed = True
    return [pid for pid in found if rows[pid][1] != "Z"]


def _reap():
    while True:
        try:
            if os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG) is None:
                return
        except ChildProcessError:
            return


def kill_tree():
    found = 0
    for _round in range(500):
        alive = _descendants()
        if not alive:
            break
        found += len(alive)
        for sig in (signal.SIGSTOP, signal.SIGKILL):
            for pid in alive:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
        time.sleep(0.005)
        _reap()
    else:
        SEAL["lingering"] += 1
    _reap()
    return found


def sealed_run(argv, cwd, extra=None):
    # One step of reviewed code; when it returns, nothing the step started is alive.
    argv = [str(item) for item in argv]
    exe = argv[0] if "/" in argv[0] else which(argv[0])
    if not exe:
        return None, argv[0] + " is not installed in the sandbox"
    left = TIMEOUT - (time.monotonic() - STARTED)
    if left <= 1:
        return -9, "the analysis ran out of time"
    log = "%s/step-%d.log" % (WORK, SEAL["steps"])
    SEAL["steps"] += 1
    out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    null = os.open(os.devnull, os.O_RDONLY)
    environment = env(extra)
    pid = os.fork()
    if pid == 0:
        try:
            os.chdir(cwd)
            os.dup2(null, 0)
            os.dup2(out, 1)
            os.dup2(out, 2)
            os.execve(exe, argv, environment)
        finally:
            os._exit(127)
    os.close(out)
    os.close(null)
    deadline, code = time.monotonic() + left, None
    while code is None:
        try:
            info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG)
        except ChildProcessError:
            code = -9
            break
        if info is None:
            if time.monotonic() > deadline:
                code = -9
                break
            time.sleep(0.005)
        elif info.si_pid == pid:
            code = info.si_status if info.si_code == os.CLD_EXITED else -info.si_status
        elif os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
            # An orphan of the step that exited after the step's main process did.
            SEAL["lingering"] += 1
    if kill_tree():
        SEAL["lingering"] += 1
    output = text(log, 1 << 20) or ""
    if code == -9 and time.monotonic() > deadline:
        return -9, "timed out: " + " ".join(argv[:2])
    return code, output


def dir_identity(path):
    info = os.lstat(path)
    return (info.st_dev, info.st_ino) if stat.S_ISDIR(info.st_mode) else None


def require_same_dir(path, identity):
    # The build may not swap its tree for another one (the pristine revision) before the copy-out.
    try:
        now = dir_identity(path)
    except OSError:
        now = None
    if identity is None or now != identity:
        finish(3, "the build replaced its working directory; nothing was copied out")


def seal_reasons():
    reasons = []
    if SEAL["signals"]:
        reasons.append("the analysis process was signalled during the build (%s)" % ",".join(
            str(s) for s in sorted(set(SEAL["signals"]))[:8]))
    if SEAL["lingering"]:
        reasons.append("the build left background processes running after it exited")
    return reasons


def seal_stamp():
    R["seal"] = {"v": 1, "started": SEAL["started"], "steps": SEAL["steps"], "tampered": seal_reasons()}


def _safe_name(name):
    parts = name.split("/")
    return (0 < len(name) <= 1024 and not name.startswith("/") and ".." not in parts and "" not in parts
            and not any(ord(ch) < 32 or ord(ch) == 127 for ch in name))


def read_files(files, root, budget, limit=2000, bound="copy-out"):
    # Only after kill_tree: no process of the build exists. Regular files only, never a link.
    rows, omitted, used, top = [], [], 0, os.path.realpath(root)
    for name, full in files[:limit]:
        why, data, size = "", b"", 0
        if not _safe_name(name):
            why = "unsafe file name"
        elif not os.path.realpath(full).startswith(top + os.sep):
            why = "outside the build tree"
        else:
            try:
                fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                fd, why = None, "not a readable regular file"
            if fd is not None:
                with os.fdopen(fd, "rb") as handle:
                    info = os.fstat(handle.fileno())
                    size = info.st_size
                    if not stat.S_ISREG(info.st_mode):
                        why = "not a regular file"
                    elif size > budget - used:
                        why = "over the %s bound" % bound
                    else:
                        data = handle.read(budget - used + 1)
                        if len(data) > budget - used:
                            why = "over the %s bound" % bound
        if why:
            omitted.append({"path": name[:1024], "why": why, "size": size})
            continue
        used += len(data)
        rows.append((name, data))
    for name, _full in files[limit:]:
        omitted.append({"path": name[:1024], "why": "over the %s file count" % bound, "size": 0})
    return rows, omitted[:500]


def copy_out(files, root, budget, limit=2000):
    rows, omitted = read_files(files, root, budget, limit)
    return [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
             "b64": base64.b64encode(data).decode("ascii")} for name, data in rows], omitted


def copy_budget():
    return max(0, int((LIMIT - int(P.get("copy_overhead") or 40000)) * 0.74))


# Raw bytes an in-sandbox inspection reads (stage B inside the sandbox, once the seal holds).
INSPECT_BYTES = 256 << 20


def fits_report(files):
    # Whether copying these files out keeps the report within LIMIT (base64 plus row metadata).
    total = 0
    for _name, full in files:
        try:
            info = os.lstat(full)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    need = len(json.dumps(R, default=str)) + total * 4 // 3 + 200 * len(files) + 4096
    return total <= copy_budget() and need <= LIMIT
"""


def embedded_source(source: str) -> str:
    """A stdlib module as a sealed driver embeds it: no docstrings, annotations, ``__future__`` or ``__all__``.

    Comments go too (``ast.unparse``) and indentation shrinks to one space per
    level: the hosted executor bounds the command length.
    """

    class _Strip(ast.NodeTransformer):
        def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST | None:
            if node.value is None:
                return None
            return ast.copy_location(ast.Assign(targets=[node.target], value=node.value), node)

        def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
            self.generic_visit(node)
            node.returns = None
            arguments = node.args
            for arg in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                arguments.vararg,
                arguments.kwarg,
            ):
                if arg is not None:
                    arg.annotation = None
            if len(node.body) > 1 and _is_docstring(node.body[0]):
                node.body = node.body[1:]
            return node

        visit_FunctionDef = _function
        visit_AsyncFunctionDef = _function

    tree = ast.parse(source)
    tree.body = [
        node
        for node in tree.body
        if not _is_docstring(node)
        and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
        and not (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in _targets(node))
        )
    ]
    text = ast.unparse(ast.fix_missing_locations(_Strip().visit(tree)))
    # ast.unparse writes every string literal on one line, so reindenting lines is safe.
    return "\n".join(" " * ((len(line) - len(line.lstrip(" "))) // 4) + line.lstrip(" ") for line in text.splitlines())


def _is_docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _targets(node: ast.Assign | ast.AnnAssign) -> list[ast.expr]:
    return list(node.targets) if isinstance(node, ast.Assign) else [node.target]


def build_driver(body: str, *, sealed: bool = False, shared: str = "") -> str:
    """The driver source. A driver that embeds shared stage-B source is minified as a whole
    (``embedded_source``: no comments, one-space indentation) to fit the hosted command limit.
    """

    text = "".join(
        [DRIVER_PRELUDE, SEAL_PRELUDE if sealed else "", shared + "\n" if shared else "", body, DRIVER_TRAILER]
    )
    return embedded_source(text) if shared else text.strip()
    return (DRIVER_PRELUDE + (SEAL_PRELUDE if sealed else "") + body + DRIVER_TRAILER).strip()


def bounded(text: object, limit: int = 400) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def first_line(text: object, limit: int = 300) -> str:
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return bounded(line, limit)
    return ""


__all__ = [
    "ANALYSIS_ENTRIES",
    "ANALYSIS_EXECUTOR",
    "ANALYSIS_MEDIA_TYPE",
    "ANALYSIS_PRODUCER_ID",
    "ANALYSIS_PROVIDER",
    "COMMAND_TOO_LARGE",
    "COMPAT_ENTRY",
    "CONFIG_KEYS",
    "COPY_OUT_OVERHEAD",
    "DRIVER_PRELUDE",
    "DRIVER_TRAILER",
    "ENTRY_FACT",
    "ENTRY_KIND",
    "FLAG_MATRIX_ENTRY",
    "INFRA_ENTRY",
    "LOCAL_REPORT_LIMIT",
    "MIGRATIONS_ENTRY",
    "PAYLOAD_RESERVE",
    "REGENERATE_ENTRY",
    "REPORT_LIMIT",
    "SEAL_PRELUDE",
    "SHIPPED_ENTRY",
    "AnalysisCatalog",
    "AnalysisSpec",
    "Evaluation",
    "Finding",
    "Observation",
    "SideRun",
    "build_driver",
    "decode_outputs",
    "decode_report",
    "driver_command",
    "embedded_source",
    "glob_regex",
    "inspection_sides",
    "omitted_outputs",
    "payload_headroom",
    "payload_problem",
    "relative_path",
    "seal_tampered",
    "sealed_inspection_problem",
    "sealed_problem",
    "side_state",
    "trusted_environment",
]
