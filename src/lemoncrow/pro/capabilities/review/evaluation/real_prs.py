"""The real-PR benchmark corpus (G3): pinned pull requests, with ground truth only where a fix commit proves it.

Gate I replays a *synthetic* corpus offline. This corpus is the other kind: real pull requests from real
repositories, pinned by commit, that a live model reviews at each depth so tokens, dollars, catch rate and noise can
be read against what actually happened afterwards. It reuses Gate I's vocabulary (``provenance.source:
public_repository``, permissive licences, the organization opt-in and the "never inside the checkout" rule for
customer data) and none of its replay machinery, because a real pull request is a git range, not a bundle of files.

A **label** is a bug a later commit fixed. It is not an opinion. Each label names the fix commit that proves it and the
commit that introduced the bug, and its ``summary`` is that fix commit's subject, copied. Where it points is derived
from git (the lines the fix edited, mapped back to the reviewed head), never written by hand. A pull request with no
labels is *unlabelled*: it still yields cost and finding counts, but no catch or noise rate, because "every finding is
noise" is false when nobody knows what is in the pull request. A reviewed pull request known to be clean must say so
(``labelled: true`` with no labels).

Format::

    version: 1
    id: real-prs-public
    visibility: public            # public | private (not mirrored; any licence) | organization (a customer's PRs: opt-in)
    line_tolerance: 3
    match: either                 # location | symbol | either
    prs:
      - id: black-3564
        repository: psf/black
        pull: 3564
        title: ...
        license: MIT
        provenance: {source: public_repository, repository: https://github.com/psf/black, commit: <head>, note: ...}
        base_sha: <merge base>
        head_sha: <the commit that introduced the bug>
        labels:
          - id: previous-defs-stale-use
            summary: <the fix commit's subject>
            symbols: [previous_defs]
            regions: [{path: src/black/lines.py, lines: [601, 601], role: fix_site}]
            evidence: {fix_commit: <sha>, fix_url: ..., introduced_by: <sha>, attribution: fix_message}
        pushes: [{id: apply-fix, apply_commit: <fix sha>, apply_paths: [src/black/lines.py]}]
        baseline: {comments_file: baseline/black-3564.json}
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast

import yaml

from lemoncrow.pro.capabilities.review.evaluation.corpus import (
    PERMISSIVE_LICENSES,
    PROVENANCE_SOURCES,
    CorpusError,
    CorpusVisibility,
    OrganizationOptIn,
    Provenance,
    bundled_roots,
    digest,
)

CORPUS_VERSION: Final[int] = 1
DEFAULT_LINE_TOLERANCE: Final[int] = 3
#: How a finding may catch a label. ``location``: it is attached near a line the fix edited. ``symbol``: it names an
#: identifier the fix edited. ``either`` (the default): one of the two; the report says which one fired.
MATCH_MODES: Final[tuple[str, ...]] = ("location", "symbol", "either")
MatchMode = Literal["location", "symbol", "either"]
ATTRIBUTIONS: Final[tuple[str, ...]] = ("fix_message", "blame")
REGION_ROLES: Final[tuple[str, ...]] = ("fix_site", "introduced")
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SLUG = re.compile(r"^[\w.-]+/[\w.-]+$")
_MAX_PRS = 500


@dataclass(frozen=True, slots=True)
class Region:
    path: str
    start_line: int
    end_line: int
    role: str = "fix_site"

    def to_wire(self) -> dict[str, Any]:
        return {"path": self.path, "lines": [self.start_line, self.end_line], "role": self.role}


@dataclass(frozen=True, slots=True)
class LabelEvidence:
    fix_commit: str
    fix_url: str = ""
    introduced_by: str = ""
    attribution: str = "blame"
    derivation: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "fix_commit": self.fix_commit,
            "fix_url": self.fix_url,
            "introduced_by": self.introduced_by,
            "attribution": self.attribution,
            "derivation": self.derivation,
        }


@dataclass(frozen=True, slots=True)
class Label:
    id: str
    summary: str
    regions: tuple[Region, ...]
    evidence: LabelEvidence
    symbols: tuple[str, ...] = ()
    line_tolerance: int | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "summary": self.summary,
            "regions": [region.to_wire() for region in self.regions],
            "symbols": list(self.symbols),
            "evidence": self.evidence.to_wire(),
            **({"line_tolerance": self.line_tolerance} if self.line_tolerance is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class PushSpec:
    """A simulated follow-up push: a pinned head, or a commit to apply on top of the reviewed head."""

    id: str
    head_sha: str = ""
    apply_commit: str = ""
    apply_paths: tuple[str, ...] = ()
    note: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "head_sha": self.head_sha,
            "apply_commit": self.apply_commit,
            "apply_paths": list(self.apply_paths),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class BaselineSource:
    """Where the baseline reviewer's comments on this pull request come from."""

    comments_file: str = ""
    fetch: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {"comments_file": self.comments_file, "fetch": self.fetch}


