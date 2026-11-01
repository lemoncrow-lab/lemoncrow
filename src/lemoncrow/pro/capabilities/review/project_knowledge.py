"""Deterministic, source-backed repository Project Knowledge (PK0).

Project Knowledge is a rebuildable read model over authoritative source, Review,
and outcome facts. This module owns no AI execution, scheduler, Review verdict,
code graph, or provider truth. Deleting this index never deletes authority.

AI narration (PK1, :mod:`.project_knowledge_narration`) is stored beside the
deterministic entries as ``ai_narrated`` rows through :meth:`ProjectKnowledgeStore.apply_narration`,
never through a projection, so it cannot rewrite an evolution event. A narrative
is written only for the generation that is current at write time, and a
deterministic change to its subject supersedes it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, Protocol, cast

from lemoncrow.pro.capabilities.review.rules import (
    ContentAccessRule,
    ModelRouteTrust,
    content_access_for_path,
)

KnowledgeSubjectKind = Literal[
    "repository",
    "path",
    "module",
    "package",
    "symbol",
    "service",
    "component",
    "contract",
    "architecture_edge",
    "data_flow_edge",
    "concept",
]
KnowledgeState = Literal["current", "superseded", "historical", "uncertain"]
KnowledgeSupport = Literal["deterministic", "source_document", "ai_narrated"]
ContentAccessClass = Literal["source_derived", "metadata_only"]
EvolutionKind = Literal["baseline", "merged_change", "source_advance"]
AdvanceAuthority = Literal["provider_observed", "local_canonical_ref", "explicit_import"]

_SUBJECT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "repository",
        "path",
        "module",
        "package",
        "symbol",
        "service",
        "component",
        "contract",
        "architecture_edge",
        "data_flow_edge",
        "concept",
    }
)
_STATES: Final[frozenset[str]] = frozenset({"current", "superseded", "historical", "uncertain"})
_SUPPORT: Final[frozenset[str]] = frozenset({"deterministic", "source_document", "ai_narrated"})
_ACCESS_CLASSES: Final[frozenset[str]] = frozenset({"source_derived", "metadata_only"})
_EVOLUTION_KINDS: Final[frozenset[str]] = frozenset({"baseline", "merged_change", "source_advance"})
_ADVANCE_AUTHORITIES: Final[frozenset[str]] = frozenset({"provider_observed", "local_canonical_ref", "explicit_import"})
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_RE = re.compile(r"^source:(new|old):(.+)@sha256:([0-9a-f]{64})$")
_OPAQUE_REF_KINDS: Final[frozenset[str]] = frozenset(
    {
        "target",
        "artifact",
        "check",
        "review",
        "revision",
        "merge",
        "deployment",
        "outcome",
        "index",
        "generation",
    }
)
_SOURCE_ADVANCE_REF_KINDS: Final[frozenset[str]] = frozenset({"source", "index", "generation"})
_MAX_QUERY_LIMIT: Final[int] = 100
_MAX_LINEAGE_EVENTS: Final[int] = 1024
_MAX_PROJECTION_ENTRIES: Final[int] = 2000
_MAX_REBUILD_PROJECTIONS: Final[int] = 1000
_MAX_STATE_ROWS: Final[int] = 100000

_SCHEMA: Final[str] = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS project_knowledge_meta (
  repository_id TEXT PRIMARY KEY,
  current_generation TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_knowledge_entries (
  entry_id TEXT PRIMARY KEY,
  repository_id TEXT NOT NULL,
  canonical_ref_id TEXT NOT NULL,
  canonical_ref TEXT NOT NULL,
  subject_kind TEXT NOT NULL,
  subject_key TEXT NOT NULL,
  knowledge_kind TEXT NOT NULL,
  body TEXT NOT NULL,
  valid_from_generation TEXT NOT NULL,
  last_observed_generation TEXT NOT NULL,
  state TEXT NOT NULL,
  support TEXT NOT NULL,
  source_refs_json TEXT NOT NULL,
  review_refs_json TEXT NOT NULL,
  revision_refs_json TEXT NOT NULL,
  merge_refs_json TEXT NOT NULL,
  source_paths_json TEXT NOT NULL,
  content_access_class TEXT NOT NULL,
  synthesis_identity TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_knowledge_query
  ON project_knowledge_entries(
    repository_id, subject_kind, subject_key, knowledge_kind, state, updated_at DESC
  );

CREATE TABLE IF NOT EXISTS project_knowledge_events (
  event_id TEXT PRIMARY KEY,
  repository_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  generation_id TEXT NOT NULL,
  previous_generation_id TEXT NOT NULL,
  source_refs_json TEXT NOT NULL,
  subject_refs_json TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  payload_digest TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_knowledge_events_repo
  ON project_knowledge_events(repository_id, observed_at DESC, event_id);
"""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(prefix: str, value: Any) -> str:
    return f"{prefix}:{hashlib.sha256(_canonical(value)).hexdigest()}"


def _clean_text(value: str, label: str, *, max_length: int = 4096) -> str:
    normalized = " ".join(str(value).split())
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    if len(normalized) > max_length:
        raise ValueError(f"{label} exceeds {max_length} characters")
    if any(ord(char) < 32 for char in normalized):
        raise ValueError(f"{label} contains control characters")
    return normalized


def _canonical_path(value: str) -> str:
    if not value or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("source path must be a non-empty canonical POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("source path must be repository-relative without traversal")
    normalized = path.as_posix()
    if normalized.startswith("../") or "/../" in normalized:
        raise ValueError("source path must not traverse outside the repository")
    return normalized


def _canonical_ref_name(value: str) -> str:
    ref = str(value).strip()
    if not ref.startswith("refs/heads/"):
        raise ValueError("canonical ref must be an explicit refs/heads/* ref")
    suffix = ref.removeprefix("refs/heads/")
    if not suffix or suffix.startswith("/") or suffix.endswith("/") or "//" in suffix:
        raise ValueError("canonical ref is malformed")
    if any(part in {"", ".", ".."} for part in suffix.split("/")):
        raise ValueError("canonical ref must not contain traversal")
    if any(char.isspace() or ord(char) < 32 for char in ref):
        raise ValueError("canonical ref contains whitespace/control characters")
    return ref


def _opaque_ref_value(value: str, kind: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 2048:
        raise ValueError(f"{kind} source ref has an invalid value")
    if any(char.isspace() or ord(char) < 32 for char in text):
        raise ValueError(f"{kind} source ref contains whitespace/control characters")
    if kind == "generation" and not _SHA256_RE.fullmatch(text):
        raise ValueError("generation source ref must carry a sha256 identity")
    return text


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """Stable repository identity independent of a checkout path."""

    provider: str
    repository_key: str

    def __post_init__(self) -> None:
        provider = _clean_text(self.provider.lower(), "repository provider", max_length=64)
        key = _clean_text(self.repository_key, "repository key", max_length=1024)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "repository_key", key)

    @property
    def id(self) -> str:
        return _digest("repository", {"provider": self.provider, "key": self.repository_key})

    def to_wire(self) -> dict[str, str]:
        return {"id": self.id, "provider": self.provider, "repository_key": self.repository_key}


_REPOSITORY_ID_RE = re.compile(r"^repository:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class StoredRepository:
    """A repository named by its stored id alone: a reader that never saw its provider/key."""

    id: str

    def __post_init__(self) -> None:
        if not _REPOSITORY_ID_RE.fullmatch(str(self.id)):
            raise ValueError("not a stored Project Knowledge repository id")


RepositoryRef = RepositoryIdentity | StoredRepository


@dataclass(frozen=True, slots=True)
class CanonicalRefIdentity:
    """Configured canonical or default branch for one repository."""

    repository: RepositoryIdentity
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _canonical_ref_name(self.name))

    @property
    def id(self) -> str:
        return _digest("ref", {"repository_id": self.repository.id, "name": self.name})

    def to_wire(self) -> dict[str, str]:
        return {"id": self.id, "repository_id": self.repository.id, "name": self.name}


@dataclass(frozen=True, slots=True)
class RepositoryGeneration:
    """One immutable generation of the configured canonical ref."""

    canonical_ref: CanonicalRefIdentity
    revision: str
    tree_digest: str = ""

    def __post_init__(self) -> None:
        revision = str(self.revision).lower().strip()
        if not _SHA_RE.fullmatch(revision):
            raise ValueError("repository generation revision must be an immutable git sha")
        tree = str(self.tree_digest).lower().strip()
        if tree and not _SHA256_RE.fullmatch(tree):
            raise ValueError("tree_digest must be a sha256 digest when present")
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "tree_digest", tree)

    @property
    def repository(self) -> RepositoryIdentity:
        return self.canonical_ref.repository

    @property
    def id(self) -> str:
        return _digest(
            "generation",
            {
                "repository_id": self.repository.id,
                "canonical_ref_id": self.canonical_ref.id,
                "revision": self.revision,
                "tree_digest": self.tree_digest,
            },
        )

    @property
    def source_ref(self) -> str:
        return f"generation:{self.id.removeprefix('generation:')}"

    def to_wire(self) -> dict[str, str]:
        return {
            "id": self.id,
            "repository_id": self.repository.id,
            "canonical_ref_id": self.canonical_ref.id,
            "canonical_ref": self.canonical_ref.name,
            "revision": self.revision,
            "tree_digest": self.tree_digest,
        }


