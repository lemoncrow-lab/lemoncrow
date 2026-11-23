"""P9-4 R15: every lookup of a person-authored object by id, anywhere in the product, is lensed or is listed.

The existence oracle in the enterprise canary proves the routes people thought of. Route shape keeps letting an id
through in a body or a query, so this checks the call site instead: every reference to a store getter that returns
something a person authored (a note, its versions, an action, a proposal, an Ask conversation or message, a request,
a record, a provider operation, a delivery) anywhere in ``src/lemoncrow/**`` or ``enterprise/server/src/lemoncrow_server/**``
(the modules that define the getters aside) must be either wrapped by ``viewer_lens.reachable`` (which answers "missing"
for a hidden author) or sit in a function on the allowlist below, with the reason it may read the object unlensed. A
new call site fails this test until someone decides which it is.

The guarantee is only this: every by-id call of a person-authored getter in the scanned trees is either the ``found``
argument (``args[1]``) of ``viewer_lens.reachable(hidden, found, author_of)`` with a hidden set that is not a literal
(or a name bound to one) and an ``author_of`` that reads ``created_by`` (or ``reviewer_id``) of what was found, or listed
in ``ALLOWED`` with a pinned count and a reason. A lens scope around a call, or a caller that sets one, exempts nothing.
The getters are derived, not remembered, and by what a method takes, not by what it is called: every public method of a
store class with an object-id parameter (``*_id``, ``*_ids``, ``id``, ``ids``, ``key``; not a singular review, session or
revision id) must be in ``GETTERS`` (a person authored what it returns), in ``NOT_PERSON_AUTHORED_GETTERS`` with a
reason, or in ``WRITES``. It does not reason about dynamic dispatch (a computed ``getattr``, ``exec``, reflection);
``getattr(x, "<getter name>")`` with a string constant is seen.
"""

from __future__ import annotations

import ast
import collections
import functools
from collections.abc import Iterable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: Getters that return a person-authored object (or its version) by an id the caller supplies.
GETTERS = frozenset(
    {
        "get_annotation",
        "get_annotation_by_source",
        "list_annotation_versions",
        "annotation_version_number",
        "get_thread_resolution_owner",
        "get_ask_conversation",
        "get_ask_message",
        "get_ask_handoff",
        "get_change_proposal",
        "get_review_request",
        "get_review_request_for_reviewer",
        "get_provider_operation",
        "get_record",
        "get_hosted_action",
        "get_review_flow_send",
        "list_delivery_operation",
        "list_deliveries",
        "list_ask_messages",
        "annotation_origin_anchor",
        "attested_fingerprints",
        "discarded_marks_on",
        "discarded_marks_since",
        "first_judgment_at",
        "frontier_revision",
        "latest_outcome",
        "marks_at_revision",
        "feedback_rows_for_reviews",
        "activity_events_for_reviews",
        "attention_reasons_for_principal",
        "viewer_states",
        "inbox_side_facts",
        "list_relationships_for_reviews",
    }
)
#: Store getters that take an id of something (other than a review, session or revision) but return nothing a person
#: authored, or that lens themselves in their definition and are keyed by the asking reviewer. Each says why.
NOT_PERSON_AUTHORED_GETTERS: dict[str, str] = {
    "find_hosted_attachment": "the record that a review is attached to a hosted server: a machine fact, no author",
    "find_provider_subject": "provider truth (a pull request as the provider reports it), no author here",
    "find_provider_subject_by_number": "provider truth (a pull request as the provider reports it), no author here",
    "find_provider_subjects_by_head": "provider truth (a pull request as the provider reports it), no author here",
    "find_provider_subjects_by_merge_group": "provider truth (a pull request as the provider reports it), no author here",
    "get_evidence": "an evidence record carries no author (SPEC J): it is a fact about the change",
    "get_github_check": "the GitHub check outbox, written by the server for the provider",
    "get_participant": "the roster entry (who may act, in what role): membership, not authored work",
    "get_provider_webhook": "a provider delivery as received: a machine record, no author",
    "get_user_settings": "the asking reviewer's own settings, keyed by her own id",
    "list_attention_reasons": "keyed by the asking principal; lensed in its definition (hidden_in), checked below",
    "list_coverage_events": "the actor's own reading activity (what she has seen), keyed by her id, no authored text; its only cross-actor reader is the signed record (review_record.py gather_record_facts), which _barred keeps from a hidden reader",
    "list_decision_events": "keyed by the asking reviewer; lensed in its definition (visible), checked below",
    "list_mark_events": "keyed by the asking reviewer; lensed in its definition (reviewer_marks), checked below",
    "list_marks": "keyed by the asking reviewer; lensed in its definition (reviewer_marks), checked below",
    "list_notification_outbox": "notifications to a recipient, written by the server for that recipient",
    "list_open_pull_refs": "provider truth (open pull requests as the provider reports them), no author here",
    "list_outcomes": "keyed by the asking reviewer; lensed in its definition (outcomes), checked below",
    "list_provider_facts": "provider truth, no author here",
    "list_provider_facts_by_kind": "provider truth, no author here",
    "list_review_requests": "keyed by the asking reviewer; lensed in its definition (visible), checked below",
    "derived_results": "derived machine rows by kind and revision (triage, readiness, post-merge, progress); the one kind a "
    "person authors, the split request, is read only through DerivedRecordLedger, which is tracked as a ledger",
    "evidence_artifact_relpath": "the path an evidence file is stored at: evidence carries no author (SPEC J)",
    "fact_watermarks": "the newest write time of a review: a timestamp, no authored content",
    "find_github_check": "the GitHub check outbox, written by the server for the provider",
    "known_names": "display names from the directory of principals: membership, not authored work",
    "policy_edit_revisions": "which revisions edited a policy file: a fact about the change, no author",
    "require_current_revision": "a guard that raises when the revision moved: it answers nothing a person wrote",
}
#: Store methods that change something, each taking the id of what it changes or creates. A write answers the one who
#: wrote it; whether a hidden reader may write to a person's note, request or action by its id (and be told no more
#: than for a made-up id) is the canary's IDOR_ROUTES, not this scan. A new write is named here on purpose.
WRITES = frozenset(
    {
        "add_change_proposal",
        "add_evidence",
        "append_decision_event",
        "apply_attention_reasons",
        "apply_provider_fact",
        "apply_request_lifecycle",
        "attach_discussion_comment",
        "begin_delivery_dispatch",
        "claim_ask_handoff_dispatch",
        "claim_delivery_target",
        "claim_github_checks",
        "claim_provider_webhook_retry",
        "clear_mark",
        "complete_intelligence_run",
        "complete_synthesis",
        "create_provider_operation",
        "create_review_request",
        "delete_annotation",
        "delete_discussion_comment",
        "delete_evidence",
        "delete_provider_facts",
        "delete_relationship",
        "dismiss_rule_suggestion",
        "fail_notification",
        "finish_review_flow_send",
        "link_applied_change_proposals",
        "mark_github_check_delivered",
        "mark_notification_delivered",
        "mark_notification_suppressed",
        "mark_provider_webhook_delayed",
        "mark_provider_webhook_failed",
        "mark_provider_webhook_processed",
        "mark_provider_webhook_settled",
        "migrate_review_request_scope",
        "patch_user_settings",
        "prepare_ask_handoff",
        "record_anchor_event",
        "record_frontier",
        "record_outcome",
        "recover_stale_delivery_attempts",
        "release_notification",
        "remove_participant",
        "renew_github_check_lease",
        "renew_notification_lease",
        "replay_github_check",
        "reset_user_settings",
        "retarget_delivery_target",
        "retry_provider_webhook",
        "schedule_review_flow_send",
        "set_attention",
        "set_mark",
        "set_revision_source_view",
        "set_viewer_state",
        "suppress_recipient_notifications",
        "transfer_thread_resolution_owner",
        "transition_hosted_action",
        "undo_review_flow_send",
        "update_annotation",
        "update_ask_handoff",
        "update_change_proposal",
        "update_delivery_operation",
        "update_delivery_target",
        "update_provider_operation",
        "update_review_request",
        "update_revision_projection",
        "update_session_status_if_current_revision",
        "upsert_participant",
        "upsert_provider_subject",
        "void_outcome",
        "write_evidence_artifact",
    }
)
#: A ledger's ``get`` (the action ledger, the split and approval ledgers, the derived-record receipts) is the getter
#: whose name is too common to list by itself. It is seen on what is built by a ``*Ledger(...)`` call (the class-style
#: name, case-sensitive) or by one of ``LEDGER_FACTORIES``; on every name or attribute assigned from one (or from a
#: parameter annotated with a ledger type) in the function, the enclosing ones, the module, or the file; and, across
#: modules, on every attribute annotated with a ledger type anywhere in the scanned trees and every function that
#: returns one (by annotation or by returning a ``*Ledger(...)``). A name that is ``ledger`` or ends in ``_ledger`` is
#: one too, whatever it was assigned from: a plain dict of that name is then listed in ``ALLOWED`` with that reason.
LEDGER_CLASS_SUFFIX = "Ledger"
LEDGER_FACTORIES = frozenset({"approval_ledger"})

#: ``self.<name>(getter, *args)`` calls the getter with the arguments (the hosted app runs storage off the event loop).
DISPATCHERS = frozenset({"_review_call"})

#: The only receivers whose ``reachable`` counts as the lens.
LENS_NAMES = frozenset({"viewer_lens", "_viewer_lens_module"})

#: Everything under these trees is scanned, apart from the modules that define the getters (by full path).
SCANNED_TREES = ("src/lemoncrow", "enterprise/server/src/lemoncrow_server")
DEFINING_MODULES = frozenset(
    {
        "src/lemoncrow/pro/capabilities/review/store.py",
        "enterprise/server/src/lemoncrow_server/review/collaboration/store.py",
        "enterprise/server/src/lemoncrow_server/review/storage/store.py",
        "enterprise/server/src/lemoncrow_server/review/providers/store.py",
        "src/lemoncrow/pro/capabilities/review/viewer_lens.py",
        "src/lemoncrow/pro/capabilities/review/session_models.py",
        "enterprise/server/src/lemoncrow_server/postgres_review.py",
    }
)