@dataclass(frozen=True, slots=True)
class RealPr:
    id: str
    repository: str
    clone_url: str
    pull: int
    title: str
    license: str
    provenance: Provenance
    base_sha: str
    head_sha: str
    labels: tuple[Label, ...] = ()
    labelled: bool = False
    pushes: tuple[PushSpec, ...] = ()
    baseline: BaselineSource | None = None
    notes: str = ""

    @property
    def slug(self) -> str:
        return self.repository.replace("/", "__")

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repository}/pull/{self.pull}" if self.pull else ""

    def inputs(self) -> dict[str, Any]:
        """What a recorded model output depends on: the pinned git inputs, never the labels or baseline files."""

        return {
            "repository": self.repository,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "pushes": [push.to_wire() for push in self.pushes],
        }

    def input_digest(self) -> str:
        return digest(self.inputs())

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository": self.repository,
            "pull": self.pull,
            "url": self.url,
            "title": self.title,
            "license": self.license,
            "provenance": self.provenance.to_wire(),
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "labelled": self.labelled,
            "labels": [label.to_wire() for label in self.labels],
            "pushes": [push.to_wire() for push in self.pushes],
            "baseline": self.baseline.to_wire() if self.baseline is not None else None,
        }


@dataclass(frozen=True, slots=True)
class RealPrCorpus:
    id: str
    visibility: CorpusVisibility
    description: str
    path: Path
    prs: tuple[RealPr, ...]
    line_tolerance: int = DEFAULT_LINE_TOLERANCE
    match: MatchMode = "either"
    organization: str = ""
    opt_in: OrganizationOptIn | None = None

    @property
    def root(self) -> Path:
        return self.path.parent

    def pr(self, pr_id: str) -> RealPr:
        for item in self.prs:
            if item.id == pr_id:
                return item
        raise KeyError(pr_id)

    def digest(self) -> str:
        return digest(
            {
                "id": self.id,
                "visibility": self.visibility,
                "line_tolerance": self.line_tolerance,
                "match": self.match,
                "prs": [pr.to_wire() for pr in self.prs],
            }
        )

    def summary(self) -> dict[str, Any]:
        labelled = [pr for pr in self.prs if pr.labelled]
        return {
            "id": self.id,
            "visibility": self.visibility,
            "organization": self.organization or None,
            "path": self.path.name,
            "digest": self.digest(),
            "prs": len(self.prs),
            "labelled_prs": len(labelled),
            "labels": sum(len(pr.labels) for pr in self.prs),
            "line_tolerance": self.line_tolerance,
            "match": self.match,
        }


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def _text(raw: Mapping[str, Any], key: str, where: str, problems: list[str], *, required: bool = True) -> str:
    value = raw.get(key)
    if value is None or value == "":
        if required:
            problems.append(f"{where}: {key} is required")
        return ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        problems.append(f"{where}: {key} must be text")
        return ""
    return str(value).strip()


def _sha(raw: Mapping[str, Any], key: str, where: str, problems: list[str], *, required: bool = True) -> str:
    value = _text(raw, key, where, problems, required=required)
    if value and not _SHA.fullmatch(value):
        problems.append(f"{where}: {key} must be a full 40-character commit sha")
        return ""
    return value