@dataclass(frozen=True, slots=True)
class ValidatedSourceRef:
    ref: str
    kind: str
    value: str
    path: str = ""


def validate_source_ref(value: str) -> ValidatedSourceRef:
    """Validate one canonical Project Knowledge provenance reference."""

    ref = str(value).strip()
    source = _SOURCE_RE.fullmatch(ref)
    if source is not None:
        side, raw_path, digest = source.groups()
        path = _canonical_path(raw_path)
        canonical = f"source:{side}:{path}@sha256:{digest}"
        return ValidatedSourceRef(ref=canonical, kind="source", value=digest, path=path)

    if ":" not in ref:
        raise ValueError("Project Knowledge source ref must be typed")
    kind, raw = ref.split(":", 1)
    if kind not in _OPAQUE_REF_KINDS:
        raise ValueError(f"unsupported Project Knowledge source ref kind: {kind!r}")
    opaque = _opaque_ref_value(raw, kind)
    return ValidatedSourceRef(ref=f"{kind}:{opaque}", kind=kind, value=opaque)


def validate_source_refs(values: Iterable[str]) -> tuple[ValidatedSourceRef, ...]:
    seen: dict[str, ValidatedSourceRef] = {}
    for value in values:
        row = validate_source_ref(value)
        seen.setdefault(row.ref, row)
    if not seen:
        raise ValueError("Project Knowledge entries and events require source refs")
    return tuple(seen[key] for key in sorted(seen))


@dataclass(frozen=True, slots=True)
class KnowledgeSubject:
    kind: KnowledgeSubjectKind
    key: str

    def __post_init__(self) -> None:
        if self.kind not in _SUBJECT_KINDS:
            raise ValueError(f"unsupported Project Knowledge subject kind: {self.kind!r}")
        key = _clean_text(self.key, "knowledge subject key", max_length=2048)
        if self.kind == "path":
            key = _canonical_path(key)
        object.__setattr__(self, "key", key)

    @property
    def id(self) -> str:
        return _digest("subject", {"kind": self.kind, "key": self.key})

    def to_wire(self) -> dict[str, str]:
        return {"id": self.id, "kind": self.kind, "key": self.key}


@dataclass(frozen=True, slots=True)
class KnowledgeKey:
    subject: KnowledgeSubject
    knowledge_kind: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "knowledge_kind",
            _clean_text(self.knowledge_kind, "knowledge kind", max_length=128),
        )

    def to_wire(self) -> dict[str, Any]:
        return {"subject": self.subject.to_wire(), "knowledge_kind": self.knowledge_kind}


@dataclass(frozen=True, slots=True)
class KnowledgeFact:
    """One deterministic or source-document fact supplied to a PK0 projection."""

    subject: KnowledgeSubject
    knowledge_kind: str
    body: str
    source_refs: tuple[str, ...]
    state: Literal["current", "historical", "uncertain"] = "current"
    support: Literal["deterministic", "source_document"] = "deterministic"
    source_paths: tuple[str, ...] = ()
    content_access_class: ContentAccessClass = "source_derived"

    def __post_init__(self) -> None:
        kind = _clean_text(self.knowledge_kind, "knowledge kind", max_length=128)
        body = _clean_text(self.body, "knowledge body", max_length=20000)
        if self.state not in {"current", "historical", "uncertain"}:
            raise ValueError("PK0 fact state must be current, historical or uncertain")
        if self.support not in {"deterministic", "source_document"}:
            raise ValueError("PK0 does not accept AI-authored facts")
        if self.content_access_class not in _ACCESS_CLASSES:
            raise ValueError("unsupported content-access class")

        validated = validate_source_refs(self.source_refs)
        paths = {_canonical_path(path) for path in self.source_paths}
        paths.update(row.path for row in validated if row.path)
        if self.subject.kind == "path":
            paths.add(self.subject.key)
        if self.content_access_class == "metadata_only" and paths:
            raise ValueError("path-backed knowledge cannot bypass content policy as metadata_only")
        if self.content_access_class == "source_derived" and not paths:
            raise ValueError("source-derived Project Knowledge must retain a source path")

        object.__setattr__(self, "knowledge_kind", kind)
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "source_refs", tuple(row.ref for row in validated))
        object.__setattr__(self, "source_paths", tuple(sorted(paths)))

    @property
    def key(self) -> KnowledgeKey:
        return KnowledgeKey(self.subject, self.knowledge_kind)


@dataclass(frozen=True, slots=True)
class ProjectKnowledgeEntry:
    repository_id: str
    canonical_ref_id: str
    canonical_ref: str
    subject: KnowledgeSubject
    knowledge_kind: str
    body: str
    valid_from_generation: str
    last_observed_generation: str
    state: KnowledgeState
    support: KnowledgeSupport
    source_refs: tuple[str, ...]
    review_refs: tuple[str, ...] = ()
    revision_refs: tuple[str, ...] = ()
    merge_refs: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()
    content_access_class: ContentAccessClass = "source_derived"
    created_at: str = ""
    updated_at: str = ""
    synthesis_identity: str = ""

    def __post_init__(self) -> None:
        if not self.canonical_ref_id.startswith("ref:"):
            raise ValueError("Project Knowledge canonical_ref_id must be a canonical ref identity")
        canonical_ref = _canonical_ref_name(self.canonical_ref)
        object.__setattr__(self, "canonical_ref", canonical_ref)
        if self.state not in _STATES:
            raise ValueError(f"unsupported Project Knowledge state: {self.state!r}")
        if self.support not in _SUPPORT:
            raise ValueError(f"unsupported Project Knowledge support: {self.support!r}")
        if self.content_access_class not in _ACCESS_CLASSES:
            raise ValueError("unsupported content-access class")
        if self.support != "ai_narrated" and self.synthesis_identity:
            raise ValueError("synthesis identity is reserved for AI-narrated knowledge")
        source_refs = tuple(row.ref for row in validate_source_refs(self.source_refs))
        review_refs = tuple(sorted(set(self.review_refs)))
        revision_refs = tuple(sorted(set(self.revision_refs)))
        merge_refs = tuple(sorted(set(self.merge_refs)))
        for ref in review_refs:
            if validate_source_ref(ref).kind != "review":
                raise ValueError("review_refs may contain only review refs")
        for ref in revision_refs:
            if validate_source_ref(ref).kind != "revision":
                raise ValueError("revision_refs may contain only revision refs")
        for ref in merge_refs:
            if validate_source_ref(ref).kind != "merge":
                raise ValueError("merge_refs may contain only merge refs")
        path_set = {_canonical_path(path) for path in self.source_paths}
        path_set.update(row.path for row in validate_source_refs(source_refs) if row.path)
        if self.subject.kind == "path":
            path_set.add(self.subject.key)
        paths = tuple(sorted(path_set))
        if self.content_access_class == "metadata_only" and paths:
            raise ValueError("path-backed knowledge cannot bypass content policy as metadata_only")
        if self.content_access_class == "source_derived" and not paths:
            raise ValueError("source-derived Project Knowledge must retain source paths")
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "review_refs", review_refs)
        object.__setattr__(self, "revision_refs", revision_refs)
        object.__setattr__(self, "merge_refs", merge_refs)
        object.__setattr__(self, "source_paths", paths)

    @property
    def entry_id(self) -> str:
        return _digest(
            "knowledge",
            {
                "repository_id": self.repository_id,
                "canonical_ref_id": self.canonical_ref_id,
                "canonical_ref": self.canonical_ref,
                "subject": self.subject.to_wire(),
                "knowledge_kind": self.knowledge_kind,
                "body": self.body,
                "valid_from_generation": self.valid_from_generation,
                "support": self.support,
                "source_refs": list(self.source_refs),
                "review_refs": list(self.review_refs),
                "revision_refs": list(self.revision_refs),
                "merge_refs": list(self.merge_refs),
                "source_paths": list(self.source_paths),
                "content_access_class": self.content_access_class,
                "synthesis_identity": self.synthesis_identity,
            },
        )

    @property
    def key(self) -> KnowledgeKey:
        return KnowledgeKey(self.subject, self.knowledge_kind)

    def to_wire(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "repository_id": self.repository_id,
            "canonical_ref_id": self.canonical_ref_id,
            "canonical_ref": self.canonical_ref,
            "subject": self.subject.to_wire(),
            "knowledge_kind": self.knowledge_kind,
            "body": self.body,
            "valid_from_generation": self.valid_from_generation,
            "last_observed_generation": self.last_observed_generation,
            "state": self.state,
            "support": self.support,
            "source_refs": list(self.source_refs),
            "review_refs": list(self.review_refs),
            "revision_refs": list(self.revision_refs),
            "merge_refs": list(self.merge_refs),
            "source_paths": list(self.source_paths),
            "content_access_class": self.content_access_class,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "synthesis_identity": self.synthesis_identity,
        }


