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

export interface ReviewTargetSpan {
  side: "old" | "new";
  start_line: number;
  end_line: number;
  hunk_ordinal: number;
}

export interface ReviewTarget {
  target_id: string;
  unit_key: string;
  kind: "symbol" | "hunk" | "file";
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

export interface ReviewSessionInfo {
  id: string;
  title: string;
  subject_type: string;
  range_mode: string;
  source_ref: string;
  status: ReviewSessionStatus;
  actor_type: string;
  reviewer_id: string;
  repo_root: string;
  updated_at: string;
  revision_number?: number;
}

export interface RevisionInfo {
  id: string;
  revision_number: number;
  range_mode: string;
  base_sha: string;
  head_sha: string;
  dirty: boolean;
  degraded: string[];
  provenance_host: string;
  provenance_model: string;
  provenance_session_id: string;
  provenance_certainty: string;
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

export interface ReviewChapter {
  key: string;
  label: string;
  reason: string;
  rows: AttentionRow[];
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

export type ReviewEvidenceKind = "screenshot" | "image" | "video" | "playwright_trace" | "document" | "live_preview";
export type ReviewEvidenceSource = "human" | "agent" | "test" | "ci" | "external";

/** Outcome/verification evidence bound to one exact ReviewRevision. */
export interface ReviewEvidence {
  id: string;
  review_id: string;
  revision_id: string;
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
  kind: "symbol" | "hunk" | "file";
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
}
export interface ReviewBriefItem {
  path: string;
  rank: number;
  reason: string;
}

export interface ReviewBrief {
  summary: string;
  themes: string[];
  major_changes?: { key: string; label: string; file_count: number; target_count: number; attention_count: number; first_path: string }[];
  review_first: ReviewBriefItem[];
  verification: { pass: number; fail: number; not_run: number; unknown: number };
  annotations: { human: number; author: number; lemoncrow: number; ai_review: number };
  artifacts: { current: number; stale: number };
}

export interface ReviewOverview {
  session: ReviewSessionInfo;
  revision: RevisionInfo;
  revision_count: number;
  packet_available: boolean;
  stats: Record<string, number>;
  title: string;
  /** Compact orientation for large reviews; derived from deterministic Intent chapters. */
  change_story?: string[];
  change_story_more?: number;
  brief?: ReviewBrief;
  provenance: ProvenanceInfo;
  evidence: EvidenceRow[];
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

/** One file's patch plus everything the right pane says about it. */
export interface FileDetail {
  path: string;
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

export type AnnotationSource = "human" | "author" | "lemoncrow" | "ai_review";

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

export interface AnnotationList {
  revision_id: string;
  annotations: Annotation[];
  counts: Record<string, number>;
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

export interface FeedbackExport {
  markdown: string;
  open: number;
  orphaned: number;
  resolved: number;
}

export interface FeedbackDelivery {
  state: "sent" | "blocked" | "failed";
  target_ref: string;
  remote_ref: string;
  message: string;
  annotation_count: number;
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
