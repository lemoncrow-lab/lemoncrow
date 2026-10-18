/**
 * The shapes `lemoncrow.pro.capabilities.review.api` actually returns.
 *
 * Hand-written rather than generated, and deliberately narrow: this file
 * describes what the workspace *reads*, not everything the Python dataclasses
 * hold. Fields are optional wherever the server may legitimately have nothing
 * to say, because "unknown" is a first-class state here and a type that forces
 * a value invites the UI to invent one.
 */

import type { AgentActionApproval, AgentApproval } from "./agentApprovalsApi";

export type MarkState =
  | "unreviewed"
  | "reviewed"
  | "needs_changes"
  | "changed_since_review"
  | "unknown";

export type GroupKey =
  | "needs_attention"
  | "changed_since_my_review"
  | "unreviewed"
  | "reviewed"
  | "mechanical";

export type ReviewSessionStatus = "open" | "finished" | "archived";
export type ReviewOutcomeKind = "comment" | "lgtm" | "changes_requested";

export interface ReviewOutcome {
  id: string;
  review_id: string;
  revision_id: string;
  reviewer_id: string;
  outcome: ReviewOutcomeKind;
  summary: string;
  created_at: string;
  stale: boolean;
}

export interface ReviewOutcomeProjection {
  review_id: string;
  revision_id: string;
  reviewer_id: string;
  current: ReviewOutcome | null;
  history: ReviewOutcome[];
}

/**
 * A reviewer's scoped request, supplied by a hosted composition. Its targets
 * are the default queue and Brief; every other target stays reachable behind a
 * visible "outside your request" control. Matching is the server's own rule:
 * a `paths` scope names target paths, a `targets` scope names unit keys.
 */
export interface ReviewRequestScope {
  kind: "paths" | "targets";
  values: readonly string[];
  requestedBy?: string;
}

export interface ReviewTargetSpan {
  side: "old" | "new";
  start_line: number;
  end_line: number;
  hunk_ordinal: number;
}

export interface ReviewTarget {
  target_id: string;
  unit_key: string;
  kind: "symbol" | "hunk" | "file" | "dependency" | "document_section";
  path: string;
  label: string;
  symbol: string;
  start_line: number;
  end_line: number;
  hunk_ordinals: number[];
  spans: ReviewTargetSpan[];
  state: MarkState;
  changed_since_mark: boolean;
  reviewed_revision_id: string;
  attention_rank: number;
  attention_level: "high" | "normal" | "mechanical";
  reasons: string[];
  additions: number;
  deletions: number;
  fingerprint_method: string;
  verification: { pass: number; fail: number; unknown: number };
  annotation_counts: { open: number; orphaned: number; addressed_needs_rereview: number };
  /** Canonical queue placement from the server; clients never re-rank. */
  priority?: ReviewTargetPriority;
  /**
   * Movement since what this reviewer last judged (or the previous Snapshot).
   * `base_only`: only the diff base moved under it (rebase/merge-base); its
   * authored patch is equivalent and the judgment was kept. Undecidable cases
   * are `authored`, never `base_only`.
   */
  movement?: ReviewTargetMovement;
}

export type ReviewTargetMovement = "unchanged" | "authored" | "base_only" | "added";

/** Deterministic classification of what moved between two Snapshots (Phase 9 item 34). */
export interface ReviewMovementSummary {
  kind: "none" | "authored" | "base_only" | "rebase_only" | "rebase_with_authored_changes";
  base_moved: boolean;
  head_moved: boolean;
  deterministic: boolean;
  base_only_paths: string[];
  authored_paths: string[];
  undecided_paths: string[];
  unit_counts: { unchanged: number; authored: number; base_only: number; added: number; removed: number };
  reason: string;
  relative_to: "your_review" | "previous_snapshot";
  from_revision_number: number;
  base_only_targets: number;
  authored_targets: number;
}

export interface ReviewPriorityReason {
  code: string;
  /** Display text; never parsed by clients. */
  detail: string;
}

export interface ReviewTargetPriority {
  version: number;
  tier: number;
  attention: "review_first" | "understand" | "routine" | "done";
  outstanding: boolean;
  position: number;
  reasons: ReviewPriorityReason[];
}

export interface ReviewProgress {
  target_count: number;
  reviewed: number;
  changed_since_review: number;
  needs_changes: number;
  unreviewed: number;
  unknown: number;
  mechanical: number;
}

export interface ReviewOutlineItem {
  path: string;
  target_count: number;
  reviewed: number;
  changed_since_review: number;
  needs_changes: number;
  unreviewed: number;
  unknown: number;
  mechanical: number;
  attention_rank: number;
  reasons: string[];
  section?: "review_first" | "unreviewed" | "mechanical" | "done";
  position?: number;
  next_target_id?: string;
  next_reason?: string;
  next_reason_code?: string;
  review_first?: number;
  understand?: number;
  routine?: number;
  /** Stable per-file paging weight supplied by the large-review startup index. */
  changed_lines?: number;
}

export interface ReviewTargetList {
  revision_id: string;
  order: "recommended" | "file";
  targets: ReviewTarget[];
  progress: ReviewProgress;
  outline: ReviewOutlineItem[];
  movement?: ReviewMovementSummary | null;
  /** False only for the bounded large-review startup projection. */
  complete?: boolean;
}

/** How the Reader presents a plugin artifact; the surface kind is never consulted. */
export type ReviewPreviewDisplay = "image" | "html" | "text" | "json" | "download";

/** Versioned preview contract a surface provider attaches to a surface. */
export interface ReviewSurfacePreviewDescriptor {
  version: number;
  renderer: string;
  display: ReviewPreviewDisplay | string;
  media_type: string;
  label: string;
  sides: Array<"old" | "new">;
  scope: string;
  options: Record<string, unknown>;
  /** Host verdict before rendering: an uninstalled/incompatible renderer is "unavailable". */
  status?: "available" | "unavailable";
  reason?: string;
}

export interface ReviewSurfaceComparisonDescriptor {
  version: number;
  source: "preview" | "run" | "web" | string;
  comparator: string;
  label: string;
  auto: boolean;
  options: Record<string, unknown>;
  status?: "available" | "unavailable";
  reason?: string;
  inferred?: boolean;
}

export interface ReviewSurfaceInfo {
  id: string;
  provider: string;
  kind: string;
  title: string;
  locator: string;
  runtime: string;
  affected_paths: string[];
  capabilities: string[];
  metadata: Record<string, unknown>;
  preview?: ReviewSurfacePreviewDescriptor | null;
  comparison?: ReviewSurfaceComparisonDescriptor | null;
}

/** A provider/renderer plugin that failed to load or to discover. */
export interface ReviewSurfacePluginIssue {
  plugin: string;
  kind: "provider" | "renderer" | string;
  reason: string;
}

export interface ReviewSurfaceList {
  revision_id: string;
  surfaces: ReviewSurfaceInfo[];
  issues?: ReviewSurfacePluginIssue[];
}

export interface ReviewSurfaceRunResult {
  surface_id: string;
  provider: string;
  revision_id: string;
  side: "old" | "new";
  status: "passed" | "failed" | "unavailable" | string;
  summary: string;
  runner: string;
  runtime: string;
  exit_code: number | null;
  duration_ms: number;
  output: string;
  data: Record<string, unknown>;
}

export interface ReviewSurfaceRunResponse {
  revision_id: string;
  result: ReviewSurfaceRunResult;
}

export type ReviewRuntimeValidationStatus =
  | "PASS"
  | "FAIL"
  | "UNKNOWN"
  | "NOT_RUN"
  | "QUEUED"
  | "RUNNING"
  | "CANCEL_REQUESTED"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED"
  | string;

export interface ReviewRuntimeValidationRequest {
  identity: string;
  review_id: string;
  revision_id: string;
  snapshot_identity: string;
  scenario_kind: "browser" | "surface" | string;
  scenario_id: string;
  side: "old" | "new";
  viewport_key: string;
  runtime_id: string;
  runtime_config_digest: string;
  trusted_policy_digest: string;
  scope_digest: string;
  target_ids: string[];
  schema_version: number;
  execution_version: number;
}

export interface ReviewRuntimeValidationJob {
  job_id?: string;
  state: string;
  attempt?: number;
  retry_count?: number;
  reclaim_count?: number;
  cancel_requested?: boolean;
  last_error?: string;
  detail?: string;
  execution_intent?: "current" | "explicit_historical" | string;
  historical?: boolean;
  superseded?: boolean;
  publication_state?: "current" | "historical" | "superseded" | string;
  result_refs?: string[];
  evidence_refs?: string[];
}

export interface ReviewRuntimeValidationRecord {
  status: ReviewRuntimeValidationStatus;
  status_reasons?: string[];
  summary?: string;
  deterministic_checks?: Array<{
    name: string;
    status: "PASS" | "FAIL" | "UNKNOWN" | string;
    expected?: unknown;
    actual?: unknown;
    detail?: string;
  }>;
  job?: ReviewRuntimeValidationJob;
}

export interface ReviewRuntimeValidationResponse {
  request: ReviewRuntimeValidationRequest;
  reused: boolean;
  execution_intent?: "current" | "explicit_historical" | string;
  historical?: boolean;
  runtime_validation: ReviewRuntimeValidationRecord;
  evidence?: ReviewEvidence;
  verification: ReviewVerificationProjection;
}

export type ReviewMapLevel = "architecture" | "file" | "symbol";
export type ReviewMapLane = "used_by" | "uses" | "";
export type ReviewMapExactness = "snapshot" | "current_index" | "inferred" | "unknown";
export type ReviewMapProjection = "change_map" | "architecture_delta";
export type ReviewArchitectureDeltaStatus = "added" | "removed" | "changed" | "unchanged" | "context_only" | "unknown";
export type ReviewMapStatus = "changed" | "new" | "impacted" | "context";

export interface ReviewMapSelection {
  kind: "review_map_node" | "review_map_edge" | string;
  review_id: string;
  revision_id: string;
  level?: ReviewMapLevel;
  focus_node_id?: string;
  node_id?: string;
  edge_id?: string;
  exactness: ReviewMapExactness | string;
  side?: "new" | "old" | string;
  path?: string;
  line?: number;
  end_line?: number;
  symbol?: string;
  target_ids?: string[];
  current_symbol_id?: string;
  source_id?: string;
  target_id?: string;
  relation?: string;
  projection?: ReviewMapProjection | string;
  delta_status?: ReviewArchitectureDeltaStatus | string;
  architecture_source?: string;
  architecture_comparison?: string;
}

export interface ReviewMapMemberSymbol {
  id: string;
  label: string;
  qualified_name: string;
  kind?: string;
  path: string;
  line: number;
  end_line: number;
  change: string;
  caller_count: number;
  usage_count: number;
  centrality_rank: number | null;
  centrality_percentile: number | null;
  target_ids?: string[];
}

export interface ReviewMapReviewProgress {
  targets: number;
  reviewed: number;
  needs_changes: number;
  changed_since_review: number;
  open_threads: number;
  attention: "high" | "normal" | "mechanical" | "";
  outstanding?: number;
  next_position?: number;
}

export interface ReviewMapNode {
  id: string;
  label: string;
  qualified_name?: string;
  kind: string;
  component?: string;
  /** Parent directory; absent from older servers (derive from `path`). */
  module?: string;
  /** Which side of the change a surrounding file sits on; "" for the subject. */
  lane?: ReviewMapLane;
  path: string;
  line: number;
  end_line: number;
  status: ReviewMapStatus;
  change?: string;
  old_path?: string;
  additions?: number;
  deletions?: number;
  category?: string;
  language?: string;
  rank?: number;
  reasons?: string[];
  review?: ReviewMapReviewProgress | null;
  symbol_count?: number;
  exactness: ReviewMapExactness;
  changed_file_count?: number;
  impacted_file_count?: number;
  paths?: string[];
  paths_truncated?: boolean;
  symbols?: ReviewMapMemberSymbol[];
  symbols_truncated?: boolean;
  target_ids: string[];
  current_symbol_ids?: string[];
  current_symbol_id?: string;
  change_kinds?: string[];
  important_public_contract: boolean;
  boundaries: string[];
  boundary_basis: string;
  uncertain?: boolean;
  uncertainty?: string;
  caller_count?: number;
  usage_count?: number;
  centrality_rank?: number | null;
  centrality_percentile?: number | null;
  relationship_count?: number;
  provenance?: { host?: string; model?: string; certainty?: string; method?: string };
  delta_status?: ReviewArchitectureDeltaStatus | string;
  architecture_provenance?: ReviewArchitectureProvenance;
  selection: ReviewMapSelection;
}

export interface ReviewMapEdge {
  id: string;
  source: string;
  target: string;
  kind: string;
  exactness: ReviewMapExactness;
  weight: number;
  base_weight?: number;
  snapshot_weight?: number;
  delta_status?: ReviewArchitectureDeltaStatus | string;
  architecture_provenance?: ReviewArchitectureProvenance;
  uncertain: boolean;
  evidence?: { kind: string; path: string; line: number; uncertainty: string; symbol?: string; snippet?: string }[];
  /** Names the relationship lands on, most frequent first. */
  symbols?: { name: string; count: number }[];
  selection: ReviewMapSelection;
}

export interface ReviewMapStats {
  changed_files?: number;
  new_files?: number;
  deleted_files?: number;
  impacted_files?: number;
  context_files?: number;
  shown_changed?: number;
  shown_impacted?: number;
  shown_context?: number;
  relationships?: number;
  shown_relationships?: number;
  filtered_relationships?: number;
  filtered_reasons?: Record<string, number>;
  architecture_changed_edges?: number;
  architecture_context_edges?: number;
  /** Symbol focus: outgoing calls could be attributed to the symbol's exact span. */
  outgoing_attributed?: boolean;
}

export interface ReviewArchitectureProvenance {
  source: "snapshot_comparison" | "snapshot_fact" | "current_index" | "inferred_explanation" | "unavailable" | string;
  exactness: ReviewMapExactness | string;
  comparison: "base_vs_snapshot" | "none" | string;
  note: string;
}

export interface ReviewArchitectureComponent {
  id: string;
  label: string;
  delta_status: ReviewArchitectureDeltaStatus | string;
  base_file_count: number;
  snapshot_file_count: number;
  paths: string[];
  paths_truncated: boolean;
}