@dataclass(frozen=True, slots=True)
class EvolutionEvent:
    repository_id: str
    kind: EvolutionKind
    generation_id: str
    previous_generation_id: str
    source_refs: tuple[str, ...]
    subject_refs: tuple[str, ...]
    observed_at: str

    def __post_init__(self) -> None:
        if self.kind not in _EVOLUTION_KINDS:
            raise ValueError(f"unsupported evolution kind: {self.kind!r}")
        refs = validate_source_refs(self.source_refs)
        object.__setattr__(self, "source_refs", tuple(row.ref for row in refs))
        object.__setattr__(self, "subject_refs", tuple(sorted(set(self.subject_refs))))
        object.__setattr__(self, "observed_at", _clean_text(self.observed_at, "event observed_at", max_length=128))

    @property
    def event_id(self) -> str:
        return _digest(
            "evolution",
            {
                "repository_id": self.repository_id,
                "kind": self.kind,
                "generation_id": self.generation_id,
                "previous_generation_id": self.previous_generation_id,
                "source_refs": list(self.source_refs),
                "subject_refs": list(self.subject_refs),
                "observed_at": self.observed_at,
            },
        )

    @property
    def payload_digest(self) -> str:
        return hashlib.sha256(_canonical(self.to_wire(include_digest=False))).hexdigest()

    def to_wire(self, *, include_digest: bool = True) -> dict[str, Any]:
        row: dict[str, Any] = {
            "event_id": self.event_id,
            "repository_id": self.repository_id,
            "kind": self.kind,
            "generation_id": self.generation_id,
            "previous_generation_id": self.previous_generation_id,
            "source_refs": list(self.source_refs),
            "subject_refs": list(self.subject_refs),
            "observed_at": self.observed_at,
        }
        if include_digest:
            row["payload_digest"] = self.payload_digest
        return row


@dataclass(frozen=True, slots=True)
class KnowledgeProjection:
    generation: RepositoryGeneration
    event: EvolutionEvent
    entries: tuple[ProjectKnowledgeEntry, ...]
    retire: tuple[KnowledgeKey, ...] = ()

    def __post_init__(self) -> None:
        if self.event.repository_id != self.generation.repository.id:
            raise ValueError("evolution event repository does not match generation")
        if self.event.generation_id != self.generation.id:
            raise ValueError("evolution event generation does not match projection")
        if len(self.entries) > _MAX_PROJECTION_ENTRIES or len(self.retire) > _MAX_PROJECTION_ENTRIES:
            raise ValueError("Project Knowledge projection exceeds 2000 affected entries/keys")
        live_keys = [entry.key for entry in self.entries if entry.state in {"current", "uncertain"}]
        if len(set(live_keys)) != len(live_keys):
            raise ValueError("projection has duplicate current knowledge keys")
        for entry in self.entries:
            if entry.support == "ai_narrated":
                raise ValueError("PK0 does not accept AI-authored projections")
            if (
                entry.canonical_ref_id != self.generation.canonical_ref.id
                or entry.canonical_ref != self.generation.canonical_ref.name
            ):
                raise ValueError("knowledge entry canonical ref does not match projection")
            if entry.valid_from_generation != self.generation.id:
                raise ValueError("knowledge entry origin does not match projection")
            if self.generation.source_ref not in entry.source_refs:
                raise ValueError("knowledge entry must cite its immutable generation")
            if self.event.kind != "merged_change" and (
                entry.review_refs
                or entry.revision_refs
                or entry.merge_refs
                or any(validate_source_ref(ref).kind not in _SOURCE_ADVANCE_REF_KINDS for ref in entry.source_refs)
            ):
                raise ValueError("canonical source projection cannot invent Review history")
            if entry.repository_id != self.generation.repository.id:
                raise ValueError("knowledge entry repository does not match projection")
            if entry.last_observed_generation != self.generation.id:
                raise ValueError("knowledge entry currentness does not match generation")

    @property
    def digest(self) -> str:
        return _digest(
            "projection",
            {
                "generation": self.generation.to_wire(),
                "event": self.event.to_wire(),
                "entries": [entry.to_wire() for entry in sorted(self.entries, key=lambda row: row.entry_id)],
                "retire": [
                    key.to_wire()
                    for key in sorted(
                        set(self.retire), key=lambda key: (key.subject.kind, key.subject.key, key.knowledge_kind)
                    )
                ],
            },
        )


@dataclass(frozen=True, slots=True)
class CanonicalAdvanceProof:
    """Proof that a canonical ref, not workspace or feature HEAD, advanced."""

    authority: AdvanceAuthority
    observed_ref: str
    descendant: bool
    uses_working_tree: bool = False

    def validate(self, previous: RepositoryGeneration, current: RepositoryGeneration) -> None:
        if self.authority not in _ADVANCE_AUTHORITIES:
            raise ValueError("canonical advance authority is unsupported")
        if previous.repository.id != current.repository.id:
            raise ValueError("canonical advance cannot cross repositories")
        if previous.canonical_ref.id != current.canonical_ref.id:
            raise ValueError("canonical advance cannot switch refs")
        if _canonical_ref_name(self.observed_ref) != current.canonical_ref.name:
            raise ValueError("observed ref is not the configured canonical ref")
        if previous.id == current.id:
            raise ValueError("canonical advance must move to a new immutable generation")
        if not self.descendant:
            raise ValueError("canonical advance must prove an immutable descendant generation")
        if self.uses_working_tree:
            raise ValueError("working-tree content cannot advance canonical Project Knowledge")


def _entry_from_fact(
    generation: RepositoryGeneration,
    fact: KnowledgeFact,
    *,
    observed_at: str,
    extra_source_refs: Sequence[str] = (),
    review_refs: Sequence[str] = (),
    revision_refs: Sequence[str] = (),
    merge_refs: Sequence[str] = (),
) -> ProjectKnowledgeEntry:
    refs = validate_source_refs((*fact.source_refs, *extra_source_refs))
    paths = tuple(sorted({*fact.source_paths, *(row.path for row in refs if row.path)}))
    return ProjectKnowledgeEntry(
        repository_id=generation.repository.id,
        canonical_ref_id=generation.canonical_ref.id,
        canonical_ref=generation.canonical_ref.name,
        subject=fact.subject,
        knowledge_kind=fact.knowledge_kind,
        body=fact.body,
        valid_from_generation=generation.id,
        last_observed_generation=generation.id,
        state=fact.state,
        support=fact.support,
        source_refs=tuple(row.ref for row in refs),
        review_refs=tuple(sorted(set(review_refs))),
        revision_refs=tuple(sorted(set(revision_refs))),
        merge_refs=tuple(sorted(set(merge_refs))),
        source_paths=paths,
        content_access_class=fact.content_access_class,
        created_at=observed_at,
        updated_at=observed_at,
    )


