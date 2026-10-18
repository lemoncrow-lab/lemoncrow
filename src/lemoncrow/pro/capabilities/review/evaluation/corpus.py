"""Evaluation corpus: past Changes with known defects and known-clean Changes.

A corpus is a directory::

    corpus.yaml          # manifest: id, visibility, license, case ids
    cases/<id>.yaml      # one Change: base/head file texts and expectations
    fixtures/<id>.json   # recorded model outputs for that case (see fixtures.py)

File texts are embedded in the case YAML (never loose source files), so corpus
content is never linted, type-checked or imported as repository code.

Visibility decides who may load a corpus:

* ``public`` -- the built-in set checked into this repository. Every case is
  ``synthetic`` or from a permissively licensed ``public_repository`` (URL,
  commit and license recorded per case).
* ``internal`` -- internal repositories; no opt-in, never customer data.
* ``organization`` -- customer Reviews. Loading requires the caller's explicit
  opt-in naming the organization, *and* an opt-in record in the manifest naming
  the same organization, approver, date and reference. An organization corpus
  is never bundled: it is refused when it lives inside the repository checkout
  or the installed package.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, cast

import yaml

CorpusVisibility = Literal["public", "internal", "organization"]
CaseKind = Literal["defect", "clean"]
ProvenanceSource = Literal["synthetic", "public_repository", "internal_repository", "organization_review"]

CORPUS_MANIFEST: Final[str] = "corpus.yaml"
CORPUS_VERSION: Final[int] = 1
REVIEW_CONFIG_PATH: Final[str] = ".lemoncrow/review.yaml"
DEFAULT_LINE_TOLERANCE: Final[int] = 3
#: Repository-relative location of the built-in public corpus.
BUILTIN_CORPUS_RELATIVE: Final[str] = "evals/review/corpus/builtin"

VISIBILITIES: Final[tuple[str, ...]] = ("public", "internal", "organization")
PROVENANCE_SOURCES: Final[tuple[str, ...]] = (
    "synthetic",
    "public_repository",
    "internal_repository",
    "organization_review",
)
#: Provenance each visibility may carry: customer Reviews only in an opted-in
#: organization corpus, internal repositories never in the public set.
_ALLOWED_PROVENANCE: Final[Mapping[str, frozenset[str]]] = {
    "public": frozenset({"synthetic", "public_repository"}),
    "internal": frozenset({"synthetic", "public_repository", "internal_repository"}),
    "organization": frozenset(PROVENANCE_SOURCES),
}
#: Licenses accepted for a public-repository case in the public corpus.
PERMISSIVE_LICENSES: Final[frozenset[str]] = frozenset(
    {"Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense", "CC0-1.0"}
)
FINDING_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "correctness",
        "security",
        "api_compatibility",
        "concurrency_state",
        "persistence_migration",
        "performance",
        "verification",
        "critical_rule",
    }
)
SEVERITIES: Final[frozenset[str]] = frozenset({"p0", "p1", "p2", "suggestion"})
BEHAVIOR_CHANGES: Final[frozenset[str]] = frozenset({"added", "changed", "removed"})

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_MAX_FILES = 64
_MAX_TEXT = 256 * 1024
_MAX_CASES = 500


class CorpusError(ValueError):
    """A corpus that cannot be loaded; ``problems`` lists every reason found."""

    def __init__(self, message: str, problems: Sequence[str] = ()) -> None:
        self.problems = tuple(problems)
        detail = "" if not self.problems else ":\n  - " + "\n  - ".join(self.problems)
        super().__init__(message + detail)


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class Provenance:
    source: ProvenanceSource
    note: str
    repository: str = ""
    commit: str = ""
    author: str = ""

    def to_wire(self) -> dict[str, str]:
        out = {"source": self.source, "note": self.note}
        for key in ("repository", "commit", "author"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out


@dataclass(frozen=True, slots=True)
class ExpectedFinding:
    """A known defect: where it is, what kind it is, how severe."""

    id: str
    path: str
    start_line: int
    end_line: int
    categories: tuple[str, ...]
    severity: str | None = None
    sources: tuple[str, ...] = ()
    line_tolerance: int | None = None
    note: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "lines": [self.start_line, self.end_line],
            "categories": list(self.categories),
            "severity": self.severity,
            "sources": list(self.sources),
            "line_tolerance": self.line_tolerance,
        }


@dataclass(frozen=True, slots=True)
class ExpectedBehavior:
    """A known behavior delta (side effect, verification weakening, ...).

    ``subject`` is an ``fnmatch`` pattern over the delta's stable subject.
    ``selections`` limits an expectation to selections where its owning
    capability can produce evidence; empty preserves the legacy all-selection
    behavior for every existing corpus expectation.
    """

    id: str
    kind: str
    subject: str
    change: str | None = None
    selections: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "subject": self.subject,
            "change": self.change,
            "selections": list(self.selections),
        }


@dataclass(frozen=True, slots=True)
class ScriptedConfirmation:
    """One finding the synthetic reviewer confirms when an investigation covers it.

    Used only by ``record --synthetic`` to author fixtures without a model; it
    is never consulted during replay.
    """

    path: str
    category: str
    title: str
    explanation: str
    consequence: str


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    title: str
    kind: CaseKind
    provenance: Provenance
    license: str
    base: Mapping[str, str]
    head: Mapping[str, str | None]
    review_config: str | None
    expected_findings: tuple[ExpectedFinding, ...]
    expected_behavior: tuple[ExpectedBehavior, ...]
    synthetic_reviewer: tuple[ScriptedConfirmation, ...]
    execution: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    document: Mapping[str, Any] = field(repr=False, compare=False, default_factory=dict)

    def input_digest(self) -> str:
        """Identity of what replay consumes; fixtures recorded on other inputs are stale."""

        document: dict[str, Any] = {
            "base": dict(sorted(self.base.items())),
            "head": dict(sorted(self.head.items())),
            "review_config": self.review_config,
        }
        # Legacy cases keep their existing model-fixture identity. Execution-aware
        # cases bind both fixture kinds to the authored execution inputs.
        if self.execution:
            document["execution"] = self.execution
        return digest(document)

    def digest(self) -> str:
        return digest(self.document)


@dataclass(frozen=True, slots=True)
class OrganizationOptIn:
    organization: str
    approved_by: str
    approved_at: str
    reference: str

    def to_wire(self) -> dict[str, str]:
        return {
            "organization": self.organization,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "reference": self.reference,
        }


@dataclass(frozen=True, slots=True)
class Corpus:
    id: str
    visibility: CorpusVisibility
    description: str
    license: str
    root: Path
    cases: tuple[EvalCase, ...]
    line_tolerance: int = DEFAULT_LINE_TOLERANCE
    organization: str = ""
    opt_in: OrganizationOptIn | None = None

    @property
    def fixtures_dir(self) -> Path:
        return self.root / "fixtures"

    def case(self, case_id: str) -> EvalCase:
        for case in self.cases:
            if case.id == case_id:
                return case
        raise KeyError(case_id)

    def cases_digest(self) -> str:
        return digest(
            {
                "id": self.id,
                "visibility": self.visibility,
                "line_tolerance": self.line_tolerance,
                "cases": {case.id: case.digest() for case in self.cases},
            }
        )

    def fixtures_digest(self) -> str:
        files: dict[str, str] = {}
        for case in self.cases:
            for suffix in (".json", ".execution.json"):
                path = self.fixtures_dir / f"{case.id}{suffix}"
                files[f"{case.id}{suffix}"] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        return digest(files)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "visibility": self.visibility,
            "license": self.license,
            "organization": self.organization or None,
            "cases": [
                {
                    "id": case.id,
                    "kind": case.kind,
                    "title": case.title,
                    "license": case.license,
                    "provenance": case.provenance.to_wire(),
                }
                for case in self.cases
            ],
            "cases_digest": self.cases_digest(),
            "fixtures_digest": self.fixtures_digest(),
        }


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def _str(raw: Mapping[str, Any], key: str, where: str, problems: list[str], *, required: bool = True) -> str:
    value = raw.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            problems.append(f"{where}: {key} is required")
        return ""
    if not isinstance(value, str):
        problems.append(f"{where}: {key} must be a string")
        return ""
    return value.strip()


def _relative_path(value: object, where: str, problems: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{where}: a file path must be a non-empty string")
        return ""
    text = value.strip()
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts or "\\" in text or (pure.parts and pure.parts[0] == ".git"):
        problems.append(f"{where}: {text!r} must be a relative path inside the repository")
        return ""
    return str(pure)


def _files(raw: object, where: str, problems: list[str], *, allow_delete: bool) -> dict[str, str | None]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        problems.append(f"{where} must map file paths to text")
        return {}
    if len(raw) > _MAX_FILES:
        problems.append(f"{where} has more than {_MAX_FILES} files")
    out: dict[str, str | None] = {}
    for key, value in raw.items():
        path = _relative_path(key, where, problems)
        if not path:
            continue
        if value is None:
            if not allow_delete:
                problems.append(f"{where}: {path} has no text")
                continue
            out[path] = None
            continue
        if not isinstance(value, str):
            problems.append(f"{where}: {path} must be text")
            continue
        if len(value.encode("utf-8")) > _MAX_TEXT:
            problems.append(f"{where}: {path} is larger than {_MAX_TEXT} bytes")
            continue
        out[path] = value
    return out


def _lines(raw: Mapping[str, Any], where: str, problems: list[str]) -> tuple[int, int]:
    if "line" in raw:
        value = raw.get("line")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value, value
        problems.append(f"{where}: line must be a positive integer")
        return 0, 0
    value = raw.get("lines")
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in value)
        and value[0] <= value[1]
    ):
        return int(value[0]), int(value[1])
    problems.append(f"{where}: lines must be [start, end] with 0 < start <= end (or line: N)")
    return 0, 0


def _expected_finding(raw: object, where: str, problems: list[str]) -> ExpectedFinding | None:
    if not isinstance(raw, Mapping):
        problems.append(f"{where} must be a mapping")
        return None
    finding_id = _str(raw, "id", where, problems)
    path = _relative_path(raw.get("path"), where, problems)
    start, end = _lines(raw, where, problems)
    categories_raw = raw.get("categories", [raw.get("category")] if raw.get("category") is not None else None)
    categories: tuple[str, ...] = ()
    if isinstance(categories_raw, list) and categories_raw:
        categories = tuple(str(item) for item in categories_raw)
        unknown = sorted(set(categories) - FINDING_CATEGORIES)
        if unknown:
            problems.append(f"{where}: unknown finding categories {unknown}")
    else:
        problems.append(f"{where}: category (or categories) is required")
    severity = raw.get("severity")
    if severity is not None and severity not in SEVERITIES:
        problems.append(f"{where}: severity must be one of {sorted(SEVERITIES)}")
        severity = None
    sources = raw.get("sources") or []
    if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
        problems.append(f"{where}: sources must be a list of finding sources")
        sources = []
    tolerance = raw.get("line_tolerance")
    if tolerance is not None and (not isinstance(tolerance, int) or isinstance(tolerance, bool) or tolerance < 0):
        problems.append(f"{where}: line_tolerance must be a non-negative integer")
        tolerance = None
    if not finding_id or not path or not start or not categories:
        return None
    return ExpectedFinding(
        id=finding_id,
        path=path,
        start_line=start,
        end_line=end,
        categories=categories,
        severity=cast(str | None, severity),
        sources=tuple(sources),
        line_tolerance=tolerance,
        note=str(raw.get("note") or ""),
    )


def _expected_behavior(raw: object, where: str, problems: list[str]) -> ExpectedBehavior | None:
    if not isinstance(raw, Mapping):
        problems.append(f"{where} must be a mapping")
        return None
    behavior_id = _str(raw, "id", where, problems)
    kind = _str(raw, "kind", where, problems)
    subject = _str(raw, "subject", where, problems)
    change = raw.get("change")
    if change is not None and change not in BEHAVIOR_CHANGES:
        problems.append(f"{where}: change must be one of {sorted(BEHAVIOR_CHANGES)}")
        change = None
    raw_selections = raw.get("selections")
    selections: tuple[str, ...] = ()
    if raw_selections is not None:
        if not isinstance(raw_selections, list) or not all(
            isinstance(item, str) and item.strip() for item in raw_selections
        ):
            problems.append(f"{where}: selections must be a list of non-empty selection labels")
        else:
            selections = tuple(dict.fromkeys(item.strip() for item in raw_selections))
    if not behavior_id or not kind or not subject:
        return None
    return ExpectedBehavior(
        id=behavior_id, kind=kind, subject=subject, change=cast(str | None, change), selections=selections
    )


def _provenance(raw: object, where: str, problems: list[str]) -> Provenance | None:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: provenance is required")
        return None
    source = raw.get("source")
    if source not in PROVENANCE_SOURCES:
        problems.append(f"{where}: provenance.source must be one of {list(PROVENANCE_SOURCES)}")
        return None
    note = _str(raw, "note", f"{where}: provenance", problems)
    repository = str(raw.get("repository") or "").strip()
    commit = str(raw.get("commit") or "").strip()
    if source in {"public_repository", "internal_repository"} and (not repository or not commit):
        problems.append(f"{where}: provenance from a repository needs repository and commit")
    return Provenance(
        source=cast(ProvenanceSource, source),
        note=note,
        repository=repository,
        commit=commit,
        author=str(raw.get("author") or "").strip(),
    )


def parse_case(raw: object, *, where: str, visibility: CorpusVisibility) -> tuple[EvalCase | None, list[str]]:
    problems: list[str] = []
    if not isinstance(raw, Mapping):
        return None, [f"{where} must be a mapping"]
    case_id = _str(raw, "id", where, problems)
    if case_id and not _ID.match(case_id):
        problems.append(f"{where}: id {case_id!r} must match {_ID.pattern}")
    kind = raw.get("kind")
    if kind not in {"defect", "clean"}:
        problems.append(f"{where}: kind must be defect or clean")
    provenance = _provenance(raw.get("provenance"), where, problems)
    license_id = _str(raw, "license", where, problems)
    if provenance is not None and provenance.source not in _ALLOWED_PROVENANCE[visibility]:
        problems.append(f"{where}: provenance {provenance.source} is not allowed in a {visibility} corpus")
    if (
        visibility == "public"
        and provenance is not None
        and provenance.source == "public_repository"
        and license_id not in PERMISSIVE_LICENSES
    ):
        problems.append(f"{where}: a public-repository case needs a permissive license, got {license_id!r}")
    base = {
        path: text
        for path, text in _files(raw.get("base"), f"{where}: base", problems, allow_delete=False).items()
        if text is not None
    }
    head = _files(raw.get("head"), f"{where}: head", problems, allow_delete=True)
    if not head:
        problems.append(f"{where}: head must change at least one file")
    for path, text in head.items():
        if text is None and path not in base:
            problems.append(f"{where}: head deletes {path}, which base does not have")
    review_config = raw.get("review_config")
    if review_config is not None and not isinstance(review_config, str):
        problems.append(f"{where}: review_config must be the text of {REVIEW_CONFIG_PATH}")
        review_config = None
    if REVIEW_CONFIG_PATH in base or REVIEW_CONFIG_PATH in head:
        problems.append(f"{where}: put the trusted-base Review config in review_config, not in base/head")
    expected = raw.get("expected") or {}
    if not isinstance(expected, Mapping):
        problems.append(f"{where}: expected must be a mapping")
        expected = {}
    findings = tuple(
        item
        for index, row in enumerate(expected.get("findings") or ())
        if (item := _expected_finding(row, f"{where}: expected.findings[{index}]", problems)) is not None
    )
    behavior = tuple(
        delta
        for index, row in enumerate(expected.get("behavior") or ())
        if (delta := _expected_behavior(row, f"{where}: expected.behavior[{index}]", problems)) is not None
    )
    ids = [finding.id for finding in findings] + [entry.id for entry in behavior]
    if len(ids) != len(set(ids)):
        problems.append(f"{where}: expectation ids must be unique within the case")
    if kind == "defect" and not findings and not behavior:
        problems.append(f"{where}: a defect case needs at least one expected finding or behavior delta")
    if kind == "clean" and findings:
        problems.append(f"{where}: a clean case cannot expect findings")
    for item in findings:
        if item.path not in head and item.path not in base:
            problems.append(f"{where}: expected finding {item.id} names {item.path}, which the case does not have")
    execution_raw = raw.get("execution") or {}
    execution: dict[str, Mapping[str, Any]] = {}
    if not isinstance(execution_raw, Mapping):
        problems.append(f"{where}: execution must map run-plan entry ids to old/new fixture results")
    else:
        for raw_entry, raw_sides in execution_raw.items():
            entry_id = str(raw_entry).strip()
            label = f"{where}: execution.{entry_id or '<empty>'}"
            if not entry_id or not _ID.match(entry_id):
                problems.append(f"{label}: entry id must match {_ID.pattern}")
                continue
            if not isinstance(raw_sides, Mapping) or not raw_sides:
                problems.append(f"{label} must map old/new/default to result mappings")
                continue
            unknown_sides = sorted(str(key) for key in raw_sides if str(key) not in {"old", "new", "default"})
            if unknown_sides:
                problems.append(f"{label}: unknown sides {unknown_sides}")
                continue
            sides: dict[str, Any] = {}
            for side, result in raw_sides.items():
                if not isinstance(result, Mapping):
                    problems.append(f"{label}.{side} must be a result mapping")
                    continue
                status = str(result.get("status") or "passed")
                if status not in {"passed", "failed", "unavailable"}:
                    problems.append(f"{label}.{side}.status must be passed, failed or unavailable")
                    continue
                sides[str(side)] = json.loads(canonical_json(dict(result)))
            if sides:
                execution[entry_id] = sides

    script_raw = raw.get("synthetic_reviewer") or {}
    confirmations: list[ScriptedConfirmation] = []
    if not isinstance(script_raw, Mapping):
        problems.append(f"{where}: synthetic_reviewer must be a mapping")
    else:
        for index, row in enumerate(script_raw.get("confirm") or ()):
            label = f"{where}: synthetic_reviewer.confirm[{index}]"
            if not isinstance(row, Mapping):
                problems.append(f"{label} must be a mapping")
                continue
            category = _str(row, "category", label, problems)
            if category and category not in FINDING_CATEGORIES:
                problems.append(f"{label}: unknown category {category!r}")
            confirmation = ScriptedConfirmation(
                path=_relative_path(row.get("path"), label, problems),
                category=category,
                title=_str(row, "title", label, problems),
                explanation=_str(row, "explanation", label, problems),
                consequence=_str(row, "consequence", label, problems),
            )
            confirmations.append(confirmation)
    if problems or provenance is None or not case_id:
        return None, problems
    return (
        EvalCase(
            id=case_id,
            title=_str(raw, "title", where, problems) or case_id,
            kind=cast(CaseKind, kind),
            provenance=provenance,
            license=license_id,
            base=base,
            head=head,
            review_config=review_config,
            expected_findings=findings,
            expected_behavior=behavior,
            synthetic_reviewer=tuple(confirmations),
            execution=execution,
            document=json.loads(canonical_json(dict(raw))),
        ),
        problems,
    )


def _inside(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def bundled_roots(repository_root: Path | None) -> tuple[Path, ...]:
    """Locations an organization corpus may never live in: this checkout and the installed package."""

    import lemoncrow

    roots = [Path(lemoncrow.__file__).resolve().parent]
    if repository_root is not None:
        roots.append(repository_root.resolve())
    return tuple(roots)


def _opt_in(raw: object, problems: list[str]) -> OrganizationOptIn | None:
    if not isinstance(raw, Mapping):
        problems.append(
            "an organization corpus needs an opt_in record (organization, approved_by, approved_at, reference)"
        )
        return None
    values = {
        key: str(raw.get(key) or "").strip() for key in ("organization", "approved_by", "approved_at", "reference")
    }
    missing = sorted(key for key, value in values.items() if not value)
    if missing:
        problems.append(f"organization opt_in record is missing {missing}")
        return None
    return OrganizationOptIn(**values)


def load_corpus(
    path: Path,
    *,
    organization_opt_in: str | None = None,
    repository_root: Path | None = None,
    require_visibility: CorpusVisibility | None = None,
    case_ids: Sequence[str] = (),
) -> Corpus:
    """Load and validate one corpus directory.

    *organization_opt_in* is the caller's explicit opt-in: the organization id
    it agrees to evaluate. *repository_root* is the checkout an organization
    corpus must not live in (``bundled_roots``).
    """

    root = path.resolve()
    manifest_path = root / CORPUS_MANIFEST
    if not manifest_path.is_file():
        raise CorpusError(f"no {CORPUS_MANIFEST} in {root}")
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CorpusError(f"{manifest_path}: {exc}") from exc
    if not isinstance(manifest, Mapping):
        raise CorpusError(f"{manifest_path} must be a mapping")
    problems: list[str] = []
    corpus_id = _str(manifest, "id", "corpus.yaml", problems)
    if manifest.get("version") != CORPUS_VERSION:
        problems.append(f"corpus.yaml: version must be {CORPUS_VERSION}")
    visibility = manifest.get("visibility")
    if visibility not in VISIBILITIES:
        raise CorpusError(f"{manifest_path}: visibility must be one of {list(VISIBILITIES)}")
    if require_visibility is not None and visibility != require_visibility:
        raise CorpusError(f"{manifest_path}: expected a {require_visibility} corpus, found {visibility}")
    organization = ""
    opt_in: OrganizationOptIn | None = None
    if visibility == "organization":
        organization = _str(manifest, "organization", "corpus.yaml", problems)
        if not organization_opt_in:
            raise CorpusError(
                f"{root} is an organization corpus ({organization or 'unnamed'}); "
                "loading it requires an explicit organization opt-in"
            )
        if organization_opt_in != organization:
            raise CorpusError(
                f"the opt-in names organization {organization_opt_in!r}, but {root} belongs to {organization!r}"
            )
        opt_in = _opt_in(manifest.get("opt_in"), problems)
        if opt_in is not None and opt_in.organization != organization:
            problems.append(f"opt_in record names {opt_in.organization!r}, not the corpus organization")
        for bundled in bundled_roots(repository_root):
            if _inside(root, bundled):
                raise CorpusError(
                    f"organization corpora are never bundled: {root} lives inside {bundled}; keep it outside "
                    "the repository checkout and the installed package"
                )
    tolerance = manifest.get("line_tolerance", DEFAULT_LINE_TOLERANCE)
    if not isinstance(tolerance, int) or isinstance(tolerance, bool) or tolerance < 0:
        problems.append("corpus.yaml: line_tolerance must be a non-negative integer")
        tolerance = DEFAULT_LINE_TOLERANCE
    listed = manifest.get("cases")
    if not isinstance(listed, list) or not listed or not all(isinstance(item, str) for item in listed):
        problems.append("corpus.yaml: cases must list case ids")
        listed = []
    if len(listed) > _MAX_CASES:
        problems.append(f"corpus.yaml: more than {_MAX_CASES} cases")
    if len(set(listed)) != len(listed):
        problems.append("corpus.yaml: case ids must be unique")
    wanted = set(case_ids)
    unknown = sorted(wanted - set(listed))
    if unknown:
        problems.append(f"unknown case ids {unknown}")
    cases: list[EvalCase] = []
    for case_id in listed:
        if wanted and case_id not in wanted:
            continue
        case_path = root / "cases" / f"{case_id}.yaml"
        where = f"cases/{case_id}.yaml"
        if not case_path.is_file():
            problems.append(f"{where} is missing")
            continue
        try:
            raw = yaml.safe_load(case_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            problems.append(f"{where}: {exc}")
            continue
        case, case_problems = parse_case(raw, where=where, visibility=cast(CorpusVisibility, visibility))
        problems.extend(case_problems)
        if case is not None:
            if case.id != case_id:
                problems.append(f"{where}: id {case.id!r} does not match its file name")
            cases.append(case)
    if problems:
        raise CorpusError(f"corpus {root} is invalid", problems)
    return Corpus(
        id=corpus_id,
        visibility=cast(CorpusVisibility, visibility),
        description=str(manifest.get("description") or ""),
        license=str(manifest.get("license") or ""),
        root=root,
        cases=tuple(cases),
        line_tolerance=int(tolerance),
        organization=organization,
        opt_in=opt_in,
    )


def builtin_corpus_path(repository_root: Path) -> Path:
    return repository_root / BUILTIN_CORPUS_RELATIVE


def load_builtin_corpus(repository_root: Path, *, case_ids: Sequence[str] = ()) -> Corpus:
    """The checked-in public corpus; anything but ``public`` visibility is refused."""

    return load_corpus(
        builtin_corpus_path(repository_root),
        repository_root=repository_root,
        require_visibility="public",
        case_ids=case_ids,
    )


__all__ = [
    "BUILTIN_CORPUS_RELATIVE",
    "CORPUS_MANIFEST",
    "DEFAULT_LINE_TOLERANCE",
    "REVIEW_CONFIG_PATH",
    "Corpus",
    "CorpusError",
    "EvalCase",
    "ExpectedBehavior",
    "ExpectedFinding",
    "OrganizationOptIn",
    "Provenance",
    "ScriptedConfirmation",
    "builtin_corpus_path",
    "bundled_roots",
    "canonical_json",
    "digest",
    "load_builtin_corpus",
    "load_corpus",
    "parse_case",
]
