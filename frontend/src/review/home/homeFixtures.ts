import type { ReviewDirectoryRow } from "../types";

/** One Inbox row as `GET /v1/reviews?view=all` sends it for a local review waiting on its reviewer (the server's `_row` shape). */
export function queueRow(overrides: Partial<ReviewDirectoryRow> & { id: string }): ReviewDirectoryRow {
  const base: ReviewDirectoryRow = {
    id: overrides.id,
    ref: `r/${overrides.id}`,
    review_path: `/r/${overrides.id}`,
    repo_id: "repo_0123456789abcdef0123456789abcdef",
    title: `Change in ${overrides.id}`,
    source_ref: "working_tree",
    range_mode: "working_tree",
    status: "open",
    revision_number: 0,
    revision_id: `rev-${overrides.id}`,
    updated_at: "2026-11-07T09:00:00+00:00",
    created_at: "2026-11-07T08:00:00+00:00",
    project: "shop",
    branch: "main",
    progress: {
      target_count: 3, reviewed: 0, changed_since_review: 0, needs_changes: 0, unreviewed: 3, unknown: 0, mechanical: 0,
      later: 0, decision: 2, quick_check: 1, accepted_by_rule: 0, decision_open: 2, quick_check_open: 1,
    },
    views: ["to_review", "waiting_on_me"],
    next_action: {
      owner_ids: ["local"],
      for_me: true,
      my_reasons: [{ principal_id: "local", kind: "review_pending", fact: "3 parts left to review", since: "2026-11-07T08:00:00Z" }],
      reasons: [{ principal_id: "local", kind: "review_pending", fact: "3 parts left to review", since: "2026-11-07T08:00:00Z" }],
    },
    attention: { score: 50, waiting_since: "2026-11-07T08:00:00Z", factors: [] },
    unsent_count: 0,
    queue: { snoozed_until: "", resume: null },
  };
  return { ...base, ...overrides };
}

/** A review that came back: the author's addressed comments are waiting for the reviewer again. */
export function cameBackRow(id: string, overrides: Partial<ReviewDirectoryRow> = {}): ReviewDirectoryRow {
  return queueRow({
    id,
    revision_number: 1,
    primary_bucket: "returned_to_me",
    next_action: {
      owner_ids: ["local"],
      for_me: true,
      my_reasons: [{ principal_id: "local", kind: "thread_returned", fact: "2 addressed comment(s) back with you", since: "" }],
      reasons: [{ principal_id: "local", kind: "thread_returned", fact: "2 addressed comment(s) back with you", since: "" }],
    },
    ...overrides,
  });
}

/** A review whose notes went to a coding agent: someone else's turn. */
export function waitingRow(id: string, overrides: Partial<ReviewDirectoryRow> = {}): ReviewDirectoryRow {
  return queueRow({
    id,
    primary_bucket: "waiting_on_agent",
    handoff_label: "Claude Code",
    views: ["blocked"],
    next_action: {
      owner_ids: ["agent"],
      for_me: false,
      my_reasons: [],
      reasons: [{ principal_id: "agent", kind: "feedback_with_agent", fact: "2 comment(s) with a coding agent", since: "" }],
    },
    ...overrides,
  });
}