#: ``(file name, function)`` -> ``({getter: number of unwrapped sites}, why)``. The number is pinned: a new site in an
#: allowlisted function is a new decision and fails until it is wrapped or counted here.
ALLOWED: dict[tuple[str, str], tuple[dict[str, int], str]] = {
    ("api.py", "_get_annotation_versions"): (
        {"list_annotation_versions": 1},
        "the note came from _reachable_note, so its author is already known to be readable",
    ),
    ("api.py", "_reachable_note"): (
        {"get_annotation": 1},
        "this is the helper: it loads the note and returns it only through reachable",
    ),
    ("api.py", "_apply_change_proposal"): (
        {"get_change_proposal": 1},
        "the proposal is passed through reachable on the next statement, before anything else is read",
    ),
    ("api.py", "_post_review_ask"): (
        {"get_ask_conversation": 1, "list_ask_messages": 2},
        "all three sit lexically inside `with _lens_scope(session)` (pinned below, with no unfiltered()/decision_read() "
        "between) and are store-lensed getters: a conversation or message of a hidden author is None or absent "
        "(checked below against every definition of the getters)",
    ),
    ("api.py", "_post_review_ask_handoff"): (
        {"get_ask_handoff": 1, "get_ask_message": 1},
        "the message is read lexically inside `with _lens_scope(session)` (pinned below), a store-lensed getter, and "
        "its hand-off row (not lensed itself) follows from it",
    ),
    ("review_actions_api.py", "candidates_for"): (
        {"get_annotation": 1, "get_change_proposal": 1},
        "store-lensed getters, reached only through prepare_as_read (preview and start routes), which sets the lens; "
        "the structural test below pins that prepare has no other caller and that prepare_as_read calls it inside "
        "viewer_lens.scoped",
    ),
    ("api.py", "_ask_message_payload"): (
        {"get_ask_handoff": 1},
        "the message was already listed to this reader; its hand-off row follows it",
    ),
    ("review_automation_api.py", "_post_annotation_sync"): (
        {"get_annotation": 1},
        "the note goes through reachable right after its session is loaded",
    ),
    ("review_record_api.py", "_stored"): (
        {"get_record": 1},
        "the record goes through _barred (the lens for a verdict's author) before it is returned",
    ),
    ("attachment.py", "_facts"): (
        {"annotation_version_number": 1, "get_review_request": 1, "latest_outcome": 1},
        "the reply's parent goes through reachable; the request must be the replaying person's own; the verdict is "
        "the replaying person's own (principal_id)",
    ),
    ("attachment.py", "_apply"): (
        {"get_review_request": 1},
        "the preconditions proved the request is the replaying person's own",
    ),
    ("app.py", "review_annotation_update"): (
        {"annotation_version_number": 4, "get_thread_resolution_owner": 1},
        "every note and version read here follows viewer_lens.reachable on the same id",
    ),
    ("app.py", "review_thread_resolution_owner"): (
        {"get_thread_resolution_owner": 1},
        "the owner is read for a note that went through viewer_lens.reachable",
    ),
    ("app.py", "review_request_update"): (
        {"get_review_request": 1},
        "the conflict re-read is of the request that went through viewer_lens.reachable",
    ),
    ("app.py", "review_request_lifecycle"): (
        {"get_review_request": 1},
        "the conflict re-read is of the request that went through viewer_lens.reachable",
    ),
    ("app.py", "review_comment"): (
        {"annotation_version_number": 1},
        "the version of the note this person has just written",
    ),
    ("app.py", "load_annotations"): (
        {"annotation_version_number": 1},
        "versions of notes already listed to this reader (a lensed list)",
    ),
    ("app.py", "review_annotation_versions"): (
        {"get_annotation": 1, "list_annotation_versions": 1},
        "a GET: the /v1 lens middleware is set, and the store-lensed getters then answer None for a hidden author",
    ),
    ("api.py", "_get_review_ask"): (
        {"get_ask_conversation": 1},
        "a GET under the lens dependency; the Ask getter is store-lensed",
    ),
    ("api.py", "_ask_conversation_summaries"): (
        {"list_ask_messages": 2},
        "called only by _get_review_ask, a GET under the lens dependency; list_ask_messages is store-lensed (ask_author_sql)",
    ),
    ("api.py", "_ask_messages_payload"): (
        {"list_ask_messages": 1},
        "called only by _get_review_ask, a GET under the lens dependency; list_ask_messages is store-lensed (ask_author_sql)",
    ),
    ("api.py", "_feedback_partition"): (
        {"list_deliveries": 1},
        "the ids are of notes its caller holds: a lensed list (_without_unseen_first_reviewer) or the author's own bundle",
    ),
    ("api.py", "_github_blocked"): (
        {"list_deliveries": 1},
        "the hosted app never passes github_publish_enabled, so _github_pull_request refuses (409) in _github_plan before this runs",
    ),
    ("api.py", "_reconcile_github_deliveries"): (
        {"list_deliveries": 1},
        "the hosted app never passes github_publish_enabled, so _github_pull_request refuses (409) in _github_plan before this runs",
    ),
    ("app.py", "_review_inbox_projection"): (
        {"get_review_request_for_reviewer": 1, "list_annotation_versions": 1, "list_deliveries": 1},
        "the Inbox projection runs under scoped_many for the viewer; the first two getters are store-lensed, and the "
        "deliveries are read only of the asking principal's own notes (created_by != principal_id is skipped)",
    ),
    ("archive_safety.py", "_pending_feedback"): (
        {"list_annotation_versions": 1, "list_deliveries": 1},
        "a discard decides on every note and warns by count (decision code)",
    ),
    ("feedback_sync.py", "published_ref"): (
        {"list_deliveries": 1},
        "the note is already in hand: the deliveries of a note its own author's sync is publishing",
    ),
    ("review_automation_api.py", "_post_dismiss"): (
        {"list_deliveries": 1},
        "hosted_owned does not refuse here (a hosted store never supports an attachment) and a POST runs under no lens, so notes whose author is hidden from the asker are skipped: his ref answers as one never published; test_a_hidden_authors_published_review_cannot_be_dismissed_and_answers_as_an_unknown_one and the canary's provider-review/dismiss IDOR_ROUTES row hold it",
    ),
    ("review.py", "_add_comment"): (
        {"get_annotation": 1},
        "the local CLI of the person who owns this checkout's Review: there is no second viewer and no hosted lens",
    ),
    ("review_actions_api.py", "subject_states"): (
        {"get_annotation": 1, "get_change_proposal": 1},
        "the listing GET: lensed by the dependency, and the getters are store-lensed",
    ),
    ("app.py", "review_participant_upsert"): (
        {"get_review_request_for_reviewer": 1},
        "keyed by the participant being added; a store-lensed getter (by reviewer)",
    ),
    ("app.py", "review_participant_remove"): (
        {"get_review_request_for_reviewer": 1},
        "keyed by the participant being removed; a store-lensed getter (by reviewer)",
    ),
    ("app.py", "review_request_create"): (
        {"get_review_request_for_reviewer": 1},
        "keyed by the reviewer being asked, to refuse a second request; a store-lensed getter (by reviewer)",
    ),
    ("app.py", "review_targets"): (
        {"get_review_request_for_reviewer": 1},
        "the caller's own request (principal.subject)",
    ),
    ("second_reviewer.py", "asked_owners"): (
        {"get_review_request_for_reviewer": 1},
        "decision code of the second-reviewer ask: it must see the real request",
    ),
    ("second_reviewer.py", "request_owner"): (
        {"get_review_request_for_reviewer": 1},
        "decision code of the second-reviewer ask: it must see the real request",
    ),
    ("team.py", "_move_request"): (
        {"get_review_request_for_reviewer": 1},
        "decision code of a hand-off (R8): it moves the real request",
    ),
    ("team.py", "_restore_request"): (
        {"get_review_request": 2},
        "decision code of a take-back (R8): it restores the real request",
    ),
    ("api.py", "_annotation_context_payload"): (
        {"list_annotation_versions": 1},
        "versions of a note already chosen for this reader's own answer",
    ),
    ("api.py", "_feedback_annotation_version"): (
        {"list_annotation_versions": 1},
        "the version of a note being delivered by its author's own request",
    ),
    ("api.py", "_settled_by_human"): (
        {"list_annotation_versions": 1},
        "walks the versions of a note from a lensed list",
    ),
    ("feedback_sync.py", "_perform"): (
        {"get_annotation": 1},
        "re-reads the note it has just synced for the person who owns it",
    ),
    ("delivery.py", "mark_feedback_addressed"): (
        {"get_annotation": 1, "list_annotation_versions": 1, "list_deliveries": 1},
        "the MCP tool of the author's own agent session, not a reviewer's request",
    ),
    ("session_comments.py", "_import"): (
        {"get_annotation": 4},
        "background import of an agent session's comments, with no viewer",
    ),
    ("session_comments.py", "_project_later"): (
        {"get_annotation": 1},
        "background import of an agent session's comments, with no viewer",
    ),
    ("local.py", "annotate_machine_finding"): ({"get_annotation": 1}, "a machine finding written by the server itself"),
    ("check_api.py", "_post_check_request"): (
        {"get_change_proposal": 1},
        "only a proposal a recipe made (created_by lemoncrow:recipe:) passes, never a person's",
    ),
    ("api.py", "_schedule_flow_send"): (
        {"get_review_flow_send": 1},
        "the local undo-send queue: _flow_send_local_only refuses any store but the local ReviewStore, hosted included",
    ),
    ("api.py", "_get_flow_send"): (
        {"get_review_flow_send": 1},
        "the local undo-send queue: _flow_send_local_only refuses any store but the local ReviewStore, hosted included",
    ),
    ("api.py", "_post_hosted_action_op"): (
        {"get_hosted_action": 1},
        "a local review's replay queue toward hosted: refused unless the store supports an attachment, and a hosted store never does",
    ),
    ("action_approval_api.py", "find"): (
        {"ledger.get": 1},
        "an agent's approval request (a machine's), not a person's work on the review",
    ),
    ("app.py", "_apply_review_provider_event"): (
        {"get_annotation_by_source": 1, "get_review_request_for_reviewer": 1},
        "a provider webhook, which has no viewer",
    ),
    ("app.py", "_record_provider_deliveries"): (
        {"list_annotation_versions": 1},
        "a provider delivery record, which has no viewer",
    ),
    ("review_reader.py", "_annotation_in_repo"): (
        {"get_annotation": 1},
        "the note is refused to a reader it is hidden from on the next statement",
    ),
    ("review_reader.py", "_mutated_review_id"): (
        {"get_annotation": 1},
        "resolves the review of a path the bridge has already resolved for this reader",
    ),
    ("review_reader.py", "_guard_thread_close"): (
        {"get_annotation": 1, "get_thread_resolution_owner": 1},
        "runs after the bridge resolved the note, which it does not for a hidden author",
    ),
    ("api.py", "_deliver_feedback"): (
        {"get_annotation": 1, "list_delivery_operation": 1},
        "the notes of an existing operation are read under viewer_lens.unfiltered() (full rows: a lensed read would "
        "answer None for a hidden note) only to ask whether it is over a hidden author's note, which is then dropped "
        "to an unknown operation; test_a_delivery_operation_over_a_hidden_authors_note_reads_as_an_unknown_one (lens on) "
        "holds the unfiltered read, and the canary's feedback/deliver IDOR_ROUTES row holds dropping the operation",
    ),
    ("api.py", "_post_github_publish"): (
        {"list_delivery_operation": 1},
        "the hosted app never passes github_publish_enabled, so _github_pull_request refuses (409) in _github_plan before this runs",
    ),
    ("attachment.py", "already_replayed"): (
        {"ledger.get": 1},
        "a receipt is keyed by the replaying person's own action id and is read only for that same person",
    ),
    ("attachment.py", "replay_attachment_action"): (
        {"ledger.get": 1},
        "known exception (SPEC J, Not covered): a receipt is keyed by the client's action id alone, so a hidden reviewer who replays "
        "with one of the first reviewer's receipt ids is told identity_mismatch where a made-up id replays normally; it needs his "
        "client-generated id, and re-keying receipts by principal is a schema change",
    ),
    ("reconcile.py", "_project_ownership"): (
        {"get_review_request_for_reviewer": 1},
        "provider reconciliation (a webhook or sync, no viewer) decides on the real requests",
    ),
    ("reconcile.py", "reconcile_review_requests"): (
        {"get_review_request_for_reviewer": 2},
        "provider reconciliation (a webhook or sync, no viewer) decides on the real requests",
    ),
    ("requests.py", "_lemoncrow_request"): (
        {"get_review_request_for_reviewer": 1},
        "reviewer routing runs inside a viewer's own request-a-reviewer POST (REVIEW_MANAGE) and must decide on the real request, "
        "so it may reactivate the first reviewer's; request_reviewer's done() blanks the id of a request of anyone hidden from her "
        "(hidden_from), in her answer and in the event, and the canary's R12-3 test fails without that",
    ),
    ("api.py", "_ledger_savings_summary_payload"): (
        {"ledger.get": 2},
        "the name rule on a plain dict: a run's savings ledger (dict[str, Any]) in core/service/api.py, not a review ledger",
    ),
    ("api.py", "_nearest_ledger_tool_name"): (
        {"ledger.get": 1},
        "the name rule on a plain dict: a run's tool ledger (dict[str, Any] | None) in core/service/api.py, not a review ledger",
    ),
    ("benchmark_report.py", "_publication"): (
        {"ledger.get": 1},
        "the name rule on a plain mapping: the benchmark run's own ledger of stops, not a review ledger",
    ),
    ("requests.py", "request_reviewer"): (
        {"get_review_request_for_reviewer": 1},
        "the request just reconciled with the provider is read to name it; done() blanks its id when the asker is hidden "
        "from its reviewer (hidden_from), as for _lemoncrow_request",
    ),
    ("api.py", "_discarded_payload"): (
        {"discarded_marks_since": 1},
        "the marks the asking reviewer discarded: keyed by session.reviewer_id, which _session sets to the authenticated reader (acting_reviewer_id): her own marks",
    ),
    ("api.py", "_overview"): (
        {"discarded_marks_on": 1},
        "the marks the asking reviewer discarded: keyed by session.reviewer_id, which _session sets to the authenticated reader (acting_reviewer_id): her own marks",
    ),
    ("api.py", "_feedback_snapshot"): (
        {"latest_outcome": 1},
        "the asking reviewer's own verdict note travels with her own feedback: keyed by session.reviewer_id, which _session sets to the authenticated reader (acting_reviewer_id): her own",
    ),
    ("api.py", "_github_plan"): (
        {"latest_outcome": 1},
        "the asking reviewer's own verdict, for the publish she is planning: keyed by session.reviewer_id, which _session sets to the authenticated reader (acting_reviewer_id): her own",
    ),
    ("api.py", "_outcome_payload"): (
        {"latest_outcome": 1},
        "the asking reviewer's own verdict: keyed by session.reviewer_id, which _session sets to the authenticated reader (acting_reviewer_id): her own",
    ),
    ("api.py", "_marks_visible_at"): (
        {"marks_at_revision": 1},
        "the asking reviewer's own marks at a revision: keyed by session.reviewer_id, which _session sets to the authenticated reader (acting_reviewer_id): her own",
    ),
    ("api.py", "_pinned_queue_ranks"): (
        {"first_judgment_at": 1},
        "when the asking reviewer first judged: keyed by session.reviewer_id, which _session sets to the authenticated reader (acting_reviewer_id): her own",
    ),
    ("api.py", "_get_queue_state"): (
        {"viewer_states": 1},
        "the acting viewer's snooze and place, keyed by session.reviewer_id (_as_acting_reviewer): nobody else's",
    ),
    ("api.py", "_list_reviews"): (
        {"viewer_states": 3},
        "the acting viewer's snooze and place per Review, keyed by _as_acting_reviewer's id: nobody else's",
    ),
    ("review.py", "_discarded_verdicts"): (
        {"discarded_marks_since": 1},
        "the local CLI reads the one local reviewer's own discarded marks (session.reviewer_id)",
    ),
    ("app.py", "review_compare"): (
        {"marks_at_revision": 2},
        "the caller's own marks at the two revisions: keyed by the authenticated principal's own id (principal.subject), never by a body or path value",
    ),
    ("app.py", "review_get"): (
        {"latest_outcome": 1},
        "the caller's own verdict (my_outcome); everyone else's comes from latest_outcomes, which lenses itself: keyed by the authenticated principal's own id (principal.subject), never by a body or path value",
    ),
    ("app.py", "review_outcomes"): (
        {"latest_outcome": 1},
        "the caller's own verdict (my_outcome); the others' are the lensed outcomes list: keyed by the authenticated principal's own id (principal.subject), never by a body or path value",
    ),
    ("attachment.py", "attachment_projection"): (
        {"latest_outcome": 1},
        "the replaying person's own verdict: keyed by principal_id, the authenticated principal",
    ),
    ("projection.py", "_viewer_queue"): (
        {"viewer_states": 1},
        "this viewer's own snooze and saved place, keyed by the viewer's principal id: nobody else's",
    ),
    ("projection.py", "_decided_since"): (
        {"latest_outcome": 1},
        "only whether a claim's holder decided after the claim: the claim itself is dropped when the holder is hidden (team_presence_api hidden=), and no verdict text is read",
    ),
    ("projection.py", "your_review_projection"): (
        {"frontier_revision": 3},
        "the viewer's own frontier revision (principal_id), the revision she last judged",
    ),
    ("local.py", "baseline_revision"): (
        {"frontier_revision": 1},
        "the local reviewer's own frontier revision, keyed by the reviewer_id the caller passes: the one local reviewer",
    ),
    ("local.py", "unseen_units"): (
        {"attested_fingerprints": 1},
        "the local reviewer's own attested fingerprints, keyed by the reviewer_id the caller passes: the one local reviewer",
    ),
    ("attention.py", "derive_attention_reasons"): (
        {"frontier_revision": 1},
        "derivation, not a reading: what the request's own reviewer is waiting on. The reasons it writes are read back through list_attention_reasons, which lenses itself (hidden_in)",
    ),
    ("notification_digest.py", "waiting_rows"): (
        {"attention_reasons_for_principal": 1},
        "keyed by the digest's own recipient; every reason goes through for_reader with that review's hidden authors (hidden_authors), as for the bell",
    ),
    ("notification_feed.py", "_gather"): (
        {"attention_reasons_for_principal": 1},
        "keyed by the caller's own subject; every reason goes through for_reader with the review's hidden set (hidden_by_review) before it is shown",
    ),
    ("notification_feed.py", "push_copy"): (
        {"attention_reasons_for_principal": 1},
        "keyed by the push's own recipient; the reason goes through for_reader with hidden_authors, and the words are made inside viewer_lens.scoped",
    ),
    ("inbox_inputs.py", "load_stored_inbox_facts"): (
        {"activity_events_for_reviews": 1},
        "store-lensed: activity_events_for_reviews calls viewer_lens.activity in its definition, so a priority "
        "override a hidden person set (his reason, his id) is not read; the Inbox reads under scoped_many",
    ),
    ("review_record.py", "gather_record_facts"): (
        {"annotation_origin_anchor": 1},
        "the anchor a note was born at, asked only of notes the record already holds (list_annotations, lensed). The signed record is the one reader, and _barred keeps it from a hidden reader",
    ),
    ("second_reviewer_api.py", "sign"): (
        {"latest_outcome": 1},
        "the signer's own verdict before she signs (viewer is the authenticated second reviewer)",
    ),
    ("team_presence_api.py", "rows_by_review"): (
        {"list_relationships_for_reviews": 1},
        "the raw team rows; every caller filters them through the presence functions with hidden= (a hidden person's presence, claim and hand-off are dropped)",
    ),
    ("team_presence_api.py", "since"): (
        {"latest_outcome": 1},
        "only whether a claim's holder decided after the claim: a hidden holder's claim is dropped by hidden= and no verdict text is read",
    ),
}