def _regions(raw: object, where: str, problems: list[str]) -> tuple[Region, ...]:
    if not isinstance(raw, list) or not raw:
        problems.append(f"{where}: regions must list at least one region")
        return ()
    out: list[Region] = []
    for index, item in enumerate(raw):
        at = f"{where}.regions[{index}]"
        if not isinstance(item, Mapping):
            problems.append(f"{at}: must be a mapping")
            continue
        path = _text(item, "path", at, problems)
        lines = item.get("lines")
        role = str(item.get("role") or "fix_site")
        if role not in REGION_ROLES:
            problems.append(f"{at}: role must be one of {list(REGION_ROLES)}")
        if (
            not isinstance(lines, list)
            or len(lines) != 2
            or not all(isinstance(n, int) and not isinstance(n, bool) and n >= 1 for n in lines)
            or lines[1] < lines[0]
        ):
            problems.append(f"{at}: lines must be [start, end] with 1 <= start <= end")
            continue
        if path.startswith("/") or ".." in Path(path).parts:
            problems.append(f"{at}: path must be repository-relative")
            continue
        out.append(Region(path, int(lines[0]), int(lines[1]), role))
    return tuple(out)


def _label(raw: object, where: str, problems: list[str]) -> Label | None:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: must be a mapping")
        return None
    label_id = _text(raw, "id", where, problems)
    if label_id and not _ID.fullmatch(label_id):
        problems.append(f"{where}: id {label_id!r} is not a valid id")
    summary = _text(raw, "summary", where, problems)
    evidence_raw = raw.get("evidence")
    if not isinstance(evidence_raw, Mapping):
        problems.append(
            f"{where}: a label needs an evidence block naming the fix commit that proves it; ground truth is never invented"
        )
        return None
    fix = _sha(evidence_raw, "fix_commit", f"{where}.evidence", problems)
    introduced = _sha(evidence_raw, "introduced_by", f"{where}.evidence", problems, required=False)
    attribution = str(evidence_raw.get("attribution") or "blame")
    if attribution not in ATTRIBUTIONS:
        problems.append(f"{where}.evidence: attribution must be one of {list(ATTRIBUTIONS)}")
    regions = _regions(raw.get("regions"), where, problems)
    symbols_raw = raw.get("symbols") or []
    if not isinstance(symbols_raw, list) or not all(isinstance(item, str) and item.strip() for item in symbols_raw):
        problems.append(f"{where}: symbols must be a list of identifiers")
        symbols_raw = []
    tolerance = raw.get("line_tolerance")
    if tolerance is not None and (not isinstance(tolerance, int) or isinstance(tolerance, bool) or tolerance < 0):
        problems.append(f"{where}: line_tolerance must be a non-negative integer")
        tolerance = None
    if not label_id or not summary or not fix or not regions:
        return None
    return Label(
        id=label_id,
        summary=summary,
        regions=regions,
        evidence=LabelEvidence(
            fix_commit=fix,
            fix_url=str(evidence_raw.get("fix_url") or ""),
            introduced_by=introduced,
            attribution=attribution,
            derivation=str(evidence_raw.get("derivation") or ""),
        ),
        symbols=tuple(str(item).strip() for item in symbols_raw),
        line_tolerance=tolerance,
    )


def _push(raw: object, where: str, problems: list[str]) -> PushSpec | None:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: must be a mapping")
        return None
    push_id = _text(raw, "id", where, problems)
    head = _sha(raw, "head_sha", where, problems, required=False)
    apply = _sha(raw, "apply_commit", where, problems, required=False)
    if bool(head) == bool(apply):
        problems.append(f"{where}: name exactly one of head_sha (a pinned head) or apply_commit (applied to the head)")
    paths = raw.get("apply_paths") or []
    if not isinstance(paths, list) or not all(isinstance(item, str) and item for item in paths):
        problems.append(f"{where}: apply_paths must be a list of paths")
        paths = []
    if head and paths:
        problems.append(f"{where}: apply_paths goes with apply_commit, not head_sha")
    return PushSpec(push_id, head, apply, tuple(paths), str(raw.get("note") or "")) if push_id else None


def _baseline(raw: object, where: str, problems: list[str]) -> BaselineSource | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: baseline must be a mapping with comments_file and/or fetch")
        return None
    return BaselineSource(str(raw.get("comments_file") or ""), bool(raw.get("fetch", False)))


def _provenance(raw: object, where: str, problems: list[str]) -> Provenance | None:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: provenance is required")
        return None
    source = raw.get("source")
    if source not in PROVENANCE_SOURCES:
        problems.append(f"{where}: provenance.source must be one of {list(PROVENANCE_SOURCES)}")
        return None
    repository = str(raw.get("repository") or "").strip()
    commit = str(raw.get("commit") or "").strip()
    if source in {"public_repository", "internal_repository"} and (not repository or not commit):
        problems.append(f"{where}: provenance from a repository needs repository and commit")
    return Provenance(
        source=cast("Any", source),
        note=str(raw.get("note") or ""),
        repository=repository,
        commit=commit,
        author=str(raw.get("author") or "").strip(),
    )


