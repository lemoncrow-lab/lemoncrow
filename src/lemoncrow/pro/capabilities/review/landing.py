"""Provider-neutral landing/readiness primitives for durable Review Changes.

LemonCrow owns the human-visible conditions and the explicit arm intent. The
connected SCM owns merge/submit execution. Nothing in this module performs Git
merges, rebases, pushes, or branch mutation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship, ReviewRevision
    from lemoncrow.pro.capabilities.review.store import ReviewStore

ARMED_LANDING_STATES = frozenset({"armed", "delegated", "queued"})
#: Condition states that do not block landing. ``waived`` is a human override
#: shown as such, never as ``pass``.
SATISFIED_CONDITION_STATES = frozenset({"pass", "not_applicable", "waived"})

_PASS_STATES = frozenset({"pass", "passed", "success", "satisfied", "ready", "complete", "completed", "merged"})
_FAIL_STATES = frozenset({"fail", "failed", "blocked", "rejected", "error"})


def normalized_requirement_state(value: object) -> str:
    state = str(value or "").strip().lower()
    if state in _PASS_STATES:
        return "pass"
    if state in _FAIL_STATES:
        return "fail"
    return "pending"


def condition(
    identifier: str,
    label: str,
    *,
    state: str,
    detail: str,
    owner: str = "lemoncrow",
    category: str = "review",
    url: str = "",
) -> dict[str, Any]:
    normalized = state if state in {"pass", "fail", "pending", "not_applicable", "waived"} else "pending"
    return {
        "id": identifier,
        "label": label,
        "state": normalized,
        "detail": detail,
        "owner": owner if owner in {"lemoncrow", "scm"} else "lemoncrow",
        "category": category,
        "url": url,
    }


def conditions_digest(conditions: Sequence[Mapping[str, Any]]) -> str:
    """Stable compare-and-swap identity for the exact conditions a human saw."""

    normalized = [
        {
            "id": str(item.get("id") or ""),
            "label": str(item.get("label") or ""),
            "state": str(item.get("state") or ""),
            "detail": str(item.get("detail") or ""),
            "owner": str(item.get("owner") or ""),
            "category": str(item.get("category") or ""),
            "url": str(item.get("url") or ""),
        }
        for item in conditions
    ]
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def readiness_summary(conditions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    blockers = [item for item in conditions if str(item.get("state") or "") not in SATISFIED_CONDITION_STATES]
    local_blockers = [item for item in blockers if str(item.get("owner") or "lemoncrow") != "scm"]
    return {
        "ready": not blockers,
        "armable": not local_blockers,
        "blocker_count": len(blockers),
        "local_blocker_count": len(local_blockers),
        "conditions_hash": conditions_digest(conditions),
    }


def landing_relationship(store: ReviewStore, review_id: str) -> ReviewRelationship | None:
    """The single durable Merge-when-ready intent for one Change, if any."""

    return next(
        (item for item in store.list_relationships(review_id, kind="landing") if item.relation == "merge_when_ready"),
        None,
    )


def landing_metadata(item: Any | None) -> dict[str, Any]:
    if item is None:
        return {}
    try:
        raw = json.loads(str(getattr(item, "metadata_json", "{}") or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def record_landing_state(
    store: ReviewStore,
    landing: ReviewRelationship,
    *,
    status: str,
    metadata_updates: Mapping[str, Any],
) -> ReviewRelationship:
    """Rewrite the landing intent's status while preserving its identity/history."""

    from dataclasses import replace

    metadata = landing_metadata(landing)
    metadata.update(dict(metadata_updates))
    return store.upsert_relationship(
        replace(
            landing,
            id="",
            status=status,
            metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        )
    )