def _callee_name(func: ast.AST) -> str:
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _callee(call: ast.Call) -> str:
    return _callee_name(call.func)


def _builds_ledger(node: ast.AST, factories: frozenset[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _callee(node)
    return name in LEDGER_FACTORIES or name in factories or (name.endswith(LEDGER_CLASS_SUFFIX) and name[:1].isupper())


def _annotated_ledger(annotation: ast.AST | None) -> bool:
    """``ReviewActionLedger``, ``x.ReviewActionLedger``, ``Optional[...]``/``... | None`` of one, or the same as a string.

    A container of ledgers (``OrderedDict[str, RunLedger]``) is not a ledger."""

    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return False
    if isinstance(annotation, ast.Name):
        return annotation.id.endswith(LEDGER_CLASS_SUFFIX)
    if isinstance(annotation, ast.Attribute):
        return annotation.attr.endswith(LEDGER_CLASS_SUFFIX)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotated_ledger(annotation.left) or _annotated_ledger(annotation.right)
    if isinstance(annotation, ast.Subscript) and _callee_name(annotation.value) in {"Optional", "Union"}:
        inner = annotation.slice
        return any(_annotated_ledger(e) for e in (inner.elts if isinstance(inner, ast.Tuple) else [inner]))
    return False


def _ledger_named(expr: ast.AST) -> bool:
    """The name rule, a catch beside the tracking: ``ledger``, ``*_ledger``, as a name or an attribute (``deps.action_ledger``)."""

    name = expr.id if isinstance(expr, ast.Name) else expr.attr if isinstance(expr, ast.Attribute) else ""
    return name == "ledger" or name.endswith("_ledger")


class _LedgerIndex:
    """What the scanned trees say, across modules, about what holds a ledger: the attributes (class fields,
    ``self.x: Ledger``) annotated with a ledger type, and the functions that return one (by their return annotation or
    by returning a ``*Ledger(...)`` call). A name here is a ledger wherever it is used."""

    def __init__(self, trees: Iterable[ast.AST]) -> None:
        attributes: set[str] = set()
        returners: set[str] = set()
        for tree in trees:
            for node in ast.walk(tree):
                if isinstance(node, ast.AnnAssign) and _annotated_ledger(node.annotation):
                    target = node.target
                    if isinstance(target, ast.Name | ast.Attribute):
                        attributes.add(target.id if isinstance(target, ast.Name) else target.attr)
                elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    if _annotated_ledger(node.returns) or any(
                        isinstance(r, ast.Return) and _builds_ledger(r.value, frozenset()) for r in ast.walk(node)
                    ):
                        returners.add(node.name)
        self.attributes = frozenset(attributes)
        self.returners = frozenset(returners)


def _scope_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """The function (or lambda) a node sits in, or ``None`` for module level."""

    cursor = parents.get(node)
    while cursor is not None:
        if isinstance(cursor, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            return cursor
        cursor = parents.get(cursor)
    return None


class _Ledgers:
    """The names and attributes of one file that hold a ledger (tracked by assignment, not by what they are called)."""

    def __init__(self, tree: ast.AST, parents: dict[ast.AST, ast.AST], index: _LedgerIndex) -> None:
        self.parents = parents
        self.names: dict[ast.AST | None, set[str]] = collections.defaultdict(set)
        self.attributes: set[str] = set(index.attributes)
        self.factories = index.returners
        assignments: list[tuple[ast.AST, ast.AST, ast.AST | None]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                arguments = node.args
                for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]:
                    if _annotated_ledger(argument.annotation):
                        self.names[node].add(argument.arg)
            elif isinstance(node, ast.Assign):
                assignments.extend((target, node.value, _scope_of(node, parents)) for target in node.targets)
            elif isinstance(node, ast.AnnAssign):
                if _annotated_ledger(node.annotation):
                    self._bind(node.target, _scope_of(node, parents))
                if node.value is not None:
                    assignments.append((node.target, node.value, _scope_of(node, parents)))
            elif isinstance(node, ast.NamedExpr):
                assignments.append((node.target, node.value, _scope_of(node, parents)))
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                assignments.append((node.optional_vars, node.context_expr, _scope_of(node.context_expr, parents)))
        changed = True
        while changed:  # `a = ReviewActionLedger(s); b = a; self.c = b`
            changed = False
            for target, value, scope in assignments:
                if self._holds(value, scope) and self._bind(target, scope):
                    changed = True

    def _bind(self, target: ast.AST, scope: ast.AST | None) -> bool:
        if isinstance(target, ast.Name) and target.id not in self.names[scope]:
            self.names[scope].add(target.id)
            return True
        if isinstance(target, ast.Attribute) and target.attr not in self.attributes:
            self.attributes.add(target.attr)
            return True
        return False

    def _holds(self, expr: ast.AST, scope: ast.AST | None) -> bool:
        if _builds_ledger(expr, self.factories):
            return True
        if isinstance(expr, ast.Name):
            cursor = scope
            while True:
                if expr.id in self.names.get(cursor, ()):
                    return True
                if cursor is None:
                    return False
                cursor = _scope_of(cursor, self.parents)
        return isinstance(expr, ast.Attribute) and expr.attr in self.attributes

    def holds(self, expr: ast.AST) -> bool:
        return self._holds(expr, _scope_of(expr, self.parents))


def _getter_named_by(node: ast.AST, ledgers: _Ledgers) -> str:
    """The getter a node refers to, or ``""``: ``x.get_annotation``, a bare ``get_record``, a ledger's ``get``, or
    ``getattr(x, "get_annotation")`` with the name as a string constant."""

    if isinstance(node, ast.Attribute) and node.attr in GETTERS:
        return node.attr
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "get"
        and (ledgers.holds(node.value) or _ledger_named(node.value))
    ):
        return "ledger.get"
    if isinstance(node, ast.Name) and node.id in GETTERS:  # a bare function such as get_record(...)
        return node.id
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value in GETTERS
    ):
        return str(node.args[1].value)
    return ""


def _is_lens_reachable(call: ast.AST) -> bool:
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "reachable"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in LENS_NAMES
    )


