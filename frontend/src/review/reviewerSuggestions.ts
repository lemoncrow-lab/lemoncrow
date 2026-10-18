import { requestBrowserJson } from "./reviewApi";

/** Advisory reviewer suggestions for one exact Snapshot (Phase 9 item 7); see `SuggestionSet.to_wire`. */
export type ReviewerCandidateKind = "user" | "team";
export type ReviewerSignal = "rule" | "codeowners" | "ownership" | "history";
export type ReviewerAccess = "granted" | "unknown" | "denied" | "not_applicable";
export type ReviewerSignalState = "available" | "absent" | "provisional" | "unavailable" | "invalid";

export interface ReviewerSuggestionReason {
  signal: ReviewerSignal;
  paths: string[];
  path_count: number;
  sources: string[];
  detail: string;
  rule_ids: string[];
  count: number;
}

export interface ReviewerSuggestion {
  kind: ReviewerCandidateKind;
  /** Durable routing identity: principal, provider durable id, or (local) handle/email. */
  key: string;
  label: string;
  principal_id: string;
  handle: string;
  provider: string;
  durable_id: string;
  score: number;
  explanation: string;
  reasons: ReviewerSuggestionReason[];
  paths: string[];
  path_count: number;
  critical_paths: string[];
  access: ReviewerAccess;
  status: "suggested" | "requested";
  /** Operator rule ids that may route this candidate automatically (only when the organization opted in). */
  automatic_rules: string[];
}

export interface ReviewerSignalStatus {
  signal: ReviewerSignal;
  state: ReviewerSignalState;
  source: string;
  detail: string;
}

export interface ReviewerHeadChange {
  path: string;
  change: string;
  note: string;
}

export interface ReviewerSuggestionSet {
  advisory: true;
  revision_id: string;
  suggestions: ReviewerSuggestion[];
  excluded: Record<string, number>;
  truncated: number;
  signals: ReviewerSignalStatus[];
  head_changes: ReviewerHeadChange[];
  automatic_routing: boolean;
  errors: string[];
  /** Hosted only: whether the caller may request a suggested reviewer. */
  can_request?: boolean;
  provider?: string;
}

export function fetchReviewerSuggestions(reviewId: string, revisionId = ""): Promise<ReviewerSuggestionSet> {
  const query = revisionId ? `?revision_id=${encodeURIComponent(revisionId)}` : "";
  return requestBrowserJson<ReviewerSuggestionSet>(`/api/reviews/${encodeURIComponent(reviewId)}/reviewer-suggestions${query}`);
}

const EXCLUSION_LABEL: Record<string, string> = {
  author: "author",
  authoring_agent: "authoring agent",
  no_access: "without access",
};

/** "Excluded: 1 author · 2 without access" (empty when nobody was excluded). */
export function exclusionSummary(excluded: Record<string, number>): string {
  const parts = Object.entries(excluded)
    .filter(([, count]) => count > 0)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([reason, count]) => `${count} ${EXCLUSION_LABEL[reason] ?? reason}`);
  return parts.length ? `Excluded: ${parts.join(" · ")}` : "";
}

const SIGNAL_LABEL: Record<ReviewerSignal, string> = {
  rule: "Reviewer rules",
  codeowners: "CODEOWNERS",
  ownership: "Earlier Reviews",
  history: "Git history",
};

/** Signals that could not contribute, stated plainly instead of silently dropped. */
export function degradedSignals(signals: ReviewerSignalStatus[]): string[] {
  return signals
    .filter((row) => row.state === "provisional" || row.state === "invalid" || row.state === "unavailable")
    .map((row) => `${SIGNAL_LABEL[row.signal] ?? row.signal}: ${row.state}${row.detail ? ` · ${row.detail}` : ""}`);
}