export interface ReviewArchitectureFlowStep {
  edge_id: string;
  from: string;
  to: string;
  relationship: string;
  delta_status: ReviewArchitectureDeltaStatus | string;
  base_weight: number;
  snapshot_weight: number;
  /** Observed when a planned run's call trace on this side matched the step; otherwise inferred (R7). */
  grounding?: "observed" | "inferred";
  trace?: {
    side: "base" | "head";
    caller_fn: string;
    callee_fn: string;
    recipe: string;
    evidence_ref: string;
    record_id: string;
  } | null;
}

export interface ReviewObservedCall {
  from: string;
  from_fn: string;
  to: string;
  to_fn: string;
  evidence_ref: string;
}

export interface ReviewArchitectureObservedFlow {
  available: boolean;
  comparable: boolean;
  grounded_steps: number;
  inferred_steps: number;
  base: ReviewObservedCall[];
  head: ReviewObservedCall[];
  added: ReviewObservedCall[];
  removed: ReviewObservedCall[];
  note: string;
}

export interface ReviewArchitectureDelta {
  version: number;
  digest: string;
  status: "ready" | "degraded" | "unavailable" | string;
  reason: string;
  base_identity: string;
  snapshot_identity: string;
  grouping: {
    strategy: string;
    version: number;
    stable_for_identical_input: boolean;
    components: ReviewArchitectureComponent[];
  };
  summary: {
    material: boolean;
    status: string;
    changed_components: number;
    changed_nodes: number;
    changed_edges: number;
    added_edges: number;
    removed_edges: number;
    modified_edges: number;
    context_only_edges: number;
    unknown_edges: number;
    important_concerns: string[];
    exactness: ReviewMapExactness | string;
    degraded: string[];
  };
  flow: {
    applicable: boolean;
    kind: string;
    reason: string;
    steps: ReviewArchitectureFlowStep[];
    observed?: ReviewArchitectureObservedFlow;
  };
  graph: ReviewMapGraph;
}

export interface ReviewArchitectureExplanation {
  summary: string;
  why_it_matters: string[];
  grounded_edge_ids: string[];
  flow: { edge_id: string; explanation: string }[];
  limitations: string[];
  provenance: "deterministic" | "inferred_explanation" | string;
  exactness: ReviewMapExactness | string;
}

export interface ReviewArchitectureExplanationResponse {
  status: "ready" | "failed" | "cancelled" | "not_applicable" | string;
  architecture_delta_digest: string;
  result: ReviewArchitectureExplanation;
  intelligence?: Record<string, unknown> | null;
  error?: string;
}

export interface ReviewMapGraph {
  level: ReviewMapLevel;
  focus: string | null;
  nodes: ReviewMapNode[];
  edges: ReviewMapEdge[];
  truncated: boolean;
  omitted_node_count?: number;
  stats?: ReviewMapStats;
  reason?: string;
}

export interface ReviewMapPayload {
  review_id: string;
  revision_id: string;
  revision_number: number;
  level: ReviewMapLevel;
  projection?: ReviewMapProjection;
  architecture_delta?: ReviewArchitectureDelta;
  snapshot: {
    exactness: "snapshot";
    tree_fingerprint: string;
    source_fingerprint: string;
    base_sha: string;
    head_sha: string;
    degraded: string[];
    /** One plain sentence per degraded signal, in the order of `degraded`. */
    degraded_notes?: { name: string; note: string }[];
    provenance: { host: string; model: string; certainty: string; method: string };
    verification: {
      pass: number;
      fail: number;
      not_run: number;
      unknown: number;
      checks: { name: string; status: string; detail: string; source: string }[];
    };
  };
  context: {
    source: "current_index" | "unavailable" | string;
    exact_for_snapshot: false;
    snapshot_head_matches_checkout: boolean;
    generation_matches_snapshot_head?: boolean;
    index: Record<string, unknown>;
    readiness?: {
      backend?: string;
      reason?: string;
      graph?: { state?: string; reason?: string };
      semantic?: { state?: string; reason?: string };
    };
    note: string;
  };
  graph: ReviewMapGraph;
}

export interface ReviewSessionInfo {
  id: string;
  ref: string;
  object?: "change" | string;
  change_ref?: string;
  title: string;
  subject_type: string;
  range_mode: string;
  source_ref: string;
  review_base_sha?: string;
  status: ReviewSessionStatus;
  actor_type: string;
  reviewer_id: string;
  repo_root: string;
  created_at?: string;
  updated_at: string;
  revision_number?: number;
  revision_id?: string;
  comment_count?: number;
  open_comment_count?: number;
  /** Present once this local Review is explicitly attached: hosted owns its collaborative state. */
  hosted?: HostedAttachmentInfo;
}

/** The hosted Change a local Review is attached to; `id`/`ref` above are then the hosted ones. */
export interface HostedAttachmentInfo {
  reference: string;
  origin: string;
  url: string;
  /** Diagnostic provenance only: the local Review this attachment lives on. */
  local_review_id: string;
  principal_id?: string;
  synced_at?: string;
  owns?: string[];
}

export type HostedActionKind = "mark" | "comment" | "reply" | "outcome" | "request_complete";
export type HostedActionState = "pending" | "applied" | "conflict" | "discarded";

/** One collaborative action queued for hosted, bound to the hosted Snapshot it was made on. */
export interface HostedAction {
  id: string;
  sequence: number;
  kind: HostedActionKind;
  snapshot_id: string;
  supersede_key: string;
  payload: Record<string, unknown>;
  preconditions: Record<string, unknown>;
  state: HostedActionState;
  reason: string;
  result: Record<string, unknown>;
  created_at: string;
  resolved_at: string;
}

export interface HostedAttachmentStatus {
  attached: boolean;
  review_id: string;
  reference?: string;
  attachment?: {
    origin: string;
    hosted_repo_id: string;
    hosted_review_id: string;
    principal_id: string;
    attached_at: string;
    synced_at: string;
    url: string;
  };
  owns?: string[];
  /** Hosted's view for this reviewer as of the last sync; ``[]``/``{}`` when it could not be read. */
  projection?: HostedProjection | unknown[];
  actions?: HostedAction[];
  counts?: Record<HostedActionState, number>;
  limits?: { unresolved: number; resolved_kept: number };
}

/** The parts of hosted's projection the Reader reads; hosted stays the authority. */
export interface HostedProjection {
  snapshot?: { id: string; number: number };
  /** The reviewer's own current verdict on hosted; ``summary`` is absent from older hosted servers. */
  my_outcome?: { id: string; outcome: string; revision_id: string; summary?: string } | null;
}

/** A Reader write on an attached Review: queued for hosted (HTTP 202), never applied locally. */
export interface HostedQueuedWrite {
  queued: true;
  reference: string;
  actions: HostedAction[];
  superseded: string[];
  counts: Record<HostedActionState, number>;
}

export interface HostedDetachResult {
  detached: true;
  review_id: string;
  discarded_unresolved: number;
  removed_resolved: number;
  kept: string;
}

export interface RevisionInfo {
  id: string;
  ref: string;
  object?: "snapshot" | string;
  snapshot_ref?: string;
  revision_number: number;
  range_mode: string;
  base_sha: string;
  head_sha: string;
  merge_base_sha?: string;
  source_fingerprint?: string;
  dirty: boolean;
  capture_kind?: "legacy" | "draft" | "commit" | "revision";
  commit_sha?: string;
  commit_number?: number | null;
  commit_state?: "active" | "superseded" | null;
  draft_number?: number | null;
  branch_ref?: string;
  worktree_key?: string;
  title?: string;
  degraded: string[];
  provenance_host: string;
  provenance_model: string;
  provenance_session_id: string;
  provenance_certainty: string;
  created_at: string;
  is_latest?: boolean;
  is_published_snapshot?: boolean;
  is_last_reviewed?: boolean;
  is_last_commented?: boolean;
  has_review_feedback?: boolean;
  previous_revision_number?: number | null;
  semantic_changed?: boolean;
  source_identity_changed?: boolean;
  base_moved?: boolean;
  review_area_count?: number;
  review_area_delta?: {
    added: number;
    changed: number;
    removed: number;
  };
  delta?: {
    added: number;
    changed: number;
    removed: number;
  };
}

export interface ReviewRevisionState {
  latest_revision_number: number | null;
  last_reviewed_revision_number: number | null;
  last_commented_revision_number: number | null;
  default_base_revision_number: number | null;
  default_target_revision_number: number | null;
}

export interface ReviewRevisionHistory extends ReviewRevisionState {
  revisions: RevisionInfo[];
}

export type FixLineStatus = "fixing" | "applied" | "applied_with_conflicts" | "abandoned";

export interface FixLineSnapshot {
  id: string;
  fix_number: number;
  display_number: number;
  created_at: string;
}

export interface FixLineChecklistItem {
  annotation_id: string;
  path: string;
  start_line: number;
  end_line: number;
  body: string;
  state: "open" | "touched" | "resolved";
}

export interface FixLine {
  id: string;
  review_id: string;
  snapshot_revision_id: string;
  snapshot_number: number;
  child_review_id: string;
  status: FixLineStatus;
  worktree_path: string;
  branch: string;
  base_commit: string;
  worktree_missing: boolean;
  applied_revision_id: string;
  applied_files: string[];
  applied_at: string;
  snapshots: FixLineSnapshot[];
  checklist: FixLineChecklistItem[];
  unreviewed: number;
}

export interface FixApplyPlan {
  fix_line_id: string;
  revision_id: string;
  fix_number: number;
  files: Array<{ path: string; outcome: "write" | "delete" | "conflict" | "unchanged"; reason: string }>;
  conflicts: string[];
  warnings: string[];
}

export interface FixLineList {
  fix_lines: FixLine[];
  fix_of: FixLine | null;
}

export interface FixLineCloseResult {
  closed: boolean;
  dirty_files: string[];
  fix_line: FixLine;
}

export interface FixLineApplyResult {
  applied: boolean;
  fix_line: FixLine;
  plan: FixApplyPlan;
}

export interface ReviewRevisionCompareFile {
  path: string;
  status: string;
  added_lines: number;
  removed_lines: number;
  patch: string;
}

export interface ReviewRevisionCompareUnit {
  unit_key: string;
  path: string;
  kind: string;
  symbol: string;
  status: string;
  from_state: string;
  to_state: string;
  from_start_line: number;
  to_start_line: number;
}

export interface ReviewRevisionComparison {
  review_id: string;
  reviewer_id: string;
  from_revision: RevisionInfo;
  to_revision: RevisionInfo;
  suggested_from_revision_number: number;
  revision_state: ReviewRevisionState;
  annotations: Annotation[];
  annotation_groups: {
    originating: string[];
    inherited: string[];
    addressed_by_target: string[];
  };
  summary: { added: number; changed: number; removed: number; unchanged: number };
  files: Array<{
    path: string;
    status: string;
    added: number;
    changed: number;
    removed: number;
    reviewed_before: number;
    reviewed_after: number;
    changed_since_review_after: number;
    needs_changes_after: number;
  }>;
  code_available: boolean;
  code_unavailable_reason: string;
  code_summary: { files: number; added_lines: number; removed_lines: number };
  code_files: ReviewRevisionCompareFile[];
  units: ReviewRevisionCompareUnit[];
}

export interface CompareSourceInfo {
  spec: string;
  label: string;
  kind: string;
}

export interface CompareFile {
  path: string;
  old_path: string;
  status: string;
  additions: number;
  deletions: number;
  patch: string;
  renderable: boolean;
  refusal: string;
  detail: string;
}

export interface SourceComparison {
  from: CompareSourceInfo;
  to: CompareSourceInfo;
  summary: { files: number; additions: number; deletions: number };
  files: CompareFile[];
}

export type ReviewActorType = "human" | "agent" | "mixed" | "unknown";

