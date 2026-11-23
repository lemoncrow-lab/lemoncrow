"""Immutable Git adapter for PK0; never reads checkout bytes or current-index facts.

Provider/BI2 automatic origination is a separate owner. Local deterministic
inventory and docs work without a graph, provider configuration or model.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

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

_DOCUMENT_SUFFIXES: Final[frozenset[str]] = frozenset({".md", ".rst", ".txt"})
#: A tree larger than this is refused even for a partial baseline: walking it is unbounded work.
_MAX_TREE_PATHS: Final[int] = 200_000
#: Canonical commits walked to place a generation in Git history.
_MAX_ORDER_COMMITS: Final[int] = 4096


def _is_document(path: str) -> bool:
    return Path(path).suffix.lower() in _DOCUMENT_SUFFIXES


class CanonicalGitKnowledge:
    """An explicitly configured canonical ref over immutable Git objects.

    The public builders accept trusted adapter facts; this adapter proves local
    ancestry and reads blobs from commits rather than accepting a boolean from a
    CLI/API caller. Explicit imported generations/provider facts are not exposed
    by this local command.
    """

    def __init__(
        self,
        root: Path,
        repository: RepositoryIdentity,
        canonical_ref: str,
        *,
        max_paths: int = 1000,
        include_document_bodies: bool = True,
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
        self.include_document_bodies = include_document_bodies

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

    def _paths(self, tree: Any, *, limit: int | None = None) -> list[str]:
        bound = self.max_paths if limit is None else limit
        result: list[str] = []
        pending = [(tree, "")]
        while pending:
            node, parent = pending.pop()
            for entry in node:
                path = parent + entry.name
                if entry.type == pygit2.GIT_OBJECT_TREE:
                    pending.append((self.git[entry.id], path + "/"))
                else:
                    result.append(path)
                    if len(result) > bound:
                        raise ValueError("Project Knowledge path bound exceeded; use an affected-path adapter")
        return sorted(result)

    def _baseline_paths(self, tree: Any, *, partial: bool) -> tuple[list[str], int]:
        """The paths a baseline records, and how many committed paths it leaves out.

        Without *partial* a tree over ``max_paths`` is refused. With it the
        baseline keeps repository documents first, then the remaining paths in
        order: documents are what Review reads, a bare inventory row is not.
        """

        # lc-debt: a partial baseline has no inventory row for the omitted paths until a
        # later advance touches them; add generation-backed subtree bootstrap to cover them.
        paths = self._paths(tree, limit=_MAX_TREE_PATHS if partial else self.max_paths)
        if len(paths) <= self.max_paths:
            return paths, 0
        ranked = sorted(paths, key=lambda path: (not _is_document(path), path))
        return sorted(ranked[: self.max_paths]), len(paths) - self.max_paths

    def generation_order(
        self, generation_ids: Iterable[str], base_revision: str
    ) -> dict[str, Literal["predates", "after_base"]]:
        """Place stored generations against a Change's Git base using Git ancestry.

        A generation is matched by its exact identity on the canonical branch,
        so only a commit this adapter would itself have recorded can answer.
        Anything not found within the walk, or unrelated to the base, is absent.
        """

        wanted = set(generation_ids)
        base = self._immutable_commit(base_revision)
        order: dict[str, Literal["predates", "after_base"]] = {}
        walker = self.git.walk(self._canonical().id, pygit2.GIT_SORT_TOPOLOGICAL)  # type: ignore[arg-type]
        for count, commit in enumerate(walker):
            if count >= _MAX_ORDER_COMMITS or not wanted:
                break
            generation = self._generation(commit).id
            if generation not in wanted:
                continue
            wanted.discard(generation)
            if commit.id == base.id or self.git.descendant_of(base.id, commit.id):
                order[generation] = "predates"
            elif self.git.descendant_of(commit.id, base.id):
                order[generation] = "after_base"
        return order

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
            if not self.include_document_bodies or not _is_document(path):
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

    def bootstrap_metadata_baseline(self, knowledge: ProjectKnowledgeStore) -> dict[str, Any]:
        """Record a canonical generation without walking entire monorepo trees.

        An operator calls this *before* accepting new merges. It creates no
        source assertions and no synthetic Review facts; future provider
        observations add changed-path facts against this exact commit. Never
        rebuilds or overwrites already-ingested historical generations.
        """
        commit = self._canonical()
        generation = self._generation(commit)
        stored = knowledge.current_generation(self.repository)
        if stored:
            if stored != generation.id:
                raise ValueError("canonical Project Knowledge already started at a different commit")
            return {"applied": False, "kind": "unchanged", "generation": generation.to_wire()}
        observed_at = datetime.fromtimestamp(commit.commit_time, UTC).isoformat()
        projection = build_baseline_projection(generation, facts=(), observed_at=observed_at)
        if self._canonical().id != commit.id:
            raise ValueError("canonical branch moved while bootstrapping the baseline")
        return {
            "applied": knowledge.apply(projection),
            "kind": "baseline",
            "coverage": "canonical_metadata_only",
            "generation": generation.to_wire(),
        }

    def previous_stored_commit(self, knowledge: ProjectKnowledgeStore, *, max_commits: int = 4096) -> str:
        """Find the exact persisted canonical generation in a bounded commit graph.

        The existing store holds a generation digest, not a caller-authored SHA.
        A provider mirror may have advanced since its last observation. Search
        immutable reachable objects and require the *same* generation identity;
        do not guess HEAD^, fall back to the Review base, or silently rebuild.
        """
        stored = knowledge.current_generation(self.repository)
        if not stored:
            raise ValueError("canonical Project Knowledge baseline is not initialized")
        head = self._canonical()
        # pygit2 exports a runtime int constant; some installed stubs declare
        # a separate SortMode enum. The runtime constant is the correct mode.
        for count, commit in enumerate(self.git.walk(head.id, pygit2.GIT_SORT_TOPOLOGICAL)):  # type: ignore[arg-type]
            if count >= max_commits:
                raise ValueError("stored canonical generation is beyond bounded history; reconcile explicitly")
            if self._generation(commit).id == stored:
                return str(commit.id)
        raise ValueError("stored canonical generation is not reachable from the configured branch")

    def sync_landed_review(
        self,
        knowledge: ProjectKnowledgeStore,
        review_store: Any,
        review_id: str,
        revision_id: str,
        *,
        merge: Any,
        previous_revision: str,
        counted_evidence: Any,
        provider_merge_result_sha: str = "",
    ) -> dict[str, Any]:
        """Persist one provider-observed Review and immutable canonical delta together.

        This entry point is for a trusted provider observer, not a public CLI
        argument or generic webhook payload. The caller must have obtained
        `merge` via `post_merge_actions.observed_merge`. The Git adapter checks
        actual canonical ancestry; the landed Review adapter checks the exact
        provider-subject/landing and persisted Snapshot provenance.
        """
        from lemoncrow.pro.capabilities.review.landed_review_knowledge import build_landed_review_knowledge

        if not previous_revision:
            raise ValueError("landed review requires an explicit previous immutable canonical revision")
        original = self._immutable_commit(previous_revision)
        commit = self._canonical()
        before = self._generation(original)
        current = self._generation(commit)
        stored = knowledge.current_generation(self.repository)
        if stored == current.id:
            event = knowledge.evolution_events(self.repository, (current.id,)).get(current.id, {})
            refs = set(event.get("source_refs", ()))
            identity = f"merge:{merge.provider}:{merge.ref}"
            # A replay is safe only for *the same* landing, Review and exact
            # Snapshot. Another tenant/Review cannot claim somebody else's
            # canonical generation by recycling a provider merge reference.
            if event.get("kind") == "merged_change" and {
                identity,
                f"review:{review_id}",
                f"revision:{revision_id}",
            }.issubset(refs):
                return {"applied": False, "kind": "unchanged", "generation": current.to_wire()}
            raise ValueError("canonical generation already advanced without this Review history")
        if stored != before.id:
            raise ValueError("previous revision does not match stored canonical generation")
        if commit.id == original.id or not self.git.descendant_of(commit.id, original.id):
            raise ValueError("canonical ref did not advance to a descendant Git commit")
        # A provider-identified immutable reviewed head must be included in the
        # landed canonical history, not merely present in some unrelated branch.
        reviewed_head = str(merge.reviewed_head or "")
        if reviewed_head:
            reviewed_commit = self._immutable_commit(reviewed_head)
            if commit.id == reviewed_commit.id:
                # A fast-forward is attributable only when the reviewed
                # Snapshot started at the *stored* canonical generation. If
                # another PR was already in the history between Snapshot base
                # and that generation, the resulting delta could claim work
                # never reviewed in this Change.
                snapshot = review_store.get_revision(revision_id)
                if snapshot is None or str(getattr(snapshot, "base_sha", "")) != str(original.id):
                    raise ValueError(
                        "canonical merge window is ambiguous; fast-forward base differs from stored generation"
                    )
            else:
                # Normal merge: the first parent is our stored generation,
                # the second is exactly the reviewed PR head.
                parents = tuple(str(parent.id) for parent in commit.parents)
                exact_merge = parents == (str(original.id), str(reviewed_commit.id))
                if not exact_merge:
                    # GitHub squash (or a one-commit rebase) rewrites the SHA.
                    # Trust this alternative ONLY when the already-persisted
                    # provider PR merge result names the exact canonical tip,
                    # its single parent is our prior generation, and the
                    # resulting tree is identical to the reviewed Snapshot.
                    # Multi-commit/interleaved histories still fail closed.
                    snapshot = review_store.get_revision(revision_id)
                    proven_single_rewrite = (
                        provider_merge_result_sha == str(commit.id)
                        and parents == (str(original.id),)
                        and snapshot is not None
                        and str(getattr(snapshot, "base_sha", "")) == str(original.id)
                        and commit.tree_id == reviewed_commit.tree_id
                    )
                    if not proven_single_rewrite:
                        raise ValueError(
                            "canonical merge window is ambiguous; cannot attribute later commits to this Review"
                        )
        changed: set[str] = set()
        for delta in self.git.diff(original.tree, commit.tree).deltas:
            changed.update((delta.old_file.path, delta.new_file.path))
            if len(changed) > self.max_paths:
                raise ValueError("Project Knowledge affected path bound exceeded")
        source_facts, degraded = self._facts(commit, sorted(changed))
        retire = tuple(
            KnowledgeKey(KnowledgeSubject("path", path), kind)
            for path in sorted(changed)
            for kind in ("inventory", "documentation")
        )
        projection = build_landed_review_knowledge(
            review_store,
            review_id,
            revision_id,
            before,
            current,
            proof=CanonicalAdvanceProof("provider_observed", self.ref.name, descendant=True),
            merge=merge,
            counted_evidence=counted_evidence,
            source_facts=source_facts,
            retire=retire,
        )
        if self._canonical().id != commit.id:
            raise ValueError("canonical ref changed while preparing landed history; retry")
        applied = knowledge.apply(projection)
        return {
            "applied": applied,
            "kind": projection.event.kind,
            "generation": current.to_wire(),
            "entries": len(projection.entries),
            "degraded_reasons": list(degraded),
            "coverage": "canonical_source_and_verified_review_history",
        }

    def sync(
        self,
        store: ProjectKnowledgeStore,
        *,
        previous_revision: str = "",
        rebuild: bool = False,
        partial_baseline: bool = False,
    ) -> dict[str, Any]:
        """Bootstrap or advance from immutable objects.

        *partial_baseline* lets a tree over ``max_paths`` start from a bounded,
        documents-first baseline whose result reports what it left out.
        """

        commit = self._canonical()
        generation = self._generation(commit)
        observed_at = datetime.fromtimestamp(commit.commit_time, UTC).isoformat()
        current = store.current_generation(self.repository)
        previous = self._immutable_commit(previous_revision) if previous_revision else None
        if previous is not None and self._generation(previous).id != current:
            raise ValueError("previous revision does not match stored canonical generation")
        if current == generation.id and not rebuild:
            return {"applied": False, "kind": "unchanged", "generation": generation.to_wire()}
        omitted = 0
        if rebuild or not current:
            paths, omitted = self._baseline_paths(commit.tree, partial=partial_baseline)
            facts, degraded = self._facts(commit, paths)
            if omitted:
                degraded = (
                    *degraded,
                    f"baseline limited to {len(paths)} of {len(paths) + omitted} committed paths, documents first",
                )
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
            "coverage": "partial_documents_first" if omitted else "path_inventory_and_document_excerpts",
            "paths_omitted": omitted,
            "canonical_ref_advanced_during_sync": str(self._canonical().id) != str(commit.id),
        }