def _projection(
    *,
    generation: RepositoryGeneration,
    previous_generation_id: str,
    kind: EvolutionKind,
    facts: Sequence[KnowledgeFact],
    observed_at: str,
    event_source_refs: Sequence[str],
    entry_source_refs: Sequence[str] = (),
    review_refs: Sequence[str] = (),
    revision_refs: Sequence[str] = (),
    merge_refs: Sequence[str] = (),
    retire: Sequence[KnowledgeKey] = (),
) -> KnowledgeProjection:
    entries = tuple(
        _entry_from_fact(
            generation,
            fact,
            observed_at=observed_at,
            extra_source_refs=entry_source_refs,
            review_refs=review_refs,
            revision_refs=revision_refs,
            merge_refs=merge_refs,
        )
        for fact in facts
    )
    subjects = tuple(sorted({entry.subject.id for entry in entries} | {key.subject.id for key in retire}))
    event = EvolutionEvent(
        repository_id=generation.repository.id,
        kind=kind,
        generation_id=generation.id,
        previous_generation_id=previous_generation_id,
        source_refs=tuple(event_source_refs),
        subject_refs=subjects,
        observed_at=observed_at,
    )
    return KnowledgeProjection(generation=generation, event=event, entries=entries, retire=tuple(retire))


def build_baseline_projection(
    generation: RepositoryGeneration,
    *,
    facts: Sequence[KnowledgeFact],
    observed_at: str,
) -> KnowledgeProjection:
    """Bootstrap structural and document facts without inventing history."""

    for fact in facts:
        kinds = {validate_source_ref(ref).kind for ref in fact.source_refs}
        if not kinds.issubset({"source", "index", "generation"}):
            raise ValueError("baseline knowledge may only cite source, index, or generation refs")
    return _projection(
        generation=generation,
        previous_generation_id="",
        kind="baseline",
        facts=facts,
        observed_at=observed_at,
        event_source_refs=(generation.source_ref,),
        entry_source_refs=(generation.source_ref,),
    )


def build_merged_change_projection(
    previous: RepositoryGeneration,
    current: RepositoryGeneration,
    *,
    proof: CanonicalAdvanceProof,
    merge_ref: str,
    review_ref: str,
    revision_ref: str,
    facts: Sequence[KnowledgeFact],
    observed_at: str,
    retire: Sequence[KnowledgeKey] = (),
) -> KnowledgeProjection:
    """Build one provider-observed landed Change from authoritative deterministic facts."""

    proof.validate(previous, current)
    if proof.authority != "provider_observed":
        raise ValueError("merged Change history requires provider-observed canonical authority")

    typed = {
        "merge": validate_source_ref(merge_ref),
        "review": validate_source_ref(review_ref),
        "revision": validate_source_ref(revision_ref),
    }
    if any(typed[name].kind != name for name in typed):
        raise ValueError("merged Change refs must be merge, review, and revision refs respectively")

    return _projection(
        generation=current,
        previous_generation_id=previous.id,
        kind="merged_change",
        facts=facts,
        observed_at=observed_at,
        event_source_refs=(merge_ref, review_ref, revision_ref, previous.source_ref, current.source_ref),
        entry_source_refs=(current.source_ref,),
        review_refs=(review_ref,),
        revision_refs=(revision_ref,),
        merge_refs=(merge_ref,),
        retire=retire,
    )


def build_source_advance_projection(
    previous: RepositoryGeneration,
    current: RepositoryGeneration,
    *,
    proof: CanonicalAdvanceProof,
    facts: Sequence[KnowledgeFact],
    observed_at: str,
    retire: Sequence[KnowledgeKey] = (),
) -> KnowledgeProjection:
    """Advance canonical history without fabricating Review or outcome facts."""

    proof.validate(previous, current)
    for fact in facts:
        for ref in fact.source_refs:
            kind = validate_source_ref(ref).kind
            if kind not in _SOURCE_ADVANCE_REF_KINDS:
                raise ValueError(
                    f"direct canonical source advance may only cite source, index, or generation refs; got {kind!r}"
                )

    return _projection(
        generation=current,
        previous_generation_id=previous.id,
        kind="source_advance",
        facts=facts,
        observed_at=observed_at,
        event_source_refs=(previous.source_ref, current.source_ref),
        entry_source_refs=(current.source_ref,),
        retire=retire,
    )


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    subjects: tuple[KnowledgeSubject, ...] = ()
    paths: tuple[str, ...] = ()
    term: str = ""
    knowledge_kinds: tuple[str, ...] = ()
    states: tuple[KnowledgeState, ...] = ("current", "uncertain", "historical")
    limit: int = 50
    scan_limit: int = 400
    offset: int = 0
    #: Only these supports (empty: all).
    supports: tuple[KnowledgeSupport, ...] = ()
    #: Leave these knowledge kinds out (a view that skips path inventory rows).
    exclude_knowledge_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= self.scan_limit <= 400 or not 0 <= self.offset <= 1000000:
            raise ValueError("Project Knowledge scan_limit/offset exceeds its bound")
        filters = (
            self.subjects,
            self.paths,
            self.knowledge_kinds,
            self.states,
            self.supports,
            self.exclude_knowledge_kinds,
        )
        if sum(map(len, filters)) > 100:
            raise ValueError("Project Knowledge query has more than 100 filters")
        if not 1 <= self.limit <= _MAX_QUERY_LIMIT:
            raise ValueError(f"Project Knowledge query limit must be between 1 and {_MAX_QUERY_LIMIT}")
        if any(state not in _STATES for state in self.states):
            raise ValueError("Project Knowledge query contains an unsupported state")
        if any(support not in _SUPPORT for support in self.supports):
            raise ValueError("Project Knowledge query contains an unsupported support")
        object.__setattr__(self, "paths", tuple(sorted({_canonical_path(path) for path in self.paths})))
        term = " ".join(self.term.split()).lower()
        if len(term) > 256:
            raise ValueError("Project Knowledge free-text term exceeds 256 characters")
        object.__setattr__(self, "term", term)


@dataclass(frozen=True, slots=True)
class KnowledgeQueryResult:
    entries: tuple[ProjectKnowledgeEntry, ...]
    filtered_by_content_access: int
    current_generation: str
    scanned: int = 0
    truncated: bool = False
    next_offset: int | None = None


@dataclass(frozen=True, slots=True)
class GenerationDelta:
    """The deterministic entries one generation introduced and the ones it superseded (PK1 input).

    ``event_kind`` is empty when the generation has no retained evolution event.
    Both tuples are visible under the content-access policy the caller passed;
    ``truncated`` means more rows matched than the bound returned.
    """

    generation_id: str
    event_kind: str
    introduced: tuple[ProjectKnowledgeEntry, ...]
    retired: tuple[ProjectKnowledgeEntry, ...]
    filtered_by_content_access: int
    truncated: bool


NarrationWrite = Literal["applied", "unchanged", "stale"]


def _visible(
    entry: ProjectKnowledgeEntry, content_access: Sequence[ContentAccessRule], route_trust: ModelRouteTrust
) -> bool:
    """Whether every source path of a source-derived entry may reach this route (metadata rows always may)."""

    if entry.content_access_class != "source_derived":
        return True
    if not entry.source_paths:
        return False
    return all(
        content_access_for_path(path, content_access, route_trust=route_trust).allowed for path in entry.source_paths
    )


class ProjectKnowledgeReviewStore(Protocol):
    """Storage host implemented by ReviewStore and its hosted PostgreSQL subclass."""

    def init(self) -> None: ...

    def project_knowledge_scope(self, *, write: bool = False) -> Any: ...


class _ReviewStoreConnection:
    """Give ReviewStore's scoped transaction the sqlite connection shape used below."""

    def __init__(self, store: ProjectKnowledgeReviewStore, *, write: bool = False) -> None:
        self._store = store
        self._write = write
        self._scope: Any = None
        self._conn: Any = None
        self._depth = 0

    def __enter__(self) -> _ReviewStoreConnection:
        if self._depth == 0:
            self._scope = self._store.project_knowledge_scope(write=self._write)
            self._conn = self._scope.__enter__()
        self._depth += 1
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._depth -= 1
        if self._depth == 0:
            scope = self._scope
            self._scope = None
            self._conn = None
            return bool(scope.__exit__(exc_type, exc, tb))
        return False

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        return self._conn.execute(sql, () if params is None else params)


