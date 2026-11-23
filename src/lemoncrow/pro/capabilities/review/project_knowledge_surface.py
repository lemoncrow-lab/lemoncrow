"""The Project Knowledge surface (UX0b): read, hide, regenerate, correct, migrate.

One product surface over PK0's derived index and PK1's narratives:

* reads are plain views: no model call, no background work, nothing written;
* hiding an AI narrative supersedes that narrative alone -- deterministic and
  source-document history and evolution events never move;
* regenerating queues one fresh narration through BI2 (the local worker, under
  the background budget); it never runs a model in the request;
* a correction is a *proposal*: a new Markdown document as a patch the person
  applies and lands through a normal reviewed Change. Nothing here writes to the
  repository, and nothing rewrites history. Once landed on the canonical branch
  PK0 reads it as a ``source_document`` entry with ordinary provenance;
* the legacy review overlay (``.lemoncrow/review.json`` and the personal
  overlay) is labelled for what it is -- input to the older live reviewer only --
  and converts, on request, into the same kind of reviewable document. It is
  never promoted to Review policy.

Local sync records where each repository was synced from
(:class:`LocalKnowledgeSources`), because PK0 stores a repository by its digest
alone and a regenerate needs the checkout to resolve the canonical content
policy.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import pygit2

from lemoncrow.core.foundation.run_file_io import RunFileLock, atomic_write_json
from lemoncrow.pro.capabilities.review.project_knowledge import (
    KnowledgeQuery,
    KnowledgeSubject,
    KnowledgeSubjectKind,
    ProjectKnowledgeEntry,
    ProjectKnowledgeStore,
    RepositoryIdentity,
    StoredRepository,
)

__all__ = [
    "CORRECTIONS_DIR",
    "LEGACY_OVERLAY_DOCUMENT",
    "LEGACY_OVERLAY_NOTE",
    "SYNC_COMMAND",
    "KnowledgeSource",
    "KnowledgeSurfaceError",
    "LocalKnowledgeSources",
    "correction_patch",
    "hide_narrative",
    "knowledge_listing",
    "knowledge_overview",
    "legacy_overlay_patch",
    "propose_correction",
    "regenerate_narrative",
]

#: Where a proposed correction lands (any committed Markdown is a PK0 source document).
CORRECTIONS_DIR: Final[str] = "docs/project-knowledge/corrections"
LEGACY_OVERLAY_DOCUMENT: Final[str] = "docs/project-knowledge/legacy-review-overlay.md"
LEGACY_OVERLAY_NOTE: Final[str] = (
    "The legacy review overlay (.lemoncrow/review.json and your personal overlay) is read only by the older "
    "live reviewer. It is not Project Knowledge and never Review policy. `lc knowledge show` lists it; "
    "`lc knowledge overlay-patch` turns it into a document you can review and land."
)
SYNC_COMMAND: Final[str] = (
    "lc knowledge project sync --repo <checkout> --repository-key <key> --canonical-ref refs/heads/<branch>"
)

MAX_CORRECTION_CHARS: Final[int] = 4000
MAX_SOURCES: Final[int] = 100
_MAX_BODY_CHARS: Final[int] = 2000
_MAX_REFS: Final[int] = 8
_NARRATIVES: Final[int] = 50
_CONTEXT_PAGE: Final[int] = 25
_EVENTS: Final[int] = 20
_MAX_OVERLAY_ITEM: Final[int] = 500
_SOURCES_FILE: Final[str] = "project_knowledge_sources.json"
_SUPPORT_LABEL: Final[Mapping[str, str]] = {
    "deterministic": "From committed source",
    "source_document": "From a repository document",
    "ai_narrated": "AI narrative",
}
_EVENT_LABEL: Final[Mapping[str, str]] = {
    "baseline": "Baseline observed",
    "merged_change": "Landed Change",
    "source_advance": "Canonical branch advanced",
}
_NARRATIVE_KIND: Final[Mapping[str, str]] = {"narrative.subject": "subject", "narrative.change": "change"}
_CONTROL: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class KnowledgeSurfaceError(ValueError):
    """A refused surface request; ``status`` is the HTTP status a server should answer."""

    def __init__(self, message: str, *, status: int = 400, reason: str = "invalid") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# where each local repository was synced from
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class KnowledgeSource:
    """The checkout and identity one local repository was last synced from."""

    provider: str
    repository_key: str
    canonical_ref: str
    repo_root: str
    updated_at: str

    @property
    def repository(self) -> RepositoryIdentity:
        return RepositoryIdentity(self.provider, self.repository_key)

    def to_wire(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "repository_key": self.repository_key,
            "canonical_ref": self.canonical_ref,
            "repo_root": self.repo_root,
            "updated_at": self.updated_at,
        }


def _git_common_dir(path: Path) -> Path | None:
    """The Git directory shared by every worktree of the repository at *path*."""

    try:
        discovered = pygit2.discover_repository(str(path.expanduser().resolve()))
        if not discovered:
            return None
        git_dir = Path(discovered)
        link = git_dir / "commondir"  # present only in a linked worktree's Git directory
        return (git_dir / link.read_text(encoding="utf-8").strip()).resolve() if link.is_file() else git_dir.resolve()
    except (OSError, ValueError, pygit2.GitError):
        return None


class LocalKnowledgeSources:
    """``<root>/project_knowledge_sources.json``: repository id -> where it was synced from.

    Written by ``lc knowledge project sync``, removed by a repository purge
    (and by a sync of another repository once more than ``MAX_SOURCES`` are
    recorded: the least recently synced is dropped). A missing record only
    costs the surface its label and regenerate; the knowledge itself stays.
    Every read-modify-write holds one advisory lock.
    """

    def __init__(self, root: Path | str) -> None:
        self.path = Path(root) / _SOURCES_FILE

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        rows = document.get("sources") if isinstance(document, dict) else None
        if not isinstance(rows, dict):
            return {}
        return {
            str(key): {field: str(value.get(field, "")) for field in KnowledgeSource.__slots__}
            for key, value in rows.items()
            if isinstance(value, dict)
        }

    def all(self) -> dict[str, KnowledgeSource]:
        out: dict[str, KnowledgeSource] = {}
        for repository_id, row in self._read().items():
            try:
                source = KnowledgeSource(**row)
                valid = source.repository.id == repository_id
            except (TypeError, ValueError):
                continue
            if valid:
                out[repository_id] = source
        return out

    def get(self, repository_id: str) -> KnowledgeSource | None:
        return self.all().get(repository_id)

    def for_checkout(self, checkout: Path) -> KnowledgeSource | None:
        """The one source synced from *checkout*'s Git repository, else ``None``.

        Identity is the shared Git directory, so a linked worktree reads what
        its main checkout synced. Names and partial paths never match, and two
        repository keys over one Git directory are ambiguous, so neither binds.
        """

        target = _git_common_dir(checkout)
        if target is None:
            return None
        matches = [source for source in self.all().values() if _git_common_dir(Path(source.repo_root)) == target]
        return matches[0] if len(matches) == 1 else None

    def record(self, repository: RepositoryIdentity, *, canonical_ref: str, repo_root: Path) -> KnowledgeSource:
        source = KnowledgeSource(
            provider=repository.provider,
            repository_key=repository.repository_key,
            canonical_ref=canonical_ref,
            repo_root=str(repo_root.resolve()),
            updated_at=_now(),
        )
        with RunFileLock(self.path):
            rows = self._read()
            rows[repository.id] = source.to_wire()
            if len(rows) > MAX_SOURCES:
                newest = sorted(rows, key=lambda key: rows[key].get("updated_at", ""), reverse=True)
                rows = {key: rows[key] for key in newest[:MAX_SOURCES]}
            atomic_write_json(self.path, {"version": 1, "sources": rows})
        return source

    def remove(self, repository_id: str) -> bool:
        with RunFileLock(self.path):
            rows = self._read()
            if rows.pop(repository_id, None) is None:
                return False
            atomic_write_json(self.path, {"version": 1, "sources": rows})
        return True


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #


def _repository(repository_id: str) -> StoredRepository:
    try:
        return StoredRepository(str(repository_id))
    except ValueError as exc:
        raise KnowledgeSurfaceError(str(exc), status=400, reason="invalid_repository") from exc


def _bounded(values: Sequence[str]) -> tuple[list[str], int]:
    return list(values[:_MAX_REFS]), max(0, len(values) - _MAX_REFS)


def _event_wire(event: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not event:
        return None
    kind = str(event.get("kind", ""))
    return {
        "kind": kind,
        "label": _EVENT_LABEL.get(kind, kind),
        "generation_id": str(event.get("generation_id", "")),
        "observed_at": str(event.get("observed_at", "")),
    }


def _entry_wire(entry: ProjectKnowledgeEntry, events: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    source_refs, source_refs_omitted = _bounded(entry.source_refs)
    source_paths, source_paths_omitted = _bounded(entry.source_paths)
    review_refs, _ = _bounded(entry.review_refs)
    merge_refs, _ = _bounded(entry.merge_refs)
    return {
        "entry_id": entry.entry_id,
        "subject": {"kind": entry.subject.kind, "key": entry.subject.key},
        "knowledge_kind": entry.knowledge_kind,
        "narrative": _NARRATIVE_KIND.get(entry.knowledge_kind) if entry.support == "ai_narrated" else None,
        "body": entry.body[:_MAX_BODY_CHARS],
        "body_truncated": len(entry.body) > _MAX_BODY_CHARS,
        "state": entry.state,
        "support": entry.support,
        "support_label": _SUPPORT_LABEL.get(entry.support, entry.support),
        "source_refs": source_refs,
        "source_refs_omitted": source_refs_omitted,
        "source_paths": source_paths,
        "source_paths_omitted": source_paths_omitted,
        "review_refs": review_refs,
        "merge_refs": merge_refs,
        "generation": entry.valid_from_generation,
        "event": _event_wire(events.get(entry.valid_from_generation)),
        "updated_at": entry.updated_at,
    }


def knowledge_listing(store: ProjectKnowledgeStore, sources: LocalKnowledgeSources) -> dict[str, Any]:
    """Every stored repository with its counts and where it was synced from (no model call)."""

    listing = store.repositories(limit=50)
    registered = sources.all()
    return {
        "available": True,
        "scope": "local",
        "repositories": [
            {
                **row,
                "source": registered[row["repository_id"]].to_wire() if row["repository_id"] in registered else None,
            }
            for row in listing["repositories"]
        ],
        "truncated": listing["truncated"],
        "sync_command": SYNC_COMMAND,
        "legacy_overlay_note": LEGACY_OVERLAY_NOTE,
    }


def knowledge_overview(
    store: ProjectKnowledgeStore,
    sources: LocalKnowledgeSources,
    repository_id: str,
    *,
    term: str = "",
    offset: int = 0,
) -> dict[str, Any]:
    """One repository: AI narratives, current source-backed context (paged), recent evolution.

    A plain view: nothing is filtered for an AI route because nothing here
    reaches a model; path inventory rows are left out of the context list.
    """

    repository = _repository(repository_id)
    generation = store.current_generation(repository)
    if not generation:
        raise KnowledgeSurfaceError("no Project Knowledge for this repository", status=404, reason="not_found")
    try:
        context_query = KnowledgeQuery(
            supports=("deterministic", "source_document"),
            states=("current", "uncertain"),
            exclude_knowledge_kinds=("inventory",),
            term=term,
            offset=offset,
            limit=_CONTEXT_PAGE,
        )
    except ValueError as exc:
        raise KnowledgeSurfaceError(str(exc), status=400, reason="invalid_query") from exc
    narratives = store.query(
        repository,
        query=KnowledgeQuery(
            supports=("ai_narrated",), states=("current", "uncertain", "historical"), limit=_NARRATIVES
        ),
    )
    context = store.query(repository, query=context_query)
    recent = store.recent_evolution_events(repository, limit=_EVENTS)
    generations = sorted({entry.valid_from_generation for entry in (*narratives.entries, *context.entries)})
    events = store.evolution_events(repository, generations[:100])
    source = sources.get(repository.id)
    return {
        "repository": {
            "repository_id": repository.id,
            "current_generation": generation,
            "source": None if source is None else source.to_wire(),
        },
        "narratives": [_entry_wire(entry, events) for entry in narratives.entries],
        "narratives_truncated": narratives.truncated,
        "context": {
            "entries": [_entry_wire(entry, events) for entry in context.entries],
            "term": context_query.term,
            "offset": offset,
            "next_offset": context.next_offset,
        },
        "evolution": [_event_wire(event) for event in recent],
        "regenerate": (
            {"available": True, "reason": ""}
            if source is not None
            else {
                "available": False,
                "reason": "LemonCrow has no record of the checkout this repository was synced from; "
                "run `lc knowledge project sync` from it once.",
            }
        ),
    }


# --------------------------------------------------------------------------- #
# hide and regenerate (AI narratives only)
# --------------------------------------------------------------------------- #


def _narrative(store: ProjectKnowledgeStore, repository: StoredRepository, entry_id: str) -> ProjectKnowledgeEntry:
    entry = store.entry(repository, str(entry_id))
    if entry is None:
        raise KnowledgeSurfaceError("no such Project Knowledge entry", status=404, reason="not_found")
    if entry.support != "ai_narrated":
        raise KnowledgeSurfaceError(
            "only an AI narrative can be hidden or regenerated; source-backed knowledge changes only "
            "through the repository (propose a correction)",
            status=409,
            reason="not_a_narrative",
        )
    return entry


def hide_narrative(
    store: ProjectKnowledgeStore, repository_id: str, entry_id: str, *, clock: Callable[[], str] = _now
) -> dict[str, Any]:
    """Hide one AI narrative from every reader (the view, Brief, Ask, AI Review context)."""

    repository = _repository(repository_id)
    entry = _narrative(store, repository, entry_id)
    hidden = store.hide_narrative(repository, entry.entry_id, at=clock())
    return {"status": "hidden" if hidden else "already_hidden", "entry_id": entry.entry_id}


def _canonical_access_digest(source: KnowledgeSource) -> str:
    from lemoncrow.pro.capabilities.review.project_knowledge_narration import content_access_digest
    from lemoncrow.pro.capabilities.review.project_knowledge_source import CanonicalGitKnowledge

    return content_access_digest(
        CanonicalGitKnowledge(Path(source.repo_root), source.repository, source.canonical_ref).content_access()
    )


def regenerate_narrative(
    store: ProjectKnowledgeStore,
    sources: LocalKnowledgeSources,
    scheduler: Callable[[], Any],
    repository_id: str,
    entry_id: str,
    *,
    access_digest: Callable[[KnowledgeSource], str] = _canonical_access_digest,
    nonce: str = "",
) -> dict[str, Any]:
    """Queue one fresh narration of a stored narrative (BI2 decides; no model call here).

    Without a record of the checkout, or without the canonical content policy,
    nothing is queued: no content may reach a model unfiltered.
    """

    from lemoncrow.pro.capabilities.review.project_knowledge_narration import originate_narrative_regeneration

    repository_ref = _repository(repository_id)
    narrative = _narrative(store, repository_ref, entry_id)
    source = sources.get(repository_ref.id)
    if source is None:
        raise KnowledgeSurfaceError(
            "LemonCrow has no record of the checkout this repository was synced from; "
            "run `lc knowledge project sync` from it once",
            status=409,
            reason="source_unknown",
        )
    try:
        digest = access_digest(source)
    except (ValueError, OSError) as exc:
        return {
            "status": "refused",
            "reason": f"content_access_unavailable: {exc}"[:300],
            "entry_id": narrative.entry_id,
        }
    try:
        origination = originate_narrative_regeneration(
            scheduler(),
            store,
            narrative,
            repository=source.repository,
            canonical_ref=source.canonical_ref,
            repo_root=Path(source.repo_root),
            access_digest=digest,
            nonce=nonce or uuid.uuid4().hex,
        )
    except ValueError as exc:
        raise KnowledgeSurfaceError(str(exc), status=409, reason="not_regenerable") from exc
    wire = origination.to_wire()
    return {**wire, "status": "queued" if origination.decision.scheduled else "refused", "entry_id": narrative.entry_id}


# --------------------------------------------------------------------------- #
# proposals: a document the person reviews and lands
# --------------------------------------------------------------------------- #


def _document_text(text: str, *, label: str, limit: int) -> str:
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    if _CONTROL.search(normalized):
        raise KnowledgeSurfaceError(f"{label} contains control characters", reason="invalid_text")
    lines = [line.rstrip() for line in normalized.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    body = "\n".join(lines)
    if not body:
        raise KnowledgeSurfaceError(f"{label} is empty", reason="invalid_text")
    if len(body) > limit:
        raise KnowledgeSurfaceError(f"{label} exceeds {limit} characters", reason="invalid_text")
    return body


def _new_file_patch(path: str, content: str) -> str:
    lines = content.split("\n")[:-1]
    return (
        "\n".join(
            [
                f"diff --git a/{path} b/{path}",
                "new file mode 100644",
                "--- /dev/null",
                f"+++ b/{path}",
                f"@@ -0,0 +1,{len(lines)} @@",
                *(f"+{line}" for line in lines),
            ]
        )
        + "\n"
    )


def _code(value: str) -> str:
    return f"`` {value} ``" if "`" in value else f"`{value}`"


def correction_patch(
    subject: KnowledgeSubject,
    text: str,
    *,
    narrative: ProjectKnowledgeEntry | None = None,
    proposed_on: str = "",
) -> dict[str, Any]:
    """A new correction document as a patch; it governs nothing until a reviewed Change lands it."""

    body = _document_text(text, label="correction", limit=MAX_CORRECTION_CHARS)
    day = (proposed_on or _now())[:10]
    slug = re.sub(r"[^a-z0-9]+", "-", subject.key.lower()).strip("-")[:48] or subject.kind
    digest = hashlib.sha256(
        json.dumps(
            {
                "subject": [subject.kind, subject.key],
                "text": body,
                "narrative": "" if narrative is None else narrative.entry_id,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:10]
    path = f"{CORRECTIONS_DIR}/{slug}-{digest}.md"
    lines = [f"# Project Knowledge correction: {subject.key}", "", body, "", "---", ""]
    lines.append(f"- Subject: {_code(f'{subject.kind}:{subject.key}')}")
    if narrative is not None:
        lines.append(
            f"- Corrects AI narration {_code(narrative.entry_id)} "
            f"(generation {_code(narrative.valid_from_generation)})."
        )
    lines.append(f"- Proposed: {day}")
    lines.append(
        "- Project Knowledge reads this document as a source document once it lands on the canonical "
        "branch through a reviewed Change. Until then nothing changes."
    )
    content = "\n".join(lines) + "\n"
    return {"path": path, "content": content, "patch": _new_file_patch(path, content)}


def propose_correction(
    store: ProjectKnowledgeStore,
    repository_id: str,
    *,
    text: str,
    entry_id: str = "",
    subject: Mapping[str, Any] | None = None,
    proposed_on: str = "",
) -> dict[str, Any]:
    """A correction for one stored entry (or a named subject) of the repository; writes nothing."""

    repository = _repository(repository_id)
    narrative: ProjectKnowledgeEntry | None = None
    if entry_id:
        entry = store.entry(repository, entry_id)
        if entry is None:
            raise KnowledgeSurfaceError("no such Project Knowledge entry", status=404, reason="not_found")
        target = entry.subject
        narrative = entry if entry.support == "ai_narrated" else None
    elif isinstance(subject, Mapping):
        try:
            target = KnowledgeSubject(
                cast(KnowledgeSubjectKind, str(subject.get("kind", ""))), str(subject.get("key", ""))
            )
        except ValueError as exc:
            raise KnowledgeSurfaceError(str(exc), reason="invalid_subject") from exc
    else:
        raise KnowledgeSurfaceError("a correction names an entry or a subject", reason="invalid_subject")
    return correction_patch(target, text, narrative=narrative, proposed_on=proposed_on)


def _overlay_item(item: object) -> str:
    """One overlay entry on one line; a cut entry says so instead of ending mid-sentence."""

    text = " ".join(_CONTROL.sub(" ", str(item)).split())
    return text if len(text) <= _MAX_OVERLAY_ITEM else text[: _MAX_OVERLAY_ITEM - 1] + "…"


def legacy_overlay_patch(
    team: Mapping[str, Sequence[str]],
    personal: Mapping[str, Sequence[str]] | None = None,
    *,
    converted_on: str = "",
) -> dict[str, Any] | None:
    """The legacy overlay as one advisory document patch (``None`` when there is nothing to convert).

    Notes stay notes; boosted and suppressed topics become plain lists. Nothing
    is turned into a Review rule: the document is context, never policy.
    """

    sections = (
        ("notes", "Notes"),
        ("boost", "Topics the overlay emphasized"),
        ("suppress", "Topics the overlay de-emphasized"),
    )
    layers = [("Team overlay (.lemoncrow/review.json)", team)]
    if personal is not None:
        layers.append(("Personal overlay", personal))
    blocks: list[str] = []
    for title, overlay in layers:
        rows: list[str] = []
        for key, heading in sections:
            items = [_overlay_item(item) for item in overlay.get(key) or ()]
            items = [item for item in items if item]
            if items:
                rows += [f"### {heading}", "", *(f"- {item}" for item in items), ""]
        if rows:
            blocks += [f"## {title}", "", *rows]
    if not blocks:
        return None
    day = (converted_on or _now())[:10]
    lines = [
        "# Legacy review overlay (converted)",
        "",
        f"Advisory context converted from the legacy review overlay on {day}. It is not Review policy: "
        "nothing here suppresses or raises a finding. Edit or delete freely; Project Knowledge reads it as a "
        "source document once it lands on the canonical branch through a reviewed Change.",
        "",
        *blocks,
    ]
    while lines and not lines[-1]:
        lines.pop()
    content = "\n".join(lines) + "\n"
    return {
        "path": LEGACY_OVERLAY_DOCUMENT,
        "content": content,
        "patch": _new_file_patch(LEGACY_OVERLAY_DOCUMENT, content),
    }