def _fixed_hidden_set(expr: ast.AST) -> bool:
    """A hidden set written into the call (``frozenset()``, ``set()``, a literal) hides nobody: not the lens."""

    return isinstance(expr, ast.Constant | ast.List | ast.Tuple | ast.Set | ast.Dict) or (
        isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id in {"frozenset", "set"}
    )


#: The fields a person-authored object names its author by: what ``author_of`` of a ``reachable`` must read.
AUTHOR_FIELDS = frozenset({"created_by", "reviewer_id"})


def _root_name(expr: ast.AST) -> str:
    """``found`` for ``found``, ``found.x``, ``found[1]``, ``found[1].get``, ``found.x(...)``."""

    while True:
        if isinstance(expr, ast.Attribute | ast.Subscript):
            expr = expr.value
        elif isinstance(expr, ast.Call):
            expr = expr.func
        else:
            return expr.id if isinstance(expr, ast.Name) else ""


def _reads_the_author(author_of: ast.AST | None) -> bool:
    """``author_of`` is a lambda whose parameter's ``.created_by`` (or ``.reviewer_id``) is read, as an attribute, a
    key (``["created_by"]``) or ``.get("created_by")``. ``updated_by``, a lambda that reads something else, a function
    name and a constant are not: ``reachable`` would then compare something else with the hidden set."""

    if not isinstance(author_of, ast.Lambda) or len(author_of.args.args) != 1:
        return False
    parameter = author_of.args.args[0].arg
    for node in ast.walk(author_of.body):
        if isinstance(node, ast.Attribute) and node.attr in AUTHOR_FIELDS and _root_name(node.value) == parameter:
            return True
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in AUTHOR_FIELDS
            and _root_name(node.value) == parameter
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in AUTHOR_FIELDS
            and _root_name(node.func.value) == parameter
        ):
            return True
    return False


