"""Check ingestion and federation (Phase 11 P11-FED, items 1-3).

One interface normalizes every deterministic quality/security result --
formatter/linter, type checker, SAST, secret scan, dependency/SCA,
schema/API compatibility, migration safety, tests/coverage, IaC/security and
existing CI/check-run outputs -- into a typed :class:`ReviewEvidence` row bound
to one exact Snapshot (``revision_id`` + provider head SHA). Imported findings
(SARIF 2.1.0, GitHub check-run annotations) are stored as an immutable JSON
artifact on that row.

Trust model (fail closed):

* The attestation class comes from :class:`IngestAuthority`, which the
  composition derives from the authenticated channel (the provider webhook
  path for ``provider_ci``). A payload never names its own authority. A human
  or caller-attested upload is ``human_observation`` / untyped and can never
  satisfy a typed required check or a run-plan entry, whatever it is titled.
* Imported text (messages, rule ids, titles) is **data**: it is bounded,
  stored verbatim and never parsed for instructions; no field of it decides a
  status, a requirement or a plan entry.
* Bounds are explicit and visible: a payload above :data:`MAX_INGEST_BYTES` is
  refused (nothing stored); more than :data:`MAX_FINDINGS` results keeps the
  most severe :data:`MAX_FINDINGS` and records the dropped count in
  :class:`Truncation`, which every surface shows; messages longer than
  :data:`MAX_MESSAGE_CHARS` are cut and flagged.

Equivalence ("prefer ingestion over rerun", consumed by R6): an
:class:`EquivalenceKey` is ``(tool identity, recipe, exact head SHA, config
digest, recipe digest)``. The recipe digest is a typed slot for the check-recipe
work package's deterministic digest (command + config inputs); this module never
computes one -- it only carries and compares the value that package supplies. :func:`ingested_satisfactions` lets a current-Snapshot typed result
satisfy a catalog entry only when the **trusted base** declares the mapping
(``ci_equivalents`` in ``.lemoncrow/review.yaml``), the result is
``provider_ci``/``lemoncrow_verification``/``signed_attestation``, completed (PASS/FAIL), bound to
this exact Snapshot, its head SHA equals the Snapshot's, the Snapshot is a
clean commit, and a pinned config digest matches. A result for another SHA, a
stale Snapshot, a dirty working tree, a pending rerun or an untyped upload
never satisfies.

Kill switch (Trust Gate T-J item 2): ``LEMONCROW_REVIEW_CI_EQUIVALENCE=off``
lets no result stand in (``equivalence_disabled``). The planner keeps each
entry's own status and says an equivalent result exists but is not used.
Nothing is stored differently, so turning it back on needs no migration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, cast

import yaml

from lemoncrow.pro.capabilities.review.criticality import (
    FINDING_SEVERITIES,
    SEVERITY_LABELS,
    SEVERITY_RANK,
    FindingSeverity,
    normalize_finding_severity,
)
from lemoncrow.pro.capabilities.review.session_models import (
    ReviewEvidence,
    ReviewEvidenceSource,
    ReviewRevision,
)

CheckCategory = Literal[
    "format",
    "lint",
    "typecheck",
    "sast",
    "secret_scan",
    "dependency_sca",
    "schema_compat",
    "migration_safety",
    "tests",
    "coverage",
    "iac_security",
    "ci",
]
CHECK_CATEGORIES: Final[tuple[str, ...]] = (
    "format",
    "lint",
    "typecheck",
    "sast",
    "secret_scan",
    "dependency_sca",
    "schema_compat",
    "migration_safety",
    "tests",
    "coverage",
    "iac_security",
    "ci",
)
IngestFormat = Literal["check", "sarif", "github_annotations", "in_toto"]

INGEST_MIME: Final[str] = "application/vnd.lemoncrow.check-ingest+json"
INGEST_SCHEMA: Final[int] = 1
#: Largest payload accepted; anything larger is refused before parsing.
MAX_INGEST_BYTES: Final[int] = 16 * 1024 * 1024
#: Findings kept per ingestion (most severe first); the rest are counted, not stored.
MAX_FINDINGS: Final[int] = 500
#: SARIF runs read per document; further runs are counted as dropped.
MAX_RUNS: Final[int] = 20
#: Results examined per document; beyond it results are counted as dropped unread.
MAX_RESULTS_SCANNED: Final[int] = 50_000
MAX_MESSAGE_CHARS: Final[int] = 1000
MAX_TAGS: Final[int] = 16
_MAX_FIELD: Final[int] = 200
_MAX_PATH: Final[int] = 512
_MAX_EQUIVALENTS: Final[int] = 64

EQUIVALENCE_VERSION: Final[str] = "eq1"
FINGERPRINT_VERSION: Final[str] = "fp1"
#: Attestation classes whose result may stand in for executing a catalog entry.
SATISFYING_CLASSES: Final[frozenset[str]] = frozenset({"provider_ci", "lemoncrow_verification", "signed_attestation"})
_TYPED_CLASSES: Final[frozenset[str]] = frozenset(
    {"provider_ci", "lemoncrow_verification", "trusted_security_feed", "signed_attestation"}
)
_STATUSES: Final[frozenset[str]] = frozenset({"PASS", "FAIL", "NOT_RUN", "UNKNOWN"})
_CWE = re.compile(r"cwe[-_/ ]?0*(\d{1,5})", re.IGNORECASE)
_SHA = re.compile(r"^[0-9a-f]{7,64}$")


class IngestRefused(ValueError):
    """The input cannot be ingested as given; nothing was stored."""


class IngestConflict(IngestRefused):
    """The result does not belong to this Snapshot (another head SHA); nothing was stored."""


def _bounded(value: object, limit: int = _MAX_FIELD) -> str:
    return " ".join(str(value or "").split())[:limit]


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


# --------------------------------------------------------------------------- #
# findings
# --------------------------------------------------------------------------- #


def _normalized_text(value: str) -> str:
    return " ".join(str(value or "").split()).lower()[:MAX_MESSAGE_CHARS]


def finding_fingerprint(tool: str, rule_id: str, path: str, message: str, snippet: str = "") -> str:
    """Line-independent identity of one finding: tool + rule + path + normalized message/snippet digest.

    The same result reported after the code around it moved keeps its
    fingerprint, so a human disposition follows it to a new Snapshot; another
    result of the same rule (different message or snippet) never shares it.
    Identical results in one file are told apart by line order only
    (:func:`_bound_findings` appends ``#<n>``).
    """

    text = _digest([_normalized_text(message), _normalized_text(snippet)])
    return "sfp_" + _digest([FINGERPRINT_VERSION, tool, rule_id, path, text])[:24]


@dataclass(frozen=True, slots=True)
class ImportedFinding:
    """One deterministic tool result at a path/line. ``message`` is untrusted data.

    ``id`` names this observation (line included); ``fingerprint`` is the
    line-independent identity dispositions and usefulness are keyed by.
    """

    id: str
    tool: str
    rule_id: str
    severity: FindingSeverity
    level: str
    path: str
    start_line: int = 0
    end_line: int = 0
    message: str = ""
    message_truncated: bool = False
    tags: tuple[str, ...] = ()
    cwe: tuple[int, ...] = ()
    in_snapshot: bool = True
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            object.__setattr__(
                self, "fingerprint", finding_fingerprint(self.tool, self.rule_id, self.path, self.message)
            )

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "fingerprint": self.fingerprint,
            "tool": self.tool,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "severity_label": SEVERITY_LABELS[self.severity],
            "level": self.level,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "message": self.message,
            "message_truncated": self.message_truncated,
            "tags": list(self.tags),
            "cwe": list(self.cwe),
            "in_snapshot": self.in_snapshot,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> ImportedFinding | None:
        severity = str(raw.get("severity") or "")
        finding_id = str(raw.get("id") or "")
        if severity not in FINDING_SEVERITIES or not finding_id:
            return None
        return cls(
            id=finding_id,
            tool=str(raw.get("tool") or ""),
            rule_id=str(raw.get("rule_id") or ""),
            severity=cast(FindingSeverity, severity),
            level=str(raw.get("level") or ""),
            path=str(raw.get("path") or ""),
            start_line=int(raw.get("start_line") or 0),
            end_line=int(raw.get("end_line") or 0),
            message=str(raw.get("message") or ""),
            message_truncated=raw.get("message_truncated") is True,
            tags=tuple(str(item) for item in raw.get("tags") or ()),
            cwe=tuple(int(item) for item in raw.get("cwe") or () if str(item).isdigit()),
            in_snapshot=raw.get("in_snapshot") is not False,
            fingerprint=_bounded(raw.get("fingerprint"), 64),
        )


@dataclass(frozen=True, slots=True)
class Truncation:
    """What an ingestion did not keep. Every surface shows :meth:`note` when set."""

    total: int = 0
    kept: int = 0
    bound: int = MAX_FINDINGS
    runs_dropped: int = 0
    results_unscanned: int = 0
    messages_truncated: int = 0
    suppressed: int = 0
    duplicates: int = 0

    @property
    def dropped(self) -> int:
        return max(0, self.total - self.kept) + self.results_unscanned

    @property
    def truncated(self) -> bool:
        return bool(self.dropped or self.runs_dropped or self.messages_truncated)

    def note(self) -> str:
        parts: list[str] = []
        if self.dropped:
            parts.append(
                f"Imported {self.kept} of {self.total + self.results_unscanned} results "
                f"(bound {self.bound} per ingestion, most severe kept); {self.dropped} not imported."
            )
        if self.runs_dropped:
            parts.append(f"{self.runs_dropped} SARIF run(s) beyond the {MAX_RUNS}-run bound were not read.")
        if self.messages_truncated:
            parts.append(f"{self.messages_truncated} message(s) cut at {MAX_MESSAGE_CHARS} characters.")
        return " ".join(parts)

    def to_wire(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "kept": self.kept,
            "dropped": self.dropped,
            "bound": self.bound,
            "runs_dropped": self.runs_dropped,
            "results_unscanned": self.results_unscanned,
            "messages_truncated": self.messages_truncated,
            "suppressed": self.suppressed,
            "duplicates": self.duplicates,
            "truncated": self.truncated,
            "note": self.note(),
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> Truncation:
        def number(key: str) -> int:
            try:
                return max(0, int(raw.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        return cls(
            total=number("total"),
            kept=number("kept"),
            bound=number("bound") or MAX_FINDINGS,
            runs_dropped=number("runs_dropped"),
            results_unscanned=number("results_unscanned"),
            messages_truncated=number("messages_truncated"),
            suppressed=number("suppressed"),
            duplicates=number("duplicates"),
        )


def display_severity(level: str, security_severity: object = None) -> FindingSeverity:
    """Producer level -> P9-PRIO finding severity (``criticality.normalize_finding_severity``).

    Never a gate: readiness reads check status only. A numeric
    ``security-severity`` wins (advisory bands); otherwise error/failure is P2
    and everything else a suggestion.
    """

    return normalize_finding_severity("static_analysis", raw=level, score=security_severity) or "suggestion"


def _cwes(values: Iterable[str]) -> tuple[int, ...]:
    found: set[int] = set()
    for value in values:
        found.update(int(match) for match in _CWE.findall(value))
    return tuple(sorted(found))


def normalize_location(uri: str, snapshot_paths: Iterable[str] = ()) -> tuple[str, bool]:
    """A repository-relative path for a producer URI, and whether it is a Snapshot file.

    Exact match first, then the longest Snapshot path the URI ends with (CI
    runners report absolute workspace paths). An unmatched location is kept
    (bounded) and flagged ``in_snapshot=False``; it is never guessed.
    """

    text = urllib.parse.unquote(str(uri or "").strip())
    if text.startswith("file://"):
        text = text[len("file://") :]
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    known = sorted({str(path) for path in snapshot_paths if path}, key=lambda item: (-len(item), item))
    if text in known:
        return text, True
    for candidate in known:
        if text.endswith("/" + candidate):
            return candidate, True
    return text[:_MAX_PATH], False


def _finding_id(tool: str, rule_id: str, path: str, start: int, end: int, message: str) -> str:
    return "sf_" + _digest([tool, rule_id, path, start, end, message[:200]])[:20]


def _make_finding(
    *,
    tool: str,
    rule_id: str,
    level: str,
    security_severity: object,
    uri: str,
    start: int,
    end: int,
    message: str,
    tags: Sequence[str],
    snapshot_paths: Sequence[str],
    snippet: str = "",
) -> ImportedFinding:
    path, in_snapshot = normalize_location(uri, snapshot_paths)
    raw_message = str(message or "")
    truncated = len(raw_message) > MAX_MESSAGE_CHARS
    bounded_message = raw_message[:MAX_MESSAGE_CHARS]
    bounded_tags = tuple(dict.fromkeys(_bounded(tag, 64) for tag in tags if _bounded(tag, 64)))[:MAX_TAGS]
    start = max(0, start)
    end = max(start, end)
    rule = _bounded(rule_id)
    return ImportedFinding(
        id=_finding_id(tool, rule, path, start, end, bounded_message),
        tool=tool,
        rule_id=rule,
        severity=display_severity(level, security_severity),
        level=_bounded(level, 32),
        path=path,
        start_line=start,
        end_line=end,
        message=bounded_message,
        message_truncated=truncated,
        tags=bounded_tags,
        cwe=_cwes((rule, *bounded_tags)),
        in_snapshot=in_snapshot,
        fingerprint=finding_fingerprint(tool, rule, path, bounded_message, snippet),
    )


def _bound_findings(
    found: Sequence[ImportedFinding],
    *,
    runs_dropped: int = 0,
    results_unscanned: int = 0,
    suppressed: int = 0,
) -> tuple[tuple[ImportedFinding, ...], Truncation]:
    unique: dict[str, ImportedFinding] = {}
    for item in found:
        unique.setdefault(item.id, item)
    # Identical results in one file share a fingerprint: line order is the only
    # tie-breaker (#2, #3 ...), assigned before the bound so it never shifts.
    # lc-debt: identical results are matched by order alone; removing the first moves its disposition to the next
    groups: dict[str, list[ImportedFinding]] = {}
    for item in unique.values():
        groups.setdefault(item.fingerprint, []).append(item)
    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda item: (item.start_line, item.end_line, item.id))
        for index, item in enumerate(rows[1:], start=2):
            unique[item.id] = replace(item, fingerprint=f"{item.fingerprint}#{index}")
    ordered = sorted(
        unique.values(),
        key=lambda item: (-SEVERITY_RANK[item.severity], item.path, item.start_line, item.rule_id, item.id),
    )
    kept = tuple(ordered[:MAX_FINDINGS])
    return kept, Truncation(
        total=len(ordered),
        kept=len(kept),
        runs_dropped=runs_dropped,
        results_unscanned=results_unscanned,
        messages_truncated=sum(1 for item in kept if item.message_truncated),
        suppressed=suppressed,
        duplicates=len(found) - len(unique),
    )


def _object(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class SarifImport:
    tool: str
    tool_version: str
    findings: tuple[ImportedFinding, ...]
    truncation: Truncation
    derived_status: str
    status_basis: str


def parse_sarif(payload: bytes, *, snapshot_paths: Sequence[str] = ()) -> SarifImport:
    """Normalize a SARIF 2.1.0 document into bounded findings.

    Accepted (unsuppressed) ``fail``-kind results become findings; ``pass`` and
    ``notApplicable`` results are not findings. Status is derived only from
    structure, never from text: FAIL when any result is ``error`` level, UNKNOWN
    when an invocation reports ``executionSuccessful: false`` (and nothing
    failed), otherwise PASS.
    """

    if len(payload) > MAX_INGEST_BYTES:
        raise IngestRefused(f"SARIF payload exceeds the {MAX_INGEST_BYTES}-byte ingestion bound; nothing was stored")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngestRefused("SARIF payload is not valid JSON") from exc
    if not isinstance(document, Mapping) or str(document.get("version") or "") != "2.1.0":
        raise IngestRefused("only SARIF version 2.1.0 is supported")
    runs = _list(document.get("runs"))
    found: list[ImportedFinding] = []
    tool_names: list[str] = []
    tool_version = ""
    failed = False
    unsuccessful = False
    suppressed = 0
    scanned = 0
    unscanned = 0
    for run in runs[:MAX_RUNS]:
        run = _object(run)
        driver = _object(_object(run.get("tool")).get("driver"))
        tool = _bounded(driver.get("name") or "sarif", 128)
        tool_names.append(tool)
        tool_version = tool_version or _bounded(driver.get("semanticVersion") or driver.get("version"), 64)
        rules = [_object(rule) for rule in _list(driver.get("rules"))]
        rules_by_id = {str(rule.get("id") or ""): rule for rule in rules if rule.get("id")}
        for invocation in _list(run.get("invocations")):
            if _object(invocation).get("executionSuccessful") is False:
                unsuccessful = True
        for raw in _list(run.get("results")):
            if scanned >= MAX_RESULTS_SCANNED:
                unscanned += 1
                continue
            scanned += 1
            result = _object(raw)
            kind = str(result.get("kind") or "fail")
            if kind in {"pass", "notApplicable"}:
                continue
            suppressions = _list(result.get("suppressions"))
            if any(str(_object(item).get("status") or "accepted") == "accepted" for item in suppressions):
                suppressed += 1
                continue
            rule_index = result.get("ruleIndex")
            rule: Mapping[str, Any] = (
                rules[rule_index]
                if isinstance(rule_index, int) and 0 <= rule_index < len(rules)
                else rules_by_id.get(str(result.get("ruleId") or ""), {})
            )
            rule_id = str(result.get("ruleId") or rule.get("id") or _object(result.get("rule")).get("id") or "")
            default = _object(rule.get("defaultConfiguration"))
            level = str(result.get("level") or default.get("level") or "warning")
            if level == "error":
                failed = True
            properties = _object(result.get("properties"))
            rule_properties = _object(rule.get("properties"))
            message = _object(result.get("message"))
            text = str(
                message.get("text")
                or message.get("markdown")
                or _object(rule.get("shortDescription")).get("text")
                or ""
            )
            location = _object(_object((_list(result.get("locations")) or [{}])[0]).get("physicalLocation"))
            region = _object(location.get("region"))
            snippet = str(_object(region.get("snippet")).get("text") or "")[:MAX_MESSAGE_CHARS]
            start = _int(region.get("startLine"))
            found.append(
                _make_finding(
                    tool=tool,
                    rule_id=rule_id,
                    level=level,
                    security_severity=properties.get("security-severity") or rule_properties.get("security-severity"),
                    uri=str(_object(location.get("artifactLocation")).get("uri") or ""),
                    start=start,
                    end=_int(region.get("endLine")) or start,
                    message=text,
                    tags=[str(tag) for tag in (*_list(rule_properties.get("tags")), *_list(properties.get("tags")))],
                    snapshot_paths=snapshot_paths,
                    snippet=snippet,
                )
            )
    findings, truncation = _bound_findings(
        found,
        runs_dropped=max(0, len(runs) - MAX_RUNS),
        results_unscanned=unscanned,
        suppressed=suppressed,
    )
    if failed:
        status, basis = "FAIL", "derived: at least one error-level result"
    elif unsuccessful:
        status, basis = "UNKNOWN", "derived: an invocation reported executionSuccessful=false"
    elif not runs:
        status, basis = "UNKNOWN", "derived: the SARIF document has no runs"
    else:
        status, basis = "PASS", "derived: no error-level result"
    return SarifImport(
        tool=", ".join(dict.fromkeys(tool_names)) or "sarif",
        tool_version=tool_version,
        findings=findings,
        truncation=truncation,
        derived_status=status,
        status_basis=basis,
    )


def parse_check_annotations(
    rows: Sequence[Mapping[str, Any]],
    *,
    tool: str,
    snapshot_paths: Sequence[str] = (),
    reported_total: int | None = None,
) -> tuple[tuple[ImportedFinding, ...], Truncation]:
    """GitHub check-run annotations (``GET .../check-runs/{id}/annotations``) as findings.

    ``reported_total`` is the provider's ``annotations_count``; when fewer rows
    were fetched, the difference is reported as not imported.
    """

    found = [
        _make_finding(
            tool=tool,
            rule_id=str(row.get("title") or ""),
            level=str(row.get("annotation_level") or "notice"),
            security_severity=None,
            uri=str(row.get("path") or ""),
            start=_int(row.get("start_line")),
            end=_int(row.get("end_line")),
            message=str(row.get("message") or ""),
            tags=(),
            snapshot_paths=snapshot_paths,
        )
        for row in rows[:MAX_RESULTS_SCANNED]
        if isinstance(row, Mapping)
    ]
    unfetched = max(0, (reported_total or 0) - len(rows)) + max(0, len(rows) - MAX_RESULTS_SCANNED)
    return _bound_findings(found, results_unscanned=unfetched)


# --------------------------------------------------------------------------- #
# equivalence
# --------------------------------------------------------------------------- #


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


@dataclass(frozen=True, slots=True)
class EquivalenceKey:
    """When two results are the same work: tool identity + recipe + exact head SHA + config digest.

    ``tool`` is the typed check identity without its attestation class
    (``<provider>:<integration>:<stable key>``). ``recipe`` is the catalog entry
    the result claims to cover (informational on the result; the trusted base
    decides). ``config_digest`` is ``""`` when unknown: a requirement that pins
    a digest is never met by an unknown one.

    ``recipe_digest`` is a typed slot for the check-recipe package's (R5)
    deterministic recipe digest (command + config inputs). It is opaque here:
    never computed, only carried from the producer and compared. A requirement
    that pins one is met only by a result stamped with the same value.
    """

    tool: str
    recipe: str
    head_sha: str
    config_digest: str = ""
    recipe_digest: str = ""

    def canonical(self) -> str:
        return ";".join(
            (
                EQUIVALENCE_VERSION,
                f"tool={_quote(self.tool)}",
                f"recipe={_quote(self.recipe)}",
                f"head={_quote(self.head_sha)}",
                f"config={_quote(self.config_digest)}",
                f"recipe_digest={_quote(self.recipe_digest)}",
            )
        )

    def to_wire(self) -> dict[str, str]:
        return {
            "tool": self.tool,
            "recipe": self.recipe,
            "head_sha": self.head_sha,
            "config_digest": self.config_digest,
            "recipe_digest": self.recipe_digest,
            "canonical": self.canonical(),
        }

    @classmethod
    def parse(cls, text: str) -> EquivalenceKey | None:
        parts = str(text or "").split(";")
        if len(parts) != 6 or parts[0] != EQUIVALENCE_VERSION:
            return None
        values: dict[str, str] = {}
        for part in parts[1:]:
            name, _, value = part.partition("=")
            values[name] = urllib.parse.unquote(value)
        if set(values) != {"tool", "recipe", "head", "config", "recipe_digest"}:
            return None
        return cls(values["tool"], values["recipe"], values["head"], values["config"], values["recipe_digest"])

    def mismatches(self, required: EquivalenceKey) -> tuple[str, ...]:
        """The :mod:`validity` reason codes why this (observed) key cannot stand in for ``required``.

        Empty exactly when it can. Head SHA must be exact and non-empty.
        """

        out: list[str] = []
        if not self.head_sha or self.head_sha != required.head_sha:
            out.append("equivalence_head_changed")
        if self.tool != required.tool:
            out.append("equivalence_tool_differs")
        if required.recipe and self.recipe and self.recipe != required.recipe:
            out.append("equivalence_recipe_differs")
        if required.recipe_digest and self.recipe_digest != required.recipe_digest:
            out.append("equivalence_recipe_digest_differs")
        if required.config_digest and self.config_digest != required.config_digest:
            out.append("equivalence_config_differs")
        return tuple(out)

    def satisfies(self, required: EquivalenceKey) -> bool:
        """This (observed) key stands in for ``required``. Head SHA must be exact and non-empty."""

        return not self.mismatches(required)


def tool_identity(evidence: ReviewEvidence) -> str:
    return ":".join((evidence.provider, evidence.integration_id, evidence.stable_key.strip()))


def equivalence_key_of(evidence: ReviewEvidence) -> EquivalenceKey | None:
    """The equivalence key a typed row carries (stamped at ingestion) or implies; ``None`` if untyped."""

    if evidence.attestation_class not in _TYPED_CLASSES or not evidence.stable_key.strip():
        return None
    stamped = EquivalenceKey.parse(evidence.attestation_scope)
    if stamped is not None:
        # The stamp never overrides the row's own typed identity or head.
        return EquivalenceKey(
            tool=tool_identity(evidence),
            recipe=stamped.recipe,
            head_sha=evidence.provider_revision,
            config_digest=stamped.config_digest,
            recipe_digest=stamped.recipe_digest,
        )
    return EquivalenceKey(tool=tool_identity(evidence), recipe="", head_sha=evidence.provider_revision)


def _snapshot_reusable(revision: ReviewRevision) -> bool:
    return bool(revision.head_sha) and not revision.dirty


def _current_typed(evidence: Iterable[ReviewEvidence], revision: ReviewRevision) -> dict[str, ReviewEvidence]:
    """Newest typed observation per check identity on exactly this Snapshot."""

    from lemoncrow.pro.capabilities.review.verification import check_key

    newest: dict[str, ReviewEvidence] = {}
    for item in sorted(
        (
            row
            for row in evidence
            if row.revision_id == revision.id
            and row.verification_status
            and row.execution_intent != "explicit_historical"
            and row.kind != "waiver"
        ),
        key=lambda row: (row.created_at, row.id),
        reverse=True,
    ):
        key = check_key(item)
        if key and key not in newest:
            newest[key] = item
    return newest


CI_EQUIVALENCE_FLAG: Final[str] = "LEMONCROW_REVIEW_CI_EQUIVALENCE"


def ci_equivalence_enabled() -> bool:
    """The CI equivalence flag and kill switch: on when unset or ``on``; any other value is off."""

    return os.environ.get(CI_EQUIVALENCE_FLAG, "").strip().lower() in ("", "on")


def find_equivalent(
    evidence: Iterable[ReviewEvidence], revision: ReviewRevision, required: EquivalenceKey
) -> ReviewEvidence | None:
    """The current-Snapshot result equivalent to ``required``, if any (R6 entry point).

    Only the newest observation of a check counts (a requested rerun that has
    not completed hides the older result), only a completed PASS/FAIL, only a
    satisfying attestation class, and only on a clean commit Snapshot whose
    head SHA is the required one.
    """

    return explain_equivalent(evidence, revision, required)[0]


def explain_equivalent(
    evidence: Iterable[ReviewEvidence], revision: ReviewRevision, required: EquivalenceKey
) -> tuple[ReviewEvidence | None, tuple[str, ...]]:
    """:func:`find_equivalent` with its reason chain (:mod:`validity` codes).

    ``(match, ())`` when a current result satisfies ``required``; otherwise
    ``(None, reasons)`` naming why the newest current result of the same check
    cannot, or ``no_current_result`` when the check has none on this Snapshot.
    ``equivalence_disabled`` while :func:`ci_equivalence_enabled` is off.
    """

    if not ci_equivalence_enabled():
        return None, ("equivalence_disabled",)
    if not _snapshot_reusable(revision):
        return None, ("snapshot_not_reusable",)
    if required.head_sha != revision.head_sha:
        return None, ("equivalence_head_changed",)
    reasons: tuple[str, ...] = ()
    for item in _current_typed(evidence, revision).values():
        same_check = tool_identity(item) == required.tool
        if item.attestation_class not in SATISFYING_CLASSES or item.verification_status not in {"PASS", "FAIL"}:
            if same_check and not reasons:
                reasons = (
                    ("attestation_class_insufficient",)
                    if item.attestation_class not in SATISFYING_CLASSES
                    else ("result_not_completed",)
                )
            continue
        observed = equivalence_key_of(item)
        if observed is None:
            continue
        mismatches = observed.mismatches(required)
        if not mismatches:
            return item, ()
        if same_check and not reasons:
            reasons = mismatches
    return None, reasons or ("no_current_result",)


@dataclass(frozen=True, slots=True)
class CiEquivalent:
    """Trusted-base declaration: this typed check result covers this catalog entry."""

    recipe: str
    check: str
    provider: str
    attestation_class: str = "provider_ci"
    integration: str = ""
    config_digest: str = ""

    def to_wire(self) -> dict[str, str]:
        return {
            "recipe": self.recipe,
            "check": self.check,
            "provider": self.provider,
            "attestation_class": self.attestation_class,
            "integration": self.integration,
            "config_digest": self.config_digest,
        }


def parse_ci_equivalents(content: str) -> tuple[tuple[CiEquivalent, ...], tuple[str, ...]]:
    """Top-level ``ci_equivalents`` from trusted-base ``.lemoncrow/review.yaml``.

    Read only from the trusted base (head-side edits never apply to their own
    Review). A malformed row is reported and ignored -- it never satisfies.
    """

    from lemoncrow.pro.capabilities.review.capability_catalog import valid_entry_id

    try:
        decoded = yaml.safe_load(content) if str(content or "").strip() else None
    except yaml.YAMLError:
        return (), ("ci_equivalents: trusted-base review config is not valid YAML",)
    if not isinstance(decoded, Mapping) or decoded.get("ci_equivalents") is None:
        return (), ()
    raw_rows = decoded.get("ci_equivalents")
    if not isinstance(raw_rows, list):
        return (), ("ci_equivalents must be a list",)
    rows: list[CiEquivalent] = []
    errors: list[str] = []
    for index, raw in enumerate(raw_rows[:_MAX_EQUIVALENTS]):
        where = f"ci_equivalents[{index}]"
        if not isinstance(raw, Mapping):
            errors.append(f"{where} must be a mapping")
            continue
        recipe = _bounded(raw.get("recipe"), 160)
        check = _bounded(raw.get("check"), 160)
        provider = _bounded(raw.get("provider"), 64)
        attestation = _bounded(raw.get("attestation_class") or "provider_ci", 64)
        digest = _bounded(raw.get("config_digest"), 128)
        if not recipe or not valid_entry_id(recipe) or recipe.endswith(":*"):
            errors.append(f"{where}.recipe must name one catalog entry")
            continue
        if not check:
            errors.append(f"{where}.check (the typed check stable key) is required")
            continue
        if attestation not in SATISFYING_CLASSES:
            errors.append(
                f"{where}.attestation_class must be provider_ci, lemoncrow_verification or signed_attestation"
            )
            continue
        if attestation == "provider_ci" and not provider:
            errors.append(f"{where}.provider is required for provider_ci")
            continue
        if attestation == "signed_attestation" and (provider != "in-toto" or not _bounded(raw.get("integration"))):
            # A signed result is the verifying key's: pin the key thumbprint, never "any trusted key".
            errors.append(f"{where} signed_attestation needs provider in-toto and the key thumbprint as integration")
            continue
        rows.append(
            CiEquivalent(
                recipe=recipe,
                check=check,
                provider=provider,
                attestation_class=attestation,
                integration=_bounded(raw.get("integration"), 128),
                config_digest=digest,
            )
        )
    if len(raw_rows) > _MAX_EQUIVALENTS:
        errors.append(f"ci_equivalents: only the first {_MAX_EQUIVALENTS} rows are read")
    return tuple(rows), tuple(errors)


@dataclass(frozen=True, slots=True)
class IngestedSatisfaction:
    """A catalog entry satisfied by an equivalent current-Snapshot result instead of a rerun."""

    entry_id: str
    evidence_id: str
    check_key: str
    attestation_class: str
    status: str
    head_sha: str
    equivalence: str
    title: str = ""
    observed_at: str = ""
    url: str = ""

    def digest_document(self) -> dict[str, str]:
        return {
            "entry_id": self.entry_id,
            "evidence_id": self.evidence_id,
            "status": self.status,
            "equivalence": self.equivalence,
        }

    def to_wire(self) -> dict[str, str]:
        return {
            **self.digest_document(),
            "check_key": self.check_key,
            "attestation_class": self.attestation_class,
            "head_sha": self.head_sha,
            "title": self.title,
            "observed_at": self.observed_at,
            "url": self.url,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> IngestedSatisfaction | None:
        entry_id = str(raw.get("entry_id") or "")
        evidence_id = str(raw.get("evidence_id") or "")
        if not entry_id or not evidence_id:
            return None
        return cls(
            entry_id=entry_id,
            evidence_id=evidence_id,
            check_key=str(raw.get("check_key") or ""),
            attestation_class=str(raw.get("attestation_class") or ""),
            status=str(raw.get("status") or ""),
            head_sha=str(raw.get("head_sha") or ""),
            equivalence=str(raw.get("equivalence") or ""),
            title=str(raw.get("title") or ""),
            observed_at=str(raw.get("observed_at") or ""),
            url=str(raw.get("url") or ""),
        )


def ingested_satisfactions(
    evidence: Iterable[ReviewEvidence],
    revision: ReviewRevision,
    equivalents: Sequence[CiEquivalent],
    *,
    recipe_digests: Mapping[str, str] | None = None,
) -> tuple[IngestedSatisfaction, ...]:
    """Catalog entries the trusted base lets a current-Snapshot result satisfy.

    ``recipe_digests`` (entry id -> recipe digest) is supplied by the
    check-recipe package when it has one; it pins ``lemoncrow_verification``
    equivalents only, because a provider CI result never carries that digest
    (its binding is the trusted-base declaration plus ``config_digest``).
    """

    if not equivalents or not _snapshot_reusable(revision):
        return ()
    current = _current_typed(evidence, revision)
    out: dict[str, IngestedSatisfaction] = {}
    for equivalent in equivalents:
        if equivalent.recipe in out:
            continue
        candidates = sorted(
            (
                item
                for key, item in current.items()
                if item.attestation_class == equivalent.attestation_class
                and item.stable_key.strip() == equivalent.check
                and item.provider == equivalent.provider
                and (not equivalent.integration or item.integration_id == equivalent.integration)
            ),
            key=lambda row: (row.created_at, row.id),
            reverse=True,
        )
        for item in candidates:
            observed = equivalence_key_of(item)
            required = EquivalenceKey(
                tool=tool_identity(item),
                recipe=equivalent.recipe,
                head_sha=revision.head_sha,
                config_digest=equivalent.config_digest,
                recipe_digest=(
                    str((recipe_digests or {}).get(equivalent.recipe) or "")
                    if equivalent.attestation_class == "lemoncrow_verification"
                    else ""
                ),
            )
            if item.verification_status not in {"PASS", "FAIL"} or observed is None:
                continue
            if not observed.satisfies(required):
                continue
            from lemoncrow.pro.capabilities.review.verification import check_key

            out[equivalent.recipe] = IngestedSatisfaction(
                entry_id=equivalent.recipe,
                evidence_id=item.id,
                check_key=check_key(item),
                attestation_class=item.attestation_class,
                status=item.verification_status,
                head_sha=item.provider_revision,
                equivalence=required.canonical(),
                title=item.title,
                observed_at=item.observed_at or item.created_at,
                url=item.url,
            )
            break
    return tuple(out[key] for key in sorted(out))


# --------------------------------------------------------------------------- #
# ingestion
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IngestAuthority:
    """Who vouches for a result. Set by the composition from the channel, never the payload."""

    attestation_class: str
    source: ReviewEvidenceSource
    provider: str = ""
    integration_id: str = ""
    attested_by: str = ""
    executor_class: str = ""

    @classmethod
    def caller_attested(cls, *, source: ReviewEvidenceSource, actor: str) -> IngestAuthority:
        """An upload through the Review API: a human observation, or a caller-attested run.

        Neither is typed authority: it supports review and is never counted as
        provider CI, LemonCrow verification or a feed attestation.
        """

        if source == "human":
            return cls(attestation_class="human_observation", source="human", attested_by=actor)
        return cls(attestation_class="", source=source, executor_class="author_machine")

    @property
    def typed(self) -> bool:
        return self.attestation_class in _TYPED_CLASSES


@dataclass(frozen=True, slots=True)
class CheckIngest:
    """One normalized result of any check category, before it is bound to a Snapshot."""

    tool: str
    category: CheckCategory
    status: str
    head_sha: str
    title: str
    stable_key: str
    tool_version: str = ""
    config_digest: str = ""
    recipe: str = ""
    # Opaque R5 recipe digest when the producer ran a check recipe; never computed here.
    recipe_digest: str = ""
    remote_identity: str = ""
    url: str = ""
    format: IngestFormat = "check"
    status_basis: str = ""
    findings: tuple[ImportedFinding, ...] = ()
    truncation: Truncation = field(default_factory=Truncation)
    observed_at: str = ""
    #: Canonical JSON of the signed envelope and the key that verified it (``in_toto`` only).
    attestation: str = ""

    def __post_init__(self) -> None:
        if self.category not in CHECK_CATEGORIES:
            raise IngestRefused(f"unknown check category {self.category!r}")
        if self.status not in _STATUSES:
            raise IngestRefused("status must be PASS, FAIL, NOT_RUN or UNKNOWN")
        if not self.tool.strip() or not self.title.strip() or not self.stable_key.strip():
            raise IngestRefused("tool, title and stable_key are required")
        if self.head_sha and not _SHA.fullmatch(self.head_sha):
            raise IngestRefused("head_sha must be a hexadecimal commit id")

    def equivalence(self, authority: IngestAuthority) -> EquivalenceKey:
        return EquivalenceKey(
            tool=":".join((authority.provider, authority.integration_id, self.stable_key.strip())),
            recipe=self.recipe,
            head_sha=self.head_sha,
            config_digest=self.config_digest,
            recipe_digest=self.recipe_digest,
        )

    def document(self, authority: IngestAuthority) -> dict[str, Any]:
        extra = {"attestation": json.loads(self.attestation)} if self.attestation else {}
        return {
            **extra,
            "schema": INGEST_SCHEMA,
            "format": self.format,
            "tool": {"name": self.tool, "version": self.tool_version},
            "category": self.category,
            "status": self.status,
            "status_basis": self.status_basis,
            "head_sha": self.head_sha,
            "config_digest": self.config_digest,
            "recipe": self.recipe,
            "remote_identity": self.remote_identity,
            "equivalence": self.equivalence(authority).to_wire(),
            "findings": [item.to_wire() for item in self.findings],
            "truncation": self.truncation.to_wire(),
        }

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for item in self.findings:
            counts[item.severity] = counts.get(item.severity, 0) + 1
        parts = [f"{self.tool}{(' ' + self.tool_version) if self.tool_version else ''} · {self.category}"]
        if self.findings or self.truncation.total:
            parts.append(
                f"{self.truncation.total or len(self.findings)} finding(s)"
                + (
                    " ("
                    + ", ".join(
                        f"{counts[key]} {SEVERITY_LABELS[key]}" for key in FINDING_SEVERITIES if counts.get(key)
                    )
                    + ")"
                    if counts
                    else ""
                )
            )
        note = self.truncation.note()
        if note:
            parts.append(note)
        return " · ".join(parts)[:2000]


@dataclass(frozen=True, slots=True)
class IngestDocument:
    """An ingestion artifact read back from its evidence row."""

    evidence: ReviewEvidence
    format: str
    tool: str
    tool_version: str
    category: str
    status_basis: str
    head_sha: str
    config_digest: str
    recipe: str
    findings: tuple[ImportedFinding, ...]
    truncation: Truncation


def is_ingest_row(evidence: ReviewEvidence) -> bool:
    return evidence.mime_type == INGEST_MIME and bool(evidence.artifact_path)


#: Parsed artifacts by content hash (immutable bytes), so read paths that run on
#: every projection (triage, static findings) do not re-read and re-parse them.
_LOADED: OrderedDict[tuple[str, str], IngestDocument] = OrderedDict()
_MAX_LOADED: Final[int] = 64


def load_ingest(store: Any, evidence: ReviewEvidence) -> IngestDocument | None:
    """Read an ingestion artifact; ``None`` for other rows or an unreadable/foreign artifact."""

    if not is_ingest_row(evidence):
        return None
    cache_key = (evidence.id, evidence.content_hash)
    cached = _LOADED.get(cache_key) if evidence.content_hash else None
    if cached is not None:
        _LOADED.move_to_end(cache_key)
        return replace(cached, evidence=evidence)
    document = _read_ingest(store, evidence)
    if document is not None and evidence.content_hash:
        _LOADED[cache_key] = document
        while len(_LOADED) > _MAX_LOADED:
            _LOADED.popitem(last=False)
    return document


def _read_ingest(store: Any, evidence: ReviewEvidence) -> IngestDocument | None:
    payload = store.read_evidence_artifact(evidence)
    if payload is None or (evidence.content_hash and hashlib.sha256(payload).hexdigest() != evidence.content_hash):
        return None
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, Mapping) or raw.get("schema") != INGEST_SCHEMA:
        return None
    tool = _object(raw.get("tool"))
    findings = tuple(
        item
        for item in (ImportedFinding.from_wire(row) for row in _list(raw.get("findings")) if isinstance(row, Mapping))
        if item is not None
    )
    return IngestDocument(
        evidence=evidence,
        format=str(raw.get("format") or "check"),
        tool=str(tool.get("name") or ""),
        tool_version=str(tool.get("version") or ""),
        category=str(raw.get("category") or "ci"),
        status_basis=str(raw.get("status_basis") or ""),
        head_sha=str(raw.get("head_sha") or ""),
        config_digest=str(raw.get("config_digest") or ""),
        recipe=str(raw.get("recipe") or ""),
        findings=findings,
        truncation=Truncation.from_wire(_object(raw.get("truncation"))),
    )


def ingest_payload(ingest: CheckIngest, authority: IngestAuthority) -> bytes:
    """The canonical artifact bytes for one ingestion (read back by :func:`load_ingest`)."""

    return _canonical(ingest.document(authority))


def record_check_ingest(
    store: Any,
    revision: ReviewRevision,
    ingest: CheckIngest,
    authority: IngestAuthority,
    *,
    expected_current_revision_id: str = "",
) -> tuple[ReviewEvidence, bool]:
    """Bind one normalized result to its exact Snapshot. Returns ``(row, created)``.

    The result's head SHA must be the Snapshot's (a result for another commit
    is refused, never re-labelled). A replay of the same observation (same
    tool, run, status and findings) returns the existing row: one immutable
    row per observation, so a rerun or completion appends a newer observation
    and readers count only the newest per check identity.

    A ``signed_attestation`` observation is its signed content only: the
    verified payload digest and the verifying key's thumbprint. Nothing the
    poster can change without re-signing (title, category, the envelope's
    unsigned bytes) makes a replayed older result a new, newer row.
    """

    if ingest.head_sha != revision.head_sha:
        raise IngestConflict(
            f"result head {ingest.head_sha[:12] or '(none)'} is not this Snapshot's head "
            f"{revision.head_sha[:12] or '(none)'}; bind it to the Snapshot it ran on"
        )
    if authority.typed and not _snapshot_reusable(revision):
        raise IngestConflict("a typed attestation needs a clean commit Snapshot with a head SHA")
    document = ingest.document(authority)
    payload = ingest_payload(ingest, authority)
    identity = [authority.attestation_class, authority.provider, authority.integration_id]
    payload_digest = ""
    if authority.attestation_class == "signed_attestation":
        signed = _object(json.loads(ingest.attestation) if ingest.attestation else None)
        payload_digest = str(signed.get("payload_digest") or "")
        if not payload_digest or signed.get("key_thumbprint") != authority.integration_id:
            raise IngestRefused("a signed attestation must carry its verified payload digest and key")
        observation = _digest({"authority": identity, "signed_payload": payload_digest})
    else:
        observation = _digest({"authority": identity, "document": document})
    source_ref = f"ingest:{observation[:32]}"
    existing = store.get_evidence_by_source(revision.review_id, authority.source, source_ref)
    if existing is None and authority.attestation_class == "signed_attestation":
        # A row stored before signed dedupe keyed it by the whole document: the same signed
        # payload and key is still that observation. lc-debt: one evidence scan per signed post;
        # drop once no Review holds a pre-dedupe signed row (or index remote_identity).
        existing = next(
            (
                row
                for row in store.list_evidence(revision.review_id)
                if row.attestation_class == "signed_attestation"
                and row.integration_id == authority.integration_id
                and row.remote_identity == payload_digest
            ),
            None,
        )
    if existing is not None:
        return existing, False
    from lemoncrow.pro.capabilities.review.store import new_evidence_id

    evidence_id = new_evidence_id()
    rel, digest, size = store.write_evidence_artifact(revision.review_id, revision.id, evidence_id, payload)
    typed = authority.typed
    try:
        record = store.add_evidence(
            ReviewEvidence(
                id=evidence_id,
                review_id=revision.review_id,
                revision_id=revision.id,
                kind="verification",
                title=_bounded(ingest.title, 160),
                artifact_path=rel,
                url=ingest.url if ingest.url.startswith("https://") else "",
                content_hash=digest,
                mime_type=INGEST_MIME,
                bytes=size,
                source=authority.source,
                source_ref=source_ref,
                verification_status=ingest.status,
                detail=ingest.summary(),
                attestation_class=authority.attestation_class,
                stable_key=_bounded(ingest.stable_key, 160) if typed or authority.source == "human" else "",
                provider=authority.provider if typed else "",
                integration_id=authority.integration_id if typed else "",
                provider_revision=ingest.head_sha,
                attestation_scope=ingest.equivalence(authority).canonical() if typed else "",
                attested_by=authority.attested_by,
                remote_identity=_bounded(ingest.remote_identity, 160),
                observed_at=ingest.observed_at,
                executor_class=authority.executor_class,
            ),
            expected_current_revision_id=expected_current_revision_id,
        )
    except ValueError:
        store.discard_artifact(rel)
        raise
    return record, True


def sarif_ingest(
    payload: bytes,
    *,
    head_sha: str,
    category: str,
    title: str = "",
    stable_key: str = "",
    status: str = "",
    config_digest: str = "",
    recipe: str = "",
    recipe_digest: str = "",
    remote_identity: str = "",
    url: str = "",
    snapshot_paths: Sequence[str] = (),
    observed_at: str = "",
) -> CheckIngest:
    """A SARIF upload as a :class:`CheckIngest`.

    ``status`` (an authoritative check conclusion) overrides the structural
    derivation; it is never read from the SARIF text.
    """

    parsed = parse_sarif(payload, snapshot_paths=snapshot_paths)
    explicit = status.upper().strip()
    return CheckIngest(
        tool=parsed.tool,
        tool_version=parsed.tool_version,
        category=cast(CheckCategory, category),
        status=explicit or parsed.derived_status,
        status_basis="reported conclusion" if explicit else parsed.status_basis,
        head_sha=head_sha,
        title=title.strip() or parsed.tool,
        stable_key=stable_key.strip() or title.strip() or parsed.tool,
        config_digest=config_digest,
        recipe=recipe,
        recipe_digest=recipe_digest,
        remote_identity=remote_identity,
        url=url,
        format="sarif",
        findings=parsed.findings,
        truncation=parsed.truncation,
        observed_at=observed_at,
    )


__all__ = [
    "CHECK_CATEGORIES",
    "CI_EQUIVALENCE_FLAG",
    "INGEST_MIME",
    "MAX_FINDINGS",
    "MAX_INGEST_BYTES",
    "MAX_MESSAGE_CHARS",
    "SATISFYING_CLASSES",
    "CheckCategory",
    "CheckIngest",
    "CiEquivalent",
    "EquivalenceKey",
    "ImportedFinding",
    "IngestAuthority",
    "IngestConflict",
    "IngestDocument",
    "IngestRefused",
    "IngestedSatisfaction",
    "SarifImport",
    "Truncation",
    "ci_equivalence_enabled",
    "display_severity",
    "equivalence_key_of",
    "explain_equivalent",
    "find_equivalent",
    "finding_fingerprint",
    "ingest_payload",
    "ingested_satisfactions",
    "is_ingest_row",
    "load_ingest",
    "normalize_location",
    "parse_check_annotations",
    "parse_ci_equivalents",
    "parse_sarif",
    "record_check_ingest",
    "sarif_ingest",
    "tool_identity",
]