def mark_landing_stale_for_revision(
    store: ReviewStore,
    review_id: str,
    revision: ReviewRevision,
) -> ReviewRelationship | None:
    """Store-only invalidation shared by every Snapshot capture path.

    A semantically different Snapshot must never inherit an armed landing
    intent. Capture paths do not all hold the provider delegate, so this marks
    the intent stale and records that provider cancellation is still owed;
    :func:`complete_pending_landing_cancel` performs it where the delegate lives.
    """

    landing = landing_relationship(store, review_id)
    if landing is None or landing.status not in ARMED_LANDING_STATES:
        return None
    armed_tree = str(landing_metadata(landing).get("tree_fingerprint") or "")
    if armed_tree and armed_tree == revision.tree_fingerprint:
        return None
    return record_landing_state(
        store,
        landing,
        status="stale",
        metadata_updates={
            "detail": ("Semantic Snapshot changed after landing was armed; provider landing cancellation is pending."),
            "provider_cancel": "pending",
            "invalidated_by_revision_id": revision.id,
            "invalidated_by_tree_fingerprint": revision.tree_fingerprint,
        },
    )


def cancel_settlement(state: str) -> str:
    """The landing status a provider's answer to *cancel* settles the intent to.

    ``merged``: the provider had already merged the reviewed head. ``blocked``:
    it had already merged something else (an unreviewed head), which must be
    shown as such and never as a routine cancel. ``failed``: withdrawal itself
    failed. Anything else means provider landing was withdrawn: ``cancelled``.
    """

    return state if state in {"merged", "blocked", "failed"} else "cancelled"


def complete_pending_landing_cancel(
    store: ReviewStore,
    review_id: str,
    cancel: Callable[[], Mapping[str, Any]],
) -> ReviewRelationship | None:
    """Cancel provider landing owed by :func:`mark_landing_stale_for_revision`."""

    landing = landing_relationship(store, review_id)
    if landing is None or landing_metadata(landing).get("provider_cancel") != "pending":
        return None
    try:
        result = cancel()
        state = str(result.get("state") or "cancelled")
        detail = str(result.get("detail") or "")
    except Exception as exc:  # provider boundary: preserve the exact failure
        state, detail = "failed", str(exc)
    if cancel_settlement(state) in {"merged", "blocked"}:
        # The provider landed before it could be withdrawn: that is history
        # (the reviewed head) or an unreviewed merge to surface, never a cancel.
        return record_landing_state(
            store,
            landing,
            status=state,
            metadata_updates={
                "provider_cancel": "done",
                "cancel_state": state,
                "provider_state": state,
                "detail": detail,
            },
        )
    failed = state == "failed"
    return record_landing_state(
        store,
        landing,
        status=landing.status,
        metadata_updates={
            "provider_cancel": "failed" if failed else "done",
            "cancel_state": state,
            "detail": (
                f"Semantic Snapshot changed; provider landing cancellation failed: {detail}"
                if failed
                else "Semantic Snapshot changed; provider landing was cancelled and must be re-armed after review."
            ),
        },
    )


_MERGEABLE_STATES = frozenset({"CLEAN", "HAS_HOOKS", "UNSTABLE"})
_MERGE_METHOD_FLAGS = {"MERGE": "--merge", "SQUASH": "--squash", "REBASE": "--rebase"}
_PULL_REQUEST_STATE_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
    "pullRequest(number:$number){id state isDraft headRefOid mergeStateStatus isInMergeQueue url "
    "autoMergeRequest{enabledAt}}"
    "viewerDefaultMergeMethod mergeCommitAllowed squashMergeAllowed rebaseMergeAllowed}}"
)
_DEQUEUE_MUTATION = "mutation($id:ID!){dequeuePullRequest(input:{id:$id}){clientMutationId}}"


def _github_result(state: str, detail: str, url: str, *, mechanism: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"supported": True, "state": state, "provider": "github", "detail": detail, "url": url}
    if mechanism:
        result["mechanism"] = mechanism
    return result