def _bound_to_a_fixed_set(name: str, node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """The name is assigned a literal or empty set (``nobody = frozenset()``) anywhere in the function the call sits
    in, in an enclosing function, or at module level: whatever else it is assigned, it is not the lens."""

    cursor: ast.AST | None = parents.get(node)
    while cursor is not None:
        if isinstance(cursor, ast.FunctionDef | ast.AsyncFunctionDef | ast.Module):
            for inner in ast.walk(cursor):
                targets: list[ast.AST] = []
                value: ast.AST | None = None
                if isinstance(inner, ast.Assign):
                    targets, value = list(inner.targets), inner.value
                elif isinstance(inner, ast.AnnAssign | ast.NamedExpr):
                    targets, value = [inner.target], inner.value
                if value is not None and _fixed_hidden_set(value):
                    if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                        return True
        cursor = parents.get(cursor)
    return False


def _argument(call: ast.Call, position: int, keyword: str) -> ast.AST | None:
    if len(call.args) > position:
        return call.args[position]
    return next((k.value for k in call.keywords if k.arg == keyword), None)


def _lookup_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """The expression that performs the lookup a getter reference stands for: its call, or the awaited
    ``self._review_call(getter, *args)`` the hosted app runs a getter through (it calls its first argument)."""

    call = parents.get(node)
    if not isinstance(call, ast.Call):
        return None
    if call.func is node:
        lookup: ast.AST = call
    elif (
        call.args
        and call.args[0] is node
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in DISPATCHERS
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
    ):
        lookup = call
    else:
        return None
    parent = parents.get(lookup)
    return parent if isinstance(parent, ast.Await) else lookup


def _wrapped(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """The lookup is itself the ``found`` argument of ``viewer_lens.reachable(hidden, found, ...)``.

    Anything else is not: a getter nested in another expression of the arguments (a walrus, an attribute of it, the
    ``author_of`` lambda), another receiver, an alias, a hidden set written as a literal or bound to one in the same
    function, an ``author_of`` that does not read ``created_by`` (or ``reviewer_id``) of what was found."""

    lookup = _lookup_of(node, parents)
    outer = parents.get(lookup) if lookup is not None else None
    if isinstance(outer, ast.keyword):
        outer = parents.get(outer)
    if not _is_lens_reachable(outer):
        return False
    assert isinstance(outer, ast.Call)
    hidden = _argument(outer, 0, "hidden")
    if hidden is None or _fixed_hidden_set(hidden):
        return False
    if isinstance(hidden, ast.Name) and _bound_to_a_fixed_set(hidden.id, outer, parents):
        return False
    return _argument(outer, 1, "found") is lookup and _reads_the_author(_argument(outer, 2, "author_of"))


def _function_of(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    cursor = parents.get(node)
    while cursor is not None:
        if isinstance(cursor, ast.FunctionDef | ast.AsyncFunctionDef):
            return cursor.name
        cursor = parents.get(cursor)
    return "<module>"


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _sites_of(sources: dict[str, str]) -> list[tuple[str, str, int, bool, str]]:
    """``(file, function, line, wrapped, getter)`` for every reference to a getter in *sources* (path -> text).

    A site is wrapped only as ``_wrapped`` says. Nothing else counts: not a lens scope around it, not what its
    callers do."""

    found: list[tuple[str, str, int, bool, str]] = []
    trees = {label: ast.parse(text) for label, text in sources.items()}
    index = _LedgerIndex(trees.values())
    for label, tree in trees.items():
        parents = _parents(tree)
        ledgers = _Ledgers(tree, parents, index)
        for node in ast.walk(tree):
            name = _getter_named_by(node, ledgers)
            if name:
                found.append((label, _function_of(node, parents), node.lineno, _wrapped(node, parents), name))
    return found


def _scanned_paths() -> list[Path]:
    paths: list[Path] = []
    for tree in SCANNED_TREES:
        for path in sorted((ROOT / tree).rglob("*.py")):
            if "__pycache__" in path.parts or str(path.relative_to(ROOT)) in DEFINING_MODULES:
                continue
            paths.append(path)
    return paths


@functools.cache
def _sites() -> list[tuple[str, str, int, bool, str]]:
    sources: dict[str, str] = {}
    for path in _scanned_paths():
        text = path.read_text(encoding="utf-8")
        # Parse only what can hold a site: a getter's name, or a ledger (its ``get`` is seen by assignment).
        if "Ledger" in text or "approval_ledger" in text or any(getter in text for getter in GETTERS):
            sources[str(path.relative_to(ROOT))] = text
    return _sites_of(sources)


def _unwrapped() -> collections.Counter[tuple[str, str, str]]:
    return collections.Counter(
        (Path(file).name, function, getter) for file, function, _line, wrapped, getter in _sites() if not wrapped
    )


def test_every_lookup_of_a_person_authored_object_is_lensed_or_listed() -> None:
    sites = _sites()
    assert len(sites) >= 20, len(sites)  # a scan that found nothing proves nothing
    found = _unwrapped()
    unclassified = []
    for (file, function, getter), count in sorted(found.items()):
        listed = ALLOWED.get((file, function), ({}, ""))[0].get(getter, 0)
        if count != listed:
            where = [
                f"{f}:{line}"
                for f, fn, line, w, g in sites
                if (Path(f).name, fn, g) == (file, function, getter) and not w
            ]
            unclassified.append(f"{file} {function}() reads {getter} {count} time(s), {listed} allowed: {where}")
    assert unclassified == [], (
        "wrap each in viewer_lens.reachable(...) or a lens scope, or list its function, getter and count in ALLOWED "
        "with the reason:\n" + "\n".join(unclassified)
    )


def test_the_allowlist_has_no_stale_entries() -> None:
    found = _unwrapped()
    stale = sorted(
        (file, function, getter)
        for (file, function), (getters, _reason) in ALLOWED.items()
        for getter in getters
        if (file, function, getter) not in found
    )
    assert stale == [], f"remove from ALLOWED: {stale}"


@pytest.mark.parametrize("entry", sorted(ALLOWED))
def test_an_allowlist_entry_says_why(entry: tuple[str, str]) -> None:
    assert len(ALLOWED[entry][1].split()) >= 4, entry


# --- getter completeness: the store getters are derived, not remembered --------------------------------------------- #

#: The singular ids of what a method is about, not of a person's object: the review, its session, its revision.
_NOT_AN_OBJECT_ID = frozenset({"review_id", "session_id", "revision_id"})
#: Parameter names that are an object id without ending in ``_id``.
_PLAIN_IDS = frozenset({"id", "ids", "key"})


@functools.cache
def _tree(relative: str) -> ast.Module:
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"))


#: The classes whose methods are the store: everything they inherit from is the store too.
STORE_ROOTS = frozenset({"ReviewStore", "HostedReviewStore", "PostgresReviewStore"})


@functools.cache
def _store_classes() -> dict[str, ast.ClassDef]:
    """``ReviewStore``, ``HostedReviewStore``, ``PostgresReviewStore`` and every base they inherit, by name, wherever
    the two trees (the defining modules included) define them."""

    index: dict[str, ast.ClassDef] = {}
    for tree in SCANNED_TREES:
        for path in sorted((ROOT / tree).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "ReviewStore" not in text and "Mixin" not in text:
                continue
            for node in ast.walk(_tree(str(path.relative_to(ROOT)))):
                if isinstance(node, ast.ClassDef):
                    index.setdefault(node.name, node)
    wanted = set(STORE_ROOTS)
    queue = list(STORE_ROOTS)
    while queue:
        cls = index.get(queue.pop())
        if cls is None:
            continue
        for base in cls.bases:
            name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else ""
            if name and name not in wanted and name in index:
                wanted.add(name)
                queue.append(name)
    return {name: index[name] for name in sorted(wanted) if name in index}


def _object_id_parameter(name: str) -> bool:
    """An ``*_id`` or ``*_ids`` (plural included: ``review_ids`` is a bulk read of many reviews' rows), or ``id``,
    ``ids``, ``key``; not the singular review, session or revision id."""

    if name in ("self", "cls") or name in _NOT_AN_OBJECT_ID:
        return False
    return name.endswith(("_id", "_ids")) or name in _PLAIN_IDS


def _store_getters() -> dict[str, list[str]]:
    """Every public method of a store class (any name: ``annotation_origin_anchor`` and ``latest_outcome`` are readers
    too) that takes an object id, with the classes that define it. A ``_private`` method is called only from inside
    the defining modules, which are not scanned for call sites."""

    found: dict[str, list[str]] = collections.defaultdict(list)
    for name, cls in _store_classes().items():
        for method in cls.body:
            if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef) or method.name.startswith("_"):
                continue
            arguments = [*method.args.posonlyargs, *method.args.args, *method.args.kwonlyargs]
            if any(_object_id_parameter(a.arg) for a in arguments):
                found[method.name].append(name)
    return dict(found)


def test_every_defining_module_exists() -> None:
    assert sorted(m for m in DEFINING_MODULES if not (ROOT / m).is_file()) == []
    scanned = {str(p.relative_to(ROOT)) for p in _scanned_paths()}
    assert scanned.isdisjoint(DEFINING_MODULES)
    # the whole trees, nothing narrower: the modules that answer requests all sit under them
    assert any(p.endswith("review/api.py") for p in scanned)
    assert any(p.endswith("lemoncrow_server/app.py") for p in scanned)
    assert any(p.endswith("cli/commands/review.py") for p in scanned)


def test_every_store_method_taking_an_id_is_classified() -> None:
    derived = _store_getters()
    assert {
        "get_annotation",
        "get_review_request_for_reviewer",
        "list_deliveries",
        "get_github_check",
        "annotation_origin_anchor",  # not named get_/list_/find_
        "latest_outcome",
        "update_annotation",
    } <= derived.keys()
    assert set(_store_classes()) >= {"ReviewStore", "HostedReviewStore", "PostgresReviewStore"}
    assert any("Mixin" in name for name in _store_classes()), "the mixins are the store too"
    unclassified = sorted(derived.keys() - GETTERS - NOT_PERSON_AUTHORED_GETTERS.keys() - WRITES)
    assert unclassified == [], (
        "a store method that takes an id: put it in GETTERS (a person authored what it returns, and every call site is "
        "wrapped or pinned in ALLOWED), in NOT_PERSON_AUTHORED_GETTERS with the reason it is not, or in WRITES if it "
        f"changes something: {unclassified}"
    )
    assert GETTERS.isdisjoint(NOT_PERSON_AUTHORED_GETTERS)
    assert GETTERS.isdisjoint(WRITES) and WRITES.isdisjoint(NOT_PERSON_AUTHORED_GETTERS)
    methods = {
        m.name
        for cls in _store_classes().values()
        for m in cls.body
        if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    stale = sorted((NOT_PERSON_AUTHORED_GETTERS.keys() | WRITES) - methods)
    assert stale == [], f"not a store method any more: {stale}"
    # a getter is defined somewhere in the trees (a store's, or another class's, like the record's)
    assert sorted(g for g in GETTERS if not _definitions(g)) == []
    gone = sorted(NOT_PERSON_AUTHORED_GETTERS.keys() - derived.keys())
    assert gone == [], f"takes no id any more: {gone}"
    assert all(len(reason.split()) >= 4 for reason in NOT_PERSON_AUTHORED_GETTERS.values())
    # a name the derivation does not see (no id parameter) is not claimed as a write: WRITES says what it is
    assert sorted(WRITES - derived.keys()) == []


# --- the self-check: the store getters the allowlist relies on really read the lens ------------------------------------ #

#: Getters that lens themselves, each with the ``viewer_lens`` calls its definition may make to do so. Chosen here,
#: not read off the reasons in ``ALLOWED``. Every definition of each (core store, hosted store, PostgreSQL's, and any
#: class-level alias) must call one of them; one that only mentions ``viewer_lens``, or only calls ``unfiltered()``
#: or ``decision_read()``, does not lens anything.
STORE_LENSED: dict[str, frozenset[str]] = {
    "get_annotation": frozenset({"hidden_in"}),
    "list_annotation_versions": frozenset({"hidden_in"}),
    "get_change_proposal": frozenset({"hidden_in", "reachable"}),
    "get_ask_conversation": frozenset({"ask_rows"}),
    "get_ask_message": frozenset({"ask_rows"}),
    "list_ask_messages": frozenset({"ask_author_sql"}),
    "get_review_request_for_reviewer": frozenset({"hidden_in"}),
    "list_attention_reasons": frozenset({"hidden_in"}),
    "list_marks": frozenset({"reviewer_marks"}),
    "list_mark_events": frozenset({"reviewer_marks"}),
    "list_outcomes": frozenset({"outcomes"}),
    "list_decision_events": frozenset({"visible"}),
    "list_review_requests": frozenset({"visible"}),
    "activity_events_for_reviews": frozenset({"activity"}),
}
#: Getters that share an ``ALLOWED`` entry with store-lensed ones without being lensed themselves (the entry's reason
#: says why: the row follows a lensed read, or is read only of the asker's own notes).
NOT_STORE_LENSED = frozenset({"get_ask_handoff", "list_deliveries"})


def _calls_the_lens(function: ast.FunctionDef | ast.AsyncFunctionDef, accepted: frozenset[str]) -> bool:
    """The definition calls ``viewer_lens.<one of accepted>(...)`` (the receiver is checked, and it is a call)."""

    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in accepted
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in LENS_NAMES
        for node in ast.walk(function)
    )


@functools.cache
def _definitions(getter: str) -> tuple[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef | ast.stmt], ...]:
    """Every ``def`` of *getter* (sync or async, in any class or module) and every class-level assignment that binds
    the name, in the two trees."""

    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef | ast.stmt]] = []
    for tree in SCANNED_TREES:
        for path in sorted((ROOT / tree).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if getter not in path.read_text(encoding="utf-8"):
                continue
            relative = str(path.relative_to(ROOT))
            for node in ast.walk(_tree(relative)):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == getter:
                    found.append((relative, node))
                elif isinstance(node, ast.ClassDef):
                    for statement in node.body:
                        targets = (
                            statement.targets
                            if isinstance(statement, ast.Assign)
                            else [statement.target] if isinstance(statement, ast.AnnAssign) else []
                        )
                        if any(isinstance(t, ast.Name) and t.id == getter for t in targets):
                            found.append((relative, statement))
    return tuple(found)


def _lens_problems(getter: str, accepted: frozenset[str]) -> list[str]:
    problems: list[str] = []
    definitions = _definitions(getter)
    if not definitions:
        return [f"{getter} is defined nowhere: the self-check reads nothing"]
    for path, node in definitions:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            problems.append(f"{path}:{node.lineno} {getter} is bound by assignment: define it, so its lens can be read")
        elif not _calls_the_lens(node, accepted):
            problems.append(f"{path}:{node.lineno} {getter} does not call viewer_lens.{'/'.join(sorted(accepted))}")
    return problems


def test_every_store_lensed_getter_calls_the_lens_in_every_definition() -> None:
    problems = [p for getter, accepted in sorted(STORE_LENSED.items()) for p in _lens_problems(getter, accepted)]
    assert problems == []
    for getter in ("get_annotation", "get_ask_message", "get_review_request_for_reviewer", "list_ask_messages"):
        assert _definitions(getter), getter
    assert any(p.endswith("review/store.py") for p, _ in _definitions("get_annotation"))
    assert any(p.endswith("collaboration/store.py") for p, _ in _definitions("get_review_request_for_reviewer"))


def test_a_getter_the_allowlist_calls_store_lensed_is_checked() -> None:
    relied_on = {
        getter
        for (_file, _function), (getters, reason) in ALLOWED.items()
        if "store-lensed" in reason
        for getter in getters
    }
    assert relied_on - NOT_STORE_LENSED <= STORE_LENSED.keys(), relied_on - STORE_LENSED.keys()
    assert STORE_LENSED.keys() <= GETTERS | NOT_PERSON_AUTHORED_GETTERS.keys()
    # a getter classed as not person-authored because it lenses itself is checked too
    lensed_here = {g for g, reason in NOT_PERSON_AUTHORED_GETTERS.items() if "lensed in its definition" in reason}
    assert lensed_here <= STORE_LENSED.keys(), lensed_here - STORE_LENSED.keys()
    # the helper getters the reasons rest on are the ones the scope sites read
    assert {"get_annotation", "get_change_proposal", "get_ask_conversation", "get_ask_message"} <= relied_on


def test_the_self_check_sees_a_getter_that_does_not_lens_itself() -> None:
    def problems(source: str, accepted: frozenset[str] = frozenset({"hidden_in"})) -> int:
        function = ast.parse(source).body[0]
        assert isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        return 0 if _calls_the_lens(function, accepted) else 1

    lensed = "def get_annotation(self, i):\n    return self._row(i) if not viewer_lens.hidden_in(self.r) else None\n"
    assert problems(lensed) == 0
    assert problems("async def get_annotation(self, i):\n    return viewer_lens.hidden_in(i)\n") == 0
    # M15: only the decision-time reads
    assert (
        problems("def get_annotation(self, i):\n    with viewer_lens.unfiltered():\n        return self._row(i)\n") == 1
    )
    assert (
        problems("def get_annotation(self, i):\n    with viewer_lens.decision_read():\n        return self._row(i)\n")
        == 1
    )
    # a name that is only mentioned, an attribute that is not called, another receiver, another call
    assert problems("def get_annotation(self, i):\n    viewer_lens\n    return self._row(i)\n") == 1
    assert problems("def get_annotation(self, i):\n    return viewer_lens.hidden_in and self._row(i)\n") == 1
    assert problems("def get_annotation(self, i):\n    return graph.hidden_in(i)\n") == 1
    assert problems("def get_annotation(self, i):\n    return viewer_lens.known(i)\n") == 1
    assert problems("def get_annotation(self, i):\n    return self._row(i)\n") == 1


# --- the sites that rely on a lens scope, read lexically ------------------------------------------------------------ #

REVIEW_API = "src/lemoncrow/pro/capabilities/review/api.py"
REVIEW_ACTIONS_API = "src/lemoncrow/pro/capabilities/review/review_actions_api.py"
#: The scope sites: a getter read inside ``with _lens_scope(session)`` in the same function, as ``(file, function)`` ->
#: ``{getter: number of reads}``. Each must sit lexically inside such a block, with no ``unfiltered()`` or
#: ``decision_read()`` (or ``_decision_scope``) between the read and the block, and not in a nested function.
SCOPE_SITES: dict[tuple[str, str], dict[str, int]] = {
    (REVIEW_API, "_post_review_ask"): {"get_ask_conversation": 1, "list_ask_messages": 2},
    (REVIEW_API, "_post_review_ask_handoff"): {"get_ask_message": 1},
}
_OPENS_A_SCOPE = frozenset({"scoped"})
_ENDS_A_SCOPE = frozenset({"unfiltered", "decision_read"})


def _opens_a_scope(expr: ast.AST) -> bool:
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    if isinstance(func, ast.Name):
        return func.id == "_lens_scope"
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _OPENS_A_SCOPE
        and isinstance(func.value, ast.Name)
        and func.value.id in LENS_NAMES
    )