export interface ReviewActivityEvent {
  id: string;
  review_id: string;
  revision_id: string;
  kind: string;
  actor_id: string;
  actor_type: ReviewActorType;
  subject_type: string;
  subject_id: string;
  summary: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface ReviewMarkEvent {
  id: string;
  review_id: string;
  reviewer_id: string;
  unit_key: string;
  revision_id: string;
  reviewed_revision_id: string;
  event_kind: "judgment" | "reconciled" | "discarded" | "migrated";
  from_state: MarkState | "";
  to_state: MarkState | "";
  content_fingerprint: string;
  previous_unit_key: string;
  actor_type: ReviewActorType;
  note: string;
  reason: string;
  created_at: string;
}

export interface AnnotationVersion {
  id: string;
  annotation_id: string;
  review_id: string;
  revision_id: string;
  version_number: number;
  body: string;
  kind: AnnotationKind;
  state: AnnotationState;
  author_response: "none" | "addressed";
  author_response_source_id: string;
  author_response_at: string;
  resolved_revision_id: string;
  changed_by: string;
  changed_by_actor: ReviewActorType;
  change_kind: "created" | "edited" | "state_changed" | "author_response" | "thread_reply" | "finding_disposition";
  finding_disposition?: FindingDisposition;
  finding_disposition_reason?: FindingDismissalReason;
  finding_disposition_revision_id?: string;
  finding_disposition_reviewer_id?: string;
  finding_disposition_evidence?: string[];
  created_at: string;
}

export interface SourceState {
  /** SSE confirms a matching observation after source has been quiet. */
  settled?: boolean;
  supported: boolean;
  changed: boolean;
  fingerprint: string;
  path_count: number;
  paths: string[];
  reason: string;
}

/** One left-pane row. `reasons` is never empty — the server guarantees it. */
export interface AttentionRow {
  unit_key: string;
  path: string;
  group: GroupKey;
  state: MarkState;
  changed_since_mark: boolean;
  attention_rank: number;
  category: string;
  reasons: string[];
  file_status: string;
  status_column: string;
  additions: number;
  deletions: number;
  is_binary: boolean;
  language: string;
  renderable: boolean;
}

export interface AttentionGroup {
  key: GroupKey;
  label: string;
  rows: AttentionRow[];
}

export type ReviewLens = "attention" | "intent" | "dependency" | "commits";

export interface ReviewChapterTarget {
  target_id: string;
  path: string;
  label: string;
  symbol: string;
  kind: "symbol" | "hunk" | "file" | string;
  start_line: number;
  end_line: number;
  state: MarkState;
  changed_since_mark: boolean;
  attention_rank: number;
}

export interface ReviewChapter {
  key: string;
  label: string;
  reason: string;
  explanation?: string;
  rows: AttentionRow[];
  targets?: ReviewChapterTarget[];
  target_count?: number;
  progress?: {
    target_count: number;
    reviewed: number;
    changed_since_review: number;
    needs_changes: number;
    unreviewed: number;
    unknown: number;
  };
  file_count: number;
  attention_count: number;
  reviewed_count: number;
  changed_count: number;
  min_attention_rank: number;
  depends_on: string[];
}

export interface ReviewChapterLenses {
  intent: ReviewChapter[];
  dependency: ReviewChapter[];
  commits: ReviewChapter[];
}

export interface DegradedNote {
  name: string;
  note: string;
}
/** Where a typed execution result was verified, and what that environment could not verify. */
export interface ExecutionVerification {
  executor_class: "author_machine" | "customer_worker" | "managed_sandbox";
  location: string;
  image_digest: string;
  network: string;
  statement: string;
  not_verified: { name: string; reason: string }[];
}
export interface EvidenceRow {
  name: string;
  status: "PASS" | "FAIL" | "NOT_RUN" | "UNKNOWN";
  detail: string;
  source: string;
  scope?: "review" | "file";
  execution?: ExecutionVerification | null;
}

export type ReviewEvidenceKind =
  | "screenshot"
  | "image"
  | "video"
  | "playwright_trace"
  | "document"
  | "live_preview"
  | "verification"
  | "sbom"
  | "provenance"
  | "vulnerability_scan"
  | "waiver"
  | "intent"
  | "premerge_check"
  | "deployment";
export type ReviewEvidenceSource = "human" | "agent" | "test" | "ci" | "external";

/** Outcome/verification evidence bound to one exact ReviewRevision. */
export interface ReviewEvidence {
  id: string;
  review_id: string;
  revision_id: string;
  revision_number: number | null;
  kind: ReviewEvidenceKind;
  title: string;
  path: string;
  url: string;
  content_hash: string;
  mime_type: string;
  bytes: number;
  source: ReviewEvidenceSource;
  source_ref: string;
  verification_status: "" | "PASS" | "FAIL" | "NOT_RUN" | "UNKNOWN";
  detail: string;
  created_at: string;
  attestation_class?:
    | ""
    | "provider_ci"
    | "lemoncrow_verification"
    | "trusted_security_feed"
    | "signed_attestation"
    | "human_observation"
    | "human_waiver";
  stable_key?: string;
  provider?: string;
  integration_id?: string;
  provider_revision?: string;
  branch?: string;
  environment?: string;
  attestation_scope?: string;
  attested_by?: string;
  remote_identity?: string;
  observed_at?: string;
  waived_requirement_key?: string;
  execution_intent?: string;
  status: "current" | "stale";
  content_url: string;
}

export interface ReviewEvidenceList {
  revision_id: string;
  evidence: ReviewEvidence[];
}

export interface ReviewEvidenceResult {
  evidence: ReviewEvidence;
}

export interface ReviewVerificationCheck {
  name: string;
  status: "PASS" | "FAIL" | "NOT_RUN" | "UNKNOWN";
  /** `session`: recorded by the authoring session, not bound to this Snapshot; never counted. */
  state: "current" | "stale" | "session";
  snapshot_id: string;
  snapshot_number: number | null;
  source: string;
  source_ref: string;
  lane: string;
  /** Typed identity of the row; the name is presentation only. */
  check_key?: string;
  stable_key?: string;
  attestation_class?: string;
  authority?: string;
  path: string;
  detail: string;
  observed_at: string;
  artifact: { evidence_id: string; kind: string; digest: string; mime_type: string; url: string } | null;
  /** Browser rows only: repository policy says this applicable scenario gates landing. */
  required?: boolean;
  browser?: ReviewBrowserCheck;
  /** A human may waive this blocking check on the latest Snapshot. */
  waivable?: boolean;
  /** A human's reasoned override on this exact Snapshot; the status stays as observed. */
  waiver?: ReviewVerificationWaiver;
}

export interface ReviewVerificationWaiver {
  evidence_id: string;
  reason: string;
  actor: string;
  snapshot_id: string;
  observed_at: string;
}

export interface ReviewVerificationWaiverResult {
  verification: ReviewVerificationProjection;
  evidence?: ReviewEvidence;
}

export interface ReviewBrowserArtifact {
  evidence_id: string;
  digest: string;
  mime_type: string;
  bytes: number;
  observed_at: string;
  snapshot_id: string;
  snapshot_number: number | null;
}

/** One frozen browser run: what the browser actually observed on one Snapshot side. */
export interface ReviewBrowserRun {
  evidence_id: string;
  record_digest: string;
  snapshot_id: string;
  snapshot_number: number | null;
  side: "new" | "old";
  status: "PASS" | "FAIL" | "NOT_RUN" | "UNKNOWN";
  status_reasons: string[];
  observed_at: string;
  source: string;
  provider: string;
  run_ref: string;
  target_url: string;
  viewport: { width?: number; height?: number; device?: string; device_scale_factor?: number | null };
  environment: Record<string, string>;
  completed: boolean;
  navigation: { status?: number | null; ok?: boolean | null; final_url?: string };
  assertions: Array<{ name: string; status: string; expected: string; actual: string; detail: string }>;
  console: Array<{ level: string; text: string; location: string }>;
  runtime_errors: Array<{ message: string; stack: string }>;
  visual: { diff_ratio: number | null; threshold: number | null; threshold_source: string };
  observations: string[];
  artifacts: Partial<Record<"screenshot" | "trace" | "video" | "diff", ReviewBrowserArtifact>>;
  started_at: string;
  finished_at: string;
  /** Baseline only: "base" = pre-change side of the same Snapshot; "previous" = an earlier Snapshot. */
  relation?: "base" | "previous";
}

export interface ReviewBrowserCheck {
  scenario: string;
  route: string;
  configured: boolean;
  applicability: string;
  run: ReviewBrowserRun | null;
  baseline: ReviewBrowserRun | null;
}

export interface ReviewBrowserSummary {
  configured: number;
  applicable: number;
  required: number;
  pass: number;
  fail: number;
  not_run: number;
  unknown: number;
  stale: number;
  policy_source: string;
  policy_error: string;
  web_routes: string[] | null;
}

export interface ReviewVerificationArtifact {
  id: string;
  kind: string;
  title: string;
  state: "current" | "stale";
  snapshot_id: string;
  snapshot_number: number | null;
  digest: string;
  source: string;
  source_ref: string;
  observed_at: string;
  verification_status: "" | "PASS" | "FAIL" | "NOT_RUN" | "UNKNOWN";
  artifact_digest: string;
  standard: string;
  format_version: string;
  observation_source: string;
}

export interface ReviewVerificationProjection {
  review_id: string;
  snapshot_id: string;
  snapshot_number: number;
  checks: ReviewVerificationCheck[];
  summary: { pass: number; fail: number; not_run: number; unknown: number; stale: number };
  artifacts: ReviewVerificationArtifact[];
  browser?: ReviewBrowserSummary;
}

export interface ReviewDependencyChange {
  ecosystem: string;
  /** Package root directory for nested manifests; "" at the repository root. */
  package_root?: string;
  identity: string;
  name: string;
  direct: boolean;
  scope: string;
  changes: string[];
  before_requirement: string;
  after_requirement: string;
  before_version: string;
  after_version: string;
  before_source: string;
  after_source: string;
  before_integrity: string;
  after_integrity: string;
  license: string;
  license_source: string;
  manifest: string;
  lockfile: string;
  dependency_paths: string[][];
  transitive_changes: number;
  artifacts: Array<{ artifact_digest: string; version: string; license: string; purl: string; sbom_evidence_id: string; standard: string }>;
  vulnerabilities: Array<{ advisory: string; severity: string; artifact_digest: string; source: string; observed_at: string; evidence_id: string }>;
  target_unit_key: string;
  target_id: string;
}

export interface ReviewDependencyProjection {
  review_id: string;
  snapshot_id: string;
  snapshot_number: number;
  supported: boolean;
  lockfiles: Array<{ path: string; manifest: string; ecosystem: string; semantic_changes: number; direct_changes: number; transitive_changes: number }>;
  changes: ReviewDependencyChange[];
  summary: { semantic_changes: number; direct_changes: number; transitive_changes: number; added: number; removed: number; version_changed: number; source_changed: number; integrity_changed: number };
  supply_chain: {
    sboms: Array<Record<string, string | number>>;
    provenance: Array<Record<string, string | number>>;
    vulnerability_scans: Array<Record<string, string | number>>;
  };
  degraded: string[];
}

export type ReviewJournalKind =
  | "intent"
  | "plan"
  | "decision"
  | "fact"
  | "question"
  | "blocker"
  | "edit"
  | "command"
  | "validation"
  | "message"
  | "tool";

export interface ReviewJournalEntry {
  id: string;
  kind: ReviewJournalKind;
  at: string;
  summary: string;
  detail: string;
  path: string;
  status: string;
  source: string;
  host: string;
  session_id: string;
}

export interface ReviewJournal {
  available: boolean;
  reason: string;
  review_id: string;
  revision_id: string;
  revision_number: number;
  session_id: string;
  host: string;
  entries: ReviewJournalEntry[];
}

/** Server-owned Development projection; identical shape for event and legacy sources. */
export type DevelopmentRole = "author" | "verifier" | "investigator";

export interface DevelopmentContributor {
  key: string;
  host: string;
  session_id: string;
  model: string;
  purpose: string;
  parent_session_id: string;
  role: DevelopmentRole;
  paths: string[];
  path_count: number;
  edit_count: number;
  checks: { pass: number; fail: number; pending: number };
  first_at: string;
  last_at: string;
  status: string;
  task: string;
  intent: string;
  latest_message: string;
  active: boolean;
  evidence: "events" | "attribution";
}

export interface DevelopmentBrief {
  id: string;
  sequence: number;
  kind: string;
  summary: string;
  detail: string;
  status: string;
  path: string;
  host: string;
  session_id: string;
  role: string;
  at: string;
  causation_id: string;
  annotation_id: string;
}

export interface DevelopmentCheck {
  name: string;
  summary: string;
  kind: string;
  status: "pass" | "fail" | "pending";
  detail: string;
  host: string;
  session_id: string;
  at: string;
  sequence: number;
}

export interface DevelopmentChangedPath {
  path: string;
  status: string;
  sessions: string[];
  edits: number;
  last_at: string;
  in_snapshot: boolean | null;
}

export interface DevelopmentFeedbackLink {
  causation_id: string;
  annotation_ids: string[];
  sessions: string[];
  summary: string;
  handoff_status: string;
  edit_count: number;
  paths: string[];
  checks: { pass: number; fail: number };
  first_at: string;
  last_at: string;
  included_in_revision: number | null;
}

export interface DevelopmentProjection {
  scope: "snapshot" | "since" | "live";
  source: "events" | "legacy_journal" | "attribution" | "none";
  available: boolean;
  headline: string;
  /** Server-written account of this projection, one sentence per fact. */
  narrative?: string[];
  intent: string;
  plan: DevelopmentBrief | null;
  contributors: DevelopmentContributor[];
  counts: { authors: number; verifiers: number; investigators: number; files: number; decisions: number; open_items: number; outside_workspace?: number };
  decisions: DevelopmentBrief[];
  open_items: DevelopmentBrief[];
  verification: { pass: number; fail: number; pending: number; checks: DevelopmentCheck[] };
  changed_paths: DevelopmentChangedPath[];
  unattributed_paths: string[];
  feedback: DevelopmentFeedbackLink[];
  /** Consequential agent actions outside the repository, newest first (Phase 9R item 17). */
  outside_workspace?: DevelopmentOutsideAction[];
  /** Human decisions on risky agent actions recorded in this projection, newest first (P12-AGENT). */
  approval_events?: DevelopmentApprovalEvent[];
  latest_event: DevelopmentBrief | null;
  event_count: number;
  cursor: number;
  revision_id?: string;
  revision_number?: number;
  watermark?: number | null;
  newer_paths?: string[];
  active?: boolean;
}

export interface DevelopmentOutsideAction {
  id: string;
  /** secret_access | git_push | cloud | database | outside_write | package_install | network */
  category: string;
  summary: string;
  detail: string;
  host: string;
  session_id: string;
  role: string;
  at: string;
  sequence: number;
  /** Exact identity of the observed action; a pre-approval covers only this. */
  action_digest?: string;
  /** `run_ledger` when LemonCrow observed it; self-reported actions are never covered. */
  source?: string;
  /** Whether a human pre-approved exactly this action; `null` when none was required. */
  approval?: AgentActionApproval;
}

export interface DevelopmentApprovalEvent {
  id: string;
  /** approved | denied */
  status: string;
  summary: string;
  detail: string;
  host: string;
  session_id: string;
  at: string;
  sequence: number;
  action_digest: string;
}

export interface ReviewDevelopment {
  review_id: string;
  revision_id: string;
  revision_number: number;
  historical: boolean;
  live: boolean;
  cursor: string;
  snapshot: DevelopmentProjection;
  /** Strictly newer than the Snapshot's watermark; never merged into `snapshot`. */
  since: DevelopmentProjection | null;
  reason: string;
  /** Approval requests for risky agent actions on this Change, newest first (P12-AGENT). */
  agent_approvals?: AgentApproval[];
}

export type DevelopmentTimelineView = "all" | "authors" | "verification" | "decisions" | "changes";
export type DevelopmentTimelineScope = "snapshot" | "since" | "all";

export interface DevelopmentEvent {
  id: string;
  sequence: number;
  kind: string;
  at: string;
  host: string;
  session_id: string;
  role: string;
  path: string;
  symbol: string;
  target_unit_key: string;
  summary: string;
  detail: string;
  status: string;
  source: string;
  causation_id: string;
  parent_event_id: string;
  annotation_id: string;
  task_id: string;
}

export interface DevelopmentTimelinePage {
  review_id: string;
  revision_id: string;
  revision_number: number;
  scope: DevelopmentTimelineScope;
  view: DevelopmentTimelineView;
  source: string;
  events: DevelopmentEvent[];
  next_before: number | null;
}

export interface ProvenanceInfo {
  status: string;
  host: string;
  model: string;
  session_id: string;
  task: string;
  certainty: string;
  match_confidence: number;
  match_reason: string;
  commands_run: string[];
  subagents: unknown[];
  /** False when this host records no reads at all. */
  reads_recorded: boolean;
  /** `null` means "reads not recorded", never "the agent skipped it". */
  inspected: boolean | null;
  uninspected_impacted: string[];
}

export interface ImpactSite {
  kind: string;
  path: string;
  old: string;
  new: string | null;
  snippet: string;
  in_patch: boolean;
  inspected_by_agent: boolean | null;
  source_path: string;
  uncertainty: string;
}

export interface SymbolInfo {
  symbol_name: string;
  qualified_name: string;
  kind: string;
  change: string;
  start_line: number;
  end_line: number;
  caller_count: number;
  centrality_rank: number | null;
  source: string;
}

/** One unit named the way both surfaces name it, plus its key for lookups. */
export interface UnitRef {
  unit_key: string;
  label: string;
}

/**
 * A verdict that was destroyed reaching this revision.
 *
 * The reviewer recorded it and the tool threw it away — correctly, the unit it
 * attested to is not in the review any more, but that is a thing done *to* the
 * reviewer's work and the browser used to report it as a bare `removed: 1`
 * alongside four other counts. It is a property of the *revision*, not of the
 * request that noticed it: reading it off the refresh response alone named it
 * for the first click and for nobody after, so a second *Re-read tree* or a
 * plain reload silently deleted an approval all over again.
 */
export interface DiscardedVerdict {
  unit_key: string;
  kind: string;
  state: MarkState;
  label: string;
  /** Why it went, in the server's words. Never re-phrased here. */
  reason: string;
}

/** What one *Re-read tree* actually did to this reviewer's position. */
export interface RevisionTargetRef {
  target_id: string;
  unit_key: string;
  kind: "symbol" | "hunk" | "file" | "dependency" | "document_section";
  path: string;
  label: string;
  symbol: string;
  start_line: number;
  state: MarkState;
  annotation_counts: { open: number; orphaned: number; addressed_needs_rereview: number };
}

export interface RevisionTargetDelta {
  preserved: RevisionTargetRef[];
  reopened: RevisionTargetRef[];
  added: RevisionTargetRef[];
  removed: RevisionTargetRef[];
  active: RevisionTargetRef[];
}

export interface RefreshInfo {
  /** False when the tree fingerprinted back to a revision already on file. */
  created: boolean;
  revision_number: number;
  /** The revision this reviewer last recorded a verdict against; 0 if none. */
  previous_revision_number: number;
  reopened: number;
  carried: number;
  /** Judgments kept across base-only movement (their unit bytes moved only because the base did). */
  rebased?: number;
  added: number;
  /** Units that left the review, named — they are gone from every pane. */
  removed: UnitRef[];
  /**
   * The revision's destroyed verdicts, repeated here for a consumer reading
   * only the refresh result. The workspace renders {@link ReviewOverview.discarded}
   * instead, because that one is also there on a reload.
   */
  discarded: DiscardedVerdict[];
  notes: string[];
  target_delta?: RevisionTargetDelta;
  /** File cache validity relative to the revision the Reader showed before refresh. */
  changed_paths?: string[];
  added_paths?: string[];
  removed_paths?: string[];
  renamed_paths?: { old_path: string; path: string }[];
  preserved_paths?: string[];
  /** Reviewer-authored source proposals whose exact applied source was captured by this revision. */
  applied_proposals?: ReviewChangeProposal[];
}
export interface ReviewBriefItem {
  path: string;
  rank: number;
  reason: string;
  reason_code?: string;
  target_id?: string;
}

export interface ReviewRuleInfo {
  ref: string;
  stable_key: string;
  instruction: string;
  paths: string[];
  origin: string;
  trust_class: string;
  source_identity: string;
  source_location: string;
  digest: string;
  authoritative: boolean;
}

export interface ReviewContentAccessRuleInfo {
  mode: "external_allowed" | "customer_local_only" | "ai_denied";
  paths: string[];
  origin: string;
  trust_class: string;
  source_identity: string;
  source_location: string;
  digest: string;
  authoritative: boolean;
}

export interface ReviewFuturePolicyChange {
  path: string;
  change: "added" | "changed" | "removed";
  category: "review_policy" | "review_rules" | "owner_routing" | "instruction_context" | string;
  summary: string;
  effects: string[];
  current_authority: false;
  future_policy: true;
}

export interface ReviewRuleSuggestion {
  id: string;
  repository_scope: string;
  candidate_statement: string;
  paths: string[];
  supporting_feedback_refs: string[];
  supporting_count: number;
  state: "suggested" | "dismissed";
  active: false;
  created_at: string;
  updated_at: string;
}

/** The reviewable patch that would adopt a rule suggestion; never applied by the product. */
export interface ReviewRuleAdoptionProposal {
  suggestion_id: string;
  state: "proposed";
  effective: false;
  stable_key: string;
  instruction: string;
  paths: string[];
  target_path: string;
  change: "added" | "changed";
  base_sha256: string;
  patch: string;
  proposed_content: string;
  supporting_feedback_refs: string[];
  how_to_land: string;
}

export interface ReviewPolicyProjection {
  authority: {
    base_state: "present" | "absent" | "provisional" | string;
    rules: ReviewRuleInfo[];
    ai_content_access: ReviewContentAccessRuleInfo[];
    rule_documents: string[];
    errors: string[];
  };
  future_changes: ReviewFuturePolicyChange[];
  suggestions: ReviewRuleSuggestion[];
}

export interface ReviewBrief {
  summary: string;
  themes: string[];
  major_changes?: {
    key: string;
    label: string;
    explanation?: string;
    file_count: number;
    target_count: number;
    attention_count: number;
    reviewed?: number;
    changed_since_review?: number;
    needs_changes?: number;
    pending?: number;
    first_path: string;
  }[];
  review_first: ReviewBriefItem[];
  verification: { pass: number; fail: number; not_run: number; unknown: number };
  annotations: { human: number; author: number; lemoncrow: number; ai_review: number };
  artifacts: { current: number; stale: number };
}

export interface ReviewAttributionInfo {
  scope: "revision" | "file" | string;
  scope_key: string;
  host: string;
  session_id: string;
  role: "primary" | "contributor" | string;
  model: string;
  certainty: string;
  method: string;
}
export interface ReviewRelationshipInfo {
  id: string;
  kind: "scm" | "change" | "work_item" | "scm_requirement" | string;
  relation: string;
  provider: string;
  external_id: string;
  target_change_ref: string;
  url: string;
  title: string;
  status: string;
  source: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface ReviewChangeContext {
  ref: string;
  scm: ReviewRelationshipInfo[];
  relations: ReviewRelationshipInfo[];
  work_items: ReviewRelationshipInfo[];
  scm_requirements: ReviewRelationshipInfo[];
  landing?: ReviewRelationshipInfo[];
  /** Read-only stack awareness (Phase 9 item 24); null when this Change is not stacked. */
  stack?: ReviewStackInfo | null;
  /** Multi-repository change set (Phase 9R item 42): linked members are counted; the set is `/change-set`. */
  change_set?: { available: boolean; linked: boolean; links: number };
}

export interface ReviewStackMember {
  position: "lower" | "upper";
  distance: number;
  change_ref: string;
  review_id: string;
  title: string;
  provider: string;
  external_id: string;
  url: string;
  head_ref: string;
  head_sha: string;
  status: string;
  readiness: { state: "landed" | "closed" | "changes_requested" | "approved" | "in_review" | "unknown"; label: string; detail: string };
  /** Lower layers: `exact` (this diff starts at its head), `moved`, or `included` (its commits are in this diff). */
  relation: "exact" | "moved" | "included" | "";
  owned_paths: string[];
}

export interface ReviewStackInfo {
  version: number;
  source: "provider" | "local_ancestry";
  /** Always read-only: LemonCrow never creates, rebases or retargets a stacked branch. */
  mutation: "read_only";
  layer: number;
  depth: number;
  base: { kind: "trunk" | "lower_change" | "includes_lower_changes"; ref: string; sha: string; change_ref: string };
  head_ref: string;
  lower: ReviewStackMember[];
  upper: ReviewStackMember[];
  note: string;
  complete: boolean;
}

/** Evidence a readiness condition relies on, with its freshness at evaluation time. */
export interface ReviewReadinessEvidenceRef {
  evidence_id: string;
  /** `external_observation` expires (advisory scans, security feeds); the others stay current for their Snapshot. */
  freshness_class: "" | "snapshot_intrinsic" | "execution_bound" | "external_observation";
  state: "" | "current" | "expired" | "unknown";
  observed_at: string;
  source_revision: string;
  expires_at: string;
  reason: string;
}

export interface ReviewLandingCondition {
  id: string;
  label: string;
  /**
   * `waived`: a human override that does not block, never shown as `pass`.
   * `unknown`: cannot be decided now (e.g. an expired external observation); blocks like `pending`.
   */
  state: "pass" | "fail" | "pending" | "unknown" | "not_applicable" | "waived";
  detail: string;
  owner: "lemoncrow" | "scm";
  category: string;
  url: string;
  reason?: string;
  evidence_refs?: ReviewReadinessEvidenceRef[];
  /** `change-set:*` conditions: the whole set's state (`ready`, `blocked`, `needs_decision`, `incomplete`). */
  set_state?: string;
}

export interface ReviewLandingState {
  supported: boolean;
  provider: string;
  state: string;
  armed: boolean;
  semantic_stale: boolean;
  revision_id: string;
  tree_fingerprint: string;
  conditions_hash: string;
  detail: string;
  url: string;
  mechanism: string;
  /** Policy generation the intent was armed (or last revalidated) under. */
  policy_generation?: string;
  /** Armed under a generation other than the current one: a stale decision awaiting revalidation. */
  policy_stale?: boolean;
  readiness_valid_until?: string;
  /** `pending` while a withdrawal (cancel/discard) is owed to the provider and not yet confirmed. */
  provider_cancel?: string;
  /** The provider's own auto-merge / merge-queue state: shown, never owned, by LemonCrow. */
  scm?: ReviewLandingScmState;
  /** `break_glass` when the landing was enabled by an authorized responder bypassing named blockers. */
  basis?: string;
  break_glass_id?: string;
  bypassed?: string[];
  auto_approval?: ReviewAutoApprovalState;
  /** Break-glass records of this Change (newest last), each with its follow-up review state. */
  break_glass?: ReviewBreakGlassRecord[];
}

export interface ReviewLandingScmState {
  managed_by: string;
  mechanism: string;
  /** `enabled` / `disabled` as the provider reports it; empty when unknown. */
  auto_merge: string;
  /** The provider merge queue entry (`queued`, `checks_requested`, `merged`, `dequeued`, …); empty when none. */
  queue_state: string;
}

/** The organization-policy approval of this Change (deterministic gates; never a model decision). */
export interface ReviewAutoApprovalState {
  /** `none`, `approving` (delivery owed), `approved`, `withdrawn`, `stale` (an earlier Snapshot's) or `unknown`. */
  state: string;
  revision_id?: string;
  decision_digest?: string;
  policy_generation?: string;
  decided_at?: string;
  delivery?: string;
  provider_cancel?: string;
  detail?: string;
  /** The latest decision on this Snapshot; `null` when none was made. */
  eligible?: boolean | null;
  /** Labels of the gates that did not pass. */
  blocking?: string[];
  evaluated_at?: string;
}

export interface ReviewBreakGlassRecord {
  id: string;
  revision_id: string;
  actor_id: string;
  reason: string;
  incident_url: string;
  bypassed: { id: string; label: string; state: string; reason: string }[];
  created_at: string;
  policy_generation: string;
  follow_up: { state: "open" | "overdue" | "completed"; due_at: string; completed_at: string; completed_by: string };
}

/** Why a Change cannot land yet, in the one vocabulary the Inbox and the SCM status share. */
export interface ReviewBlockingReason {
  id: string;
  owner: "lemoncrow" | "scm";
  state: string;
  action: string;
}

/** Durable identity of the effective Review policy readiness was evaluated against. */
export interface ReviewPolicyGeneration {
  digest: string;
  short: string;
  /** `provisional` when a policy layer (e.g. the trusted base) could not be read. */
  authority: "final" | "provisional";
  layers: { layer: string; source: string; digest: string; state: "final" | "provisional" }[];
}

/** One concrete citation of a pre-merge check result (path, intent, description, fact, verification, rule). */
export interface ReviewPremergeEvidence {
  kind: string;
  ref: string;
  detail: string;
}

/** An authorized human waiver: bound to one policy generation and scope; it never turns a check into PASS. */
export interface ReviewPremergeWaiver {
  evidence_id: string;
  revision_id: string;
  snapshot_number: number | null;
  requirement_key: string;
  actor: string;
  reason: string;
  scope: "snapshot" | "review";
  expires_at: string;
  policy_generation: string;
  policy_generation_short: string;
  paths: string[];
  created_at: string;
}

export interface ReviewPremergeCheck {
  id: string;
  requirement_key: string;
  title: string;
  /** `warning`: a trial that never blocks; `would_block` says what enforcing it would do. */
  mode: "error" | "warning";
  status: "PASS" | "FAIL" | "UNKNOWN" | "NOT_RUN";
  state: "not_applicable" | "pass" | "waived" | "fail" | "unknown" | "pending";
  applicable: boolean;
  detail: string;
  evidence: ReviewPremergeEvidence[];
  evaluator: string;
  source: string;
  layer: string;
  definition_digest: string;
  blocking: boolean;
  would_block: boolean;
  waivable: boolean;
  waiver: ReviewPremergeWaiver | null;
  /** Why an earlier waiver no longer applies (expired, policy changed, out of scope). */
  waiver_note: string;
  model: { status: string; explanation: string; evidence: { kind: string; ref: string; quote?: string }[]; reason: string; model: string } | null;
  /** Why a model-evaluated check is NOT_RUN: `limited_by_budget`, `skipped`, `not_planned`, `ai_unavailable`, `stale`, `pending`. */
  not_run_reason?: string;
}

/** One immutable capture of a linked intent source (or the PR description). */
export interface ReviewIntentCapture {
  evidence_id: string;
  revision_id: string;
  snapshot_number: number | null;
  ref: string;
  kind: string;
  origin: string;
  role: string;
  title: string;
  digest: string;
  state: string;
  truncated: boolean;
  source_revision: string;
  fetched_at: string;
  url: string;
  note: string;
  actor: string;
  created_at: string;
}

export interface ReviewIntentBinding {
  ref: string;
  kind: string;
  origin: string;
  bound: ReviewIntentCapture;
  /** Newer capture that differs from the bound one; never silently re-bound. */
  latest: ReviewIntentCapture | null;
  changed: boolean;
  changed_note: string;
  /** The provider deleted the linked source; checks stay bound to the captured text. */
  deleted?: boolean;
}

/** The Snapshot's effective run-plan entry that schedules model-evaluated checks. */
export interface ReviewPremergeRunState {
  entry: string;
  /** A run-plan entry status, or `not_planned` when the plan does not select the entry. */
  status: string;
  reason: string;
  origin: string;
  depth: string;
  executor: string;
  plan_digest: string;
}

export interface ReviewPremergePolicyCheck {
  id: string;
  title: string;
  mode: "error" | "warning";
  source: string;
  layer: string;
  model_evaluated: boolean;
  waivable: boolean;
  waivable_by: string[];
}

/** Pre-merge checks on the readiness Snapshot (present only when a trusted policy configures them). */
export interface ReviewPremergeProjection {
  configured?: boolean;
  checks: ReviewPremergeCheck[];
  policy_errors: string[];
  policy_generation: string;
  evaluated_at: string;
  trial: { would_block: string[]; summary: string };
  snapshot_id: string;
  snapshot_number: number;
  policy: {
    checks: ReviewPremergePolicyCheck[];
    waivers: { allowed: string[]; max_hours: number };
    errors: string[];
    sources: string[];
  };
  intents: ReviewIntentBinding[];
  intent_changed: string[];
  description: {
    title: string;
    digest: string;
    evidence_id: string;
    revision_id: string;
    snapshot_number?: number | null;
    origin: string;
    truncated: boolean;
    body_chars: number;
  } | null;
  /** Why model-evaluated checks cannot run (they stay NOT_RUN, never PASS); empty when available. */
  ai_unavailable: string;
  /** How the effective run plan schedules model-evaluated checks (absent when none are configured). */
  run?: ReviewPremergeRunState | null;
}

export interface ReviewLandingReadiness {
  review_id: string;
  snapshot_id: string;
  snapshot_number: number;
  tree_fingerprint: string;
  source_fingerprint: string;
  conditions: ReviewLandingCondition[];
  ready: boolean;
  armable: boolean;
  blocker_count: number;
  local_blocker_count: number;
  conditions_hash: string;
  /** Every unsatisfied condition as a short action (LemonCrow-owned first), at most 20 rows. */
  blocking_reasons?: ReviewBlockingReason[];
  work_items: ReviewRelationshipInfo[];
  landing: ReviewLandingState;
  policy_generation?: ReviewPolicyGeneration;
  evaluated_at?: string;
  /** Earliest expiry of time-sensitive evidence a satisfied condition relies on; empty when none. */
  valid_until?: string;
  freshness_policy?: { external_max_age_seconds: number; source: string; error: string };
  premerge?: ReviewPremergeProjection;
}

export interface ReviewOverview {
  session: ReviewSessionInfo;
  /** Durable Change context; external systems remain authoritative for their own workflow state. */
  change?: ReviewChangeContext;
  /** Real local checkout path; empty unless the server holds a trusted workspace binding. */
  workspace_root?: string;
  revision: RevisionInfo;
  revision_count: number;
  historical?: boolean;
  latest_revision_number?: number;
  latest_revision?: RevisionInfo;
  last_reviewed_revision_number?: number | null;
  last_commented_revision_number?: number | null;
  default_base_revision_number?: number | null;
  default_target_revision_number?: number | null;
  historical_judgment_complete?: boolean;
  packet_available: boolean;
  stats: Record<string, number>;
  title: string;
  /** Compact orientation for large reviews; derived from deterministic Intent chapters. */
  change_story?: string[];
  change_story_more?: number;
  brief?: ReviewBrief | null;
  /** True only for the bounded large-review startup projection. */
  startup_compact?: boolean;
  /** Current authority, future-policy changes, and inert learned-rule suggestions. */
  policy?: ReviewPolicyProjection;
  provenance: ProvenanceInfo;
  attributions?: ReviewAttributionInfo[];
  evidence: EvidenceRow[];
  /** Observable author-session context frozen with this exact revision. */
  journal?: ReviewJournalEntry[];
  degraded: DegradedNote[];
  groups: AttentionGroup[];
  group_counts: Record<GroupKey, number>;
  /** Derived organization only. Files/marks remain the durable source of truth. */
  chapters?: ReviewChapterLenses;
  unit_count: number;
  target_count?: number;
  progress?: ReviewProgress;
  outline?: ReviewOutlineItem[];
  mark_count: number;
  discarded: DiscardedVerdict[];
  /** Present only on the response to a refresh: what that refresh changed. */
  refreshed?: RefreshInfo;
  /** Movement since this reviewer's last judged Snapshot; absent on history views. */
  movement?: ReviewMovementSummary | null;
}
export interface MarkdownPreview {
  kind: "markdown";
  old_content: string;
  new_content: string;
}

export interface CsvPreview {
  kind: "csv";
  old_content: string;
  new_content: string;
}

export interface WebPreview {
  kind: "web";
  framework: "next" | "astro" | "vite" | string;
  routes: string[];
  default_route: string;
  root?: string;
}

export interface MediaPreview {
  kind: "media";
  media_type: string;
  media_kind: "image" | "video" | "audio" | "pdf";
}

export type FilePreview = MarkdownPreview | CsvPreview | WebPreview | MediaPreview;

/** One file's patch plus everything the right pane says about it. */
export interface ReviewFileContents {
  path: string;
  revision_id: string;
  old_path: string;
  status: string;
  old_content: string | null;
  new_content: string | null;
}

export interface ReviewComparisonFileContents extends ReviewFileContents {
  from_revision_id: string;
  to_revision_id: string;
}

export interface FileDetail {
  path: string;
  /** Exact Review revision backing this projection. */
  revision_id?: string;
  old_path?: string;
  status?: string;
  language?: string;
  additions?: number;
  deletions?: number;
  patch: string;
  renderable: boolean;
  /** `""` when the file rendered; otherwise why it did not. */
  refusal: string;
  detail: string;
  /** Full, revision-pinned rich preview metadata where available. */
  preview?: FilePreview | null;
  impact?: ImpactSite[];
  symbols?: SymbolInfo[];
  provenance?: ProvenanceInfo;
  evidence?: EvidenceRow[];
  degraded: DegradedNote[];
  /** Semantic delta for a rename-only, mode-only, symlink or submodule change; null for an ordinary diff. */
  special_change?: ReviewSpecialChange | null;
}

/** Server projection over the packet entry (8F); never derived in the browser. */
export type ReviewSpecialChange =
  | { kind: "rename"; old_path: string; new_path: string; copied: boolean; similarity: number; mode: string }
  | {
    kind: "mode";
    old_path: string;
    new_path: string;
    old_mode: string;
    new_mode: string;
    executable: "added" | "removed" | "unchanged";
  }
  | {
    kind: "symlink";
    old_path: string;
    new_path: string;
    old_type: "symlink" | "file" | "absent";
    new_type: "symlink" | "file" | "absent";
    old_target: string;
    new_target: string;
    target_recorded: boolean;
    target_unchanged: boolean;
    resolved_target: string;
    escapes_repository: boolean;
    trust_area: string;
    warnings: string[];
  }
  | {
    kind: "submodule";
    old_path: string;
    new_path: string;
    change: "added" | "removed" | "updated";
    old_commit: string;
    new_commit: string;
    repository_url: string;
    commit_url: string;
    compare_url: string;
  }
  | { kind: "metadata"; old_path: string; new_path: string; old_mode: string; new_mode: string; modes_recorded: boolean };

export interface MarkResult {
  unit_key: string;
  requested: MarkState;
  recorded: MarkState;
  downgraded: boolean;
  note: string;
  /** R21 reader projection. Legacy groups remain until R26. */
  target?: ReviewTarget;
  progress?: ReviewProgress;
  outline_updates?: ReviewOutlineItem[];
  /** Every target's fresh queue placement: a verdict moves targets it did not judge. */
  priorities?: Record<string, ReviewTargetPriority>;
  outline?: ReviewOutlineItem[];
  groups: AttentionGroup[];
  group_counts: Record<GroupKey, number>;
}

export interface BulkUndoResult {
  undone: { unit_key: string; path: string }[];
  skipped: { unit_key: string; path: string; reason: string }[];
  progress?: ReviewProgress;
  outline_updates?: ReviewOutlineItem[];
  priorities?: Record<string, ReviewTargetPriority>;
  outline?: ReviewOutlineItem[];
  group_counts: Record<GroupKey, number>;
}

export interface BulkMarkResult {
  marked: { unit_key: string; path: string; event_id: string }[];
  skipped: { unit_key: string; path: string; reason: string }[];
  progress?: ReviewProgress;
  outline_updates?: ReviewOutlineItem[];
  priorities?: Record<string, ReviewTargetPriority>;
  outline?: ReviewOutlineItem[];
  group_counts: Record<GroupKey, number>;
}

/** Plan §5.5's four dispositions. There is no fifth, on purpose. */
export type AnnotationKind = "comment" | "request_change" | "suggestion" | "looks_good";

export type AnnotationState = "open" | "resolved" | "orphaned" | "obsolete";

export type ReviewChangeProposalState = "proposed" | "applied" | "conflicted" | "dismissed" | "superseded";

export interface ReviewChangeProposal {
  id: string;
  review_id: string;
  base_revision_id: string;
  base_revision_number: number;
  path: string;
  start_line: number;
  end_line: number;
  original_text: string;
  replacement_text: string;
  patch: string;
  state: ReviewChangeProposalState;
  target_unit_key: string;
  annotation_id: string;
  intent: string;
  conflict_reason: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  applied_at: string;
  result_revision_id: string;
  result_target_ids: string[];
  can_apply: boolean;
}

export interface ReviewChangeProposalList {
  revision_id: string;
  proposals: ReviewChangeProposal[];
  source_mutation_supported: boolean;
}

export interface ReviewProposalSelection {
  revision_id: string;
  path: string;
  start_line: number;
  end_line: number;
  side: "additions";
  text: string;
}

export interface ReviewChangeProposalResult {
  proposal: ReviewChangeProposal;
}

export interface ReviewChangeProposalApplyResult {
  proposal: ReviewChangeProposal;
  source_state: SourceState;
}

export type AnnotationSource = "human" | "author" | "lemoncrow" | "ai_review";
export type FindingDisposition = "" | "open" | "confirmed_by_reviewer" | "dismissed" | "fixed" | "superseded_stale";
export type FindingDismissalReason = "" | "incorrect" | "accepted_risk" | "duplicate" | "not_applicable";

export type ReviewDirectoryStatus = "all" | ReviewSessionStatus;

/** A Reviews-list row; in the Inbox view mode it also carries the per-viewer Inbox signals. */
export interface ReviewDirectoryRow extends Partial<InboxSignals> {
  id: string;
  ref: string;
  review_path: string;
  repo_id: string;
  title: string;
  auto_title?: string;
  title_is_custom?: boolean;
  source_ref: string;
  range_mode?: string;
  status: ReviewSessionStatus;
  revision_number: number;
  revision_id: string;
  progress?: ReviewProgress;
  updated_at: string;
  created_at: string;
  directory?: string;
  project?: string;
  project_root?: string;
  worktree?: string;
  worktree_root?: string;
  branch?: string;
  revision?: string;
  detached?: boolean;
  /** The inbox bucket that best says what this Review needs now. */
  primary_bucket?: string;
  reason?: string;
  returned_thread_count?: number;
  waiting_on_agent_count?: number;
  waiting_on_author_count?: number;
  /** Who a waiting handoff is with: "author", a host label, or "coding agent". */
  handoff_label?: string;
  /** An archived Review's provider cancellation still owed by its discard (until the provider confirms). */
  owed_cancellation?: { provider: string; landing_status: string; reason: string; detail: string; error: string } | null;
}
export interface ReviewDirectoryPage extends Partial<Omit<InboxViewPageMeta, "status" | "query" | "next_cursor">> {
  reviews: ReviewDirectoryRow[];
  status: ReviewDirectoryStatus | "none";
  query: string;
  next_cursor: string;
}

/* -- Review Inbox views (P9-INBOX): the view-mode contract of GET /v1/reviews?view=... ------------- */

export type InboxView =
  | "all"
  | "to_review"
  | "assigned_to_my_teams"
  | "authored_by_me"
  | "participated"
  | "waiting_on_me"
  | "ready"
  | "blocked";

export type InboxWorkflowState =
  | "needs_review"
  | "waiting_on_reviewer"
  | "waiting_on_author"
  | "waiting_on_checks"
  | "needs_decision"
  | "ready_to_merge"
  | "monitoring"
  | "closed";

/** Where the work stands; independent of priority, next-action owner and readiness. */
export interface InboxWorkflow {
  state: InboxWorkflowState;
  label: string;
  rule: string;
  explanation: string;
}

/** P9-PRIO triage: what to open first, never who acts next. */
export interface InboxPriority {
  level: "urgent" | "high" | "normal" | "low";
  computed: boolean;
  computed_level: string;
  criticality: string;
  evidence_state: string;
  overridden: boolean;
  revision_id: string;
}

export interface InboxReason {
  principal_id: string;
  kind: string;
  fact: string;
  since: string;
}

export interface InboxNextAction {
  owner_ids: string[];
  for_me: boolean;
  my_reasons: InboxReason[];
  reasons: InboxReason[];
}

/** The stored readiness record as the Inbox may state it; only `ready` can make a Review ready to merge. */
export interface InboxReadiness {
  state: "ready" | "not_ready" | "stale" | "unknown";
  reason: string;
  evaluated_at: string;
  valid_until: string;
  blocker_count: number;
  scm_blockers: number;
  pending_checks: number;
  failed_checks: number;
  scm_conditions: number;
  /** The exact blocking actions from the content-free readiness record (bounded; `reason` counts the rest). */
  blocking?: { id: string; owner: string; state: string; action: string }[];
}

/** Your review / team review / merge readiness: three axes, never folded into one status. */
export interface InboxAxes {
  your_review: { state: string; [key: string]: unknown };
  team_review: { state: string; waiting_on?: string[]; reason?: string; [key: string]: unknown };
  scm: {
    state: "ready" | "not_ready" | "unknown" | "not_connected";
    connected: boolean;
    provider: string;
    failed_checks: number;
    pending_checks: number;
    [key: string]: unknown;
  };
}

export interface InboxAttentionFactor {
  key: string;
  label: string;
  points: number;
  detail: string;
}

/** The viewer's explainable attention score; it orders the Inbox and changes nothing. */
export interface InboxAttention {
  score: number;
  waiting_since: string;
  factors: InboxAttentionFactor[];
}

/** The per-viewer fields every view-mode row carries (local and hosted). */
export interface InboxSignals {
  views: InboxView[];
  workflow: InboxWorkflow;
  priority: InboxPriority;
  next_action: InboxNextAction;
  readiness: InboxReadiness;
  axes: InboxAxes;
  attention: InboxAttention;
  teams: string[];
  team_requests: number;
}

export interface InboxFilterDefinition {
  view: InboxView;
  status: ReviewDirectoryStatus;
  repos: string[];
  teams: string[];
  workflow_states: InboxWorkflowState[];
  priorities: string[];
  query: string;
}

export interface SavedInboxFilter {
  id: string;
  name: string;
  definition: InboxFilterDefinition;
  created_at: string;
  updated_at: string;
}

export interface SavedInboxFilterList {
  filters: SavedInboxFilter[];
  limit: number;
  teams_supported?: boolean;
}

/** Page metadata of the view mode; `total` spans every page, `counts` every view. */
export interface InboxViewPageMeta {
  mode: "views";
  view: InboxView | "none";
  status: ReviewDirectoryStatus | "none";
  query: string;
  selection: Omit<InboxFilterDefinition, "view" | "status"> & {
    view: InboxView | "none";
    status: ReviewDirectoryStatus | "none";
    matches_nothing: boolean;
  };
  filter: SavedInboxFilter | null;
  counts: Record<InboxView, number>;
  total: number;
  limit: number;
  next_cursor: string;
  as_of: string;
  repositories: number;
  views: { id: InboxView; label: string }[];
  workflow_states: { id: InboxWorkflowState; label: string }[];
  teams_supported: boolean;
}

/** `@pierre/diffs`' name for a diff side. Ours is `old`/`new`; the edge translates. */
export type DiffSide = "additions" | "deletions";

/**
 * One comment as the server holds it.
 *
 * `start_line`/`end_line` are the server's *current* answer, recomputed by the
 * relocation ladder on every revision — they are a projection of the anchor,
 * never the anchor itself, and the browser never sends them back.
 *
 * `anchored` is the server's own verdict and not something the UI re-derives:
 * an orphan keeps the line it last held for display, so a client that inferred
 * "has a line number, therefore points somewhere" would draw it on a line that
 * no longer means anything.
 */
export interface Annotation {
  id: string;
  review_id: string;
  revision_id: string;
  parent_id: string;
  kind: AnnotationKind;
  state: AnnotationState;
  body: string;
  created_by: string;
  created_by_actor: string;
  source?: AnnotationSource;
  source_id?: string;
  agent_role?: string;
  agent_host?: string;
  agent_model?: string;
  parent_session_id?: string;
  source_message_id?: string;
  comment_scope?: "code" | "change";
  title?: string;
  evidence?: string[];
  /** Canonical rule provenance for AI findings; never source-code evidence. */
  rule_refs?: string[];
  /** Kept for run details only; the default finding UI does not display raw model confidence. */
  confidence?: number | null;
  finding_disposition?: FindingDisposition;
  finding_disposition_reason?: FindingDismissalReason;
  finding_disposition_revision_id?: string;
  finding_disposition_reviewer_id?: string;
  finding_disposition_evidence?: string[];
  author_response?: "none" | "addressed";
  author_response_source_id?: string;
  author_response_at?: string;
  origin_revision_number?: number | null;
  relation_to_revision?: "originating" | "inherited" | "future";
  /** Stacked Change: the lower layer that introduced the anchored code (feedback and fixes belong there). */
  owning_change?: { change_ref: string; review_id: string; title: string; reason: string };
  author_addressed_revision_number?: number | null;
  addressed_by_revision?: boolean;
  resolved_revision_number?: number | null;
  turn_owner_kind?: "none" | "author" | "reviewer";
  turn_owner_id?: string;
  is_human_judgment?: boolean;
  /**
   * Client-only: an AI finding the current finding threshold hides
   * (`projection.triage.hidden_finding_ids`). Lists fold it behind one
   * "N below threshold" disclosure; counts and readiness still include it.
   */
  below_threshold?: boolean;
  created_at: string;
  updated_at: string;
  anchor_method: string;
  /** The rung in plain language, from the server. Never render the raw method. */
  anchor_method_label: string;
  /**
   * True only for `identical_blob`: the file is byte-identical, so the line the
   * reviewer picked was never re-derived. Every other successful rung *searched*
   * for the text again, and a card that drew the two alike would lend this one's
   * credibility to a heuristic.
   */
  anchor_exact: boolean;
  /** Why the anchor is where it is, in a sentence. Empty only on a fresh one. */
  anchor_detail: string;
  anchored: boolean;
  path: string;
  side: string;
  start_line: number;
  end_line: number;
  file_level?: boolean;
  unit_key: string;
  /** The definition this comment sits in **now**. Empty when no rung found it. */
  symbol: string;
  /**
   * The definition it was *written* against, set only when nothing located it
   * since. Render it as "originally in", never as "in": an orphan's card that
   * names a symbol as though it still existed denies in one line what the
   * anchor badge above it asserts.
   */
  origin_symbol: string;
}

export interface FeedbackStatus {
  open_total: number;
  unpublished: number;
  published: number;
  in_flight: number;
  addressed: number;
  /** Present only for a Review opened from a GitHub pull request: which comments are on the PR. */
  github?: { unpublished: number; published: number; in_flight: number };
}

export interface AnnotationList {
  revision_id: string;
  revision_number?: number;
  annotations: Annotation[];
  counts: Record<string, number>;
  feedback?: FeedbackStatus;
  /** Current exact-session handoff capability for these comments. */
  delivery?: FeedbackDeliveryCapability;
}
export interface AnnotationResult {
  annotation: Annotation;
  /** Present when a root request-change also updated its ReviewTarget. */
  target?: ReviewTarget | null;
  progress?: ReviewProgress | null;
  outline_updates?: ReviewOutlineItem[];
  priorities?: Record<string, ReviewTargetPriority> | null;
  outline?: ReviewOutlineItem[] | null;
}

/** What the server will store, assembled from a `SelectedLineRange` plus a body. */
export interface AnnotationDraft {
  path: string;
  start_line: number;
  end_line: number;
  side: DiffSide;
  body: string;
  kind: AnnotationKind;
  file_level?: boolean;
  parent_id?: string;
  /** Explicit current ReviewTarget to block when this is a root request_change. */
  target_unit_key?: string;
  /** Default-on in the reader for a root request_change; ignored for ordinary comments/replies. */
  mark_target?: boolean;
}

export interface RelatedSource {
  path: string;
  language: string;
  start_line: number;
  end_line: number;
  focus_line: number;
  text: string;
}

export interface ReviewAskScope {
  target_unit_key: string;
  path: string;
  symbol: string;
  side: string;
  start_line: number;
  end_line: number;
}

/** One Project Knowledge citation: the evolution event, Review or committed source it came from. */
export interface ReviewProjectCitation {
  ref: string;
  kind: "generation" | "source" | "review" | "revision" | "merge" | "index" | string;
  label?: string;
  resolved: boolean;
  review_id?: string;
  generation_id?: string;
  event_id?: string;
  path?: string;
}

export interface ReviewProjectEvolutionEvent {
  event_id: string;
  kind: "baseline" | "merged_change" | "source_advance" | string;
  generation_id: string;
  previous_generation_id: string;
  observed_at: string;
}

/** PK2: one landed-history entry. Advisory context, never Snapshot evidence. */
export interface ReviewProjectContextEntry {
  entry_id: string;
  context_class: "project_knowledge" | "ai_narration";
  subject: { kind: string; key: string };
  knowledge_kind: string;
  summary: string;
  summary_truncated: boolean;
  state: "current" | "superseded" | "historical" | "uncertain";
  state_label: string;
  freshness_label: string;
  stale_for_snapshot: boolean;
  support: string;
  canonical_ref: string;
  relevance: "changed_path" | "subject" | "neighborhood" | "question";
  /** Order relative to this Change, proven from the generation chain; absent from older servers. */
  relation_to_change?: "unrelated" | "this_change" | "predates" | "after_base" | "unknown_order";
  source_paths: string[];
  /** Source paths beyond the bounded list. */
  source_paths_omitted?: number;
  observed_at: string;
  history: { origin: ReviewProjectEvolutionEvent | null; superseded_by: ReviewProjectEvolutionEvent | null };
  citations: ReviewProjectCitation[];
  /** Citations beyond the bounded list; they were never looked up. */
  citations_omitted?: number;
}

export interface ReviewProjectContext {
  purpose: "brief" | "map" | "ask" | "ai_review";
  available: boolean;
  reason: string;
  context_class: "project_knowledge";
  authority: "advisory";
  authority_note: string;
  current_generation: string;
  entries: ReviewProjectContextEntry[];
  narration: ReviewProjectContextEntry[];
  omitted: number;
  truncated: boolean;
  filtered_by_content_access: number;
}

export interface ReviewProjectContextResponse {
  review_id: string;
  revision_id: string;
  revision_number: number;
  project_context: ReviewProjectContext;
}

export interface ReviewProjectContextSelection {
  paths?: string[];
  components?: string[];
  edges?: string[];
  symbols?: string[];
}

export interface ReviewAskReference {
  kind?: "source" | "review_map_node" | "review_map_edge" | "project_knowledge" | string;
  label?: string;
  exactness?: "snapshot" | "current_index" | "historical" | "ai_narration" | string;
  /** project_knowledge references only. */
  entry_id?: string;
  state?: string;
  freshness_label?: string;
  stale_for_snapshot?: boolean;
  review_id?: string;
  path?: string;
  side?: string;
  start_line?: number;
  end_line?: number;
  target_unit_key?: string;
  symbol?: string;
  map_selection?: ReviewMapSelection;
  evidence_id?: string;
}

export interface ReviewAskAction {
  kind?: "agent_handoff";
  handoff_instruction?: string;
  operation_id?: string;
  instruction_hash?: string;
  expected_revision_id?: string;
  expected_tree_fingerprint?: string;
  expected_source_fingerprint?: string;
  delivery?: FeedbackDeliveryCapability;
  handoff?: {
    state: "prepared" | "dispatching" | "sent" | "blocked" | "failed" | "uncertain";
    target_ref: string;
    remote_ref: string;
    last_error: string;
    updated_at: string;
  };
}

export interface ReviewAskMessage {
  id: string;
  review_id: string;
  revision_id: string;
  revision_number: number | null;
  role: "human" | "assistant";
  intent: "question" | "change_request";
  body: string;
  scope: ReviewAskScope;
  context?: { selection?: ReviewMapSelection; label?: string };
  references: ReviewAskReference[];
  action: ReviewAskAction;
  created_at: string;
  conversation_id?: string;
}

export interface ReviewAskConversationOrigin {
  kind: "review" | "code" | "map" | "selection" | string;
  label?: string;
  revision_number?: number | null;
  path?: string;
  symbol?: string;
  start_line?: number;
  end_line?: number;
}

export interface ReviewAskConversationSummary {
  id: string;
  title: string;
  origin: ReviewAskConversationOrigin;
  created_at: string;
  updated_at: string;
  message_count: number;
  legacy?: boolean;
}

export type ReviewAskDriver = "configured" | "claude" | "codex" | "opencode" | "ollama" | "openai" | "litellm";

export interface ReviewAskAIOptions {
  driver: ReviewAskDriver;
  model: string;
  models: string[];
  /** Drivers this server runs; a hosted server omits the agent CLIs. */
  drivers?: ReviewAskDriver[];
  models_by_driver?: Partial<Record<ReviewAskDriver, string[]>>;
}

export interface ReviewAskConversation {
  review_id: string;
  latest_revision_number: number;
  messages: ReviewAskMessage[];
  conversations?: ReviewAskConversationSummary[];
  active_conversation_id?: string;
  ai?: ReviewAskAIOptions;
}

export interface ReviewAskResult {
  review_id: string;
  latest_revision_number: number;
  conversation_id?: string;
  human: ReviewAskMessage;
  assistant: ReviewAskMessage;
}

export interface ReviewAskInput {
  revision_number: number;
  base_revision_number?: number;
  body: string;
  conversation_id?: string;
  driver?: ReviewAskDriver;
  model?: string;
  mentioned_paths?: string[];
  evidence_ids?: string[];
  context?: ReviewMapSelection;
  target_unit_key?: string;
  path?: string;
  symbol?: string;
  side?: "new" | "old" | "additions" | "deletions";
  start_line?: number;
  end_line?: number;
  selected_text?: string;
}

export interface ReviewAskHandoffResult {
  state: "prepared" | "dispatching" | "sent" | "blocked" | "failed" | "uncertain";
  target_ref: string;
  remote_ref: string;
  message: string;
  operation_id: string;
}

/** How the Reader sends a handoff: resume or inbox an exact session, start one, copy it, or publish to the PR. */
export type FeedbackRouteKind = "agent" | "new_session" | "export" | "github";

export interface FeedbackDeliveryCapability {
  supported: boolean;
  host: string;
  session_id: string;
  target_ref: string;
  label: string;
  reason: string;
  kind?: FeedbackRouteKind;
  /** The saved destination this route came from. */
  destination?: ReviewDestinationKind;
  /** Author routing found no exact session, so this is the fallback route. */
  fallback?: boolean;
  targets?: string[];
}

export type ReviewDestinationKind = "auto" | "session" | "new_session" | "github_pr" | "export";

/** Where this Review's feedback goes: what the reviewer saved and what it resolves to now. */
export interface ReviewDestinationState {
  review_id: string;
  destination: {
    kind: ReviewDestinationKind;
    host: string;
    session_id: string;
    external_ref: string;
    updated_at: string;
  };
  effective: {
    kind: FeedbackRouteKind;
    label: string;
    host: string;
    session_id: string;
    available: boolean;
    fallback: boolean;
    reason: string;
  };
}

export interface ReviewDestinationOption {
  kind: ReviewDestinationKind;
  label: string;
  detail: string;
  available: boolean;
  reason: string;
  host?: string;
  session_id?: string;
  external_ref?: string;
  /** Most recent recorded activity for this exact session. */
  activity_at?: string;
}

export interface ReviewDestinationOptions extends ReviewDestinationState {
  options: ReviewDestinationOption[];
}

export interface ReviewDestinationChoice {
  kind: ReviewDestinationKind;
  host?: string;
  session_id?: string;
  external_ref?: string;
}

export interface FeedbackExport {
  markdown: string;
  open: number;
  orphaned: number;
  resolved: number;
  revision_id: string;
  feedback_hash: string;
  operation_id: string;
  annotation_versions: Record<string, number>;
  delivery: FeedbackDeliveryCapability;
  status?: FeedbackStatus;
}

export interface FeedbackDelivery {
  state: "prepared" | "leased" | "dispatching" | "working" | "queued" | "sent" | "consumed" | "partial" | "blocked" | "failed" | "uncertain";
  target_ref: string;
  remote_ref: string;
  message: string;
  annotation_count: number;
  operation_id: string;
}

/** What publishing this Review to its GitHub pull request would post. Nothing is posted yet. */
export interface GitHubPublishPreview {
  pr: { owner: string; repo: string; number: number; url: string };
  commit_id: string;
  event: "APPROVE" | "REQUEST_CHANGES" | "COMMENT";
  summary: string;
  inline: { annotation_id: string; path: string; side: "LEFT" | "RIGHT"; start_line: number; line: number; body: string }[];
  folded: { annotation_id: string; location: string; body: string; reason: string }[];
  notices: string[];
  plan_hash: string;
  review_id: string;
  revision_id: string;
  operation_id: string;
  head_sha: string;
  stale: boolean;
  empty: boolean;
}

export interface GitHubPublishResult {
  state: "sent";
  operation_id: string;
  remote_ref: string;
  html_url: string;
  adopted: boolean;
  event: GitHubPublishPreview["event"];
  inline: number;
  folded: number;
}

/** Exact target-based completion snapshot used by the finish sheet and mutation result. */
export interface ReviewClosure {
  status: ReviewSessionStatus;
  /** Fresh analysis limitations from the same preview used to build this closure. */
  degraded?: DegradedNote[];
  target_count: number;
  reviewed_targets: number;
  unreviewed_targets: number;
  changed_since_review: number;
  needs_changes: number;
  unknown_targets: number;
  open_comments: number;
  orphaned_comments: number;
  failed_verification: number;
  unresolved_verification: number;
  /** Superseded proof kept for audit/history; not itself outstanding work. */
  previous_revision_evidence: number;
  discarded_verdicts: number;
  /** Decision progress, separate from code inspection (Phase 3b). */
  decisions?: {
    total: number;
    open: number;
    change_requested: number;
    deferred: number;
    needs_reconsideration: number;
    implementation_changed: number;
    unverified: number;
    accepted: number;
  };
  /** Claims the current evidence does not support (spec §4.7). */
  evidence_gaps?: {
    total: number;
    claims: { id: string; item_id: string; title: string; summary: ReviewProjectionClaim["summary"]; summary_text: string }[];
  };
}

/* ------------------------------------------------------------------------ */
/* Review projection (GET /reviews/{id}/projection) — the Brief's payload.   */
/* Server-ordered; clients render it and never re-rank it.                  */
/* ------------------------------------------------------------------------ */

export type ReviewProjectionAttention = "review_first" | "understand" | "routine" | "done";

export interface ReviewProjectionQuestion {
  id: string;
  reason: string;
  title: string;
  detail: string;
  item_ids: string[];
  target_ids: string[];
  actions: string[];
  tier: number;
  /** "ai" when a validated model synthesis raised it (tier 3 at most). */
  origin?: "lemoncrow" | "ai";
}

export interface ReviewProjectionResolution {
  ref: { path: string; symbol: string; start_line: number; end_line: number };
  outcome: string;
  target_ids: string[];
  detail: string;
}

export interface ReviewProjectionItem {
  id: string;
  key: string;
  kind: "behavior" | "mechanical" | "area" | "other";
  title: string;
  before: string;
  after: string;
  rationale: string;
  /** "inferred": an AI explanation of the changed code, never the author's words. */
  rationale_origin: "author" | "inferred" | "none";
  source_label: string;
  producer: string;
  stale: boolean;
  attention: ReviewProjectionAttention;
  tier: number;
  position: number | null;
  primary_target_ids: string[];
  related_target_ids: string[];
  claim_ids: string[];
  question_ids: string[];
  concerns: { scope: string; reason: string }[];
  unresolved_refs: ReviewProjectionResolution[];
  limitations?: string[];
  /** The author states no external behavior is intended to change (spec §3). */
  internal_only?: boolean;
}

export interface ReviewProjectionEvidence {
  key: string;
  title: string;
  status: string;
  state: string;
  snapshot_number: number | null;
  mapping: "explicit" | "inferred";
  relevance: string;
  /** When the check ran, who recorded it, and its environment when recorded (spec §8). */
  observed_at?: string;
  source?: string;
  environment?: Record<string, string>;
}

export interface ReviewProjectionClaim {
  id: string;
  statement: string;
  source: string;
  target_ids: string[];
  limitations: string[];
  evidence: ReviewProjectionEvidence[];
  summary: "observed" | "author_cited" | "inferred" | "inferred_failing" | "partial" | "contradictory" | "none";
  summary_text: string;
  result_line: string;
}

export interface ReviewProjectionDecision {
  key: string;
  producer: string;
  question: string;
  options: { key: string; label: string }[];
  selected_option: string;
  reason_human_required: string;
  assumptions: { key: string; value: string; statement: string }[];
  contract_scope: string[];
  relied_checks: string[];
  target_ids: string[];
  stale: boolean;
  read_only: boolean;
  /** Phase 3b: present once decisions carry human dispositions. */
  conflict?: boolean;
  positions?: ReviewDecisionPosition[];
  state?: ReviewDecisionState;
  actions?: ReviewDecisionAction[];
}

export type ReviewDecisionAction = "accept" | "reconfirm" | "request_change" | "ask" | "defer" | "reopen";
export type ReviewDecisionDisposition = "open" | "accepted" | "change_requested" | "deferred";
export type ReviewDecisionValidity =
  | "current"
  | "implementation_changed_since_acceptance"
  | "needs_reconsideration"
  | "unverified";

export interface ReviewDecisionPosition {
  producer: string;
  selected_option: string;
  option_keys: string[];
  assumptions: { key: string; value: string }[];
}

export interface ReviewDecisionState {
  disposition: ReviewDecisionDisposition;
  validity: ReviewDecisionValidity | null;
  changed_inputs: string[];
  outstanding: boolean;
  navigable: boolean;
  withdrawn: boolean;
  trigger: { kind: string; check_key: string; note: string } | null;
  trigger_fired: boolean;
  accepted_option: string;
  latest_event_id: string;
  /** The decision scope this state shows; a decision write must name it. */
  semantic_fingerprint?: string;
  latest_action: string;
  latest_reason: string;
  event_snapshot_number: number | null;
  reason: string;
  detail: string;
  tier: number;
  limitations: string[];
}

export interface ReviewDecisionEventInput {
  action: Exclude<ReviewDecisionAction, "ask">;
  option_key?: string;
  reason?: string;
  trigger?: string;
  feedback_body?: string;
}

export interface ReviewDecisionEventResult {
  event: {
    id: string;
    action: string;
    disposition: ReviewDecisionDisposition;
    option_key: string;
    revision_id: string;
    trigger_kind: string;
    trigger_check_key: string;
    trigger_note: string;
  };
  decision: ReviewProjectionDecision;
}

export interface ReviewSnapshotDelta {
  since_snapshot: number;
  added: string[];
  changed: string[];
  removed: number;
  stale_evidence: string[];
  reopened_decisions: string[];
  /** The behavior and items the moved targets belong to, in Brief order (spec §9). */
  items?: { id: string; title: string; kind: ReviewProjectionItem["kind"]; changed: string[]; added: string[] }[];
}

export type ReviewIntelligenceMode =
  | "deterministic_only"
  | "cached_synthesis"
  | "synthesized"
  | "skipped_by_budget"
  | "unavailable"
  | "error";

export interface ReviewModelContextExclusion {
  target_id?: string;
  path: string;
  mode: "external_allowed" | "customer_local_only" | "ai_denied" | string;
  reason: string;
  route_trust?: string;
  policy_refs?: string[];
  digest?: string;
}

export interface ReviewExecutionTrace {
  execution_locus?: "managed" | "customer" | "local" | string;
  worker_id?: string;
  execution_class?: string;
  provider?: string;
  model?: string;
  model_route_id?: string;
  model_data_locus?: "customer_local" | "external" | "unknown" | "managed" | string;
  tools?: string[];
  source_ranges?: string[];
  evidence_refs?: string[];
  artifact_refs?: string[];
  external_domains?: string[];
  policy_digest?: string;
  content_access_exclusions?: string[];
  source_trust?: string;
  duration_ms?: number;
  budget?: Record<string, unknown>;
  provider_calls?: number;
  provider_cost?: number | null;
  currency?: string;
  cache_status?: string;
}

export interface ReviewAiExecutionStatus {
  status: string;
  job_id?: string;
  revision_id?: string;
  effort?: string;
  trigger?: string;
  execution_locus?: "managed" | "customer" | "local" | string;
  metering_owner?: "lemoncrow_managed" | "customer_provider" | string;
  budget?: Record<string, unknown>;
  provider?: string;
  model?: string;
  cache_status?: string;
  exclusions?: string[];
  network_policy?: Record<string, unknown>;
  trace?: ReviewExecutionTrace;
  cancel_requested?: boolean;
  /** An explicit reviewer/admin fresh re-run (P11 item 15b), not an ordinary request. */
  fresh_rerun?: boolean;
  /**
   * Hosted only (G10-FAIR, RC8): why a queued job has not started -- a fair share it
   * would borrow past once a slot idles (`organization_model_budget`,
   * `repository_model_budget`, `organization_deep_budget`, `repository_deep_budget`),
   * a hard ceiling it never passes (`organization_model_ceiling`, `repository_model_ceiling`,
   * `organization_deep_ceiling`, `repository_deep_ceiling`), `retry_backoff` or
   * `capacity`. Empty unless queued.
   */
  wait_reason?: string;
}

/** Requested vs observed model of one AI run (P11 item 15a); never a guessed default. */
export interface ReviewModelIdentity {
  driver: string;
  requested_model: string;
  observed_models: string[];
  observed_truncated: boolean;
  unverified_model?: string;
  source: "observed" | "pinned" | "unreported_default" | "unverified";
  label: string;
  families?: string[];
}

export type ReviewIndependenceStatus =
  | "independent"
  | "not_independent"
  | "unknown_author"
  | "unreported_reviewer"
  | "unverified_reviewer"
  | "unknown_reviewer"
  | "not_applicable"
  | "not_run";

export type ReviewIndependenceRequirement =
  | "not_required"
  | "not_applicable"
  | "preferred_met"
  | "preferred_unmet"
  | "satisfied"
  | "unsatisfied";

/** Is the AI reviewer provably independent of the agent that authored the change (P11 item 14)? */
export interface ReviewAiIndependence {
  status: ReviewIndependenceStatus;
  independent: boolean;
  reason: string;
  author?: { host: string; model: string; family: string; label: string };
  reviewers: ReviewModelIdentity[];
  reviewers_not_shown: number;
  policy: {
    mode: "off" | "prefer" | "require";
    origin: { layer: string; source: string };
    paths: string[];
    matched_paths: string[];
    matched_path_count: number;
  };
  requirement: ReviewIndependenceRequirement;
}

/** What a fresh re-run would execute and spend, previewed at the action boundary. */
export interface ReviewAiFreshRerunPreview {
  available: boolean;
  reason: string;
  effort: string;
  entries: string[];
  estimated_model_calls: number;
  budget: { calls_used: number; max_calls: number; remaining: number };
  within_budget: boolean;
  cost_class: string;
  execution_locus: "local" | "managed" | "customer" | "" | string;
  metering_owner: "lemoncrow_managed" | "customer_provider" | "" | string;
  model_data_locus: string;
  provider: string;
  model: string;
  reuses_cache: false;
}

export interface ReviewAiAssurance {
  identity: ReviewModelIdentity | null;
  independence: ReviewAiIndependence;
  execution: {
    latest_mode: "fresh_rerun" | "executed" | "";
    fresh_reruns: number;
    fresh_rerun_runs: number;
    latest_fresh_at: string;
    runs: number;
  };
  fresh_rerun: ReviewAiFreshRerunPreview;
}

export interface ReviewAiReviewStatus {
  status: "none" | "skipped_unnecessary" | "pending" | "ready" | "failed" | "cancelled";
  effort: "auto" | "quick" | "thorough" | "deep";
  candidates: number;
  findings: number;
  provider?: string;
  model?: string;
  error?: string;
  budget: { calls_used: number; max_calls: number };
  skipped_by_budget?: number;
  /** Backend-derived Auto eligibility for this exact latest Snapshot. */
  auto_eligible?: boolean;
  coverage?: {
    human_deterministic_targets: number;
    ai_context_eligible_targets: number;
    excluded_by_policy: ReviewModelContextExclusion[];
    excluded_count: number;
  };
  rules?: { applicable_count: number; base_state: string; errors: string[] };
  /** Phase-7 execution authority projection; absent when no durable execution exists yet. */
  execution?: ReviewAiExecutionStatus;
  /** Observed identity, reviewer independence and the fresh re-run preview (Phase 11). */
  assurance?: ReviewAiAssurance;
}

export interface ReviewAiReviewRunResult {
  revision_id: string;
  status: "completed" | "skipped_unnecessary" | "degraded" | "cancelled";
  effort: "auto" | "quick" | "thorough" | "deep";
  candidates: number;
  hypotheses: number;
  findings: { confirmed: number; not_confirmed: number; unknown: number; annotations_created: number };
  annotation_ids: string[];
  budget: { calls_used: number; max_calls: number };
  provider_calls: number;
  cache_reuse: number;
  skipped_by_budget: number;
  provider_errors: string[];
  reviewer: { driver: string; model: string; independent: boolean; identities?: ReviewModelIdentity[] };
  /** ``fresh_rerun`` when an explicit fresh re-run executed this result. */
  execution?: { mode: "standard" | "fresh_rerun" };
  coverage?: {
    human_deterministic_targets: number;
    ai_checked_targets: number;
    checked_target_ids: string[];
    excluded_by_policy: ReviewModelContextExclusion[];
  };
  provider_cost?: number | null;
  currency?: string | null;
  /** How a manual depth request was answered: raised to a floor (with the floor and origin), never lowered. */
  depth_request?: {
    entry: string;
    requested: string;
    effective: string | null;
    state: "honored" | "raised" | "limited_by_budget" | "limited" | "refused";
    reason: string;
  };
  /** Focused per-rule checks: one bounded question per rule, with its state. */
  rule_checks?: Array<{ ref: string; stable_key: string; state: string; reason: string; answer?: string }>;
}

export interface ReviewSynthesisStatus {
  status: "disabled" | "none" | "pending" | "ready" | "failed";
  mode?: ReviewIntelligenceMode;
  model?: string;
  driver?: string;
  error?: string;
  candidates?: number;
  rejected_count?: number;
  unsynthesized?: number;
  policy_digest?: string;
  cache_hit?: boolean;
  excluded_by_policy?: ReviewModelContextExclusion[];
  /** This Review's cumulative model calls against its shared bound. */
  budget?: { calls_used: number; max_calls: number };
}

export interface ReviewTourEvidenceRef {
  claim_id: string;
  key: string;
  title: string;
  status: string;
  state: string;
  snapshot_number: number | null;
}

export interface ReviewTourStep {
  id: string;
  semantic_key: string;
  title: string;
  why: string;
  why_origin: "deterministic" | "inferred";
  target_ids: string[];
  stale_target_ids: string[];
  change_type: "current" | "added" | "changed" | "reopened";
  signals: string[];
  claim_ids: string[];
  decision_refs: string[];
  evidence_refs: ReviewTourEvidenceRef[];
  stale: boolean;
  synthesis_needed: boolean;
  position: number;
  implementation: {
    target_id: string;
    path: string;
    symbol: string;
    start_line: number;
    end_line: number;
  } | null;
}

export interface ReviewTourProjection {
  schema_version: number;
  steps: ReviewTourStep[];
  step_count: number;
  synthesis?: ReviewSynthesisStatus;
}

export interface ReviewProjection {
  /** Short Snapshot summary; supporting items remain available in review details. */
  summary?: {
    paragraphs: string[];
    statements?: {
      text: string;
      origin: "author" | "inferred" | "observed" | "mixed" | string;
      target_ids: string[];
      claim_ids: string[];
    }[];
    origin: "author" | "inferred" | "observed" | "mixed";
    scope: string;
    verification: string;
    attention: string;
    additional_explanations: number;
  };
  projection_version: number;
  review_id: string;
  revision_id: string;
  snapshot_number: number | null;
  historical: boolean;
  plan: {
    schema_version: number;
    input_digest: string;
    intents: { producer: string; intent: string; origin?: "guidance" | "captured" }[];
    limitations: string[];
  };
  actions: ReviewProjectionQuestion[];
  questions: ReviewProjectionQuestion[];
  items: ReviewProjectionItem[];
  claims: ReviewProjectionClaim[];
  decisions: ReviewProjectionDecision[];
  coverage: {
    target_count: number;
    author_explained: number;
    fallback_grouped: number;
    other: number;
    routine: number;
    unresolved_refs: number;
    unexplained_targets: number;
    file_count: number;
    synthesized?: number;
    unsynthesized_target_ids?: string[];
  };
  /** Presentation roles over exact targets; never changes Review scope or judgment. */
  material?: {
    files: {
      path: string;
      role: "implementation" | "tests" | "documentation" | "generated" | "agent_guidance" | "localization" | "assets" | "other";
      source: "gitattributes" | "inferred";
      target_ids: string[];
      additions: number;
      deletions: number;
      changed_lines: number;
      collapsed_default: boolean;
    }[];
    roles: Partial<Record<"implementation" | "tests" | "documentation" | "generated" | "agent_guidance" | "localization" | "assets" | "other", { files: number; changed_lines: number }>>;
    implementation_changed_lines: number;
    total_changed_lines: number;
    explicit_metadata_state: "present" | "absent" | "unavailable";
  };
  counts: { review_first: number; understand: number; routine: number; done: number; decisions: number };
  /** Optional AI gap filling; reading the projection never triggers inference. */
  synthesis?: ReviewSynthesisStatus;
  /** Cost-aware independent Review status; projection reads are passive. */
  ai_review?: ReviewAiReviewStatus;
  /** Current rule authority, future policy, and inert suggestions; no separate Rules view. */
  policy?: ReviewPolicyProjection;
  /** What changed since the previous Snapshot; null on the first. */
  delta?: ReviewSnapshotDelta | null;
  /** Ordered navigation projection over the same Review targets. */
  tour?: ReviewTourProjection;
  decision_progress?: { total: number; settled: number; outstanding: number; deferred: number; withdrawn: number };
  queue: string[];
  /** Observed base-vs-head behavior deltas (contract v1); null when nothing ran or applies. */
  behavior?: ReviewBehaviorSummary | null;
  /** Review-level priority, criticality, severity and evidence with an explanation (Phase 9). */
  triage?: ReviewTriage;
}

/** Which layer and file/commit (or actor) set a triage value. */
export interface ReviewTriageOrigin {
  layer: string;
  source: string;
  commit: string;
}

export interface ReviewTriageExplanation {
  field: string;
  value: string;
  reason: string;
  origin: ReviewTriageOrigin;
}

/** Lightweight per-Review triage projection; priority, criticality and severity stay separate. */
export interface ReviewTriage {
  version: number;
  review_id: string;
  revision_id: string;
  priority: "urgent" | "high" | "normal" | "low";
  computed_priority: "urgent" | "high" | "normal" | "low";
  criticality: "critical" | "high" | "normal" | "low";
  severity_summary: {
    p0: number;
    p1: number;
    p2: number;
    suggestion: number;
    unrated: number;
    stale: number;
    below_threshold: number;
    highest: "p0" | "p1" | "p2" | "suggestion" | null;
    /** The same counts by source: AI findings, behavior changes, vulnerability advisories, static findings. */
    by_source?: Record<
      string,
      { p0: number; p1: number; p2: number; suggestion: number; unrated: number; stale: number }
    >;
  };
  finding_threshold: { value: "critical" | "high" | "medium" | "suggestions"; label: string; origin: ReviewTriageOrigin };
  evidence_state: "current" | "stale" | "missing";
  review_state: "not_reviewed" | "in_progress" | "reviewed" | "unknown";
  evidence_label: string;
  release_urgency: "unknown";
  dependency_changes: number;
  required_failures: number;
  override: {
    level: string;
    reason: string;
    scope: "review" | "snapshot";
    actor_id: string;
    actor_type: string;
    revision_id: string;
    created_at: string;
    event_id: string;
  } | null;
  explanation: ReviewTriageExplanation[];
  hidden_finding_ids: string[];
  /** `focus.require`: review categories policy requires on changed paths (a readiness input). */
  required_focus?: { category: string; paths: string[]; origins: ReviewTriageOrigin[] }[];
  policy_state: "final" | "provisional" | "invalid";
  policy_errors: string[];
  computed: boolean;
}

export type ReviewBehaviorKind =
  | "test_outcome"
  | "api_response"
  | "visual"
  | "side_effect"
  | "migration"
  | "verification_weakening"
  | "spec_delta"
  | "compatibility"
  | "agent_instruction"
  | "artifact"
  | "infra";

export interface ReviewBehaviorObservation {
  side: "base" | "head";
  status: string;
  digest: string;
  summary: string;
  evidence_ref: string;
}

export interface ReviewBehaviorEvidenceRef {
  ref: string;
  kind: "artifact" | "check";
  label: string;
  digest: string;
  side: string;
  role: string;
  evidence_id: string;
  media_type: string;
}

/** Served only by the per-target read; a judgment must echo `digest`. */
export interface ReviewBehaviorEvidence {
  digest: string;
  base: ReviewBehaviorObservation | null;
  head: ReviewBehaviorObservation | null;
  refs: ReviewBehaviorEvidenceRef[];
  facts: { name: string; value: string }[];
}

export interface ReviewBehaviorTarget {
  behavior_key: string;
  kind: ReviewBehaviorKind | string;
  kind_label: string;
  producer: string;
  title: string;
  summary: string;
  change: "added" | "changed" | "removed";
  severity: "low" | "medium" | "high";
  critical: boolean;
  state: "unreviewed" | "reviewed" | "needs_changes" | "changed_since_review" | "unknown";
  changed_since_mark: boolean;
  reviewed_revision_id: string;
  fingerprint: string;
  environment_fingerprint: string;
  executor_class: "" | "author_machine" | "customer_worker" | "managed_sandbox";
  paths: string[];
  target_ids: string[];
  unverified_integrations: string[];
  priority: ReviewTargetPriority;
  bulk_eligible: boolean;
  evidence?: ReviewBehaviorEvidence;
}

export interface ReviewExecutionCoverage {
  runs: number;
  changed_targets: number;
  exercised: number;
  never_executed_count: number;
  never_executed: { target_id: string; path: string; label: string }[];
}

export interface ReviewBehaviorStatement {
  text: string;
  behavior_keys: string[];
  evidence_refs: string[];
  origin: "deterministic" | "inferred";
}

export interface ReviewBehaviorNarrative {
  mode: "inferred" | "deterministic";
  statements: ReviewBehaviorStatement[];
  deterministic: ReviewBehaviorStatement[];
  synthesis: Partial<ReviewSynthesisStatus> & { status?: string; mode?: string };
}

export interface ReviewManualCheckResult {
  status: "PASS" | "FAIL" | "UNKNOWN" | string;
  note: string;
  attested_by: string;
  observed_at: string;
  evidence_id: string;
  attestation_class: "human_observation";
  label: "Human observation";
}

export interface ReviewManualCheckItem {
  item_id: string;
  title: string;
  integration: string;
  steps: string[];
  behavior_keys: string[];
  paths: string[];
  origin: "policy" | "behavior";
  required: boolean;
  result: ReviewManualCheckResult | null;
}

export interface ReviewManualChecklist {
  required: boolean;
  policy_source: string;
  policy_error: string;
  attestation_class: "human_observation";
  recorded: number;
  items: ReviewManualCheckItem[];
}

/** The `side_effects.static` run on this Snapshot and its delta against the base register. */
export interface ReviewStaticInventory {
  status: "ran" | "pending" | "skipped" | "inapplicable" | "limited_by_budget" | "not_available" | "on_request" | "not_run" | string;
  reason: string;
  register: {
    key?: string;
    base_sha?: string;
    state?: "complete" | "truncated" | "changed_files_only" | string;
    files_scanned?: number;
    identities?: number;
    note?: string;
  };
  register_delta: {
    added?: number;
    removed?: number;
    changed?: number;
    by_category?: Record<string, { added: number; removed: number; changed: number }>;
    /** Changes another review surface judges (dependencies: the dependency review); never a second target. */
    judged_elsewhere?: Record<string, number>;
  };
  degraded: string[];
  files_analyzed: number;
  targets: number;
  /** The process that ran it: "local" or the hosted "server" (never a sandbox). */
  executed_by?: string;
}

/** A behavior target that makes the Snapshot ineligible for auto-approval. */
export interface ReviewAutoApprovalBlocker {
  code: string;
  behavior_key: string;
  title: string;
  paths: string[];
}

export interface ReviewAgentAction {
  category: string;
  summary: string;
  detail?: string;
  host: string;
  session_id: string;
  at: string;
  /** Whether a human pre-approved exactly this action; `null` when none was required. */
  approval?: AgentActionApproval;
}

export interface ReviewBehaviorSummary {
  contract_version: number;
  revision_id: string;
  total: number;
  progress: {
    total: number;
    reviewed: number;
    unreviewed: number;
    changed_since_review: number;
    needs_changes: number;
    unknown: number;
    critical: number;
    outstanding: number;
  };
  targets: ReviewBehaviorTarget[];
  unchanged: number;
  execution_ran: boolean;
  coverage: ReviewExecutionCoverage | null;
  checklist: ReviewManualChecklist | null;
  narrative: ReviewBehaviorNarrative | null;
  degraded: string[];
  producers: string[];
  static_inventory?: ReviewStaticInventory;
  auto_approval?: { blocked: boolean; blockers: ReviewAutoApprovalBlocker[] };
  agent_actions?: ReviewAgentAction[];
  /** Approval requests for risky agent actions, newest first; empty on a historical Snapshot. */
  agent_approvals?: AgentApproval[];
}

/** Who can sign a review record here (organization key, personal key) and why not. */
export interface ReviewRecordSigningStatus {
  mode: string;
  available: boolean;
  reason: string;
  /** not_configured | not_available | forbidden | provider_error, or "" when available. */
  code: string;
  key_id: string;
  algorithm: string;
  issuer_kind: string;
  issuer_id: string;
}

export interface ReviewRecordDecision {
  kind: string;
  outcome: string;
  actor: { id: string; type: string };
  decided_at: string;
  reference: string;
  detail: Record<string, string>;
}

/** One stored review record (DSSE envelope); `label` always says UNSIGNED for unsigned records. */
export interface ReviewRecordSummary {
  record_id: string;
  review_id: string;
  revision_id: string;
  payload_digest: string;
  signing_state: "signed" | "unsigned" | string;
  signing_mode: string;
  key_id: string;
  issued_at: string;
  decision_kind: string;
  label: string;
  decision?: string;
  decided_by?: string;
}

export interface ReviewRecordStatus {
  review_id: string;
  snapshot_id: string;
  snapshot_number: number;
  decision: ReviewRecordDecision | null;
  signing: ReviewRecordSigningStatus;
  records: ReviewRecordSummary[];
}

export interface ReviewRecordVerification {
  status: "verified" | "unsigned" | "invalid" | "revoked";
  verified: boolean;
  headline: string;
  reasons: string[];
  payload_digest: string;
  key_id: string;
  key_status: string;
  key_revoked_at: string;
  signing_state: string;
  signing_mode: string;
  exit_code: number;
}

export interface ReviewRecordExportResult {
  record: ReviewRecordSummary;
  envelope: Record<string, unknown>;
  verification: ReviewRecordVerification;
  store: Record<string, string>;
  matches_store: boolean;
  reproduced_digest: string;
  reproduced: boolean;
}

/** DS0: one summary statement and the exact thread messages (`annotation:<id>`) it is drawn from. */
export interface ThreadSummaryStatement {
  text: string;
  message_refs: string[];
}

/** A thread's stored AI summary (derived presentation) beside its authoritative state. */
export interface ThreadSummaryView {
  thread_id: string;
  eligible: boolean;
  /** Read from the comments themselves, never from the summary. */
  thread: {
    state: AnnotationState;
    resolved_revision_id: string;
    turn_owner_kind: string;
    author_response: string;
    message_count: number;
    last_message_at: string;
  };
  summary: {
    topic: ThreadSummaryStatement;
    open_asks: ThreadSummaryStatement[];
    points: ThreadSummaryStatement[];
    message_refs: string[];
    /** False once a message was added or edited after the summary was made. */
    current: boolean;
    driver: string;
    model: string;
    created_at: string;
    run_id: string;
  } | null;
  last_attempt: { status: string; error: string; created_at: string } | null;
}

export interface ThreadSummaryRead {
  available: boolean;
  summary: ThreadSummaryView | null;
}

export interface ThreadSummaryRun {
  result: {
    status: "ready" | "reused" | "failed" | "unavailable" | "not_eligible";
    reason: string;
    provider_calls: number;
    cache_hit: boolean;
  };
  summary: ThreadSummaryView;
}
