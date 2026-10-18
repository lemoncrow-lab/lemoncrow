"""Immutable Git adapter for PK0; never reads checkout bytes or current-index facts.

Provider/BI2 automatic origination is a separate owner. Local deterministic
inventory and docs work without a graph, provider configuration or model.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pygit2

from lemoncrow.pro.capabilities.review.project_knowledge import (
    CanonicalAdvanceProof,
    CanonicalRefIdentity,
    KnowledgeFact,
    KnowledgeKey,
    KnowledgeSubject,
    ProjectKnowledgeStore,
    RepositoryGeneration,
    RepositoryIdentity,
    build_baseline_projection,
    build_source_advance_projection,
)


class CanonicalGitKnowledge:
    """An explicitly configured canonical ref over immutable Git objects.

    The public builders accept trusted adapter facts; this adapter proves local
    ancestry and reads blobs from commits rather than accepting a boolean from a
    CLI/API caller. Explicit imported generations/provider facts are not exposed
    by this local command.
    """

    def __init__(
        self, root: Path, repository: RepositoryIdentity, canonical_ref: str, *, max_paths: int = 1000
    ) -> None:
        if not 1 <= max_paths <= 1000:
            raise ValueError("Project Knowledge path bound must be between 1 and 1000")
        git_dir = pygit2.discover_repository(str(root.resolve()))
        if git_dir is None:
            raise ValueError("Project Knowledge requires a Git repository")
        self.git = pygit2.Repository(git_dir)
        self.repository = repository
        self.ref = CanonicalRefIdentity(repository, canonical_ref)
        self.max_paths = max_paths

    def _generation(self, commit: Any) -> RepositoryGeneration:
        tree_digest = hashlib.sha256(str(commit.tree_id).encode("ascii")).hexdigest()
        return RepositoryGeneration(self.ref, str(commit.id), tree_digest=tree_digest)

    def _canonical(self) -> Any:
        try:
            return self.git.lookup_reference(self.ref.name).resolve().peel(pygit2.Commit)
        except (KeyError, ValueError, pygit2.GitError) as exc:
            raise ValueError("configured canonical Git ref is unavailable") from exc

    def _immutable_commit(self, revision: str) -> Any:
        # Reject refs, abbreviated hashes, rev syntax, trees and workspace HEAD.
        RepositoryGeneration(self.ref, revision)
        try:
            commit = self.git[pygit2.Oid(hex=revision)]
        except (KeyError, ValueError, pygit2.GitError) as exc:
            raise ValueError("immutable canonical generation is unavailable") from exc
        if not isinstance(commit, pygit2.Commit):
            raise ValueError("canonical generation must be an immutable commit")
        return commit

    def _paths(self, tree: Any, prefix: str = "") -> list[str]:
        # lc-debt: baseline inventory requires a bounded whole-tree walk;
        # add generation-backed affected-subtree bootstrap for larger repositories.
        result: list[str] = []
        pending = [(tree, prefix)]
        while pending:
            node, parent = pending.pop()
            for entry in node:
                path = parent + entry.name
                if entry.type == pygit2.GIT_OBJECT_TREE:
                    pending.append((self.git[entry.id], path + "/"))
                else:
                    result.append(path)
                    if len(result) > self.max_paths:
                        raise ValueError("Project Knowledge path bound exceeded; use an affected-path adapter")
        return sorted(result)

    def _facts(self, commit: Any, paths: list[str]) -> tuple[tuple[KnowledgeFact, ...], tuple[str, ...]]:
        generation = self._generation(commit)
        facts = []
        degraded = []
        for path in sorted(set(paths)):
            try:
                entry = commit.tree[path]
            except KeyError:
                continue
            if entry.filemode not in (pygit2.GIT_FILEMODE_BLOB, pygit2.GIT_FILEMODE_BLOB_EXECUTABLE):
                degraded.append(f"unsupported Git object: {path}")
                continue
            facts.append(
                KnowledgeFact(
                    subject=KnowledgeSubject("path", path),
                    knowledge_kind="inventory",
                    body=f"Committed path {path}; Git blob {entry.id}.",
                    source_refs=(generation.source_ref,),
                    source_paths=(path,),
                )
            )
            if Path(path).suffix.lower() not in {".md", ".rst", ".txt"}:
                continue
            blob = cast(pygit2.Blob, self.git[entry.id])
            if blob.size > 65536:
                degraded.append(f"document exceeds 65536 bytes: {path}")
                continue
            try:
                text = blob.data.decode("utf-8")
            except UnicodeDecodeError:
                degraded.append(f"document is not UTF-8: {path}")
                continue
            excerpt = " ".join(text.split())[:12000]
            if not excerpt:
                continue
            if len(" ".join(text.split())) > 12000:
                degraded.append(f"document excerpt limited to 12000 characters: {path}")
            facts.append(
                KnowledgeFact(
                    subject=KnowledgeSubject("path", path),
                    knowledge_kind="documentation",
                    body=excerpt,
                    support="source_document",
                    source_refs=(f"source:new:{path}@sha256:{hashlib.sha256(blob.data).hexdigest()}",),
                )
            )
        return tuple(facts), tuple(degraded)

    def content_access(self) -> Any:
        """Resolve retrieval policy from current canonical blobs, never workspace."""
        from lemoncrow.pro.capabilities.review.rules import resolve_review_governance
        from lemoncrow.pro.capabilities.review.trusted_base import resolve_trusted_base_document

        commit = self._canonical()

        def document(path: str) -> Any:
            try:
                entry = commit.tree[path]
            except KeyError:
                return resolve_trusted_base_document(path, "")
            if entry.filemode != pygit2.GIT_FILEMODE_BLOB:
                raise ValueError("canonical content policy must be a regular committed file")
            blob = cast(pygit2.Blob, self.git[entry.id])
            if blob.size > 65536:
                raise ValueError("canonical content policy exceeds 65536 bytes")
            try:
                content = blob.data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("canonical content policy is not UTF-8") from exc
            return resolve_trusted_base_document(path, content)

        governance = resolve_review_governance(document(".lemoncrow/review.yaml"), read_base_document=document)
        if governance.errors or governance.base_state == "provisional":
            raise ValueError("canonical content policy is invalid or unavailable")
        return governance.content_access

    def sync(
        self, store: ProjectKnowledgeStore, *, previous_revision: str = "", rebuild: bool = False
    ) -> dict[str, Any]:
        commit = self._canonical()
        generation = self._generation(commit)
        observed_at = datetime.fromtimestamp(commit.commit_time, UTC).isoformat()
        current = store.current_generation(self.repository)
        previous = self._immutable_commit(previous_revision) if previous_revision else None
        if previous is not None and self._generation(previous).id != current:
            raise ValueError("previous revision does not match stored canonical generation")
        if current == generation.id and not rebuild:
            return {"applied": False, "kind": "unchanged", "generation": generation.to_wire()}
        if rebuild or not current:
            paths = self._paths(commit.tree)
            facts, degraded = self._facts(commit, paths)
            projection = build_baseline_projection(generation, facts=facts, observed_at=observed_at)
            if self._canonical().id != commit.id:
                raise ValueError("canonical ref advanced during bootstrap; retry")
            if rebuild:
                store.rebuild(self.repository, (projection,))
                applied = True
            else:
                applied = store.apply(projection)
        else:
            if previous is None:
                raise ValueError("canonical advance requires the stored --previous-revision")
            if previous.id == commit.id or not self.git.descendant_of(commit.id, previous.id):
                raise ValueError("configured canonical ref must advance to an immutable descendant")
            affected: set[str] = set()
            for delta in self.git.diff(previous.tree, commit.tree).deltas:
                affected.update((delta.old_file.path, delta.new_file.path))
                if len(affected) > self.max_paths:
                    raise ValueError("Project Knowledge affected path bound exceeded")
            paths = sorted(affected)
            facts, degraded = self._facts(commit, paths)
            retire = tuple(
                KnowledgeKey(KnowledgeSubject("path", path), kind)
                for path in paths
                for kind in ("inventory", "documentation")
            )
            projection = build_source_advance_projection(
                self._generation(previous),
                generation,
                proof=CanonicalAdvanceProof("local_canonical_ref", self.ref.name, descendant=True),
                facts=facts,
                retire=retire,
                observed_at=observed_at,
            )
            if self._canonical().id != commit.id:
                raise ValueError("canonical ref advanced during refresh; retry")
            applied = store.apply(projection)
        # This is an immutable observation, not a claim that Git cannot move
        # after publication. Store.apply compares the source-generation predecessor.
        return {
            "applied": applied,
            "kind": projection.event.kind,
            "generation": generation.to_wire(),
            "entries": len(projection.entries),
            "degraded_reasons": list(degraded),
            "coverage": "path_inventory_and_document_excerpts",
            "canonical_ref_advanced_during_sync": str(self._canonical().id) != str(commit.id),
        }