def parse_pr(raw: object, *, where: str, visibility: CorpusVisibility) -> tuple[RealPr | None, list[str]]:
    problems: list[str] = []
    if not isinstance(raw, Mapping):
        return None, [f"{where}: must be a mapping"]
    pr_id = _text(raw, "id", where, problems)
    if pr_id and not _ID.fullmatch(pr_id):
        problems.append(f"{where}: id {pr_id!r} is not a valid id")
    where = f"{where} ({pr_id})" if pr_id else where
    repository = _text(raw, "repository", where, problems)
    if repository and not _SLUG.fullmatch(repository):
        problems.append(f"{where}: repository must be OWNER/NAME")
    clone_url = str(raw.get("clone_url") or "").strip() or (
        f"https://github.com/{repository}.git" if repository else ""
    )
    pull_raw = raw.get("pull", 0)
    if not isinstance(pull_raw, int) or isinstance(pull_raw, bool) or pull_raw < 0:
        problems.append(f"{where}: pull must be a pull request number")
        pull_raw = 0
    license_id = _text(raw, "license", where, problems)
    provenance = _provenance(raw.get("provenance"), where, problems)
    base = _sha(raw, "base_sha", where, problems)
    head = _sha(raw, "head_sha", where, problems)
    if provenance is not None:
        allowed = {"public": {"synthetic", "public_repository"}, "private": {"synthetic", "public_repository"}}.get(
            visibility
        )
        if allowed is not None and provenance.source not in allowed:
            problems.append(f"{where}: provenance {provenance.source} is not allowed in a {visibility} corpus")
        if (
            provenance.source == "public_repository"
            and visibility == "public"
            and license_id not in PERMISSIVE_LICENSES
        ):
            problems.append(f"{where}: a public-repository case needs a permissive license, got {license_id!r}")
        if provenance.commit and head and provenance.commit != head:
            problems.append(f"{where}: provenance.commit must be the reviewed head_sha")
    labels: list[Label] = []
    label_raw = raw.get("labels") or []
    if not isinstance(label_raw, list):
        problems.append(f"{where}: labels must be a list")
        label_raw = []
    for index, item in enumerate(label_raw):
        label = _label(item, f"{where}.labels[{index}]", problems)
        if label is not None:
            labels.append(label)
    if len({label.id for label in labels}) != len(labels):
        problems.append(f"{where}: label ids must be unique")
    labelled_raw = raw.get("labelled")
    if labelled_raw is not None and not isinstance(labelled_raw, bool):
        problems.append(f"{where}: labelled must be true or false")
    labelled = bool(labels) if labelled_raw is None else bool(labelled_raw)
    if labels and not labelled:
        problems.append(f"{where}: labelled is false but labels are present")
    pushes: list[PushSpec] = []
    push_raw = raw.get("pushes") or []
    if not isinstance(push_raw, list):
        problems.append(f"{where}: pushes must be a list")
        push_raw = []
    for index, item in enumerate(push_raw):
        push = _push(item, f"{where}.pushes[{index}]", problems)
        if push is not None:
            pushes.append(push)
    if len({push.id for push in pushes}) != len(pushes):
        problems.append(f"{where}: push ids must be unique")
    baseline = _baseline(raw.get("baseline"), where, problems)
    if problems or provenance is None or not pr_id:
        return None, problems
    return (
        RealPr(
            id=pr_id,
            repository=repository,
            clone_url=clone_url,
            pull=int(pull_raw),
            title=str(raw.get("title") or ""),
            license=license_id,
            provenance=provenance,
            base_sha=base,
            head_sha=head,
            labels=tuple(labels),
            labelled=labelled,
            pushes=tuple(pushes),
            baseline=baseline,
            notes=str(raw.get("notes") or ""),
        ),
        problems,
    )


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def load_real_pr_corpus(
    path: Path,
    *,
    organization_opt_in: str | None = None,
    repository_root: Path | None = None,
    pr_ids: Sequence[str] = (),
) -> RealPrCorpus:
    """Load and validate one real-PR corpus file; every problem found is listed in the error."""

    resolved = path.resolve()
    if not resolved.is_file():
        raise CorpusError(f"no real-PR corpus file at {resolved}")
    try:
        manifest = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CorpusError(f"{resolved}: {exc}") from exc
    if not isinstance(manifest, Mapping):
        raise CorpusError(f"{resolved} must be a mapping")
    problems: list[str] = []
    corpus_id = _text(manifest, "id", resolved.name, problems)
    if manifest.get("version") != CORPUS_VERSION:
        problems.append(f"{resolved.name}: version must be {CORPUS_VERSION}")
    visibility = manifest.get("visibility")
    if visibility not in ("public", "private", "organization"):
        raise CorpusError(f"{resolved}: visibility must be public, private or organization")
    organization = ""
    opt_in: OrganizationOptIn | None = None
    if visibility == "organization":
        organization = _text(manifest, "organization", resolved.name, problems)
        if not organization_opt_in:
            raise CorpusError(
                f"{resolved} is an organization corpus ({organization or 'unnamed'}); "
                "loading it requires an explicit organization opt-in"
            )
        if organization_opt_in != organization:
            raise CorpusError(
                f"the opt-in names organization {organization_opt_in!r}, but {resolved} belongs to {organization!r}"
            )
        record = manifest.get("opt_in")
        if not isinstance(record, Mapping) or not all(
            str(record.get(key) or "").strip() for key in ("organization", "approved_by", "approved_at", "reference")
        ):
            problems.append(
                "an organization corpus needs an opt_in record (organization, approved_by, approved_at, reference)"
            )
        else:
            opt_in = OrganizationOptIn(
                **{key: str(record[key]).strip() for key in ("organization", "approved_by", "approved_at", "reference")}
            )
            if opt_in.organization != organization:
                problems.append(f"opt_in record names {opt_in.organization!r}, not the corpus organization")
        for bundled in bundled_roots(repository_root):
            if _inside(resolved, bundled):
                raise CorpusError(
                    f"organization corpora are never bundled: {resolved} lives inside {bundled}; keep it outside "
                    "the repository checkout and the installed package"
                )
    tolerance = manifest.get("line_tolerance", DEFAULT_LINE_TOLERANCE)
    if not isinstance(tolerance, int) or isinstance(tolerance, bool) or tolerance < 0:
        problems.append(f"{resolved.name}: line_tolerance must be a non-negative integer")
        tolerance = DEFAULT_LINE_TOLERANCE
    match = str(manifest.get("match") or "either")
    if match not in MATCH_MODES:
        problems.append(f"{resolved.name}: match must be one of {list(MATCH_MODES)}")
        match = "either"
    listed = manifest.get("prs")
    if not isinstance(listed, list) or not listed:
        problems.append(f"{resolved.name}: prs must list at least one pull request")
        listed = []
    if len(listed) > _MAX_PRS:
        problems.append(f"{resolved.name}: more than {_MAX_PRS} pull requests")
    prs: list[RealPr] = []
    for index, raw in enumerate(listed):
        pr, found = parse_pr(raw, where=f"prs[{index}]", visibility=cast(CorpusVisibility, visibility))
        problems.extend(found)
        if pr is not None:
            prs.append(pr)
    if len({pr.id for pr in prs}) != len(prs):
        problems.append(f"{resolved.name}: pull request ids must be unique")
    wanted = set(pr_ids)
    unknown = sorted(wanted - {pr.id for pr in prs})
    if unknown:
        problems.append(f"unknown pull request ids {unknown}")
    if problems:
        raise CorpusError(f"real-PR corpus {resolved} is invalid", problems)
    return RealPrCorpus(
        id=corpus_id,
        visibility=cast(CorpusVisibility, visibility),
        description=str(manifest.get("description") or ""),
        path=resolved,
        prs=tuple(pr for pr in prs if not wanted or pr.id in wanted),
        line_tolerance=int(tolerance),
        match=cast(MatchMode, match),
        organization=organization,
        opt_in=opt_in,
    )


__all__ = [
    "ATTRIBUTIONS",
    "CORPUS_VERSION",
    "DEFAULT_LINE_TOLERANCE",
    "MATCH_MODES",
    "BaselineSource",
    "Label",
    "LabelEvidence",
    "MatchMode",
    "PushSpec",
    "RealPr",
    "RealPrCorpus",
    "Region",
    "load_real_pr_corpus",
    "parse_pr",
]