def _github_pull_request_state(pr: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError, gh_json, run_gh

    payload = gh_json(
        lambda args, stdin: run_gh(args, stdin),
        [
            "api",
            "graphql",
            "-f",
            f"query={_PULL_REQUEST_STATE_QUERY}",
            "-f",
            f"owner={pr.owner}",
            "-f",
            f"name={pr.repo}",
            "-F",
            f"number={pr.number}",
        ],
    )
    data = payload.get("data") if isinstance(payload, Mapping) else None
    repository = data.get("repository") if isinstance(data, Mapping) else None
    pull = repository.get("pullRequest") if isinstance(repository, Mapping) else None
    if not isinstance(repository, Mapping) or not isinstance(pull, Mapping):
        raise GitHubCliError("GitHub returned an unexpected pull-request shape")
    return pull, repository


def _merge_method_flag(repository: Mapping[str, Any]) -> str | None:
    allowed = {
        "MERGE": bool(repository.get("mergeCommitAllowed")),
        "SQUASH": bool(repository.get("squashMergeAllowed")),
        "REBASE": bool(repository.get("rebaseMergeAllowed")),
    }
    preferred = str(repository.get("viewerDefaultMergeMethod") or "").upper()
    for method in (preferred, "MERGE", "SQUASH", "REBASE"):
        if allowed.get(method):
            return _MERGE_METHOD_FLAGS[method]
    return None


def _withdraw_provider_landing(pr: Any, pull: Mapping[str, Any]) -> list[str]:
    """Remove every GitHub-side landing that could merge a later head."""

    from lemoncrow.pro.capabilities.review.sources.github import run_gh

    withdrawn: list[str] = []
    if pull.get("autoMergeRequest"):
        run_gh(["pr", "merge", str(pr.number), "--repo", pr.slug, "--disable-auto"])
        withdrawn.append("GitHub auto-merge was disabled")
    if pull.get("isInMergeQueue") and pull.get("id"):
        run_gh(["api", "graphql", "-f", f"query={_DEQUEUE_MUTATION}", "-f", f"id={pull['id']}"])
        withdrawn.append("the pull request was removed from the GitHub merge queue")
    return withdrawn


def _merge_exact_head(pr: Any, repository: Mapping[str, Any], head_sha: str, url: str) -> dict[str, Any]:
    from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError, run_gh

    flag = _merge_method_flag(repository)
    if flag is None:
        return _github_result("blocked", "The repository allows no merge method for this GitHub account.", url)
    # GitHub checks --match-head-commit atomically at merge time: a push that
    # lands after this Snapshot was reviewed makes the merge fail instead of
    # silently merging unreviewed code. Never --auto and never --admin.
    try:
        run_gh(["pr", "merge", str(pr.number), "--repo", pr.slug, "--match-head-commit", head_sha, flag])
    except GitHubCliError:
        # Another merge of the same pull request (the landing watch, the arm
        # request, or a human on GitHub) may have won; record what GitHub did.
        raced, _repository = _github_pull_request_state(pr)
        if str(raced.get("state") or "").upper() != "MERGED" or str(raced.get("headRefOid") or "") != head_sha:
            raise
    after, _repository = _github_pull_request_state(pr)
    if str(after.get("state") or "").upper() == "MERGED":
        return _github_result(
            "merged",
            f"GitHub merged exactly the reviewed head {head_sha[:12]}.",
            url,
            mechanism="exact_head_merge",
        )
    return _github_result(
        "queued",
        f"GitHub accepted the reviewed head {head_sha[:12]} into its merge queue; LemonCrow withdraws it if the head moves.",
        url,
        mechanism="exact_head_merge",
    )


def _local_github_step(
    session: Any,
    revision: Any,
    *,
    action: str,
    may_merge: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError, parse_pr_source_ref

    pr = parse_pr_source_ref(str(getattr(session, "source_ref", "") or ""))
    if pr is None:
        return {
            "supported": False,
            "state": "unsupported",
            "provider": "",
            "detail": "No connected SCM landing target is available.",
        }
    head_sha = str(getattr(revision, "head_sha", "") or "")
    try:
        pull, repository = _github_pull_request_state(pr)
        url = str(pull.get("url") or pr.url)
        if action not in {"arm", "reconcile", "cancel"}:
            return _github_result("failed", f"Unknown landing action {action!r}.", url)
        state = str(pull.get("state") or "").upper()
        remote_head = str(pull.get("headRefOid") or "")
        if state == "MERGED":
            # Whatever was asked, a merge GitHub already performed is history.
            if head_sha and remote_head == head_sha:
                return _github_result("merged", f"GitHub merged the reviewed head {head_sha[:12]}.", url)
            return _github_result(
                "blocked",
                f"GitHub merged head {remote_head[:12] or 'unknown'}, not the reviewed head {head_sha[:12] or 'unknown'}.",
                url,
            )
        if action == "cancel":
            withdrawn = _withdraw_provider_landing(pr, pull)
            detail = "; ".join(withdrawn) + "." if withdrawn else ""
            return _github_result(
                "cancelled",
                detail[:1].upper() + detail[1:]
                or "No GitHub-side landing was pending; the LemonCrow intent was withdrawn.",
                url,
            )
        if not head_sha:
            return _github_result("failed", "This Snapshot has no immutable GitHub head commit.", url)
        if state != "OPEN":
            return _github_result("blocked", "GitHub pull request was closed without merging.", url)
        if remote_head != head_sha:
            withdrawn = _withdraw_provider_landing(pr, pull)
            moved = f"GitHub head moved from {head_sha[:12]} to {remote_head[:12] or 'unknown'}"
            if action == "arm":
                return _github_result(
                    "blocked", f"{moved}; capture the new Snapshot and review it before arming landing.", url
                )
            return _github_result(
                "stale",
                f"{moved} after landing was armed; the new head has not been reviewed"
                + (f" ({'; '.join(withdrawn)})" if withdrawn else "")
                + ". Review the new Snapshot and re-arm.",
                url,
            )
        if bool(pull.get("isDraft")):
            return _github_result(
                "blocked" if action == "arm" else "armed",
                "GitHub pull request is still a draft.",
                url,
                mechanism="" if action == "arm" else "lemoncrow_watch",
            )
        withdrawn = []
        if action == "arm" and pull.get("autoMergeRequest"):
            # Provider auto-merge follows later pushes by writers; replace it
            # with the exact-head intent the human is arming.
            withdrawn = _withdraw_provider_landing(pr, {"autoMergeRequest": pull.get("autoMergeRequest")})
        elif pull.get("isInMergeQueue"):
            return _github_result(
                "queued",
                f"The reviewed head {head_sha[:12]} is in the GitHub merge queue.",
                url,
                mechanism="exact_head_merge",
            )
        merge_state = str(pull.get("mergeStateStatus") or "").upper()
        if merge_state in _MERGEABLE_STATES:
            if may_merge is not None and not may_merge():
                return _github_result(
                    "armed", "The landing intent changed while GitHub was checked; nothing merged.", url
                )
            return _merge_exact_head(pr, repository, head_sha, url)
        detail = (
            f"Waiting for GitHub requirements ({merge_state.lower() or 'unknown'}); LemonCrow merges exactly the "
            f"reviewed head {head_sha[:12]} once they pass. The check runs while the local LemonCrow server is running."
        )
        if withdrawn:
            detail = f"{'; '.join(withdrawn)}. {detail}"
        return _github_result("armed", detail, url, mechanism="lemoncrow_watch")
    except GitHubCliError as exc:
        return {"supported": True, "state": "failed", "provider": "github", "detail": str(exc), "url": pr.url}


def local_github_landing(session: Any, revision: Any, *, action: str) -> dict[str, Any]:
    """Land a local GitHub pull request only at its exact reviewed head.

    GitHub auto-merge keeps following new pushes by anyone with write access,
    so it cannot carry a human judgment about one immutable Snapshot. Arming
    therefore never enables it: LemonCrow holds the intent and asks GitHub to
    merge the reviewed head with ``--match-head-commit`` once GitHub reports
    the pull request mergeable (now, or later from the local landing watch).
    GitHub stays authoritative for the merge itself and for merge queues.
    LemonCrow never invokes ``git merge``/``git rebase``.
    """

    return _local_github_step(session, revision, action=action)


def _record_if_unchanged(
    store: ReviewStore,
    landing: ReviewRelationship,
    *,
    status: str,
    metadata_updates: Mapping[str, Any],
) -> ReviewRelationship | None:
    """A watch write that loses to any concurrent human cancel, re-arm or capture."""

    metadata = landing_metadata(landing)
    metadata.update(dict(metadata_updates))
    written = store.update_relationship_if_unchanged(
        landing,
        status=status,
        metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
    )
    return written if written is not None else landing_relationship(store, landing.review_id)


def _withdraw_discarded_landing(
    store: ReviewStore,
    session: Any,
    landing: ReviewRelationship,
    armed_revision: ReviewRevision | None,
) -> ReviewRelationship | None:
    """A discarded Change never lands: withdraw whatever GitHub could still merge."""

    result = _local_github_step(session, armed_revision, action="cancel")
    state = str(result.get("state") or "")
    detail = str(result.get("detail") or "")
    if state == "failed":
        # Keep the intent armed so the next watch pass retries the withdrawal;
        # the watch itself never merges a discarded Change.
        error = f"The Change was discarded, but withdrawing GitHub landing failed: {detail}"[:500]
        if landing_metadata(landing).get("watch_error") == error:
            return landing
        return _record_if_unchanged(store, landing, status=landing.status, metadata_updates={"watch_error": error})
    settled = cancel_settlement(state)
    updates: dict[str, Any] = {"cancel_state": state, "provider_state": state, "watch_error": ""}
    if settled in {"merged", "blocked"}:
        updates["detail"] = detail
    else:
        updates["detail"] = "The Change was discarded; Merge when ready was withdrawn." + (
            f" {detail}" if detail else ""
        )
    if settled == "merged":
        return record_landing_state(store, landing, status=settled, metadata_updates=updates)
    return _record_if_unchanged(store, landing, status=settled, metadata_updates=updates)


def reconcile_local_github_landing(store: ReviewStore, session: Any) -> ReviewRelationship | None:
    """Advance one armed local GitHub intent; never merges an unreviewed head.

    Every write here is compare-and-set against the intent as read, so a human
    cancel (or a new Snapshot) that lands while GitHub is being asked always
    wins; only a merge GitHub already performed is recorded unconditionally.
    A discarded Change is never merged: its intent is withdrawn instead.
    """

    landing = landing_relationship(store, session.id)
    if landing is None or landing.status not in ARMED_LANDING_STATES:
        return None
    metadata = landing_metadata(landing)
    armed_revision = store.get_revision(str(metadata.get("revision_id") or ""))
    if str(getattr(session, "status", "") or "") == "archived":
        return _withdraw_discarded_landing(store, session, landing, armed_revision)
    latest = store.latest_revision(session.id)
    if armed_revision is None or latest is None:
        return _record_if_unchanged(
            store,
            landing,
            status="stale",
            metadata_updates={"detail": "The armed Snapshot is no longer available; review and re-arm."},
        )
    armed_tree = str(metadata.get("tree_fingerprint") or armed_revision.tree_fingerprint)
    if latest.tree_fingerprint != armed_tree:
        mark_landing_stale_for_revision(store, session.id, latest)
        settled = complete_pending_landing_cancel(
            store,
            session.id,
            lambda: _local_github_step(session, armed_revision, action="cancel"),
        )
        return settled or landing_relationship(store, session.id)

    def unchanged() -> bool:
        current = landing_relationship(store, session.id)
        return current is not None and (current.status, current.metadata_json, current.updated_at) == (
            landing.status,
            landing.metadata_json,
            landing.updated_at,
        )

    result = _local_github_step(session, armed_revision, action="reconcile", may_merge=unchanged)
    state = str(result.get("state") or "")
    detail = str(result.get("detail") or "")
    if state in {"failed", "unsupported"}:
        # A transient provider error must never flip the intent; keep waiting
        # and surface the latest watch failure.
        if metadata.get("watch_error") == detail[:500]:
            return landing
        return _record_if_unchanged(
            store, landing, status=landing.status, metadata_updates={"watch_error": detail[:500]}
        )
    if state == landing.status and metadata.get("detail") == detail and not metadata.get("watch_error"):
        return landing
    updates: dict[str, Any] = {"detail": detail, "provider_state": state, "watch_error": ""}
    if result.get("mechanism"):
        updates["mechanism"] = str(result["mechanism"])
    if state == "merged":
        return record_landing_state(store, landing, status=state, metadata_updates=updates)
    return _record_if_unchanged(store, landing, status=state, metadata_updates=updates)


__all__ = [
    "ARMED_LANDING_STATES",
    "cancel_settlement",
    "complete_pending_landing_cancel",
    "condition",
    "conditions_digest",
    "landing_metadata",
    "landing_relationship",
    "local_github_landing",
    "mark_landing_stale_for_revision",
    "normalized_requirement_state",
    "readiness_summary",
    "reconcile_local_github_landing",
    "record_landing_state",
]
