/**
 * The shapes `lemoncrow.pro.capabilities.review.api` actually returns.
 *
 * Hand-written rather than generated, and deliberately narrow: this file
 * describes what the workspace *reads*, not everything the Python dataclasses
 * hold. Fields are optional wherever the server may legitimately have nothing
 * to say, because "unknown" is a first-class state here and a type that forces
 * a value invites the UI to invent one.
 */

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

export interface ReviewTargetSpan {
  side: "old" | "new";
  start_line: number;
  end_line: number;
  hunk_ordinal: number;
}

export interface ReviewTarget {
  target_id: string;
  unit_key: string;
  kind: "symbol" | "hunk" | "file" | "dependency";
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
}

export interface ReviewTargetList {
  revision_id: string;
  order: "recommended" | "file";
  targets: ReviewTarget[];
  progress: ReviewProgress;
  outline: ReviewOutlineItem[];
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

export type ReviewMapLevel = "architecture" | "symbol";
export type ReviewMapExactness = "snapshot" | "current_index";
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
}

export interface ReviewMapMemberSymbol {
  id: string;
  label: string;
  qualified_name: string;
  path: string;
  line: number;
  end_line: number;
  change: string;
  caller_count: number;
  usage_count: number;
  centrality_rank: number | null;
  centrality_percentile: number | null;
}

export interface ReviewMapNode {
  id: string;
  label: string;
  qualified_name?: string;
  kind: string;
  component?: string;
  path: string;
  line: number;
  end_line: number;
  status: ReviewMapStatus;
  change?: string;
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
  selection: ReviewMapSelection;
}

export interface ReviewMapEdge {
  id: string;
  source: string;
  target: string;
  kind: string;
  exactness: ReviewMapExactness;
  weight: number;
  uncertain: boolean;
  evidence?: { kind: string; path: string; line: number; uncertainty: string }[];
  selection: ReviewMapSelection;
}

export interface ReviewMapGraph {
  level: ReviewMapLevel;
  focus: string | null;
  nodes: ReviewMapNode[];
  edges: ReviewMapEdge[];
  truncated: boolean;
  omitted_node_count?: number;
  reason?: string;
}

export interface ReviewMapPayload {
  review_id: string;
  revision_id: string;
  revision_number: number;
  level: ReviewMapLevel;
  snapshot: {
    exactness: "snapshot";
    tree_fingerprint: string;
    source_fingerprint: string;
    base_sha: string;
    head_sha: string;
    degraded: string[];
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
  degraded: string[];
  provenance_host: string;
  provenance_model: string;
  provenance_session_id: string;
  provenance_certainty: string;
  created_at: string;
  is_latest?: boolean;
  is_last_reviewed?: boolean;
  is_last_commented?: boolean;
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
  change_kind: "created" | "edited" | "state_changed" | "author_response" | "thread_reply";
  created_at: string;
}

export interface SourceState {
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
export interface EvidenceRow {
  name: string;
  status: "PASS" | "FAIL" | "NOT_RUN" | "UNKNOWN";
  detail: string;
  source: string;
  scope?: "review" | "file";
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
  | "waiver";
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
  counts: { authors: number; verifiers: number; investigators: number; files: number; decisions: number; open_items: number };
  decisions: DevelopmentBrief[];
  open_items: DevelopmentBrief[];
  verification: { pass: number; fail: number; pending: number; checks: DevelopmentCheck[] };
  changed_paths: DevelopmentChangedPath[];
  unattributed_paths: string[];
  feedback: DevelopmentFeedbackLink[];
  latest_event: DevelopmentBrief | null;
  event_count: number;
  cursor: number;
  revision_id?: string;
  revision_number?: number;
  watermark?: number | null;
  newer_paths?: string[];
  active?: boolean;
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
  kind: "symbol" | "hunk" | "file" | "dependency";
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
}

export interface ReviewLandingCondition {
  id: string;
  label: string;
  /** `waived`: a human override that does not block, never shown as `pass`. */
  state: "pass" | "fail" | "pending" | "not_applicable" | "waived";
  detail: string;
  owner: "lemoncrow" | "scm";
  category: string;
  url: string;
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
  work_items: ReviewRelationshipInfo[];
  landing: ReviewLandingState;
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
  brief?: ReviewBrief;
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
}

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
  groups: AttentionGroup[];
  group_counts: Record<GroupKey, number>;
}

export interface BulkMarkResult {
  marked: { unit_key: string; path: string }[];
  skipped: { unit_key: string; path: string; reason: string }[];
  progress?: ReviewProgress;
  outline_updates?: ReviewOutlineItem[];
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

export type ReviewDirectoryStatus = "all" | ReviewSessionStatus;

export interface ReviewDirectoryRow {
  id: string;
  ref: string;
  review_path: string;
  repo_id: string;
  title: string;
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
}
export interface ReviewDirectoryPage {
  reviews: ReviewDirectoryRow[];
  status: ReviewDirectoryStatus;
  query: string;
  next_cursor: string;
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
  title?: string;
  evidence?: string[];
  confidence?: number | null;
  author_response?: "none" | "addressed";
  author_response_source_id?: string;
  author_response_at?: string;
  origin_revision_number?: number | null;
  relation_to_revision?: "originating" | "inherited" | "future";
  author_addressed_revision_number?: number | null;
  addressed_by_revision?: boolean;
  resolved_revision_number?: number | null;
  turn_owner_kind?: "none" | "author" | "reviewer";
  turn_owner_id?: string;
  is_human_judgment?: boolean;
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

export interface ReviewAskReference {
  kind?: "source" | "review_map_node" | "review_map_edge" | string;
  label?: string;
  exactness?: "snapshot" | "current_index" | string;
  path?: string;
  side?: string;
  start_line?: number;
  end_line?: number;
  target_unit_key?: string;
  symbol?: string;
  map_selection?: ReviewMapSelection;
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
}

export type ReviewAskDriver = "configured" | "claude" | "codex" | "opencode" | "ollama" | "openai" | "litellm";

export interface ReviewAskAIOptions {
  driver: ReviewAskDriver;
  model: string;
  models: string[];
  models_by_driver?: Partial<Record<ReviewAskDriver, string[]>>;
}

export interface ReviewAskConversation {
  review_id: string;
  latest_revision_number: number;
  messages: ReviewAskMessage[];
  ai?: ReviewAskAIOptions;
}

export interface ReviewAskResult {
  review_id: string;
  latest_revision_number: number;
  human: ReviewAskMessage;
  assistant: ReviewAskMessage;
}

export interface ReviewAskInput {
  revision_number: number;
  base_revision_number?: number;
  body: string;
  driver?: ReviewAskDriver;
  model?: string;
  context?: ReviewMapSelection;
  target_unit_key?: string;
  path?: string;
  symbol?: string;
  side?: "new" | "old" | "additions" | "deletions";
  start_line?: number;
  end_line?: number;
}

export interface ReviewAskHandoffResult {
  state: "prepared" | "dispatching" | "sent" | "blocked" | "failed" | "uncertain";
  target_ref: string;
  remote_ref: string;
  message: string;
  operation_id: string;
}

export interface FeedbackDeliveryCapability {
  supported: boolean;
  host: string;
  session_id: string;
  target_ref: string;
  label: string;
  reason: string;
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
}