def _ends_a_scope(expr: ast.AST) -> bool:
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    if isinstance(func, ast.Name):
        return func.id == "_decision_scope"
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _ENDS_A_SCOPE
        and isinstance(func.value, ast.Name)
        and func.value.id in LENS_NAMES
    )


def _inside_a_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Lexically in the body of ``with <opens a scope>`` of the same function, with nothing that ends the scope on the
    way and no deferral (a nested function, a lambda, a generator) between."""

    child: ast.AST = node
    cursor = parents.get(node)
    while cursor is not None:
        if isinstance(cursor, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.GeneratorExp):
            return False
        if isinstance(cursor, ast.With | ast.AsyncWith) and child in cursor.body:
            expressions = [item.context_expr for item in cursor.items]
            if any(_ends_a_scope(e) for e in expressions):
                return False
            if any(_opens_a_scope(e) for e in expressions):
                return True
        child, cursor = cursor, parents.get(cursor)
    return False


def _scope_site_problems(source: str, function: str, expected: dict[str, int]) -> list[str]:
    tree = ast.parse(source)
    parents = _parents(tree)
    defined = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == function
    ]
    if len(defined) != 1:
        return [f"{function} is defined {len(defined)} times"]
    seen: collections.Counter[str] = collections.Counter()
    problems: list[str] = []
    ledgers = _Ledgers(tree, parents, _LedgerIndex([tree]))
    for node in ast.walk(defined[0]):
        name = _getter_named_by(node, ledgers) if isinstance(node, ast.Attribute) else ""
        if name in expected:
            seen[name] += 1
            if not _inside_a_scope(node, parents):
                problems.append(f"{function}:{node.lineno} {name} is not inside a lens scope")
    if dict(seen) != expected:
        problems.append(f"{function} reads {dict(seen)}, the scope sites say {expected}")
    return problems


def test_the_scope_sites_sit_lexically_inside_a_lens_scope() -> None:
    for (path, function), expected in SCOPE_SITES.items():
        assert _scope_site_problems((ROOT / path).read_text(encoding="utf-8"), function, expected) == [], function
    # the scope itself opens the lens: api.py's _lens_scope is defined once, over viewer_lens.scoped
    tree = _tree(REVIEW_API)
    scope = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_lens_scope"]
    assert len(scope) == 1
    assert any(
        isinstance(n, ast.With)
        and any(_opens_a_scope(i.context_expr) for i in n.items)
        and "_lens_scope" not in ast.dump(n)
        for n in ast.walk(scope[0])
    )
    # and every ALLOWED entry that says "lexically inside" is one of the scope sites
    said = {
        (file, function) for (file, function), (_getters, reason) in ALLOWED.items() if "lexically inside" in reason
    }
    assert said == {(Path(path).name, function) for path, function in SCOPE_SITES}


def test_the_scope_site_check_sees_a_scope_that_is_missing_or_ended() -> None:
    inside = "def f(s):\n    with _lens_scope(s):\n        return store.get_ask_message(k)\n"
    assert _scope_site_problems(inside, "f", {"get_ask_message": 1}) == []
    as_lens = "def f(s):\n    with viewer_lens.scoped(s.id, h):\n        return store.get_ask_message(k)\n"
    assert _scope_site_problems(as_lens, "f", {"get_ask_message": 1}) == []
    cases = {
        "M14 removed": "def f(s):\n    return store.get_ask_message(k)\n",
        "unfiltered between": "def f(s):\n    with _lens_scope(s):\n        with viewer_lens.unfiltered():\n            return store.get_ask_message(k)\n",
        "decision_read between": "def f(s):\n    with _lens_scope(s):\n        with viewer_lens.decision_read():\n            return store.get_ask_message(k)\n",
        "decision scope between": "def f(s):\n    with _lens_scope(s):\n        with _decision_scope(s):\n            return store.get_ask_message(k)\n",
        "after the block": "def f(s):\n    with _lens_scope(s):\n        pass\n    return store.get_ask_message(k)\n",
        "in a nested function": "def f(s):\n    with _lens_scope(s):\n        def later():\n            return store.get_ask_message(k)\n    return later()\n",
        "in a lambda": "def f(s):\n    with _lens_scope(s):\n        later = lambda: store.get_ask_message(k)\n    return later()\n",
        "other receiver": "def f(s):\n    with graph.scoped(s.id, h):\n        return store.get_ask_message(k)\n",
        "other context manager": "def f(s):\n    with open(s):\n        return store.get_ask_message(k)\n",
        "in the with item": "def f(s):\n    with _lens_scope(store.get_ask_message(k)):\n        return 1\n",
        "one more read": "def f(s):\n    with _lens_scope(s):\n        a = store.get_ask_message(k)\n    return store.get_ask_message(a)\n",
    }
    for label, source in cases.items():
        assert _scope_site_problems(source, "f", {"get_ask_message": 1}), label


def _references(tree: ast.AST, name: str) -> list[ast.AST]:
    return [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id == name) or (isinstance(node, ast.Attribute) and node.attr == name)
    ]


def _prepare_problems(source: str) -> list[str]:
    """``candidates_for`` is called only by ``prepare``; ``prepare`` only by ``prepare_as_read``, inside a scope."""

    tree = ast.parse(source)
    parents = _parents(tree)
    problems: list[str] = []
    for name, caller in (("candidates_for", "prepare"), ("prepare", "prepare_as_read")):
        uses = [n for n in _references(tree, name) if not isinstance(parents.get(n), ast.FunctionDef)]
        callers = sorted({_function_of(n, parents) for n in uses})
        if callers != [caller] or len(uses) != 1:
            problems.append(f"{name} is referenced {len(uses)} time(s), from {callers}: only {caller} may")
        elif name == "prepare" and not _inside_a_scope(uses[0], parents):
            problems.append("prepare_as_read does not call prepare inside a lens scope")
    return problems


def test_candidates_for_is_reached_only_through_prepare_as_read_inside_the_lens() -> None:
    source = (ROOT / REVIEW_ACTIONS_API).read_text(encoding="utf-8")
    assert _prepare_problems(source) == []
    # it is a closure of the module's router factory, so no other module can name it
    tree = ast.parse(source)
    defined = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "candidates_for"]
    assert len(defined) == 1
    assert _function_of(defined[0], _parents(tree)) != "<module>"


def test_the_prepare_check_sees_another_caller_or_a_missing_scope() -> None:
    good = (
        "def candidates_for():\n    return 1\n\n"
        "def prepare(s):\n    return candidates_for()\n\n"
        "def prepare_as_read(s):\n    with viewer_lens.scoped(s.id, h):\n        return prepare(s)\n"
    )
    assert _prepare_problems(good) == []
    assert _prepare_problems(good + "\ndef route(s):\n    return prepare(s)\n")  # a second caller of prepare
    assert _prepare_problems(good + "\ndef route(s):\n    return candidates_for()\n")  # a second caller
    assert _prepare_problems(good.replace("with viewer_lens.scoped(s.id, h):", "if True:"))
    assert _prepare_problems(good.replace("viewer_lens.scoped", "viewer_lens.unfiltered"))


# --- the rule itself: snippets the scanner must flag (each is a way a check once failed open) --------------------------- #


def _unwrapped_in(sources: dict[str, str]) -> list[tuple[str, str]]:
    return sorted((function, getter) for _file, function, _line, wrapped, getter in _sites_of(sources) if not wrapped)


def test_only_the_found_argument_of_the_lens_reachable_is_wrapped() -> None:
    wrapped = (
        "def f(s):\n    return viewer_lens.reachable(hidden(s), store.get_annotation(k), lambda n: n.created_by)\n"
    )
    assert _unwrapped_in({"a.py": wrapped}) == []
    module = "def f(s):\n    return _viewer_lens_module.reachable(hidden(s), store.get_annotation(k), lambda n: n.created_by)\n"
    assert _unwrapped_in({"a.py": module}) == []
    keywords = "def f(s):\n    return viewer_lens.reachable(found=store.get_annotation(k), author_of=lambda n: n.created_by, hidden=hidden(s))\n"
    assert _unwrapped_in({"a.py": keywords}) == []
    hosted = (
        "async def f(self, s):\n"
        "    return viewer_lens.reachable(await self._second_hidden(s), await self._review_call(store.get_annotation, k), lambda n: n.created_by)\n"
    )
    assert _unwrapped_in({"a.py": hosted}) == []
    bare = "def f(s):\n    return store.get_annotation(k)\n"
    assert _unwrapped_in({"a.py": bare}) == [("f", "get_annotation")]
    # the id-taking helper is not a wrapper: nothing here is passed to reachable
    note = "def f(s):\n    return _reachable_note(s, store.get_annotation(k))\n"
    assert _unwrapped_in({"a.py": note}) == [("f", "get_annotation")]


def test_a_reachable_that_does_not_hide_anything_wraps_nothing() -> None:
    """M8-M11: each is a way ``reachable`` once appeared around a getter that nothing was hidden from."""

    cases = {
        "M8 walrus in the arguments": "def f(s):\n    return viewer_lens.reachable(hidden(s), (g := store.get_annotation(k)), who)\n",
        "M8 a walrus keeps the read outside": "def f(s):\n    viewer_lens.reachable(hidden(s), x, (g := store.get_annotation(k)))\n    return g\n",
        "M9 empty frozenset": "def f(s):\n    return viewer_lens.reachable(frozenset(), store.get_annotation(k), who)\n",
        "M9 empty set call": "def f(s):\n    return viewer_lens.reachable(set(), store.get_annotation(k), who)\n",
        "M9 set literal": "def f(s):\n    return viewer_lens.reachable({'sam'}, store.get_annotation(k), who)\n",
        "M9 empty tuple": "def f(s):\n    return viewer_lens.reachable((), store.get_annotation(k), who)\n",
        "M9 empty list": "def f(s):\n    return viewer_lens.reachable([], store.get_annotation(k), who)\n",
        "M9 a constant": "def f(s):\n    return viewer_lens.reachable(None, store.get_annotation(k), who)\n",
        "M9 keyword": "def f(s):\n    return viewer_lens.reachable(hidden=frozenset(), found=store.get_annotation(k), author_of=who)\n",
        "M10 the author lambda": "def f(s):\n    return viewer_lens.reachable(hidden(s), note, lambda n: store.get_annotation(n.parent_id).created_by)\n",
        "M10 an attribute of the read": "def f(s):\n    return viewer_lens.reachable(hidden(s), store.get_annotation(k).parent_id, who)\n",
        "M10 the hidden argument": "def f(s):\n    return viewer_lens.reachable(store.get_annotation(k), note, who)\n",
        "M11 another receiver": "def f(s):\n    return graph.reachable(hidden(s), store.get_annotation(k), who)\n",
        "M11 a bare name": "def f(s):\n    return reachable(hidden(s), store.get_annotation(k), who)\n",
        "M11 a self method": "def f(self, s):\n    return self.reachable(hidden(s), store.get_annotation(k), who)\n",
        "an alias is no call": "def f(s):\n    g = store.get_annotation\n    return viewer_lens.reachable(hidden(s), g(k), who)\n",
        "dispatched on another object": "async def f(s):\n    return viewer_lens.reachable(hidden(s), await pool._review_call(store.get_annotation, k), who)\n",
        "dispatched under another name": "async def f(self, s):\n    return viewer_lens.reachable(hidden(s), await self._call_later(store.get_annotation, k), who)\n",
    }
    for label, source in cases.items():
        assert ("f", "get_annotation") in _unwrapped_in({"a.py": source}), label


def test_a_reachable_wraps_only_when_it_compares_the_author_with_a_real_hidden_set() -> None:
    """M-author: ``updated_by`` (or anything but the author) is compared with the hidden set; M-nobody: the name is a
    set written out. Neither hides anything, so neither wraps."""

    def call(hidden: str, author_of: str, before: str = "") -> str:
        return f"def f(s):\n{before}    return viewer_lens.reachable({hidden}, store.get_annotation(k), {author_of})\n"

    ok = [
        call("hidden(s)", "lambda n: n.created_by"),
        call("hidden(s)", "lambda n: n.reviewer_id"),
        call("hidden(s)", "lambda n: str(n[1].get('created_by') or '')"),
        call("hidden(s)", "lambda n: n['created_by']"),
        call("hidden(s)", "lambda n: n.thread.created_by"),
        call("hidden", "lambda n: n.created_by", "    hidden = deps.hidden(s)\n"),
        call(
            "hidden", "lambda n: n.created_by", "    hidden = frozenset() if deps.hidden is None else deps.hidden(s)\n"
        ),
    ]
    for source in ok:
        assert _unwrapped_in({"a.py": source}) == [], source
    bad = {
        "updated_by": call("hidden(s)", "lambda n: n.updated_by"),
        "another field": call("hidden(s)", "lambda n: n.id"),
        "a constant": call("hidden(s)", "lambda n: 'sam'"),
        "a name": call("hidden(s)", "who"),
        "a function": call("hidden(s)", "author_of"),
        "no author_of": "def f(s):\n    return viewer_lens.reachable(hidden(s), store.get_annotation(k))\n",
        "a lambda of two parameters": call("hidden(s)", "lambda n, m: n.created_by"),
        "created_by of another object": call("hidden(s)", "lambda n: other.created_by"),
        "updated_by under a get": call("hidden(s)", "lambda n: n.get('updated_by')"),
        "created_by of the getter": call("hidden(s)", "lambda n: store.get_annotation(n.parent_id).created_by"),
        "nobody = frozenset()": call("nobody", "lambda n: n.created_by", "    nobody = frozenset()\n"),
        "nobody = set()": call("nobody", "lambda n: n.created_by", "    nobody = set()\n"),
        "nobody = a literal": call("nobody", "lambda n: n.created_by", "    nobody = {'x'}\n"),
        "nobody, annotated": call("nobody", "lambda n: n.created_by", "    nobody: frozenset[str] = frozenset()\n"),
        "nobody, later reassigned": call(
            "nobody", "lambda n: n.created_by", "    nobody = frozenset()\n    if s:\n        nobody = hidden(s)\n"
        ),
    }
    for label, source in bad.items():
        assert ("f", "get_annotation") in _unwrapped_in({"a.py": source}), label
    enclosing = (
        "def outer(s):\n    nobody = frozenset()\n\n    def f(k):\n"
        "        return viewer_lens.reachable(nobody, store.get_annotation(k), lambda n: n.created_by)\n    return f\n"
    )
    assert ("f", "get_annotation") in _unwrapped_in({"a.py": enclosing})
    module = (
        "nobody = frozenset()\n\ndef f(k):\n"
        "    return viewer_lens.reachable(nobody, store.get_annotation(k), lambda n: n.created_by)\n"
    )
    assert ("f", "get_annotation") in _unwrapped_in({"a.py": module})


def test_a_dispatcher_is_defined_once() -> None:
    """``DISPATCHERS`` name the method a getter is passed to (``self._review_call(getter, *args)``). A second definition
    of the name could do something else with the getter, so the one the scan understands must be the only one."""

    for name in sorted(DISPATCHERS):
        defined = [
            (str(path.relative_to(ROOT)), node.lineno)
            for path in _scanned_paths()
            if name in path.read_text(encoding="utf-8")
            for node in ast.walk(_tree(str(path.relative_to(ROOT))))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
        ]
        assert len(defined) == 1, f"{name} is defined {len(defined)} times: {defined}"


def test_a_ledger_get_is_seen_on_what_was_assigned_from_a_ledger() -> None:
    """M4a/b/c: the holder is followed by assignment, not by what it is called; a dict ``ledger`` is not one."""

    seen = [("f", "ledger.get")]
    cases = {
        "M4a a name assigned from a call": "def f(store, ids):\n    actions = ReviewActionLedger(store)\n    return actions.get(ids, k)\n",
        "M4a a call inline": "def f(store):\n    return ReviewActionLedger(store).get(ids, k)\n",
        "M4a a qualified call": "def f(store):\n    led = ledgers.SplitLedger(store)\n    return led.get(k)\n",
        "M4a a factory": "def f(store):\n    box = approval_ledger(store)\n    return box.get(k)\n",
        "M4a a walrus": "def f(store):\n    if (box := ReviewActionLedger(store)):\n        return box.get(k)\n",
        "M4a a with": "def f(store):\n    with DerivedRecordLedger(store, 'x') as box:\n        return box.get(k)\n",
        "M4a through a second name": "def f(store):\n    a = ReviewActionLedger(store)\n    b = a\n    return b.get(k)\n",
        "M4b an attribute": "class C:\n    def __init__(self, store):\n        self.box = ReviewActionLedger(store)\n\n    def f(self):\n        return self.box.get(k)\n",
        "M4b an attribute set from a name": "class C:\n    def __init__(self, store):\n        box = ReviewActionLedger(store)\n        self.box = box\n\n    def f(self):\n        return self.box.get(k)\n",
        "M4c an annotated parameter": "def f(box: ReviewActionLedger, k):\n    return box.get(k)\n",
        "M4c an optional parameter": "def f(box: ReviewActionLedger | None, k):\n    return box.get(k)\n",
        "M4c a string annotation": "def f(box: 'ReviewActionLedger', k):\n    return box.get(k)\n",
        "M4c an annotated assignment": "def f(k):\n    box: ReviewActionLedger = make()\n    return box.get(k)\n",
        "a closure over the enclosing function": "def g(store):\n    box = ReviewActionLedger(store)\n\n    def f(k):\n        return box.get(k)\n    return f\n",
        "a module-level ledger": "box = ReviewActionLedger(store)\n\ndef f(k):\n    return box.get(k)\n",
        "a local factory": "def make(store):\n    return ReviewActionLedger(store)\n\ndef f(store):\n    return make(store).get(k)\n",
    }
    for label, source in cases.items():
        found = _unwrapped_in({"a.py": source})
        assert any(g == "ledger.get" for _f, g in found), label
    assert _unwrapped_in({"a.py": cases["M4a a name assigned from a call"]}) == seen
    not_ledgers = {
        "a container of ledgers": "_by_id: OrderedDict[str, RunLedger] = OrderedDict()\n\ndef f(k):\n    return _by_id.get(k)\n",
        "a lowercase callee": "def f(store):\n    box = make_ledger(store)\n    return box.get(k)\n",
        "a name of another function": "def g(store):\n    box = ReviewActionLedger(store)\n\ndef f(box, k):\n    return box.get(k)\n",
    }
    for label, source in not_ledgers.items():
        assert _unwrapped_in({"a.py": source}) == [], label


def test_a_ledger_is_seen_across_modules_and_by_its_name() -> None:
    """N1, N1b, N1c: the miss R14 found in the per-file tracking. The index is built over every scanned module."""

    seen = [("f", "ledger.get")]
    unannotated = "def f(ledger, ids, k):\n    return ledger.get(ids, k)\n"
    assert _unwrapped_in({"a.py": unannotated}) == seen  # N1: the name rule
    assert _unwrapped_in({"a.py": "def f(self, k):\n    return self.action_ledger.get(k)\n"}) == seen
    other_module_field = "class ReviewActionDeps:\n    action_ledger: ReviewActionLedger | None = None\n"
    deps = "def f(deps, k):\n    return deps.actions.get(k)\n"
    # N1b: the field is annotated in another module, and has a name the name rule does not know
    holder = "class ReviewActionDeps:\n    actions: ReviewActionLedger | None = None\n"
    assert _unwrapped_in({"a.py": deps}) == []
    assert _unwrapped_in({"a.py": deps, "deps.py": holder}) == seen
    assert (
        _unwrapped_in({"a.py": "def f(deps, k):\n    return deps.action_ledger.get(k)\n", "d.py": other_module_field})
        == seen
    )
    # N1c: a helper in another module returns a ledger (by annotation, or by returning one)
    caller = "def f(store, k):\n    return history_for(store).get(k)\n"
    by_annotation = "def history_for(store) -> ReviewActionLedger:\n    return _cache[store]\n"
    by_return = "def history_for(store):\n    return ReviewActionLedger(store)\n"
    assert _unwrapped_in({"a.py": caller}) == []
    assert _unwrapped_in({"a.py": caller, "h.py": by_annotation}) == seen
    assert _unwrapped_in({"a.py": caller, "h.py": by_return}) == seen
    assigned = "def f(store, k):\n    box = history_for(store)\n    return box.get(k)\n"
    assert _unwrapped_in({"a.py": assigned, "h.py": by_annotation}) == seen
    # a container of ledgers is not a ledger, in another module either
    container = "_by_id: OrderedDict[str, RunLedger] = OrderedDict()\n"
    assert _unwrapped_in({"a.py": "def f(k):\n    return _by_id.get(k)\n", "c.py": container}) == []


def test_a_getter_inside_a_lens_scope_is_flagged_until_it_is_listed() -> None:
    """The scope is not an exemption: every such site is in ALLOWED, with its count, or the test fails."""

    for scope in (
        "_lens_scope(session)",
        "viewer_lens.scoped(session.id, hidden_of(session))",
        "viewer_lens.scoped_many(hidden_by_review(session))",
    ):
        source = f"def handler(session):\n    with {scope}:\n        return store.get_annotation(key)\n"
        assert _unwrapped_in({"a.py": source}) == [("handler", "get_annotation")], scope
    prepare = (
        "def prepare(session):\n    return store.get_annotation(session.note_id)\n\n"
        "def prepare_as_read(session):\n    with _lens_scope(session):\n        return prepare(session)\n"
    )
    assert _unwrapped_in({"a.py": prepare}) == [("prepare", "get_annotation")]
    nested = (
        "def handler(session):\n    with _lens_scope(session):\n        with viewer_lens.unfiltered():\n"
        "            return store.get_annotation(key)\n"
    )
    assert _unwrapped_in({"a.py": nested}) == [("handler", "get_annotation")]


def test_a_getter_that_is_bound_deferred_or_looked_up_by_name_is_flagged() -> None:
    cases = {
        "alias": "def f(s):\n    with _lens_scope(s):\n        g = store.get_annotation\n    return g(k)\n",
        "partial": "def f(s):\n    with _lens_scope(s):\n        g = functools.partial(store.get_annotation, k)\n    return g()\n",
        "generator": "def f(s):\n    with _lens_scope(s):\n        rows = (store.get_annotation(i) for i in ids)\n    return list(rows)\n",
        "generator function": "def f(s):\n    with _lens_scope(s):\n        def rows():\n            yield store.get_annotation(k)\n    return list(rows())\n",
        "coroutine": "async def f(s):\n    with _lens_scope(s):\n        pending = fetch(store.get_annotation, k)\n    return await pending\n",
        "decorated route": "@router.get('/x')\ndef route(s):\n    return store.get_annotation(k)\n\ndef other(s):\n    with _lens_scope(s):\n        return route(s)\n",
        "getattr": "def f(s):\n    return getattr(store, 'get_annotation')(k)\n",
        "getattr in a scope": "def f(s):\n    with _lens_scope(s):\n        return getattr(store, 'get_change_proposal')(k)\n",
    }
    for label, source in cases.items():
        assert _unwrapped_in({"a.py": source}), label
    assert _unwrapped_in({"a.py": cases["getattr"]}) == [("f", "get_annotation")]
    assert _unwrapped_in({"a.py": "def f(s):\n    return getattr(store, 'other_name')(k)\n"}) == []
    assert _unwrapped_in({"a.py": "def f(s):\n    return getattr(store, name)(k)\n"}) == []  # not a constant: not seen
    local_scope = (
        "def _lens_scope(s):\n    return nullcontext()\n\n"
        "def f(s):\n    with _lens_scope(s):\n        return store.get_annotation(k)\n"
    )
    assert _unwrapped_in({"a.py": local_scope}) == [("f", "get_annotation")]
    fake_module = "import contextlib as viewer_lens\n\ndef f(s):\n    with viewer_lens.suppress(Exception):\n        return store.get_annotation(k)\n"
    assert _unwrapped_in({"a.py": fake_module}) == [("f", "get_annotation")]