class _StandaloneConnection:
    """Own and close one SQLite transaction; nested scopes never commit early."""

    def __init__(self, path: Path, *, write: bool) -> None:
        self._conn = sqlite3.connect(path, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._write = write
        self._depth = 0

    def __enter__(self) -> _StandaloneConnection:
        if self._depth == 0:
            try:
                self._conn.execute("BEGIN IMMEDIATE" if self._write else "BEGIN")
            except BaseException:
                self._conn.close()
                raise
        self._depth += 1
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        self._depth -= 1
        if self._depth == 0:
            try:
                if exc_type is None:
                    self._conn.commit()
                else:
                    self._conn.rollback()
            finally:
                self._conn.close()
        return False

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self._conn.execute(sql, params)

    def executescript(self, sql: str) -> Any:
        return self._conn.executescript(sql)


class ProjectKnowledgeStore:
    """Derived repository index backed by standalone SQLite or the Review store.

    Product paths should pass ReviewStore/PostgresReviewStore so PK0 shares the
    existing migration, tenant and RLS authority. A Path remains useful for
    deterministic standalone rebuild tooling and tests.
    """

    def __init__(self, storage: Path | str | ProjectKnowledgeReviewStore) -> None:
        if isinstance(storage, (str, Path)):
            self.path: Path | None = Path(storage)
            self._review_store: ProjectKnowledgeReviewStore | None = None
        else:
            self.path = None
            self._review_store = storage
        self._initialized = False

    def init(self) -> None:
        if self._initialized:
            return
        if self._review_store is not None:
            self._review_store.init()
            self._initialized = True
            return
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
        self._initialized = True

    def _connect(self, *, write: bool = False) -> Any:
        if self._review_store is not None:
            return _ReviewStoreConnection(self._review_store, write=write)
        assert self.path is not None
        return _StandaloneConnection(self.path, write=write)

    def _ensure(self) -> None:
        self.init()

    @staticmethod
    def _json(values: Sequence[str]) -> str:
        return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _unjson(value: str) -> tuple[str, ...]:
        parsed = json.loads(value)
        if not isinstance(parsed, list) or not all(isinstance(row, str) for row in parsed):
            raise ValueError("invalid Project Knowledge stored JSON")
        return tuple(parsed)

    @classmethod
    def _entry_from_row(cls, row: sqlite3.Row) -> ProjectKnowledgeEntry:
        return ProjectKnowledgeEntry(
            repository_id=str(row["repository_id"]),
            canonical_ref_id=str(row["canonical_ref_id"]),
            canonical_ref=str(row["canonical_ref"]),
            subject=KnowledgeSubject(cast(KnowledgeSubjectKind, str(row["subject_kind"])), str(row["subject_key"])),
            knowledge_kind=str(row["knowledge_kind"]),
            body=str(row["body"]),
            valid_from_generation=str(row["valid_from_generation"]),
            last_observed_generation=str(row["last_observed_generation"]),
            state=cast(KnowledgeState, str(row["state"])),
            support=cast(KnowledgeSupport, str(row["support"])),
            source_refs=cls._unjson(str(row["source_refs_json"])),
            review_refs=cls._unjson(str(row["review_refs_json"])),
            revision_refs=cls._unjson(str(row["revision_refs_json"])),
            merge_refs=cls._unjson(str(row["merge_refs_json"])),
            source_paths=cls._unjson(str(row["source_paths_json"])),
            content_access_class=cast(ContentAccessClass, str(row["content_access_class"])),
            synthesis_identity=str(row["synthesis_identity"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _current_generation(conn: sqlite3.Connection, repository_id: str) -> str:
        row = conn.execute(
            "SELECT current_generation FROM project_knowledge_meta WHERE repository_id = ?",
            (repository_id,),
        ).fetchone()
        return "" if row is None else str(row["current_generation"])

    def current_generation(self, repository: RepositoryRef) -> str:
        self._ensure()
        with self._connect() as conn:
            return self._current_generation(conn, repository.id)

    def _supersede(
        self,
        conn: sqlite3.Connection,
        repository_id: str,
        key: KnowledgeKey,
        generation_id: str,
        observed_at: str,
    ) -> None:
        conn.execute(
            """
            UPDATE project_knowledge_entries
               SET state = 'superseded',
                   last_observed_generation = ?,
                   updated_at = ?
             WHERE repository_id = ?
               AND subject_kind = ?
               AND subject_key = ?
               AND knowledge_kind = ?
               AND state IN ('current', 'uncertain')
            """,
            (
                generation_id,
                observed_at,
                repository_id,
                key.subject.kind,
                key.subject.key,
                key.knowledge_kind,
            ),
        )

    def _insert_entry(self, conn: sqlite3.Connection, entry: ProjectKnowledgeEntry) -> None:
        conn.execute(
            """
            INSERT INTO project_knowledge_entries(
              entry_id, repository_id, canonical_ref_id, canonical_ref,
              subject_kind, subject_key, knowledge_kind, body,
              valid_from_generation, last_observed_generation, state, support,
              source_refs_json, review_refs_json, revision_refs_json, merge_refs_json,
              source_paths_json, content_access_class, synthesis_identity, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.entry_id,
                entry.repository_id,
                entry.canonical_ref_id,
                entry.canonical_ref,
                entry.subject.kind,
                entry.subject.key,
                entry.knowledge_kind,
                entry.body,
                entry.valid_from_generation,
                entry.last_observed_generation,
                entry.state,
                entry.support,
                self._json(entry.source_refs),
                self._json(entry.review_refs),
                self._json(entry.revision_refs),
                self._json(entry.merge_refs),
                self._json(entry.source_paths),
                entry.content_access_class,
                entry.synthesis_identity,
                entry.created_at,
                entry.updated_at,
            ),
        )

    def _apply(self, conn: sqlite3.Connection, projection: KnowledgeProjection) -> bool:
        repository_id = projection.generation.repository.id
        existing_event = conn.execute(
            "SELECT payload_digest FROM project_knowledge_events WHERE event_id = ?",
            (projection.event.event_id,),
        ).fetchone()
        if existing_event is not None:
            if str(existing_event["payload_digest"]) != projection.digest:
                raise ValueError("Project Knowledge event identity collision")
            return False

        current = self._current_generation(conn, repository_id)
        expected = projection.event.previous_generation_id
        if current != expected:
            raise ValueError(
                "Project Knowledge source generation is stale or out of order: "
                f"expected previous {current or '<none>'}, got {expected or '<none>'}"
            )

        supersede_keys = {entry.key for entry in projection.entries if entry.state in {"current", "uncertain"}}
        supersede_keys.update(projection.retire)
        ordered = sorted(
            supersede_keys,
            key=lambda row: (row.subject.kind, row.subject.key, row.knowledge_kind),
        )
        for key in ordered:
            self._supersede(
                conn,
                repository_id,
                key,
                projection.generation.id,
                projection.event.observed_at,
            )
        # A current AI narrative describes its subject's deterministic facts; once
        # they change it is no longer current (PK1). Historical narratives of an
        # immutable landed Change stay historical.
        for subject_kind, subject_key in sorted({(key.subject.kind, key.subject.key) for key in ordered}):
            conn.execute(
                """
                UPDATE project_knowledge_entries
                   SET state = 'superseded',
                       last_observed_generation = ?,
                       updated_at = ?
                 WHERE repository_id = ?
                   AND subject_kind = ?
                   AND subject_key = ?
                   AND support = 'ai_narrated'
                   AND state IN ('current', 'uncertain')
                """,
                (projection.generation.id, projection.event.observed_at, repository_id, subject_kind, subject_key),
            )
        for entry in projection.entries:
            self._insert_entry(conn, entry)

        conn.execute(
            """
            INSERT INTO project_knowledge_events(
              event_id, repository_id, kind, generation_id, previous_generation_id,
              source_refs_json, subject_refs_json, observed_at, payload_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                projection.event.event_id,
                repository_id,
                projection.event.kind,
                projection.event.generation_id,
                projection.event.previous_generation_id,
                self._json(projection.event.source_refs),
                self._json(projection.event.subject_refs),
                projection.event.observed_at,
                projection.digest,
            ),
        )
        conn.execute(
            """
            INSERT INTO project_knowledge_meta(repository_id, current_generation, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(repository_id) DO UPDATE SET
              current_generation = excluded.current_generation,
              updated_at = excluded.updated_at
            """,
            (repository_id, projection.generation.id, projection.event.observed_at),
        )
        return True

    def apply(self, projection: KnowledgeProjection) -> bool:
        self._ensure()
        with self._connect(write=True) as conn:
            with conn:
                return self._apply(conn, projection)

    def query(
        self,
        repository: RepositoryRef,
        *,
        query: KnowledgeQuery | None = None,
        content_access: Sequence[ContentAccessRule] = (),
        route_trust: ModelRouteTrust = "unknown",
    ) -> KnowledgeQueryResult:
        """Return bounded entries after evaluating the current access policy."""

        self._ensure()
        spec = query or KnowledgeQuery()
        clauses = ["repository_id = ?"]
        args: list[Any] = [repository.id]

        if spec.states:
            clauses.append(f"state IN ({','.join('?' for _ in spec.states)})")
            args.extend(spec.states)
        if spec.knowledge_kinds:
            clauses.append(f"knowledge_kind IN ({','.join('?' for _ in spec.knowledge_kinds)})")
            args.extend(spec.knowledge_kinds)
        if spec.supports:
            clauses.append(f"support IN ({','.join('?' for _ in spec.supports)})")
            args.extend(spec.supports)
        if spec.exclude_knowledge_kinds:
            clauses.append(f"knowledge_kind NOT IN ({','.join('?' for _ in spec.exclude_knowledge_kinds)})")
            args.extend(spec.exclude_knowledge_kinds)
        if spec.subjects:
            terms = []
            for subject in spec.subjects:
                terms.append("(subject_kind = ? AND subject_key = ?)")
                args.extend((subject.kind, subject.key))
            clauses.append("(" + " OR ".join(terms) + ")")
        # lc-debt: JSON path and free-text LIKE filters scan scoped derived rows;
        # add a rebuildable path/FTS index if retained repository state grows large.
        if spec.paths:
            path_terms = []
            for path in spec.paths:
                encoded = json.dumps(path, ensure_ascii=False)
                escaped = encoded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                path_terms.append("(subject_kind = 'path' AND subject_key = ? OR source_paths_json LIKE ? ESCAPE '\\')")
                args.extend((path, f"%{escaped}%"))
            clauses.append("(" + " OR ".join(path_terms) + ")")
        if spec.term:
            escaped = spec.term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(LOWER(subject_key) LIKE ? ESCAPE '\\' "
                "OR LOWER(knowledge_kind) LIKE ? ESCAPE '\\' "
                "OR LOWER(body) LIKE ? ESCAPE '\\')"
            )
            args.extend((pattern, pattern, pattern))

        sql = "SELECT * FROM project_knowledge_entries WHERE " + " AND ".join(clauses) + """
              ORDER BY CASE state
                         WHEN 'current' THEN 0
                         WHEN 'uncertain' THEN 1
                         WHEN 'historical' THEN 2
                         ELSE 3
                       END,
                       updated_at DESC,
                       entry_id
              LIMIT ? OFFSET ?
            """
        args.extend((spec.scan_limit + 1, spec.offset))

        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
            current_generation = self._current_generation(conn, repository.id)

        visible: list[ProjectKnowledgeEntry] = []
        filtered = 0
        processed = 0
        for row in rows[: spec.scan_limit]:
            processed += 1
            entry = self._entry_from_row(row)
            if not _visible(entry, content_access, route_trust):
                filtered += 1
                continue
            visible.append(entry)
            if len(visible) >= spec.limit:
                break

        return KnowledgeQueryResult(
            entries=tuple(visible),
            filtered_by_content_access=filtered,
            current_generation=current_generation,
            scanned=processed,
            truncated=processed < len(rows),
            next_offset=spec.offset + processed if processed < len(rows) else None,
        )

    def evolution_events(
        self,
        repository: RepositoryRef,
        generation_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        """Return the stored evolution event of each named generation (read-only, bounded).

        Retrieval consumers use this to open an entry's source history: the
        landed/advance/baseline event that introduced or superseded it. A
        generation pruned by retention simply has no event.
        """

        wanted = tuple(sorted({str(value) for value in generation_ids if str(value)}))
        if not wanted:
            return {}
        if len(wanted) > _MAX_QUERY_LIMIT:
            raise ValueError(f"Project Knowledge event lookup exceeds {_MAX_QUERY_LIMIT} generations")
        self._ensure()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM project_knowledge_events WHERE repository_id = ? AND generation_id IN ("
                + ",".join("?" for _ in wanted)
                + ") ORDER BY observed_at DESC, event_id",
                (repository.id, *wanted),
            ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            # One event per generation in the forward chain; on a replayed
            # rebuild the latest observation wins.
            out.setdefault(str(row["generation_id"]), self._event_wire(row))
        return out

    @classmethod
    def _event_wire(cls, row: Any) -> dict[str, Any]:
        return {
            "event_id": str(row["event_id"]),
            "kind": str(row["kind"]),
            "generation_id": str(row["generation_id"]),
            "previous_generation_id": str(row["previous_generation_id"]),
            "source_refs": list(cls._unjson(str(row["source_refs_json"]))),
            "observed_at": str(row["observed_at"]),
        }

    def recent_evolution_events(self, repository: RepositoryRef, *, limit: int = 20) -> list[dict[str, Any]]:
        """The newest retained evolution events, newest first (read-only, bounded)."""

        if not 1 <= limit <= _MAX_QUERY_LIMIT:
            raise ValueError(f"Project Knowledge event listing limit must be between 1 and {_MAX_QUERY_LIMIT}")
        self._ensure()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM project_knowledge_events WHERE repository_id = ? "
                "ORDER BY observed_at DESC, event_id LIMIT ?",
                (repository.id, limit),
            ).fetchall()
        return [self._event_wire(row) for row in rows]

    def has_merged_review_event(
        self,
        repository: RepositoryRef,
        *,
        merge_ref: str,
        review_ref: str,
        revision_ref: str,
    ) -> bool:
        """Check a durable exact landing receipt, independent of recent-event limits.

        A narrow, repository-scoped JSON-text candidate lookup avoids paging
        through arbitrarily old merges. Always parse and check exact source
        references: a LIKE match is a search hint, not attribution proof.
        """
        required = {merge_ref, review_ref, revision_ref}
        for reference, kind in ((merge_ref, "merge"), (review_ref, "review"), (revision_ref, "revision")):
            if validate_source_ref(reference).kind != kind:
                raise ValueError(f"expected an exact {kind} source reference")
        encoded = json.dumps(review_ref, ensure_ascii=False)
        escaped = encoded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        self._ensure()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source_refs_json FROM project_knowledge_events "
                "WHERE repository_id = ? AND kind = 'merged_change' "
                "AND source_refs_json LIKE ? ESCAPE '\\'",
                (repository.id, f"%{escaped}%"),
            ).fetchall()
        return any(required.issubset(self._unjson(str(row["source_refs_json"]))) for row in rows)

    def repositories(self, *, limit: int = 50) -> dict[str, Any]:
        """Stored repositories, most recently advanced first, with their live entry counts (read-only, bounded).

        A repository is named by its stored id alone: its provider and key live
        only where it was synced (the local source registry, a hosted tenant row).
        """

        if not 1 <= limit <= _MAX_QUERY_LIMIT:
            raise ValueError(f"Project Knowledge repository listing limit must be between 1 and {_MAX_QUERY_LIMIT}")
        self._ensure()
        counts: dict[str, dict[str, int]] = {}
        refs: dict[str, tuple[str, str]] = {}
        with self._connect() as conn:
            metas = conn.execute(
                "SELECT repository_id, current_generation, updated_at FROM project_knowledge_meta "
                "ORDER BY updated_at DESC, repository_id LIMIT ?",
                (limit + 1,),
            ).fetchall()
            shown = [str(row["repository_id"]) for row in metas[:limit]]
            # lc-debt: the counts and the canonical ref are GROUP BY scans over every entry of the
            # listed repositories on each read; keep per-support counters in project_knowledge_meta
            # (maintained by apply/apply_narration/hide) if large repositories make the view slow.
            if shown:
                marks = ",".join("?" for _ in shown)
                for row in conn.execute(
                    "SELECT repository_id, support, COUNT(*) AS entries FROM project_knowledge_entries "
                    f"WHERE repository_id IN ({marks}) AND state IN ('current', 'uncertain', 'historical') "
                    "GROUP BY repository_id, support",
                    shown,
                ).fetchall():
                    counts.setdefault(str(row["repository_id"]), {})[str(row["support"])] = int(row["entries"])
                for row in conn.execute(
                    "SELECT repository_id, canonical_ref, MAX(updated_at) AS seen FROM project_knowledge_entries "
                    f"WHERE repository_id IN ({marks}) GROUP BY repository_id, canonical_ref",
                    shown,
                ).fetchall():
                    repository_id, seen = str(row["repository_id"]), str(row["seen"] or "")
                    if repository_id not in refs or seen > refs[repository_id][1]:
                        refs[repository_id] = (str(row["canonical_ref"]), seen)
        rows = [
            {
                "repository_id": str(meta["repository_id"]),
                "canonical_ref": refs.get(str(meta["repository_id"]), ("", ""))[0],
                "current_generation": str(meta["current_generation"]),
                "updated_at": str(meta["updated_at"]),
                "entries": {
                    support: counts.get(str(meta["repository_id"]), {}).get(support, 0)
                    for support in ("deterministic", "source_document", "ai_narrated")
                },
            }
            for meta in metas[:limit]
        ]
        return {"repositories": rows, "truncated": len(metas) > limit}

    def entry(self, repository: RepositoryRef, entry_id: str) -> ProjectKnowledgeEntry | None:
        """One stored entry of the repository, in any state (read-only)."""

        self._ensure()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM project_knowledge_entries WHERE repository_id = ? AND entry_id = ?",
                (repository.id, str(entry_id)),
            ).fetchone()
        return None if row is None else self._entry_from_row(row)

    def hide_narrative(self, repository: RepositoryRef, entry_id: str, *, at: str) -> bool:
        """Supersede one live AI narrative so no reader shows or forwards it again (UX0b).

        Only an ``ai_narrated`` row moves; deterministic and source-document
        history never does, and an evolution event is never touched. False when
        the repository has no live narrative with that id.
        """

        stamp = _clean_text(at, "hidden_at", max_length=128)
        self._ensure()
        with self._connect(write=True) as conn:
            with conn:
                count = conn.execute(
                    """
                    UPDATE project_knowledge_entries
                       SET state = 'superseded', updated_at = ?
                     WHERE repository_id = ? AND entry_id = ?
                       AND support = 'ai_narrated' AND state IN ('current', 'uncertain', 'historical')
                    """,
                    (stamp, repository.id, str(entry_id)),
                ).rowcount
        return int(count or 0) == 1

    def generation_parents(self, repository: RepositoryIdentity) -> dict[str, str]:
        """Return ``generation_id -> previous_generation_id`` for the newest retained events (read-only).

        The forward evolution chain is the only ordering PK0 records between
        generations, so a consumer that must decide whether one generation is
        at or before another walks these links. A baseline maps to ``""``.
        """

        # lc-debt: only the newest 1024 events are scanned, so older ancestry is
        # unknown to a caller (which must treat it as unordered, never as proof).
        # Upgrade: persist a generation depth/ancestor index if deep walks matter.
        self._ensure()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT generation_id, previous_generation_id FROM project_knowledge_events "
                "WHERE repository_id = ? ORDER BY observed_at DESC, event_id LIMIT ?",
                (repository.id, _MAX_LINEAGE_EVENTS),
            ).fetchall()
        out: dict[str, str] = {}
        for row in rows:
            out.setdefault(str(row["generation_id"]), str(row["previous_generation_id"]))
        return out

    def generation_delta(
        self,
        repository: RepositoryIdentity,
        generation_id: str,
        *,
        limit: int = 200,
        content_access: Sequence[ContentAccessRule] = (),
        route_trust: ModelRouteTrust = "unknown",
    ) -> GenerationDelta:
        """The deterministic entries *generation_id* introduced and superseded, visible to this route (bounded).

        AI-narrated rows are never part of a delta. Introduced means
        ``valid_from_generation`` is the generation; retired means an older entry
        that this generation superseded.
        """

        if not 1 <= limit <= 400:
            raise ValueError("Project Knowledge generation delta limit must be between 1 and 400")
        wanted = str(generation_id)
        self._ensure()
        # lc-debt: no (repository_id, valid_from/last_observed generation) index, so a
        # delta scans the repository's derived rows (bounded by _MAX_STATE_ROWS retention).
        # Upgrade: add the two generation indexes to the SQLite/PostgreSQL migrations.
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM project_knowledge_entries
                 WHERE repository_id = ?
                   AND support <> 'ai_narrated'
                   AND (valid_from_generation = ?
                        OR (state = 'superseded' AND last_observed_generation = ? AND valid_from_generation <> ?))
                 ORDER BY subject_kind, subject_key, knowledge_kind, entry_id
                 LIMIT ?
                """,
                (repository.id, wanted, wanted, wanted, limit + 1),
            ).fetchall()
            event = conn.execute(
                "SELECT kind FROM project_knowledge_events WHERE repository_id = ? AND generation_id = ? "
                "ORDER BY observed_at DESC, event_id LIMIT 1",
                (repository.id, wanted),
            ).fetchone()
        introduced: list[ProjectKnowledgeEntry] = []
        retired: list[ProjectKnowledgeEntry] = []
        filtered = 0
        for row in rows[:limit]:
            entry = self._entry_from_row(row)
            if not _visible(entry, content_access, route_trust):
                filtered += 1
                continue
            (introduced if entry.valid_from_generation == wanted else retired).append(entry)
        return GenerationDelta(
            generation_id=wanted,
            event_kind="" if event is None else str(event["kind"]),
            introduced=tuple(introduced),
            retired=tuple(retired),
            filtered_by_content_access=filtered,
            truncated=len(rows) > limit,
        )

    def narration_identities(
        self, repository: RepositoryIdentity, generation_id: str
    ) -> dict[tuple[str, str, str], str]:
        """``(subject kind, subject key, knowledge kind) -> synthesis identity`` of the live narratives of a generation."""

        self._ensure()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT subject_kind, subject_key, knowledge_kind, synthesis_identity
                  FROM project_knowledge_entries
                 WHERE repository_id = ?
                   AND valid_from_generation = ?
                   AND support = 'ai_narrated'
                   AND state IN ('current', 'uncertain', 'historical')
                 ORDER BY entry_id
                 LIMIT ?
                """,
                (repository.id, str(generation_id), _MAX_PROJECTION_ENTRIES),
            ).fetchall()
        return {
            (str(row["subject_kind"]), str(row["subject_key"]), str(row["knowledge_kind"])): str(
                row["synthesis_identity"]
            )
            for row in rows
        }

    def apply_narration(self, narration: ProjectKnowledgeEntry) -> NarrationWrite:
        """Store one AI narrative; never touches deterministic rows or evolution events.

        A ``current`` narrative is written only while its generation is the
        repository's current one (``stale`` otherwise: a newer generation won)
        and only for a subject that still has a current deterministic entry; it
        supersedes the subject's previous narrative of the same kind. A
        ``historical`` narrative describes one immutable landed generation and
        needs that generation's evolution event; it replaces an earlier
        narration of the same generation. Re-applying the same narrative is
        ``unchanged``.
        """

        if narration.support != "ai_narrated" or not narration.synthesis_identity.startswith("synthesis:"):
            raise ValueError("apply_narration stores only AI-narrated entries with a synthesis identity")
        if narration.state not in {"current", "historical"}:
            raise ValueError("an AI narrative is current (a subject) or historical (a landed generation)")
        if narration.valid_from_generation != narration.last_observed_generation:
            raise ValueError("an AI narrative is observed at the generation it narrates")
        generation = narration.valid_from_generation
        self._ensure()
        with self._connect(write=True) as conn:
            with conn:
                if conn.execute(
                    "SELECT 1 FROM project_knowledge_entries WHERE entry_id = ?", (narration.entry_id,)
                ).fetchone():
                    return "unchanged"
                if narration.state == "current":
                    if self._current_generation(conn, narration.repository_id) != generation:
                        return "stale"
                    backed = conn.execute(
                        """
                        SELECT 1 FROM project_knowledge_entries
                         WHERE repository_id = ? AND subject_kind = ? AND subject_key = ?
                           AND support <> 'ai_narrated' AND state IN ('current', 'uncertain')
                         LIMIT 1
                        """,
                        (narration.repository_id, narration.subject.kind, narration.subject.key),
                    ).fetchone()
                    if backed is None:
                        raise ValueError("an AI narrative needs a source-backed current subject")
                    replaced = "state IN ('current', 'uncertain')"
                else:
                    event = conn.execute(
                        "SELECT 1 FROM project_knowledge_events WHERE repository_id = ? AND generation_id = ? LIMIT 1",
                        (narration.repository_id, generation),
                    ).fetchone()
                    if event is None:
                        return "stale"
                    replaced = "state = 'historical' AND valid_from_generation = ?"
                conn.execute(
                    f"""
                    UPDATE project_knowledge_entries
                       SET state = 'superseded', updated_at = ?
                     WHERE repository_id = ? AND subject_kind = ? AND subject_key = ? AND knowledge_kind = ?
                       AND support = 'ai_narrated' AND {replaced}
                    """,
                    (
                        narration.updated_at,
                        narration.repository_id,
                        narration.subject.kind,
                        narration.subject.key,
                        narration.knowledge_kind,
                        *(() if narration.state == "current" else (generation,)),
                    ),
                )
                self._insert_entry(conn, narration)
        return "applied"

    def purge_repository(self, repository: RepositoryIdentity) -> int:
        """Delete only derived Project Knowledge for the repository."""

        self._ensure()
        with self._connect(write=True) as conn:
            with conn:
                counts = [
                    conn.execute(
                        "DELETE FROM project_knowledge_entries WHERE repository_id = ?",
                        (repository.id,),
                    ).rowcount,
                    conn.execute(
                        "DELETE FROM project_knowledge_events WHERE repository_id = ?",
                        (repository.id,),
                    ).rowcount,
                    conn.execute(
                        "DELETE FROM project_knowledge_meta WHERE repository_id = ?",
                        (repository.id,),
                    ).rowcount,
                ]
        return sum(max(0, int(count or 0)) for count in counts)

    def purge_paths(self, repository: RepositoryIdentity, paths: Sequence[str]) -> int:
        """Delete source-derived payloads supporting a hard source/path purge."""
        normalized = tuple(sorted({_canonical_path(path) for path in paths}))
        if not normalized or len(normalized) > 100:
            raise ValueError("Project Knowledge source purge requires 1 to 100 paths")
        terms = []
        args: list[Any] = [repository.id]
        for path in normalized:
            encoded = json.dumps(path, ensure_ascii=False)
            escaped = encoded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            terms.append("source_paths_json LIKE ? ESCAPE '\\'")
            args.append(f"%{escaped}%")
        self._ensure()
        with self._connect(write=True) as conn:
            count = conn.execute(
                "DELETE FROM project_knowledge_entries WHERE repository_id = ? AND (" + " OR ".join(terms) + ")", args
            ).rowcount
        return max(0, int(count or 0))

    def prune_before(self, repository: RepositoryIdentity, *, cutoff: str) -> dict[str, int]:
        """Apply retention to rebuildable historical derived rows only.

        A historical AI narrative describes one landed generation and is kept
        only while that generation's evolution event is: when retention drops
        the event, the narrative goes with it (it would otherwise render with no
        surviving source history, Gate K).
        """

        threshold = _clean_text(cutoff, "retention cutoff", max_length=128)
        self._ensure()
        with self._connect(write=True) as conn:
            with conn:
                entries = conn.execute(
                    """
                    DELETE FROM project_knowledge_entries
                     WHERE repository_id = ?
                       AND state IN ('historical', 'superseded')
                       AND updated_at < ?
                    """,
                    (repository.id, threshold),
                ).rowcount
                events = conn.execute(
                    """
                    DELETE FROM project_knowledge_events
                     WHERE repository_id = ?
                       AND observed_at < ?
                       AND generation_id <> COALESCE(
                         (SELECT current_generation
                            FROM project_knowledge_meta
                           WHERE repository_id = ?),
                         ''
                       )
                    """,
                    (repository.id, threshold, repository.id),
                ).rowcount
                orphaned = conn.execute(
                    """
                    DELETE FROM project_knowledge_entries
                     WHERE repository_id = ?
                       AND support = 'ai_narrated'
                       AND state = 'historical'
                       AND valid_from_generation NOT IN (
                         SELECT generation_id
                           FROM project_knowledge_events
                          WHERE repository_id = ?
                       )
                    """,
                    (repository.id, repository.id),
                ).rowcount
        return {
            "entries": max(0, int(entries or 0)) + max(0, int(orphaned or 0)),
            "events": max(0, int(events or 0)),
        }

    def rebuild(
        self,
        repository: RepositoryIdentity,
        projections: Sequence[KnowledgeProjection],
    ) -> str:
        """Replace derived state from an ordered deterministic projection stream."""

        if len(projections) > _MAX_REBUILD_PROJECTIONS or sum(len(p.entries) for p in projections) > _MAX_STATE_ROWS:
            raise ValueError("Project Knowledge rebuild exceeds 1000 projections or 100000 entries")
        if any(projection.generation.repository.id != repository.id for projection in projections):
            raise ValueError("rebuild projections must belong to the requested repository")

        self._ensure()
        with self._connect(write=True) as conn:
            with conn:
                conn.execute("DELETE FROM project_knowledge_entries WHERE repository_id = ?", (repository.id,))
                conn.execute("DELETE FROM project_knowledge_events WHERE repository_id = ?", (repository.id,))
                conn.execute("DELETE FROM project_knowledge_meta WHERE repository_id = ?", (repository.id,))
                for projection in projections:
                    self._apply(conn, projection)
        return self.deterministic_digest(repository)

    def deterministic_digest(self, repository: RepositoryIdentity) -> str:
        """Digest complete deterministic derived state to prove rebuild equivalence.

        AI narratives are excluded: a rebuild replays deterministic projections
        only, and narration is replaceable presentation (it drops with the rebuild).
        """

        self._ensure()
        with self._connect() as conn:
            for table in ("project_knowledge_entries", "project_knowledge_events"):
                count = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE repository_id = ?", (repository.id,)
                ).fetchone()
                if count is not None and int(count["n"]) > _MAX_STATE_ROWS:
                    raise ValueError("Project Knowledge digest exceeds 100000 rows; use scoped retention first")
            entries = [
                self._entry_from_row(row).to_wire()
                for row in conn.execute(
                    """
                    SELECT * FROM project_knowledge_entries
                     WHERE repository_id = ? AND support <> 'ai_narrated'
                     ORDER BY entry_id
                    """,
                    (repository.id,),
                ).fetchall()
            ]
            events = [
                {
                    "event_id": str(row["event_id"]),
                    "repository_id": str(row["repository_id"]),
                    "kind": str(row["kind"]),
                    "generation_id": str(row["generation_id"]),
                    "previous_generation_id": str(row["previous_generation_id"]),
                    "source_refs": list(self._unjson(str(row["source_refs_json"]))),
                    "subject_refs": list(self._unjson(str(row["subject_refs_json"]))),
                    "observed_at": str(row["observed_at"]),
                    "payload_digest": str(row["payload_digest"]),
                }
                for row in conn.execute(
                    """
                    SELECT * FROM project_knowledge_events
                     WHERE repository_id = ?
                     ORDER BY event_id
                    """,
                    (repository.id,),
                ).fetchall()
            ]
            current = self._current_generation(conn, repository.id)
        return _digest(
            "knowledge-state",
            {
                "repository_id": repository.id,
                "current_generation": current,
                "entries": entries,
                "events": events,
            },
        )


__all__ = [
    "CanonicalAdvanceProof",
    "CanonicalRefIdentity",
    "ContentAccessClass",
    "EvolutionEvent",
    "GenerationDelta",
    "KnowledgeFact",
    "KnowledgeKey",
    "KnowledgeProjection",
    "KnowledgeQuery",
    "KnowledgeQueryResult",
    "KnowledgeState",
    "KnowledgeSubject",
    "NarrationWrite",
    "ProjectKnowledgeEntry",
    "ProjectKnowledgeReviewStore",
    "ProjectKnowledgeStore",
    "RepositoryGeneration",
    "RepositoryIdentity",
    "RepositoryRef",
    "StoredRepository",
    "ValidatedSourceRef",
    "build_baseline_projection",
    "build_merged_change_projection",
    "build_source_advance_projection",
    "validate_source_ref",
    "validate_source_refs",
]
