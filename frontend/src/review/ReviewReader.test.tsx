import { describe, expect, it, beforeEach, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import ReviewReader from "./ReviewReader";
import type { FileDetail, ReviewChangeProposal, RevisionTargetRef, ReviewOutlineItem, ReviewOverview, ReviewTarget, ReviewTargetList, SourceState } from "./types";

const stream = vi.hoisted(() => ({
  scrollToTarget: vi.fn(),
  scrollToFile: vi.fn(),
  scrollToSurface: vi.fn(),
  lastProps: null as any,
}));

vi.mock("./ReviewStream", async () => {
  const React = await import("react");
  return {
    default: React.forwardRef((props: any, ref: any) => {
      stream.lastProps = props;
      React.useImperativeHandle(ref, () => ({
        scrollToTarget: stream.scrollToTarget,
        scrollToFile: stream.scrollToFile,
        scrollToSurface: stream.scrollToSurface,
      }));
      return <div data-testid="reader-stream-mock">{props.targets.length} streamed targets</div>;
    }),
  };
});

const drawer = vi.hoisted(() => ({ lastProps: null as any }));
const mapSheet = vi.hoisted(() => ({ lastProps: null as any }));
vi.mock("./ContextDrawer", () => ({
  default: (props: any) => {
    drawer.lastProps = props;
    return (
      <div data-testid="context-drawer-mock">
        <span>{props.tab} · {props.pinned ? "pinned" : "overlay"}</span>
        <button type="button" onClick={props.onClose}>Close context mock</button>
      </div>
    );
  },
}));
vi.mock("./ReviewMapSheet", () => ({
  default: (props: any) => {
    mapSheet.lastProps = props;
    return (
      <div role="dialog" aria-label="Review Map mock">
        <button
          type="button"
          onClick={() => props.onOpenSelection({
            kind: "review_map_node",
            review_id: "review-1",
            revision_id: "rrv-1",
            level: "symbol",
            node_id: "symbol:fn0",
            exactness: "snapshot",
            path: "src/file-0.py",
            line: 1,
            end_line: 2,
            target_ids: ["target-0"],
          })}
        >
          Open mapped target
        </button>
        <button
          type="button"
          onClick={() => props.onOpenSelection({
            kind: "review_map_node",
            review_id: "review-1",
            revision_id: "rrv-1",
            level: "symbol",
            node_id: "impact:outside",
            exactness: "snapshot",
            path: "src/unchanged-caller.py",
            line: 44,
            end_line: 44,
            target_ids: [],
          })}
        >
          Open mapped impact
        </button>
        <button
          type="button"
          onClick={() => props.onAskSelection({
            kind: "review_map_node",
            review_id: "review-1",
            revision_id: "rrv-1",
            level: "symbol",
            focus_node_id: "symbol:fn0",
            node_id: "symbol:fn0",
            exactness: "snapshot",
            path: "src/file-0.py",
            line: 1,
            end_line: 2,
            symbol: "fn_0",
            target_ids: ["target-0"],
          }, "fn_0")}
        >
          Ask mapped node
        </button>
      </div>
    );
  },
}));

const DEVELOPMENT = {
  review_id: "review-1",
  revision_id: "rrv-1",
  revision_number: 0,
  historical: false,
  live: true,
  cursor: "3",
  snapshot: {
    scope: "snapshot", source: "events", available: true, headline: "Auth now rejects expired sessions.", intent: "Harden session expiry",
    plan: null,
    contributors: [{
      key: "claude:s1", host: "claude", session_id: "s1", model: "claude-opus", purpose: "implement", parent_session_id: "",
      role: "author", paths: ["src/file-0.py"], path_count: 1, edit_count: 2, checks: { pass: 1, fail: 0, pending: 0 },
      first_at: "", last_at: "", status: "", task: "", intent: "", latest_message: "", active: false, evidence: "events",
    }],
    counts: { authors: 1, verifiers: 0, investigators: 0, files: 1, decisions: 0, open_items: 0 },
    decisions: [], open_items: [], verification: { pass: 1, fail: 0, pending: 0, checks: [] },
    changed_paths: [], unattributed_paths: [], feedback: [], latest_event: null, event_count: 3, cursor: 3,
  },
  since: null,
  reason: "",
};

const api = vi.hoisted(() => ({
  adoptBootstrapFragment: vi.fn(),
  deliverFeedback: vi.fn(),
  fetchOverview: vi.fn(),
  fetchTargets: vi.fn(),
  fetchAnnotations: vi.fn(),
  fetchReviewEvidence: vi.fn(),
  fetchReviewReadiness: vi.fn(),
  armReviewLanding: vi.fn(),
  cancelReviewLanding: vi.fn(),
  fetchReviewVerification: vi.fn(),
  fetchReviewDependencies: vi.fn(),
  fetchReviewJournal: vi.fn(),
  fetchReviewDevelopment: vi.fn(),
  fetchHistoricalReviewDevelopment: vi.fn(),
  fetchReviewAsk: vi.fn(),
  postReviewAsk: vi.fn(),
  sendReviewAskHandoff: vi.fn(),
  fetchReviewLiveState: vi.fn(),
  fetchReviewOutcome: vi.fn(),
  fetchReviewPreparation: vi.fn(),
  fetchReviewPatch: vi.fn(),
  fetchRevisions: vi.fn(),
  fetchRevisionComparison: vi.fn(),
  fetchReviewSurfaces: vi.fn(),
  runReviewSurface: vi.fn(),
  fetchFile: vi.fn(),
  fetchFinishReview: vi.fn(),
  fetchSourceComparison: vi.fn(),
  fetchSourceState: vi.fn(),
  fetchChangeProposals: vi.fn(),
  fetchProposalSelection: vi.fn(),
  createChangeProposal: vi.fn(),
  applyChangeProposal: vi.fn(),
  postBulkReviewed: vi.fn(),
  postMark: vi.fn(),
  postRefresh: vi.fn(),
  postReviewOutcome: vi.fn(),
  postAnnotation: vi.fn(),
  patchAnnotation: vi.fn(),
  fetchRelatedSource: vi.fn(),
  finishReview: vi.fn(),
  exportFeedback: vi.fn(),
  uploadReviewEvidence: vi.fn(),
  linkReviewEvidence: vi.fn(),
}));

vi.mock("./reviewApi", () => ({
  adoptBootstrapFragment: api.adoptBootstrapFragment,
  selectReviewId: vi.fn(),
  reviewDirectoryHref: () => "/reviews#t=t",
  reviewHref: (reviewId: string) => `/r/${reviewId}`,
  historicalReviewHref: (reviewId: string, revisionNumber: number) => `/r/${reviewId}?base=${revisionNumber}&target=${revisionNumber}`,
  reviewCompareHref: (reviewId: string, base: number, target: number) => `/r/${reviewId}?base=${base}&target=${target}`,
  sourceCompareHref: (fromRef: string, toRef: string) => `/r/x/${encodeURIComponent(fromRef)}..${encodeURIComponent(toRef)}`,
  deliverFeedback: api.deliverFeedback,
  fetchOverview: api.fetchOverview,
  fetchTargets: api.fetchTargets,
  fetchAnnotations: api.fetchAnnotations,
  fetchReviewEvidence: api.fetchReviewEvidence,
  fetchReviewReadiness: api.fetchReviewReadiness,
  armReviewLanding: api.armReviewLanding,
  cancelReviewLanding: api.cancelReviewLanding,
  fetchReviewVerification: api.fetchReviewVerification,
  fetchReviewDependencies: api.fetchReviewDependencies,
  fetchReviewJournal: api.fetchReviewJournal,
  fetchReviewDevelopment: api.fetchReviewDevelopment,
  fetchHistoricalReviewDevelopment: api.fetchHistoricalReviewDevelopment,
  fetchReviewDevelopmentEvents: vi.fn(async () => ({ events: [], next_before: null })),
  fetchReviewAsk: api.fetchReviewAsk,
  postReviewAsk: api.postReviewAsk,
  sendReviewAskHandoff: api.sendReviewAskHandoff,
  fetchReviewLiveState: api.fetchReviewLiveState,
  fetchReviewOutcome: api.fetchReviewOutcome,
  fetchReviewPreparation: api.fetchReviewPreparation,
  fetchReviewPatch: api.fetchReviewPatch,
  fetchRevisions: api.fetchRevisions,
  fetchRevisionComparison: api.fetchRevisionComparison,
  fetchReviewSurfaces: api.fetchReviewSurfaces,
  runReviewSurface: api.runReviewSurface,
  fetchFile: api.fetchFile,
  fetchFinishReview: api.fetchFinishReview,
  fetchSourceComparison: api.fetchSourceComparison,
  fetchSourceState: api.fetchSourceState,
  fetchChangeProposals: api.fetchChangeProposals,
  fetchProposalSelection: api.fetchProposalSelection,
  createChangeProposal: api.createChangeProposal,
  applyChangeProposal: api.applyChangeProposal,
  postBulkReviewed: api.postBulkReviewed,
  postMark: api.postMark,
  postRefresh: api.postRefresh,
  postReviewOutcome: api.postReviewOutcome,
  postAnnotation: api.postAnnotation,
  patchAnnotation: api.patchAnnotation,
  fetchRelatedSource: api.fetchRelatedSource,
  finishReview: api.finishReview,
  exportFeedback: api.exportFeedback,
  uploadReviewEvidence: api.uploadReviewEvidence,
  linkReviewEvidence: api.linkReviewEvidence,
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

function makeTarget(index: number): ReviewTarget {
  const fileIndex = Math.floor(index / 10);
  const path = `src/file-${fileIndex}.py`;
  return {
    target_id: `target-${index}`,
    unit_key: `sym-${index}`,
    kind: "symbol",
    path,
    label: `${path}::fn_${index}`,
    symbol: `fn_${index}`,
    start_line: index + 1,
    end_line: index + 2,
    hunk_ordinals: [index],
    spans: [{ side: "new", start_line: index + 1, end_line: index + 1, hunk_ordinal: index }],
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: index + 1,
    attention_level: index < 3 ? "high" : "normal",
    reasons: index < 3 ? ["public contract changed"] : ["+1 -0"],
    additions: 1,
    deletions: 0,
    fingerprint_method: "symbol_body_sha256",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
  };
}

const TARGETS = Array.from({ length: 30 }, (_, index) => makeTarget(index));

function deltaRef(target: ReviewTarget): RevisionTargetRef {
  const { target_id, unit_key, kind, path, label, symbol, start_line, state, annotation_counts } = target;
  return { target_id, unit_key, kind, path, label, symbol, start_line, state, annotation_counts };
}

function changeProposal(overrides: Partial<ReviewChangeProposal> = {}): ReviewChangeProposal {
  return {
    id: "rcp-1",
    review_id: "review-1",
    base_revision_id: "rrv-1",
    base_revision_number: 0,
    path: "src/file-0.py",
    start_line: 1,
    end_line: 1,
    original_text: "old_line\n",
    replacement_text: "new_line\n",
    patch: "--- a/src/file-0.py\n+++ b/src/file-0.py\n@@ -1 +1 @@\n-old_line\n+new_line\n",
    state: "proposed",
    target_unit_key: "sym-0",
    annotation_id: "",
    intent: "Use the corrected value.",
    conflict_reason: "",
    created_by: "local",
    created_at: "2026-09-22T17:00:00Z",
    updated_at: "2026-09-22T17:00:00Z",
    applied_at: "",
    result_revision_id: "",
    result_target_ids: [],
    can_apply: true,
    ...overrides,
  };
}

function outline(): ReviewOutlineItem[] {
  return [0, 1, 2].map((index) => ({
    path: `src/file-${index}.py`,
    target_count: 10,
    reviewed: 0,
    changed_since_review: 0,
    needs_changes: 0,
    unreviewed: 10,
    unknown: 0,
    mechanical: 0,
    attention_rank: index * 10 + 1,
    reasons: index === 0 ? ["public contract changed"] : ["+10 -0"],
  }));
}

const OVERVIEW: ReviewOverview = {
  session: {
    id: "review-1",
    ref: "r/review-1",
    title: "working tree",
    subject_type: "local_change",
    range_mode: "working_tree",
    source_ref: "",
    status: "open",
    actor_type: "agent",
    reviewer_id: "local",
    repo_root: "/repo",
    updated_at: "",
  },
  revision: {
    id: "rrv-1",
    ref: "rr/rrv-1",
    revision_number: 0,
    range_mode: "working_tree",
    base_sha: "abc",
    head_sha: "",
    dirty: true,
    degraded: [],
    provenance_host: "claude",
    provenance_model: "",
    provenance_session_id: "",
    provenance_certainty: "none",
    created_at: "",
  },
  revision_count: 1,
  packet_available: true,
  stats: {},
  title: "working tree",
  change_story: ["Review reader", "target progress", "continuous diff"],
  provenance: {
    status: "unknown",
    host: "",
    model: "",
    session_id: "",
    task: "",
    certainty: "none",
    match_confidence: 0,
    match_reason: "",
    commands_run: [],
    subagents: [],
    reads_recorded: false,
    inspected: null,
    uninspected_impacted: [],
  },
  evidence: [],
  degraded: [],
  groups: [],
  group_counts: {
    needs_attention: 0,
    changed_since_my_review: 0,
    unreviewed: 3,
    reviewed: 0,
    mechanical: 0,
  },
  unit_count: 63,
  target_count: 30,
  progress: {
    target_count: 30,
    reviewed: 0,
    changed_since_review: 0,
    needs_changes: 0,
    unreviewed: 30,
    unknown: 0,
    mechanical: 0,
  },
  outline: outline(),
  mark_count: 0,
  discarded: [],
};
const TARGET_LIST: ReviewTargetList = {
  revision_id: "rrv-1",
  order: "recommended",
  targets: TARGETS,
  progress: OVERVIEW.progress!,
  outline: outline(),
};

function smallManyFileReviewFixture(fileCount = 20): { overview: ReviewOverview; targets: ReviewTargetList } {
  const targets = Array.from({ length: fileCount }, (_, index) => {
    const base = makeTarget(index);
    const path = `src/small-${index}.py`;
    return {
      ...base,
      path,
      label: `${path}::fn_${index}`,
      symbol: `fn_${index}`,
      attention_rank: index + 1,
    };
  });
  const rows: ReviewOutlineItem[] = targets.map((target, index) => ({
    path: target.path,
    target_count: 1,
    reviewed: 0,
    changed_since_review: 0,
    needs_changes: 0,
    unreviewed: 1,
    unknown: 0,
    mechanical: 0,
    attention_rank: index + 1,
    reasons: target.reasons,
  }));
  const progress = {
    target_count: fileCount,
    reviewed: 0,
    changed_since_review: 0,
    needs_changes: 0,
    unreviewed: fileCount,
    unknown: 0,
    mechanical: 0,
  };
  return {
    overview: { ...OVERVIEW, unit_count: fileCount * 2, target_count: fileCount, progress, outline: rows },
    targets: { revision_id: OVERVIEW.revision.id, order: "recommended", targets, progress, outline: rows },
  };
}

function largeReviewFixture(): { overview: ReviewOverview; targets: ReviewTargetList } {
  const targets = Array.from({ length: 1500 }, (_, index) => {
    const fileIndex = Math.floor(index / 3);
    const path = `src/large-${fileIndex}.py`;
    const base = makeTarget(index);
    return {
      ...base,
      target_id: `large-target-${index}`,
      unit_key: `large-sym-${index}`,
      path,
      label: `${path}::fn_${index}`,
      symbol: `fn_${index}`,
      start_line: (index % 3) + 1,
      end_line: (index % 3) + 2,
      spans: [{ side: "new" as const, start_line: (index % 3) + 1, end_line: (index % 3) + 1, hunk_ordinal: 0 }],
      attention_rank: index + 1,
    };
  });
  const rows: ReviewOutlineItem[] = Array.from({ length: 500 }, (_, index) => ({
    path: `src/large-${index}.py`,
    target_count: 3,
    reviewed: 0,
    changed_since_review: 0,
    needs_changes: 0,
    unreviewed: 3,
    unknown: 0,
    mechanical: 0,
    attention_rank: index + 1,
    reasons: index < 10 ? ["public contract changed"] : ["+3 -0"],
  }));
  const progress = {
    target_count: 1500,
    reviewed: 0,
    changed_since_review: 0,
    needs_changes: 0,
    unreviewed: 1500,
    unknown: 0,
    mechanical: 0,
  };
  return {
    overview: { ...OVERVIEW, unit_count: 3200, target_count: 1500, progress, outline: rows },
    targets: { revision_id: "rrv-large", order: "recommended", targets, progress, outline: rows },
  };
}

function handoffAnnotation(id = "ann-1", addressed = false) {
  return {
    id,
    review_id: "review-1",
    revision_id: "rrv-1",
    parent_id: "",
    kind: "request_change",
    state: "open",
    body: "Keep invalid sessions visible",
    created_by: "local",
    created_by_actor: "human",
    source: "human",
    source_id: "local",
    title: "",
    evidence: [],
    confidence: null,
    author_response: addressed ? "addressed" : "none",
    author_response_source_id: addressed ? "session-1" : "",
    author_response_at: addressed ? "2026-09-23T15:00:00+00:00" : "",
    is_human_judgment: true,
    created_at: "2026-09-23T14:59:00+00:00",
    updated_at: addressed ? "2026-09-23T15:00:00+00:00" : "2026-09-23T14:59:00+00:00",
    anchor_method: "identical_blob",
    anchor_method_label: "file unchanged since the comment",
    anchor_exact: true,
    anchor_detail: "",
    anchored: true,
    path: "src/file-0.py",
    side: "new",
    start_line: 1,
    end_line: 1,
    file_level: false,
    unit_key: "sym-0",
    symbol: "",
    origin_symbol: "",
  };
}


const ADVANCED_OVERVIEW: ReviewOverview = {
  ...OVERVIEW,
  revision: { ...OVERVIEW.revision, id: "rrv-2", revision_number: 1 },
  revision_count: 2,
};

/** Refresh onto a new revision whose dependent target read dies, leaving the reader stranded. */
async function strandReaderOnAdvancedRevision() {
  api.postRefresh.mockResolvedValueOnce(ADVANCED_OVERVIEW);
  api.fetchTargets
    .mockResolvedValueOnce(TARGET_LIST)
    .mockRejectedValueOnce(new Error("gateway restarted"));

  render(<ReviewReader />);
  expect(await screen.findByText("30 streamed targets")).toBeTruthy();

  await userEvent.click(screen.getByLabelText("Review actions"));
  await userEvent.click(screen.getByRole("button", { name: "Update from current source" }));
  expect(await screen.findByText("Snapshot updated elsewhere")).toBeTruthy();
}

beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
  window.localStorage.clear();
  window.history.replaceState(null, "", "/review");
  document.documentElement.classList.remove("light", "dark");
  stream.lastProps = null;
  drawer.lastProps = null;
  mapSheet.lastProps = null;
  api.adoptBootstrapFragment.mockReturnValue({ token: "t", reviewId: "review-1" });
  api.fetchReviewPreparation.mockResolvedValue({ state: "ready", detail: "" });
  api.fetchOverview.mockResolvedValue(OVERVIEW);
  api.fetchTargets.mockResolvedValue(TARGET_LIST);
  api.fetchRevisions.mockResolvedValue({
    revisions: [{ ...OVERVIEW.revision, is_latest: true }],
    latest_revision_number: OVERVIEW.revision.revision_number,
    last_reviewed_revision_number: null,
    last_commented_revision_number: null,
    default_base_revision_number: OVERVIEW.revision.revision_number,
    default_target_revision_number: OVERVIEW.revision.revision_number,
  });
  api.fetchRevisionComparison.mockResolvedValue({
    review_id: OVERVIEW.session.id,
    reviewer_id: OVERVIEW.session.reviewer_id,
    from_revision: OVERVIEW.revision,
    to_revision: OVERVIEW.revision,
    suggested_from_revision_number: OVERVIEW.revision.revision_number,
    revision_state: {
      latest_revision_number: OVERVIEW.revision.revision_number,
      last_reviewed_revision_number: null,
      last_commented_revision_number: null,
      default_base_revision_number: OVERVIEW.revision.revision_number,
      default_target_revision_number: OVERVIEW.revision.revision_number,
    },
    annotations: [],
    annotation_groups: { originating: [], inherited: [], addressed_by_target: [] },
    summary: { added: 0, changed: 0, removed: 0, unchanged: 0 },
    files: [],
    code_available: true,
    code_unavailable_reason: "",
    code_summary: { files: 0, added_lines: 0, removed_lines: 0 },
    code_files: [],
    units: [],
  });
  api.fetchAnnotations.mockResolvedValue({ revision_id: "rrv-1", annotations: [], counts: {} });
  api.fetchReviewEvidence.mockResolvedValue({ revision_id: "rrv-1", evidence: [] });
  api.fetchReviewReadiness.mockResolvedValue({
    review_id: "review-1", snapshot_id: "rrv-1", snapshot_number: 0, tree_fingerprint: "tree-1", source_fingerprint: "source-1",
    conditions: [], ready: true, armable: true, blocker_count: 0, local_blocker_count: 0, conditions_hash: "conditions-1", work_items: [],
    landing: { supported: false, provider: "", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "" },
  });
  api.armReviewLanding.mockImplementation(async () => api.fetchReviewReadiness());
  api.cancelReviewLanding.mockImplementation(async () => api.fetchReviewReadiness());
  api.fetchReviewVerification.mockResolvedValue({
    review_id: "review-1",
    snapshot_id: "rrv-1",
    snapshot_number: 0,
    checks: [],
    summary: { pass: 0, fail: 0, not_run: 0, unknown: 0, stale: 0 },
    artifacts: [],
  });
  api.fetchReviewDependencies.mockResolvedValue({
    review_id: "review-1",
    snapshot_id: "rrv-1",
    snapshot_number: 0,
    supported: false,
    lockfiles: [],
    changes: [],
    summary: { semantic_changes: 0, direct_changes: 0, transitive_changes: 0, added: 0, removed: 0, version_changed: 0, source_changed: 0, integrity_changed: 0 },
    supply_chain: { sboms: [], provenance: [], vulnerability_scans: [] },
    degraded: [],
  });
  api.fetchReviewAsk.mockResolvedValue({
    review_id: OVERVIEW.session.id,
    latest_revision_number: OVERVIEW.revision.revision_number,
    messages: [],
  });
  api.fetchReviewJournal.mockResolvedValue({
    available: true,
    reason: "",
    review_id: "review-1",
    revision_id: "rrv-1",
    revision_number: 0,
    session_id: "session-1",
    host: "claude",
    entries: [],
  });
  api.fetchReviewDevelopment.mockResolvedValue(DEVELOPMENT);
  api.fetchReviewLiveState.mockImplementation(() => new Promise(() => {}));
  api.fetchReviewOutcome.mockResolvedValue({
    review_id: "review-1",
    revision_id: "rrv-1",
    reviewer_id: "local",
    current: null,
    history: [],
  });
  api.postReviewOutcome.mockImplementation(async (_id: string, outcome: string, summary: string) => ({
    review_id: "review-1",
    revision_id: "rrv-1",
    reviewer_id: "local",
    current: {
      id: "rot-1",
      review_id: "review-1",
      revision_id: "rrv-1",
      reviewer_id: "local",
      outcome,
      summary,
      created_at: "2026-09-18T00:00:00Z",
      stale: false,
    },
    history: [],
  }));
  api.fetchReviewSurfaces.mockResolvedValue({ revision_id: "rrv-1", surfaces: [] });
  api.fetchChangeProposals.mockResolvedValue({ revision_id: "rrv-1", proposals: [], source_mutation_supported: true });
  api.runReviewSurface.mockImplementation(async (_reviewId: string, provider: string, surfaceId: string, side: "old" | "new") => ({
    revision_id: "rrv-1",
    result: {
      surface_id: surfaceId,
      provider,
      revision_id: "rrv-1",
      side,
      status: "passed",
      summary: "check passed",
      runner: "docker-compose",
      runtime: "app-stack",
      exit_code: 0,
      duration_ms: 5,
      output: "",
      data: { services: ["api", "app"] },
    },
  }));
  api.fetchFile.mockImplementation(async (_id: string, path: string) => ({
    path,
    status: "modified",
    additions: 10,
    deletions: 0,
    patch: "patch",
    renderable: true,
    refusal: "",
    detail: "",
    degraded: [],
  }));
  api.fetchSourceState.mockResolvedValue({ supported: true, changed: false, fingerprint: "same", path_count: 3, paths: [], reason: "" });
  api.postAnnotation.mockResolvedValue({ annotation: {} });
  api.postBulkReviewed.mockResolvedValue({
    marked: [],
    skipped: [],
    progress: TARGET_LIST.progress,
    outline_updates: [],
    group_counts: OVERVIEW.group_counts,
  });
  api.exportFeedback.mockResolvedValue({
    markdown: "## Review feedback\n",
    open: 1,
    orphaned: 0,
    resolved: 0,
    revision_id: "rrv-1",
    feedback_hash: "feedback-hash-1",
    operation_id: "fop-1",
    annotation_versions: { "ann-1": 1 },
    delivery: {
      supported: true,
      host: "claude",
      session_id: "session-1",
      target_ref: "claude:session-1",
      label: "Claude",
      reason: "",
    },
  });
  api.deliverFeedback.mockResolvedValue({
    state: "sent",
    target_ref: "claude:session-1",
    remote_ref: "message-1",
    message: "sent",
    annotation_count: 1,
    operation_id: "fop-1",
  });
  api.postMark.mockImplementation(async (_id: string, unitKey: string, state: string) => {
    const old = TARGETS.find((target) => target.unit_key === unitKey)!;
    const target = { ...old, state };
    return {
      unit_key: unitKey,
      requested: state,
      recorded: state,
      downgraded: false,
      note: "",
      target,
      progress: { ...TARGET_LIST.progress, reviewed: state === "reviewed" ? 1 : 0, unreviewed: state === "reviewed" ? 29 : 30 },
      outline_updates: [],
      groups: [],
      group_counts: OVERVIEW.group_counts,
    };
  });
  const finishSummary = {
    status: "open" as const,
    target_count: 30,
    reviewed_targets: 0,
    unreviewed_targets: 30,
    changed_since_review: 0,
    needs_changes: 0,
    unknown_targets: 0,
    open_comments: 0,
    orphaned_comments: 0,
    failed_verification: 0,
    unresolved_verification: 0,
    previous_revision_evidence: 0,
    discarded_verdicts: 0,
  };
  api.fetchFinishReview.mockResolvedValue(finishSummary);
  api.finishReview.mockResolvedValue({ ...finishSummary, status: "finished" });
});

describe("ReviewReader R22 shell", () => {
  it("opens a 30-target review as one stream instead of one selected file", async () => {
    render(<ReviewReader />);

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(screen.getByText((_text, element) => element?.textContent === "30 review areas")).toBeTruthy();
    expect(screen.getByText((_text, element) => element?.textContent === "0 reviewed")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(3));
    expect(new Set(api.fetchFile.mock.calls.map((call) => call[1]))).toEqual(
      new Set(["src/file-0.py", "src/file-1.py", "src/file-2.py"]),
    );
  });

  it("uses the canonical public Review ref in the browser tab title", async () => {
    document.title = "Review";

    render(<ReviewReader />);

    await waitFor(() => expect(document.title).toBe("Review: r/review-1"));
  });

  it("switches back to an already-loaded revision pair without another compare request", async () => {
    const rev0 = { ...OVERVIEW.revision, revision_number: 0 };
    const rev1 = { ...OVERVIEW.revision, id: "rrv-2", ref: "rr/rrv-2", revision_number: 1 };
    const latestOverview: ReviewOverview = {
      ...OVERVIEW,
      revision: rev1,
      revision_count: 2,
      latest_revision_number: 1,
      latest_revision: rev1,
    };
    api.fetchOverview.mockResolvedValue(latestOverview);
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, revision_id: rev1.id });
    api.fetchRevisions.mockResolvedValue({
      revisions: [
        { ...rev0, is_last_reviewed: true },
        { ...rev1, is_latest: true },
      ],
      latest_revision_number: 1,
      last_reviewed_revision_number: 0,
      last_commented_revision_number: 0,
      default_base_revision_number: 0,
      default_target_revision_number: 1,
    });
    api.fetchRevisionComparison.mockImplementation(async (_id: string, base = 0, target = 1) => {
      const fromRevision = base === 0 ? rev0 : rev1;
      const toRevision = target === 0 ? rev0 : rev1;
      const changed = base !== target;
      return {
        review_id: OVERVIEW.session.id,
        reviewer_id: OVERVIEW.session.reviewer_id,
        from_revision: fromRevision,
        to_revision: toRevision,
        suggested_from_revision_number: 0,
        revision_state: {
          latest_revision_number: 1,
          last_reviewed_revision_number: 0,
          last_commented_revision_number: 0,
          default_base_revision_number: 0,
          default_target_revision_number: 1,
        },
        annotations: [],
        annotation_groups: { originating: [], inherited: [], addressed_by_target: [] },
        summary: { added: 0, changed: changed ? 1 : 0, removed: 0, unchanged: changed ? 0 : 1 },
        files: [],
        code_available: true,
        code_unavailable_reason: "",
        code_summary: { files: changed ? 1 : 0, added_lines: changed ? 1 : 0, removed_lines: changed ? 1 : 0 },
        code_files: changed ? [{
          path: "src/file-0.py",
          status: "changed",
          added_lines: 1,
          removed_lines: 1,
          patch: "--- rev-0/src/file-0.py\n+++ rev-1/src/file-0.py\n@@ -1 +1 @@\n-old\n+new\n",
        }] : [],
        units: [],
      };
    });
    window.history.replaceState(null, "", "/review?base=0&target=1");

    render(<ReviewReader />);

    expect(await screen.findByText("Comparing Snapshot 0 → Snapshot 1")).toBeTruthy();
    await waitFor(() => expect(api.fetchRevisionComparison).toHaveBeenCalledTimes(1));

    // Comparison selection now lives under Snapshot history rather than in a
    // permanent Reader selector. A shareable URL still drives the same cached
    // comparison state.
    await act(async () => {
      window.history.replaceState(null, "", "/review?base=1&target=1");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await waitFor(() => expect(api.fetchRevisionComparison).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("revision-compare-bar")).toBeNull();
    expect(screen.getByText("30 streamed targets")).toBeTruthy();

    // Restoring the already-loaded pair is a pure client-cache switch and makes
    // no third API request.
    await act(async () => {
      window.history.replaceState(null, "", "/review?base=0&target=1");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(await screen.findByText("Comparing Snapshot 0 → Snapshot 1")).toBeTruthy();
    await waitFor(() => expect(api.fetchRevisionComparison).toHaveBeenCalledTimes(2));
  });

  it("keeps the Git HEAD preset inside the same Review Reader", async () => {
    const rev0 = { ...OVERVIEW.revision, revision_number: 0 };
    const rev1 = { ...OVERVIEW.revision, id: "rrv-2", ref: "rr/rrv-2", revision_number: 1 };
    const latestOverview: ReviewOverview = {
      ...OVERVIEW,
      revision: rev1,
      revision_count: 2,
      latest_revision_number: 1,
      latest_revision: rev1,
    };
    api.fetchOverview.mockResolvedValue(latestOverview);
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, revision_id: rev1.id });
    api.fetchRevisions.mockResolvedValue({
      revisions: [
        { ...rev0, is_last_reviewed: true },
        { ...rev1, is_latest: true },
      ],
      latest_revision_number: 1,
      last_reviewed_revision_number: 0,
      last_commented_revision_number: 0,
      default_base_revision_number: 0,
      default_target_revision_number: 1,
    });
    api.fetchSourceComparison.mockResolvedValue({
      from: { ref: "git/HEAD", label: "Git HEAD", kind: "git" },
      to: { ref: rev1.ref, label: "rev 1", kind: "review_revision" },
      summary: { files: 1, additions: 1, deletions: 1 },
      files: [{
        path: "src/file-0.py",
        old_path: "src/file-0.py",
        status: "modified",
        additions: 1,
        deletions: 1,
        patch: "--- HEAD/src/file-0.py\n+++ rev-1/src/file-0.py\n@@ -1 +1 @@\n-old\n+new\n",
        renderable: true,
        refusal: "",
        detail: "",
      }],
    });
    window.history.replaceState(null, "", "/review?base=head&target=1");

    render(<ReviewReader />);

    expect(await screen.findByText("Comparing Source → Snapshot 1")).toBeTruthy();
    await waitFor(() => expect(api.fetchSourceComparison).toHaveBeenCalledWith(
      "git/HEAD",
      rev1.ref,
      "r/review-1",
    ));
    expect(api.fetchRevisionComparison).not.toHaveBeenCalled();
    expect(screen.queryByRole("combobox", { name: /Compare .* revision/i })).toBeNull();
    expect(stream.lastProps.comparisonMode).toBe(false);
    expect(stream.lastProps.targets.some((target: ReviewTarget) => target.path === "src/file-0.py")).toBe(true);
  });

  it("uses the same Ask drawer for whole-review and target-scoped questions", async () => {
    render(<ReviewReader />);

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Ask about this review" }));
    expect(await screen.findByText("Ask, challenge, or request a change")).toBeTruthy();
    expect(api.fetchReviewAsk).toHaveBeenCalledWith("review-1");

    await userEvent.click(screen.getByRole("button", { name: "Close Ask" }));
    expect(screen.queryByText("Ask, challenge, or request a change")).toBeNull();

    act(() => stream.lastProps.onAsk(TARGETS[0]));
    expect(await screen.findByText("src/file-0.py · fn_0 · line 1")).toBeTruthy();
    expect(screen.getByText("Ask, challenge, or request a change")).toBeTruthy();
  });

  it("offers a retry when the initial review load fails", async () => {
    api.fetchOverview.mockRejectedValueOnce(new Error("gateway offline"));

    render(<ReviewReader />);

    expect(await screen.findByText("Review couldn't load")).toBeTruthy();
    expect(screen.getByText("gateway offline")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(api.fetchOverview).toHaveBeenCalledTimes(2);
  });

  it("waits for a real diff before showing the Reader shell, then opens automatically", async () => {
    const pending = new Map([
      ["src/file-0.py", deferred<FileDetail>()],
      ["src/file-1.py", deferred<FileDetail>()],
      ["src/file-2.py", deferred<FileDetail>()],
    ]);
    api.fetchFile.mockImplementation(async (_id: string, path: string) => pending.get(path)!.promise);

    render(<ReviewReader />);

    await screen.findByText("Preparing first visual diff");
    const loadingScreen = screen.getByTestId("review-loading-screen");
    expect(loadingScreen).toHaveClass("fixed", "inset-0", "h-dvh", "overflow-hidden", "overscroll-none");
    expect(loadingScreen).not.toHaveClass("h-screen", "min-h-screen");
    expect(screen.getByText("LemonCrow Review")).toBeTruthy();
    expect(screen.getByText("Snapshot")).toBeTruthy();
    expect(screen.getByText("Structure")).toBeTruthy();
    expect(screen.getByText("First diff")).toBeTruthy();
    expect(document.querySelector(".animate-pulse")).toBeNull();
    expect(screen.queryByTestId("reader-stream-mock")).toBeNull();

    await act(async () => {
      pending.get("src/file-1.py")!.resolve({
        path: "src/file-1.py",
        status: "modified",
        additions: 10,
        deletions: 0,
        patch: "patch",
        renderable: true,
        refusal: "",
        detail: "",
        degraded: [],
      });
      await Promise.resolve();
    });

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(stream.lastProps.pendingVisibleFileCount).toBe(2);

    await act(async () => {
      for (const path of ["src/file-0.py", "src/file-2.py"]) {
        pending.get(path)!.resolve({
          path,
          status: "modified",
          additions: 10,
          deletions: 0,
          patch: "patch",
          renderable: true,
          refusal: "",
          detail: "",
          degraded: [],
        });
      }
      await Promise.resolve();
    });
  });

  it("opens source while impact enrichment is still running in the background", async () => {
    const preparation = deferred<any>();
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockResolvedValueOnce({ ...TARGET_LIST, targets: [...TARGETS].reverse() });
    api.adoptBootstrapFragment.mockReturnValueOnce({
      token: "t",
      reviewId: "review-1",
      preparationId: "prepare-1",
      sourceReady: true,
    });
    api.fetchReviewPreparation
      .mockReturnValueOnce(preparation.promise)
      .mockResolvedValue({ state: "ready", detail: "" });

    render(<ReviewReader />);

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(api.fetchOverview).toHaveBeenCalledTimes(1);
    // The preparation status request is still unresolved: source-ready bootstrap
    // must never hold the first diff behind this extra round trip.
    expect(api.fetchReviewPreparation).toHaveBeenCalledWith("review-1", "prepare-1");

    await act(async () => {
      preparation.resolve({ state: "impact_background", detail: "" });
      await Promise.resolve();
    });

    expect(screen.getByText(/analyzing impact in background · source is ready to review/i)).toBeTruthy();
    expect(api.fetchOverview).toHaveBeenCalledTimes(1);

    await waitFor(() => expect(api.fetchReviewPreparation.mock.calls.length).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(api.fetchOverview.mock.calls.length).toBeGreaterThanOrEqual(2));
    // Late impact enrichment can update ranks/reasons, but it must not move the
    // source the reviewer already started reading.
    expect(stream.lastProps.targets[0].target_id).toBe("target-0");
  });

  it("shows the source review while comments, evidence, and surface discovery are still pending", async () => {
    const annotations = deferred<any>();
    const evidence = deferred<any>();
    const surfaces = deferred<any>();
    api.fetchAnnotations.mockReturnValueOnce(annotations.promise);
    api.fetchReviewEvidence.mockReturnValueOnce(evidence.promise);
    api.fetchReviewSurfaces.mockReturnValueOnce(surfaces.promise);

    render(<ReviewReader />);

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledWith("review-1", "src/file-0.py"));
    await waitFor(() => expect(api.fetchReviewEvidence).toHaveBeenCalledWith("review-1", "rrv-1"));
    await waitFor(() => expect(api.fetchReviewSurfaces).toHaveBeenCalledWith("review-1", "rrv-1"));
    expect(stream.lastProps.surfaces).toEqual([]);

    await act(async () => {
      annotations.resolve({ revision_id: "rrv-1", annotations: [], counts: {} });
      evidence.resolve({ revision_id: "rrv-1", evidence: [] });
      surfaces.resolve({ revision_id: "rrv-1", surfaces: [] });
      await Promise.resolve();
    });
  });

  it("keeps source review usable but never turns failed auxiliary state into empty state", async () => {
    api.fetchAnnotations.mockRejectedValueOnce(new Error("comments offline"));
    api.fetchReviewEvidence.mockRejectedValueOnce(new Error("evidence offline"));
    api.fetchReviewOutcome.mockRejectedValueOnce(new Error("outcome offline"));
    api.fetchReviewSurfaces.mockRejectedValueOnce(new Error("surface offline"));

    render(<ReviewReader />);

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(await screen.findByText("Discussion unavailable")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Review comments: unavailable" })).toBeTruthy();
    // Landing readiness is server-authoritative and can remain ready even if
    // optional Reader panels fail to fetch. The panel failures must stay
    // visible rather than being collapsed into empty state.
    expect(screen.getByRole("button", { name: /Review readiness:/i }).getAttribute("aria-label")).toContain("Ready to land");
    expect(await screen.findByText(/Review discussions unavailable: comments offline/)).toBeTruthy();
    expect(screen.getByText(/Review evidence unavailable: evidence offline/)).toBeTruthy();
    expect(screen.getByText(/Review outcome unavailable: outcome offline/)).toBeTruthy();
    expect(screen.getByText(/Rendered review surfaces unavailable: surface offline/)).toBeTruthy();
  });

  it("arms provider landing only with the exact readiness snapshot and condition hash", async () => {
    const landing = {
      review_id: "review-1", snapshot_id: "rrv-1", snapshot_number: 0, tree_fingerprint: "tree-1", source_fingerprint: "source-1",
      conditions: [{ id: "checks", label: "Required checks", state: "pending", detail: "GitHub waiting on CI", owner: "scm", category: "scm", url: "" }],
      ready: false, armable: true, blocker_count: 1, local_blocker_count: 0, conditions_hash: "conditions-1", work_items: [],
      landing: { supported: true, provider: "github", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "https://github.example/pr/1", mechanism: "" },
    };
    api.fetchReviewReadiness.mockResolvedValue(landing);
    api.armReviewLanding.mockResolvedValueOnce({
      ...landing,
      landing: { ...landing.landing, state: "delegated", armed: true, revision_id: "rrv-1", tree_fingerprint: "tree-1", conditions_hash: "conditions-1", mechanism: "auto_merge_or_merge_queue" },
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: /Review readiness:/i }));
    expect(await screen.findByText("Not ready · 1 condition")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Arm Merge when ready" }));

    await waitFor(() => expect(api.armReviewLanding).toHaveBeenCalledWith("review-1", {
      expected_revision_id: "rrv-1",
      expected_tree_fingerprint: "tree-1",
      expected_conditions_hash: "conditions-1",
    }));
    expect(await screen.findByText("armed")).toBeTruthy();
  });

  it("does not list a human-waived condition among the landing blockers", async () => {
    api.fetchReviewReadiness.mockResolvedValue({
      review_id: "review-1", snapshot_id: "rrv-1", snapshot_number: 0, tree_fingerprint: "tree-1", source_fingerprint: "source-1",
      conditions: [
        { id: "verification", label: "Verification waived", state: "waived", detail: "1 check(s) waived by a human.", owner: "lemoncrow", category: "verification", url: "" },
        { id: "checks", label: "Required checks", state: "pending", detail: "GitHub waiting on CI", owner: "scm", category: "scm", url: "" },
      ],
      ready: false, armable: true, blocker_count: 1, local_blocker_count: 0, conditions_hash: "conditions-1", work_items: [],
      landing: { supported: true, provider: "github", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "" },
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Review readiness:/i }).getAttribute("title")).toBe("Required checks");
    });
  });

  it("fails readiness closed when the server-authoritative landing projection is unavailable", async () => {
    api.fetchReviewReadiness.mockRejectedValue(new Error("readiness offline"));

    render(<ReviewReader />);

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => {
      const readinessButton = screen.getByRole("button", { name: /Review readiness:/i });
      expect(readinessButton.getAttribute("aria-label")).toContain("Landing readiness unavailable");
    });
    await userEvent.click(screen.getByRole("button", { name: /Review readiness:/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Landing readiness unavailable: readiness offline");
    expect(screen.queryByRole("button", { name: "Arm Merge when ready" })).toBeNull();
  });

  it("automatically validates Docker services independently", async () => {
    const surfaces = ["api", "app"].map((service) => ({
      id: `app-stack:${service}`,
      provider: "service",
      kind: "service",
      title: service,
      locator: service,
      runtime: "app-stack",
      affected_paths: ["src/file-0.py"],
      capabilities: ["source", "execute", "results", "compare"],
      metadata: { surface_id: "app-stack", service, runner: "docker-compose" },
    }));
    api.fetchReviewSurfaces.mockResolvedValueOnce({ revision_id: "rrv-1", surfaces });

    render(<ReviewReader />);

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.runReviewSurface).toHaveBeenCalledTimes(2));
    expect(api.runReviewSurface).toHaveBeenCalledWith("review-1", "service", "app-stack:api", "new", "rrv-1");
    expect(api.runReviewSurface).toHaveBeenCalledWith("review-1", "service", "app-stack:app", "new", "rrv-1");
    await waitFor(() => {
      expect(stream.lastProps.surfaceRuns["service:app-stack:api"]?.new?.status).toBe("passed");
      expect(stream.lastProps.surfaceRuns["service:app-stack:app"]?.new?.status).toBe("passed");
    });
  });

  it("does not automatically execute mutating or parameterized API requests", async () => {
    api.fetchReviewSurfaces.mockResolvedValueOnce({
      revision_id: "rrv-1",
      surfaces: [
        {
          id: "backend/bruno/Mutate.bru",
          provider: "bruno",
          kind: "api.request",
          title: "Mutate",
          locator: "POST {{baseUrl}}/items",
          runtime: "api-runtime",
          affected_paths: ["src/file-0.py"],
          capabilities: ["source", "execute", "results", "compare"],
          metadata: { method: "POST", url: "{{baseUrl}}/items", runner: "http-service", surface_id: "api" },
        },
        {
          id: "backend/bruno/Get Item.bru",
          provider: "bruno",
          kind: "api.request",
          title: "Get Item",
          locator: "GET {{baseUrl}}/items/:id",
          runtime: "api-runtime",
          affected_paths: ["src/file-0.py"],
          capabilities: ["source", "execute", "results", "compare"],
          metadata: { method: "GET", url: "{{baseUrl}}/items/:id", runner: "http-service", surface_id: "api" },
        },
      ],
    });

    render(<ReviewReader />);

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchReviewSurfaces).toHaveBeenCalledTimes(1));
    await act(async () => { await Promise.resolve(); });
    expect(api.runReviewSurface).not.toHaveBeenCalled();
  });

  it("opens a dedicated surface even when it has no source target in the current target list", async () => {
    window.history.replaceState(
      null,
      "",
      "/review?review-item=surface&review-provider=web&review-surface=%2Fstandalone#t=token&r=review-1",
    );
    api.fetchReviewSurfaces.mockResolvedValueOnce({
      revision_id: "rrv-1",
      surfaces: [{
        id: "/standalone",
        provider: "web",
        kind: "web.route",
        title: "/standalone",
        locator: "/standalone",
        runtime: "",
        affected_paths: ["src/standalone.tsx"],
        capabilities: ["preview", "compare"],
        metadata: {},
      }],
    });
    api.fetchFile.mockResolvedValueOnce({
      path: "src/standalone.tsx",
      status: "modified",
      additions: 1,
      deletions: 1,
      patch: "patch",
      renderable: true,
      refusal: "",
      detail: "",
      degraded: [],
      preview: { kind: "web", framework: "next", routes: ["/standalone"], default_route: "/standalone" },
    });

    render(<ReviewReader />);

    expect(await screen.findByText("0 streamed targets")).toBeTruthy();
    expect(stream.lastProps.dedicatedItem).toEqual({ kind: "surface", provider: "web", id: "/standalone" });
    expect(stream.lastProps.surfaces).toHaveLength(1);
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(1));
    expect(api.fetchFile).toHaveBeenCalledWith("review-1", "src/standalone.tsx");
  });

it("caps first-diff preparation by file count even when a small review has many files", async () => {
    const small = smallManyFileReviewFixture();
    api.fetchOverview.mockResolvedValueOnce(small.overview);
    api.fetchTargets.mockResolvedValueOnce(small.targets);
    api.fetchFile.mockImplementation(() => new Promise<FileDetail>(() => {}));

    render(<ReviewReader />);

    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(5));
    expect(new Set(api.fetchFile.mock.calls.map((call) => call[1]))).toEqual(
      new Set(Array.from({ length: 5 }, (_, index) => `src/small-${index}.py`)),
    );
  });

it("prepares a rendered-surface placement path in the background without expanding the visible diff page", async () => {
    const large = largeReviewFixture();
    api.fetchOverview.mockResolvedValueOnce(large.overview);
    api.fetchTargets.mockResolvedValueOnce(large.targets);
    api.fetchReviewSurfaces.mockResolvedValueOnce({
      revision_id: "rrv-large",
      surfaces: [{
        id: "frontend:/",
        provider: "web",
        kind: "web.route",
        title: "/",
        locator: "/",
        runtime: "",
        affected_paths: ["src/large-499.py"],
        capabilities: ["preview", "compare"],
        metadata: { framework: "vite", root: "frontend" },
      }],
    });

    render(<ReviewReader />);

    expect(await screen.findByText("1500 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledWith("review-1", "src/large-499.py"));
    for (let index = 0; index < 5; index += 1) {
      expect(api.fetchFile).toHaveBeenCalledWith("review-1", `src/large-${index}.py`);
    }
    // Prefetching never reveals hidden files: the first page is still the only
    // mounted page even after the surface placement source is prepared.
    expect(stream.lastProps.remainingFileCount).toBe(495);
  });

it("keeps a 500-file / 1,500-target review visually bounded while preparing the next page", async () => {
    const large = largeReviewFixture();
    api.fetchOverview.mockResolvedValueOnce(large.overview);
    api.fetchTargets.mockResolvedValueOnce(large.targets);

    render(<ReviewReader />);

    expect(await screen.findByText("1500 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(15));
    expect(new Set(api.fetchFile.mock.calls.map((call) => call[1]))).toEqual(
      new Set(Array.from({ length: 15 }, (_, index) => `src/large-${index}.py`)),
    );
    // 30 hidden files are warm, but Load more still owns visibility.
    expect(stream.lastProps.remainingFileCount).toBe(495);
  });

  it("never uses pagination to reveal files that are still being prepared", async () => {
    const large = largeReviewFixture();
    const file5 = deferred<FileDetail>();
    const file6 = deferred<FileDetail>();
    api.fetchOverview.mockResolvedValueOnce(large.overview);
    api.fetchTargets.mockResolvedValueOnce(large.targets);
    api.fetchFile.mockImplementation(async (_id: string, path: string) => {
      if (path === "src/large-5.py") return file5.promise;
      if (path === "src/large-6.py") return file6.promise;
      return {
        path,
        status: "modified",
        additions: 10,
        deletions: 0,
        patch: "patch",
        renderable: true,
        refusal: "",
        detail: "",
        degraded: [],
      };
    });

    render(<ReviewReader />);

    expect(await screen.findByText("1500 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledWith("review-1", "src/large-5.py"));
    expect(stream.lastProps.readyHiddenFileCount).toBe(0);
    expect(stream.lastProps.preparingHiddenFileCount).toBeGreaterThan(0);

    act(() => stream.lastProps.onLoadMore());
    expect(stream.lastProps.remainingFileCount).toBe(495);
    expect(api.fetchFile.mock.calls.some((call) => call[1] === "src/large-6.py")).toBe(false);

    await act(async () => {
      file5.resolve({
        path: "src/large-5.py",
        status: "modified",
        additions: 10,
        deletions: 0,
        patch: "patch",
        renderable: true,
        refusal: "",
        detail: "",
        degraded: [],
      });
      await Promise.resolve();
    });
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledWith("review-1", "src/large-6.py"));
    await waitFor(() => expect(stream.lastProps.readyHiddenFileCount).toBe(1));

    act(() => stream.lastProps.onLoadMore());
    await waitFor(() => expect(stream.lastProps.remainingFileCount).toBe(494));
    expect(api.fetchFile.mock.calls.filter((call) => call[1] === "src/large-5.py")).toHaveLength(1);

    await act(async () => {
      file6.resolve({
        path: "src/large-6.py",
        status: "modified",
        additions: 10,
        deletions: 0,
        patch: "patch",
        renderable: true,
        refusal: "",
        detail: "",
        degraded: [],
      });
      await Promise.resolve();
    });
  });

  it("lets an explicit file click bypass a blocked background preparation queue", async () => {
    const large = largeReviewFixture();
    const background = deferred<FileDetail>();
    api.fetchOverview.mockResolvedValueOnce(large.overview);
    api.fetchTargets.mockResolvedValueOnce(large.targets);
    api.fetchFile.mockImplementation(async (_id: string, path: string) => {
      if (path === "src/large-5.py") return background.promise;
      return {
        path,
        status: "modified",
        additions: 10,
        deletions: 0,
        patch: "patch",
        renderable: true,
        refusal: "",
        detail: "",
        degraded: [],
      };
    });

    render(<ReviewReader />);

    expect(await screen.findByText("1500 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledWith("review-1", "src/large-5.py"));
    expect(api.fetchFile.mock.calls.some((call) => call[1] === "src/large-6.py")).toBe(false);

    await userEvent.click(screen.getByText("large-499.py"));

    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledWith("review-1", "src/large-499.py"));
    expect(api.fetchFile.mock.calls.some((call) => call[1] === "src/large-6.py")).toBe(false);
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].path).toBe("src/large-499.py"));

    await act(async () => {
      background.resolve({
        path: "src/large-5.py",
        status: "modified",
        additions: 10,
        deletions: 0,
        patch: "patch",
        renderable: true,
        refusal: "",
        detail: "",
        degraded: [],
      });
      await Promise.resolve();
    });
  });

  it("loads more by changed-line budget instead of a fixed file count", async () => {
    const large = largeReviewFixture();
    api.fetchOverview.mockResolvedValueOnce(large.overview);
    api.fetchTargets.mockResolvedValueOnce(large.targets);

    render(<ReviewReader />);

    expect(await screen.findByText("1500 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(15));
    expect(stream.lastProps.remainingFileCount).toBe(495);

    act(() => stream.lastProps.onLoadMore());

    // The next 10-file speculative page was already prepared, so visibility
    // changes without refetching it. Preparation then warms the following 10.
    await waitFor(() => expect(stream.lastProps.remainingFileCount).toBe(485));
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(25));
    expect(new Set(api.fetchFile.mock.calls.map((call) => call[1]))).toEqual(
      new Set(Array.from({ length: 25 }, (_, index) => `src/large-${index}.py`)),
    );
  });

  it("lets one very large file consume the next change budget by itself", async () => {
    const large = largeReviewFixture();
    const targets = large.targets.targets.map((target) => target.path === "src/large-5.py"
      ? { ...target, additions: 400, deletions: 0 }
      : target);
    api.fetchOverview.mockResolvedValueOnce(large.overview);
    api.fetchTargets.mockResolvedValueOnce({ ...large.targets, targets });

    render(<ReviewReader />);

    expect(await screen.findByText("1500 streamed targets")).toBeTruthy();
    // Three 400-line targets make file 5 a ~1,200-line next page, so background
    // preparation stops after that one file.
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(6));
    expect(api.fetchFile.mock.calls.at(-1)?.[1]).toBe("src/large-5.py");

    act(() => stream.lastProps.onLoadMore());

    await waitFor(() => expect(stream.lastProps.remainingFileCount).toBe(494));
    // Revealing the warm large file does not fetch it twice.
    expect(api.fetchFile.mock.calls.filter((call) => call[1] === "src/large-5.py")).toHaveLength(1);
  });

  it("r judges the current target and advances to the next target", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("r");

    await waitFor(() => expect(api.postMark).toHaveBeenCalledWith("review-1", "sym-0", "reviewed", "rrv-1"));
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1");
    expect(await screen.findByText((_text, element) => element?.textContent === "1 reviewed")).toBeTruthy();
  });
  it("review-and-advance stays on the next hunk in the same file before jumping files", async () => {
    const interleaved = [TARGETS[0], TARGETS[10], TARGETS[1]];
    api.fetchTargets.mockResolvedValueOnce({
      ...TARGET_LIST,
      targets: interleaved,
      progress: { ...TARGET_LIST.progress, target_count: 3, unreviewed: 3 },
      outline: [
        { ...outline()[0], target_count: 2, unreviewed: 2 },
        { ...outline()[1], target_count: 1, unreviewed: 1 },
      ],
    });

    render(<ReviewReader />);
    expect(await screen.findByText("3 streamed targets")).toBeTruthy();
    stream.scrollToTarget.mockClear();

    await act(async () => stream.lastProps.onMark(TARGETS[0], "reviewed", true));

    await waitFor(() => expect(api.postMark).toHaveBeenCalledWith("review-1", "sym-0", "reviewed", "rrv-1"));
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1");
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].path).toBe("src/file-0.py");
  });

  it("review-and-advance stops at the end instead of cycling to earlier targets", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    stream.scrollToTarget.mockClear();

    await act(async () => stream.lastProps.onMark(TARGETS.at(-1), "reviewed", true));

    await waitFor(() => expect(api.postMark).toHaveBeenCalledWith("review-1", "sym-29", "reviewed", "rrv-1"));
    expect(stream.scrollToTarget).not.toHaveBeenCalled();
  });

  it("] and [ move through the attention path", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("]");
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1");

    stream.scrollToTarget.mockClear();
    await userEvent.keyboard("[[");
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-0");
  });

  it("J moves between files while j remains target navigation", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("j");
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1");

    stream.scrollToTarget.mockClear();
    await userEvent.keyboard("J");
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].path).toBe("src/file-1.py");
  });

  it("switches to file order without changing target identity or refetching loaded patches", async () => {
    const fileOrdered: ReviewTargetList = { ...TARGET_LIST, order: "file", targets: [...TARGETS] };
    api.fetchTargets.mockResolvedValueOnce(TARGET_LIST).mockResolvedValueOnce(fileOrdered);

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(3));

    await userEvent.keyboard("j");
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1"));
    stream.scrollToTarget.mockClear();

    await userEvent.click(screen.getByLabelText("Review actions"));
    const order = screen.getByRole("combobox", { name: "Review order" });
    await userEvent.selectOptions(order, "file");

    await waitFor(() => expect(api.fetchTargets).toHaveBeenLastCalledWith("review-1", "file"));
    await waitFor(() => expect((screen.getByRole("combobox", { name: "Review order" }) as HTMLSelectElement).value).toBe("file"));
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1"));
    expect(api.postMark).not.toHaveBeenCalled();
    expect(api.fetchFile).toHaveBeenCalledTimes(3);
  });

  it("keeps file order when refreshing to a new revision", async () => {
    const fileOrdered: ReviewTargetList = { ...TARGET_LIST, order: "file", targets: [...TARGETS] };
    const refreshedOverview: ReviewOverview = {
      ...OVERVIEW,
      revision: { ...OVERVIEW.revision, id: "rrv-2", revision_number: 1 },
      revision_count: 2,
    };
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockResolvedValueOnce(fileOrdered)
      .mockResolvedValueOnce({ ...fileOrdered, revision_id: "rrv-2" });
    api.postRefresh.mockResolvedValueOnce(refreshedOverview);

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Review order" }), "file");
    await waitFor(() => expect(api.fetchTargets).toHaveBeenLastCalledWith("review-1", "file"));

    await userEvent.click(screen.getByRole("button", { name: "Update from current source" }));

    await waitFor(() => expect(api.postRefresh).toHaveBeenCalledWith("review-1"));
    await waitFor(() => expect(api.fetchTargets).toHaveBeenLastCalledWith("review-1", "file"));
    expect((screen.getByRole("combobox", { name: "Review order" }) as HTMLSelectElement).value).toBe("file");
  });

  it("focus mode collapses the outline and / reopens search with keyboard focus", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(screen.getByLabelText("Collapse review outline")).toBeTruthy();

    await userEvent.keyboard("f");
    expect(await screen.findByLabelText("Open review outline")).toBeTruthy();
    expect(screen.getByTestId("reader-stream-mock")).toBeTruthy();

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await waitFor(() => expect(document.activeElement).toBe(search));
    expect(screen.getByLabelText("Collapse review outline")).toBeTruthy();
  });

  it("p opens searchable review actions and executes the existing feedback flow", async () => {
    api.fetchAnnotations.mockResolvedValueOnce({
      revision_id: "rrv-1",
      annotations: [],
      counts: {},
      feedback: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
      delivery: {
        supported: true,
        host: "claude",
        session_id: "session-1",
        target_ref: "claude:session-1",
        label: "Claude",
        reason: "",
      },
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("p");
    const dialog = await screen.findByRole("dialog", { name: "Review actions" });
    const filter = within(dialog).getByRole("textbox", { name: "Filter review actions" });
    await waitFor(() => expect(document.activeElement).toBe(filter));
    await userEvent.type(filter, "send 1 to claude");
    await userEvent.keyboard("{Enter}");

    await waitFor(() => expect(api.exportFeedback).toHaveBeenCalledWith("review-1"));
    expect(screen.queryByRole("dialog", { name: "Review actions" })).toBeNull();
    expect(await screen.findByRole("region", { name: "Review feedback" })).toBeTruthy();
  });

  it("opens Review Map and routes an exact map node back to the existing target stream", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Review map" }));
    expect(await screen.findByRole("dialog", { name: "Review Map mock" })).toBeTruthy();
    expect(mapSheet.lastProps).toMatchObject({ reviewId: "review-1", revisionNumber: 0, historical: false });

    await userEvent.click(screen.getByRole("button", { name: "Open mapped target" }));
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalledWith(expect.objectContaining({ target_id: "target-0" }), "smooth"));
    expect(screen.queryByRole("dialog", { name: "Review Map mock" })).toBeNull();
  });

  it("opens the existing Change Ask drawer from Review Map context", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Review map" }));
    await userEvent.click(await screen.findByRole("button", { name: "Ask mapped node" }));

    expect(screen.queryByRole("dialog", { name: "Review Map mock" })).toBeNull();
    expect(await screen.findByText("Review Map · fn_0")).toBeTruthy();
    expect(api.fetchReviewAsk).toHaveBeenCalledWith("review-1");
    expect(mapSheet.lastProps.onAskSelection).toEqual(expect.any(Function));
  });

  it("reopens Review Map from a persisted Ask Map reference", async () => {
    const selection = {
      kind: "review_map_node",
      review_id: "review-1",
      revision_id: "rrv-1",
      level: "symbol",
      focus_node_id: "symbol:fn0",
      node_id: "symbol:fn0",
      exactness: "snapshot",
      path: "src/file-0.py",
      line: 1,
      end_line: 2,
      symbol: "fn_0",
      target_ids: ["target-0"],
    };
    api.fetchReviewAsk.mockResolvedValueOnce({
      review_id: "review-1",
      latest_revision_number: 0,
      messages: [{
        id: "ram-map-answer",
        review_id: "review-1",
        revision_id: "rrv-1",
        revision_number: 0,
        role: "assistant",
        intent: "question",
        body: "Map answer",
        scope: { target_unit_key: "", path: "src/file-0.py", symbol: "fn_0", side: "new", start_line: 1, end_line: 2 },
        context: { selection, label: "fn_0" },
        references: [{ kind: "review_map_node", label: "fn_0", map_selection: selection }],
        action: {},
        created_at: "2026-09-24T00:00:00+00:00",
      }],
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Ask about this review" }));
    await userEvent.click(await screen.findByRole("button", { name: "Map · fn_0" }));

    expect(await screen.findByRole("dialog", { name: "Review Map mock" })).toBeTruthy();
    expect(mapSheet.lastProps.initialSelection).toMatchObject({
      kind: "review_map_node",
      node_id: "symbol:fn0",
      revision_id: "rrv-1",
    });
  });

  it("opens Snapshot-exact impacted source from Review Map through the existing related-source viewer", async () => {
    api.fetchRelatedSource.mockResolvedValueOnce({
      path: "src/unchanged-caller.py",
      language: "python",
      start_line: 40,
      end_line: 48,
      focus_line: 44,
      text: "def caller():\n    return changed()\n",
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Review map" }));
    await userEvent.click(await screen.findByRole("button", { name: "Open mapped impact" }));

    await waitFor(() => expect(api.fetchRelatedSource).toHaveBeenCalledWith("review-1", "src/unchanged-caller.py", 44));
    expect(screen.queryByRole("dialog", { name: "Review Map mock" })).toBeNull();
    expect(await screen.findByText("src/unchanged-caller.py")).toBeTruthy();
  });

  it("opens the compact change overview and jumps from a major change into the stream", async () => {
    api.fetchReviewVerification.mockResolvedValueOnce({
      review_id: "review-1",
      snapshot_id: "rrv-1",
      snapshot_number: 0,
      checks: [
        { name: "unit", status: "PASS", state: "current", snapshot_id: "rrv-1", snapshot_number: 0, source: "test", source_ref: "", lane: "local", path: "", detail: "", observed_at: "", artifact: null },
        { name: "lint", status: "PASS", state: "current", snapshot_id: "rrv-1", snapshot_number: 0, source: "test", source_ref: "", lane: "local", path: "", detail: "", observed_at: "", artifact: null },
        { name: "typecheck", status: "PASS", state: "current", snapshot_id: "rrv-1", snapshot_number: 0, source: "test", source_ref: "", lane: "local", path: "", detail: "", observed_at: "", artifact: null },
        { name: "browser", status: "FAIL", state: "current", snapshot_id: "rrv-1", snapshot_number: 0, source: "test", source_ref: "", lane: "local", path: "", detail: "", observed_at: "", artifact: null },
        { name: "integration", status: "UNKNOWN", state: "current", snapshot_id: "rrv-1", snapshot_number: 0, source: "ci", source_ref: "", lane: "ci", path: "", detail: "", observed_at: "", artifact: null },
      ],
      summary: { pass: 3, fail: 1, not_run: 0, unknown: 1, stale: 0 },
      artifacts: [],
    });
    api.fetchOverview.mockResolvedValueOnce({
      ...OVERVIEW,
      brief: {
        summary: "3 files across Review reader and target progress; 1 currently deserves focused attention.",
        themes: ["Review reader", "Target progress"],
        major_changes: [{
          key: "intent:review-reader",
          label: "Review reader",
          explanation: "Groups the target-aware continuous Reader flow.",
          file_count: 2,
          target_count: 15,
          attention_count: 1,
          reviewed: 0,
          changed_since_review: 0,
          needs_changes: 0,
          pending: 15,
          first_path: "src/file-1.py",
        }],
        review_first: [],
        verification: { pass: 3, fail: 1, not_run: 0, unknown: 1 },
        annotations: { human: 1, author: 0, lemoncrow: 2, ai_review: 0 },
        artifacts: { current: 2, stale: 1 },
      },
      chapters: {
        intent: [{
          key: "intent:review-reader",
          label: "Review reader",
          reason: "Groups the target-aware continuous Reader flow.",
          explanation: "Groups the target-aware continuous Reader flow.",
          rows: [{ path: "src/file-1.py" }, { path: "src/file-2.py" }],
          targets: [],
          target_count: 15,
          progress: { target_count: 15, reviewed: 0, changed_since_review: 0, needs_changes: 0, unreviewed: 15, unknown: 0 },
          file_count: 2,
          attention_count: 1,
          reviewed_count: 0,
          changed_count: 0,
          min_attention_rank: 1,
          depends_on: [],
        }],
        dependency: [],
        commits: [],
      },
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Open change overview" }));

    const dialog = await screen.findByRole("dialog", { name: "Change overview" });
    expect(within(dialog).getByText("Review reader")).toBeTruthy();
    expect(within(dialog).getByText("2 files · 15 review areas")).toBeTruthy();
    expect(within(dialog).getByText("0/15 reviewed")).toBeTruthy();
    expect(within(dialog).getByText("1 failed")).toBeTruthy();
    expect(within(dialog).getByText("1 previous Snapshot")).toBeTruthy();

    await userEvent.click(within(dialog).getByRole("button", { name: /Open Review reader conceptual change/i }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Change overview" })).toBeNull());
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].path).toBe("src/file-1.py"));
  });

  it("? opens shortcut help and Escape restores the reviewer's focus", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const primary = screen.getByRole("button", { name: "Review next area" });
    primary.focus();

    await userEvent.keyboard("?");
    const dialog = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(dialog).getByText("Mark reviewed and advance")).toBeTruthy();
    expect(within(dialog).getByText("Review actions")).toBeTruthy();

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(primary));
  });

  it("search jumps to a distant target without loading the intervening large-review corpus", async () => {
    const large = largeReviewFixture();
    api.fetchOverview.mockResolvedValueOnce(large.overview);
    api.fetchTargets.mockResolvedValueOnce(large.targets);

    render(<ReviewReader />);
    expect(await screen.findByText("1500 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(15));

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "large-499 fn_1499");
    expect(await screen.findByText("1 review area · 1 file")).toBeTruthy();
    expect(screen.getByText("1 streamed targets")).toBeTruthy();

    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledWith("review-1", "src/large-499.py"));
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("large-target-1499"));
    const fetchedPaths = new Set(api.fetchFile.mock.calls.map((call) => call[1]));
    expect(fetchedPaths.has("src/large-499.py")).toBe(true);
    expect(fetchedPaths.has("src/large-100.py")).toBe(false);

    await userEvent.keyboard("{Escape}");
    expect(await screen.findByText("1500 streamed targets")).toBeTruthy();
  });

  it("Review next area escapes a transient search and jumps to the global next judgment", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "file-2.py");
    expect(await screen.findByText(/10 review areas · 1 file/)).toBeTruthy();

    stream.scrollToTarget.mockClear();
    await userEvent.click(screen.getByRole("button", { name: "Review next area" }));
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-0");
    expect((screen.getByRole("textbox", { name: "Search review" }) as HTMLInputElement).value).toBe("");
  });

  it("search includes already-loaded comment text without another request", async () => {
    api.fetchAnnotations.mockResolvedValueOnce({
      revision_id: "rrv-1",
      annotations: [{
        id: "ann-search",
        review_id: "review-1",
        revision_id: "rrv-1",
        parent_id: "",
        kind: "comment",
        state: "open",
        body: "Retry hazard in this state transition",
        title: "Database retry",
        unit_key: "sym-11",
        path: "src/file-1.py",
        side: "new",
        start_line: 12,
        end_line: 12,
      }],
      counts: {},
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "database hazard");
    expect(await screen.findByText("1 review area · 1 file")).toBeTruthy();
    expect(screen.getByText("1 streamed targets")).toBeTruthy();

    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-11"));
    expect(api.fetchAnnotations).toHaveBeenCalledTimes(1);
  });

  it("bulk-reviews the full file remainder even when search shows only one target", async () => {
    const marked = TARGETS.slice(3, 10).map((target) => ({ unit_key: target.unit_key, path: target.path }));
    const skipped = TARGETS.slice(0, 3).map((target) => ({
      unit_key: target.unit_key,
      path: target.path,
      reason: "still needs individual attention",
    }));
    api.postBulkReviewed.mockResolvedValueOnce({
      marked,
      skipped,
      progress: { ...TARGET_LIST.progress, reviewed: 7, unreviewed: 23 },
      outline_updates: [],
      group_counts: OVERVIEW.group_counts,
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "fn_3");
    expect(await screen.findByText("1 streamed targets")).toBeTruthy();

    await act(async () => stream.lastProps.onBulkReview("src/file-0.py"));

    await waitFor(() => expect(api.postBulkReviewed).toHaveBeenCalledWith(
      "review-1",
      TARGETS.slice(0, 10).map((target) => target.unit_key),
      "rrv-1",
    ));
    await waitFor(() => expect(stream.lastProps.targets[0].state).toBe("reviewed"));
    expect(screen.getByText((_text, element) => element?.textContent === "7 reviewed")).toBeTruthy();
    expect(screen.getByText(/7 marked reviewed · 3 kept for individual review/)).toBeTruthy();
  });

  it("keeps preserved patches mounted while revision-scoped file metadata refreshes", async () => {
    const nextOverview: ReviewOverview = {
      ...ADVANCED_OVERVIEW,
      refreshed: {
        created: true,
        revision_number: 1,
        previous_revision_number: 0,
        reopened: 0,
        carried: 30,
        added: 0,
        removed: [],
        discarded: [],
        notes: [],
        changed_paths: ["src/file-2.py"],
        added_paths: [],
        removed_paths: [],
        renamed_paths: [],
        preserved_paths: ["src/file-0.py", "src/file-1.py"],
      },
    };
    api.postRefresh.mockResolvedValueOnce(nextOverview);
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockResolvedValueOnce({ ...TARGET_LIST, revision_id: "rrv-2" });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(3));
    api.fetchFile.mockClear();
    let releasePreserved: (detail: FileDetail) => void = () => {};
    api.fetchFile.mockImplementation(async (_id: string, path: string) => {
      if (path === "src/file-0.py") {
        return new Promise<FileDetail>((resolve) => { releasePreserved = resolve; });
      }
      return {
        path,
        status: "modified",
        additions: 10,
        deletions: 0,
        patch: path === "src/file-2.py" ? "NEW-PATCH-rrv2" : "patch",
        renderable: true,
        refusal: "",
        detail: "",
        degraded: [],
      };
    });

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Update from current source" }));

    // The changed patch is required before refresh unlocks, while preserved
    // patches remain mounted and their revision-scoped metadata refreshes in
    // the background. file-0 is deliberately still in flight here.
    await waitFor(() => expect(stream.lastProps.busy).toBe(false));
    expect(api.fetchFile).toHaveBeenCalledWith("review-1", "src/file-2.py");
    expect(api.fetchFile).toHaveBeenCalledWith("review-1", "src/file-0.py");
    expect(api.fetchFile).toHaveBeenCalledWith("review-1", "src/file-1.py");
    expect(stream.lastProps.details["src/file-0.py"]?.patch).toBe("patch");
    expect(stream.lastProps.details["src/file-2.py"]?.patch).toBe("NEW-PATCH-rrv2");

    await act(async () => {
      releasePreserved({
        path: "src/file-0.py",
        status: "modified",
        additions: 10,
        deletions: 0,
        patch: "patch",
        renderable: true,
        refusal: "",
        detail: "",
        provenance: {
          ...OVERVIEW.provenance,
          task: "fresh revision metadata",
        },
        degraded: [],
      });
      await Promise.resolve();
    });
    await waitFor(() => expect(stream.lastProps.details["src/file-0.py"]?.provenance?.task).toBe("fresh revision metadata"));
  });

  it("after refresh shows only the target delta and keeps preserved reviewed work out of the queue", async () => {
    const nextTargets = TARGETS.map((target, index) => ({
      ...target,
      state: index < 24 ? "reviewed" as const : "changed_since_review" as const,
      changed_since_mark: index >= 24,
      reviewed_revision_id: "rrv-1",
    }));
    const active = nextTargets.slice(24).map(deltaRef);
    const preserved = nextTargets.slice(0, 24).map(deltaRef);
    const nextOverview: ReviewOverview = {
      ...OVERVIEW,
      revision: { ...OVERVIEW.revision, id: "rrv-2", revision_number: 1 },
      revision_count: 2,
      progress: { ...OVERVIEW.progress!, reviewed: 24, changed_since_review: 6, unreviewed: 0 },
      refreshed: {
        created: true,
        revision_number: 1,
        previous_revision_number: 0,
        reopened: 6,
        carried: 24,
        added: 0,
        removed: [],
        discarded: [],
        notes: [],
        target_delta: { preserved, reopened: active, added: [], removed: [], active },
      },
    };
    const nextList: ReviewTargetList = {
      ...TARGET_LIST,
      revision_id: "rrv-2",
      targets: nextTargets,
      progress: nextOverview.progress!,
    };
    api.postRefresh.mockResolvedValueOnce(nextOverview);
    api.fetchTargets.mockResolvedValueOnce(TARGET_LIST).mockResolvedValueOnce(nextList);

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Update from current source" }));

    expect(await screen.findByText("6 streamed targets")).toBeTruthy();
    const deltaBar = screen.getByTestId("revision-delta");
    expect(within(deltaBar).getByText("6 returned")).toBeTruthy();
    expect(within(deltaBar).getByText("6 changed")).toBeTruthy();
    expect(within(deltaBar).queryByText(/still pending/)).toBeNull();
    expect(screen.queryByText("24 preserved")).toBeNull();
    await userEvent.click(screen.getByText("Details"));
    expect(screen.getByText("Still reviewed · 24")).toBeTruthy();
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-24"));

    await userEvent.click(screen.getByRole("button", { name: "Show all" }));
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
  });

  it("ignores a file response from the previous revision after refresh", async () => {
    let releaseStale: (detail: FileDetail) => void = () => {};
    const stale = new Promise<FileDetail>((resolve) => { releaseStale = resolve; });
    const calls = new Map<string, number>();
    api.fetchFile.mockImplementation(async (_id: string, path: string) => {
      const count = (calls.get(path) ?? 0) + 1;
      calls.set(path, count);
      if (path === "src/file-2.py" && count === 1) return stale;
      return {
        path,
        status: "modified",
        additions: 1,
        deletions: 0,
        patch: path === "src/file-2.py" ? "NEW-PATCH-rrv2" : "patch",
        renderable: true,
        refusal: "",
        detail: "",
        degraded: [],
      };
    });
    const nextOverview: ReviewOverview = {
      ...OVERVIEW,
      revision: { ...OVERVIEW.revision, id: "rrv-2", revision_number: 1 },
      revision_count: 2,
    };
    api.postRefresh.mockResolvedValueOnce(nextOverview);
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockResolvedValueOnce({ ...TARGET_LIST, revision_id: "rrv-2" });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(calls.get("src/file-2.py")).toBe(1));

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Update from current source" }));
    await waitFor(() => expect(stream.lastProps.details["src/file-2.py"]?.patch).toBe("NEW-PATCH-rrv2"));

    await act(async () => {
      releaseStale({
        path: "src/file-2.py",
        status: "modified",
        additions: 1,
        deletions: 0,
        patch: "OLD-PATCH-rrv1",
        renderable: true,
        refusal: "",
        detail: "",
        degraded: [],
      });
      await Promise.resolve();
    });
    expect(stream.lastProps.details["src/file-2.py"]?.patch).toBe("NEW-PATCH-rrv2");
  });

  it("previews the target-based finish sheet before mutating review status", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Record review outcome…" }));

    await waitFor(() => expect(api.fetchFinishReview).toHaveBeenCalledWith("review-1"));
    const finishDialog = await screen.findByRole("dialog", { name: "Record review outcome" });
    expect(finishDialog).toBeTruthy();
    expect(api.finishReview).not.toHaveBeenCalled();
    expect(within(finishDialog).getByText("0/30")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Record outcome anyway" }));
    await waitFor(() => expect(api.finishReview).toHaveBeenCalledWith("review-1", "finished", "rrv-1"));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Record review outcome" })).toBeNull());
    expect(screen.getByRole("button", { name: "Reopen review" })).toBeTruthy();
  });

  it("returns focus to the primary review action when the finish sheet is dismissed", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const primaryButton = screen.getByRole("button", { name: "Review next area" });

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Record review outcome…" }));
    const close = await screen.findByRole("button", { name: "Close review outcome" });
    await waitFor(() => expect(document.activeElement).toBe(close));
    await userEvent.click(screen.getByRole("button", { name: "Continue reviewing" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Record review outcome" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(primaryButton));
    expect(api.finishReview).not.toHaveBeenCalled();
  });
  it("restores focus to the Context trigger after closing the drawer", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    const trigger = screen.getByRole("button", { name: "Open context" });
    await userEvent.click(trigger);
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Close context mock" }));
    await waitFor(() => expect(screen.queryByTestId("context-drawer-mock")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("opens Context on demand, accepts an exact tab, and Escape closes it", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(screen.queryByTestId("context-drawer-mock")).toBeNull();

    await act(async () => stream.lastProps.onContext(TARGETS[0], "checks"));
    expect(await screen.findByText("checks · overlay")).toBeTruthy();
    expect(drawer.lastProps.target.target_id).toBe("target-0");

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("context-drawer-mock")).toBeNull());
  });

  it("passes live exact-session journal context into the Agent drawer", async () => {
    api.fetchReviewJournal.mockResolvedValue({
      available: true,
      reason: "",
      review_id: "review-1",
      revision_id: "rrv-1",
      revision_number: 0,
      session_id: "session-1",
      host: "claude",
      entries: [{
        id: "jrnl-1",
        kind: "fact",
        at: "2026-09-23T18:00:00+00:00",
        summary: "Verified: checkout reuses validate_cart()",
        detail: "",
        path: "src/file-0.py",
        status: "",
        source: "run_ledger",
        host: "claude",
        session_id: "session-1",
      }],
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchReviewJournal).toHaveBeenCalledWith("review-1"));
    await act(async () => stream.lastProps.onContext(TARGETS[0], "agent"));

    expect(await screen.findByText("agent · overlay")).toBeTruthy();
    expect(drawer.lastProps.journal.entries[0].summary).toContain("checkout reuses validate_cart");
    expect(drawer.lastProps.journal.session_id).toBe("session-1");
  });

  it("keeps routine review to one header row and opens Development from it", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchReviewDevelopment).toHaveBeenCalledWith("review-1"));

    // No stacked attention / development / source bars in the routine state.
    expect(document.querySelector(".review-context-strip")).toBeNull();
    expect(document.querySelector(".review-attention-bar")).toBeNull();

    const action = await screen.findByRole("button", { name: /Development: 1 contributor in Snapshot 0/ });
    await userEvent.click(action);
    const dialog = await screen.findByRole("dialog", { name: "Development" });
    expect(within(dialog).getByText("Auth now rejects expired sessions.")).toBeTruthy();
    expect(within(dialog).getByText("Harden session expiry")).toBeTruthy();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Development" })).toBeNull());
  });

  it("refetches only Development when its live cursor advances", async () => {
    const first = deferred<any>();
    const second = deferred<any>();
    api.fetchReviewLiveState
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
      .mockImplementation(() => new Promise(() => {}));

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchReviewLiveState).toHaveBeenCalledTimes(1));
    const base = {
      changed: true, revision_id: "rrv-1", packet_cursor: "packet-1",
      annotations_cursor: "annotations-1", journal_cursor: "journal-1",
    };
    await act(async () => {
      first.resolve({ ...base, cursor: "live-1", development_cursor: "3" });
      await Promise.resolve();
    });
    await waitFor(() => expect(api.fetchReviewLiveState).toHaveBeenCalledTimes(2));
    const overviewCalls = api.fetchOverview.mock.calls.length;
    const annotationCalls = api.fetchAnnotations.mock.calls.length;
    const developmentCalls = api.fetchReviewDevelopment.mock.calls.length;
    api.fetchReviewDevelopment.mockResolvedValueOnce({
      ...DEVELOPMENT,
      cursor: "5",
      since: { ...DEVELOPMENT.snapshot, scope: "since", event_count: 2, newer_paths: ["src/new.py"], active: true },
    });

    await act(async () => {
      second.resolve({ ...base, cursor: "live-2", development_cursor: "5" });
      await Promise.resolve();
    });

    await waitFor(() => expect(api.fetchReviewDevelopment.mock.calls.length).toBe(developmentCalls + 1));
    expect(api.fetchOverview.mock.calls.length).toBe(overviewCalls);
    expect(api.fetchAnnotations.mock.calls.length).toBe(annotationCalls);
    expect(await screen.findByRole("button", { name: /agents working now · 1 newer file/ })).toBeTruthy();
  });

  it("notices a newer revision on the first live-state response", async () => {
    const first = deferred<any>();
    api.fetchReviewLiveState
      .mockReturnValueOnce(first.promise)
      .mockImplementation(() => new Promise(() => {}));

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchReviewLiveState).toHaveBeenCalledTimes(1));

    await act(async () => {
      first.resolve({
        cursor: "live-newer", changed: true, revision_id: "rrv-2", packet_cursor: "packet-2",
        annotations_cursor: "annotations-2", journal_cursor: "journal-2",
      });
      await Promise.resolve();
    });

    expect(await screen.findByText("Snapshot updated elsewhere")).toBeTruthy();
  });

  it("uses one live-state cursor for metadata and same-revision projection updates", async () => {
    const first = deferred<any>();
    const second = deferred<any>();
    const third = deferred<any>();
    api.fetchReviewLiveState
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
      .mockReturnValueOnce(third.promise)
      .mockImplementation(() => new Promise(() => {}));

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchReviewLiveState).toHaveBeenCalledTimes(1));

    const overviewCalls = api.fetchOverview.mock.calls.length;
    const annotationCalls = api.fetchAnnotations.mock.calls.length;
    const journalCalls = api.fetchReviewJournal.mock.calls.length;

    await act(async () => {
      first.resolve({
        cursor: "live-1", changed: true, revision_id: "rrv-1", packet_cursor: "packet-1",
        annotations_cursor: "annotations-1", journal_cursor: "journal-1",
      });
      await Promise.resolve();
    });
    await waitFor(() => expect(api.fetchReviewLiveState).toHaveBeenCalledTimes(2));

    await act(async () => {
      second.resolve({
        cursor: "live-2", changed: true, revision_id: "rrv-1", packet_cursor: "packet-1",
        annotations_cursor: "annotations-2", journal_cursor: "journal-2",
      });
      await Promise.resolve();
    });
    await waitFor(() => expect(api.fetchAnnotations.mock.calls.length).toBe(annotationCalls + 1));
    await waitFor(() => expect(api.fetchReviewJournal.mock.calls.length).toBe(journalCalls + 1));
    expect(api.fetchOverview.mock.calls.length).toBe(overviewCalls);
    await waitFor(() => expect(api.fetchReviewLiveState).toHaveBeenCalledTimes(3));

    await act(async () => {
      third.resolve({
        cursor: "live-3", changed: true, revision_id: "rrv-1", packet_cursor: "packet-2",
        annotations_cursor: "annotations-2", journal_cursor: "journal-2",
      });
      await Promise.resolve();
    });
    await waitFor(() => expect(api.fetchOverview.mock.calls.length).toBeGreaterThan(overviewCalls));
    expect(screen.getByText("30 streamed targets")).toBeTruthy();
  });

  it("restores keyboard Context focus to the active review row instead of body", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const activeRow = screen.getByRole("button", { name: /file-0.py/i });

    await userEvent.keyboard("e");
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("context-drawer-mock")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(activeRow));
  });

  it("pins Context only by explicit choice and remembers that choice for the tab", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await act(async () => stream.lastProps.onContext(TARGETS[0], "impact"));
    expect(await screen.findByText("impact · overlay")).toBeTruthy();

    act(() => drawer.lastProps.onTogglePin());
    await waitFor(() => expect(screen.getByTestId("context-drawer-mock").textContent).toContain("impact · pinned"));
    expect(window.sessionStorage.getItem("lemoncrow.review.reader.contextPinned")).toBe("1");
  });

  it("applies a request-change response to target state without a second mark request", async () => {
    const blocked = { ...TARGETS[0], state: "needs_changes" as const, annotation_counts: { ...TARGETS[0].annotation_counts, open: 1 } };
    api.postAnnotation.mockResolvedValueOnce({
      annotation: { id: "ann-1" },
      target: blocked,
      progress: { ...TARGET_LIST.progress, needs_changes: 1, unreviewed: 29 },
      outline_updates: [],
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    act(() => stream.lastProps.onCreate({
      path: blocked.path,
      start_line: blocked.start_line,
      end_line: blocked.start_line,
      side: "additions",
      body: "Guard this path.",
      kind: "request_change",
      target_unit_key: blocked.unit_key,
      mark_target: true,
    }));

    await waitFor(() => expect(api.postAnnotation).toHaveBeenCalledTimes(1));
    expect(api.postMark).not.toHaveBeenCalled();
    expect(await screen.findByText((_text, element) => element?.textContent === "1 needs changes")).toBeTruthy();
  });

  it("sends prepared feedback through the exact coding-agent session action", async () => {
    api.fetchOverview.mockResolvedValueOnce({
      ...OVERVIEW,
      revision: {
        ...OVERVIEW.revision,
        provenance_host: "codex",
        provenance_certainty: "exact",
        provenance_session_id: "codex-session-1",
      },
    });
    api.exportFeedback.mockResolvedValueOnce({
      markdown: "## Review feedback\n",
      open: 1,
      orphaned: 0,
      resolved: 0,
      revision_id: "rrv-1",
      feedback_hash: "codex-feedback-hash",
      operation_id: "fop-codex",
      annotation_versions: { "ann-1": 1 },
      delivery: {
        supported: true,
        host: "codex",
        session_id: "codex-session-1",
        target_ref: "codex:codex-session-1",
        label: "Codex",
        reason: "",
      },
    });
    api.deliverFeedback.mockResolvedValueOnce({
      state: "sent",
      target_ref: "codex:codex-session-1",
      remote_ref: "pid:42",
      message: "sent",
      annotation_count: 1,
      operation_id: "fop-codex",
    });
    api.fetchAnnotations
      .mockResolvedValueOnce({
        revision_id: "rrv-1",
        annotations: [],
        counts: {},
        feedback: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
        delivery: {
          supported: true,
          host: "codex",
          session_id: "codex-session-1",
          target_ref: "codex:codex-session-1",
          label: "Codex",
          reason: "",
        },
      })
      .mockResolvedValue({
        revision_id: "rrv-1",
        annotations: [],
        counts: {},
        feedback: { open_total: 1, unpublished: 0, published: 1, in_flight: 0, addressed: 0 },
        delivery: {
          supported: true,
          host: "codex",
          session_id: "codex-session-1",
          target_ref: "codex:codex-session-1",
          label: "Codex",
          reason: "",
        },
      });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Send 1 to Codex…" }));
    expect(await screen.findByRole("button", { name: "Send 1 to Codex" })).toBeTruthy();
    expect(api.deliverFeedback).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Send 1 to Codex" }));
    await waitFor(() => expect(api.deliverFeedback).toHaveBeenCalledWith(
      "review-1",
      expect.objectContaining({ operation_id: "fop-codex", feedback_hash: "codex-feedback-hash" }),
    ));
    expect(await screen.findByText("Sent to Codex")).toBeTruthy();
    const waiting = screen.getByRole("button", { name: "Waiting on Codex" }) as HTMLButtonElement;
    expect(waiting.disabled).toBe(true);
  });

  it("keeps the sent state when the post-delivery discussion refresh fails", async () => {
    const deliveryCapability = {
      supported: true,
      host: "codex",
      session_id: "codex-session-1",
      target_ref: "codex:codex-session-1",
      label: "Codex",
      reason: "",
    };
    api.fetchAnnotations
      .mockResolvedValueOnce({
        revision_id: "rrv-1",
        annotations: [],
        counts: {},
        feedback: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
        delivery: deliveryCapability,
      })
      .mockRejectedValueOnce(new Error("comments offline"));
    api.exportFeedback.mockResolvedValueOnce({
      markdown: "## Review feedback\n",
      open: 1,
      orphaned: 0,
      resolved: 0,
      revision_id: "rrv-1",
      feedback_hash: "codex-feedback-hash",
      operation_id: "fop-codex",
      annotation_versions: { "ann-1": 1 },
      delivery: deliveryCapability,
      status: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
    });
    api.deliverFeedback.mockResolvedValueOnce({
      state: "sent",
      target_ref: "codex:codex-session-1",
      remote_ref: "pid:42",
      message: "sent",
      annotation_count: 1,
      operation_id: "fop-codex",
    });

    render(<ReviewReader sourceProbeIntervalMs={60_000} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Send 1 to Codex…" }));
    await userEvent.click(await screen.findByRole("button", { name: "Send 1 to Codex" }));

    expect(await screen.findByText("Sent to Codex")).toBeTruthy();
    const waiting = await screen.findByRole("button", { name: "Waiting on Codex" }) as HTMLButtonElement;
    expect(waiting.disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "Send 1 to Codex…" })).toBeNull();
    expect(await screen.findByText(/Feedback was sent, but the review state could not refresh: comments offline/)).toBeTruthy();
  });

  it("surfaces an addressed response even when source bytes do not change", async () => {
    const firstLive = deferred<any>();
    const addressedLive = deferred<any>();
    api.fetchReviewLiveState
      .mockReturnValueOnce(firstLive.promise)
      .mockReturnValueOnce(addressedLive.promise)
      .mockImplementation(() => new Promise(() => {}));
    const base = {
      id: "ann-response",
      review_id: "review-1",
      revision_id: "rrv-1",
      parent_id: "",
      kind: "request_change",
      state: "open",
      body: "Keep invalid sessions visible",
      created_by: "local",
      created_by_actor: "human",
      source: "human",
      source_id: "local",
      title: "",
      evidence: [],
      confidence: null,
      author_response: "none",
      author_response_source_id: "",
      author_response_at: "",
      is_human_judgment: true,
      created_at: "2026-09-17T18:00:00+00:00",
      updated_at: "2026-09-17T18:00:00+00:00",
      anchor_method: "identical_blob",
      anchor_method_label: "file unchanged since the comment",
      anchor_exact: true,
      anchor_detail: "",
      anchored: true,
      path: "src/file-0.py",
      side: "new",
      start_line: 1,
      end_line: 1,
      file_level: false,
      unit_key: "sym-0",
      symbol: "",
      origin_symbol: "",
    };
    api.fetchAnnotations
      .mockResolvedValueOnce({
        revision_id: "rrv-1",
        annotations: [base],
        counts: {},
        feedback: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
        delivery: {
          supported: true,
          host: "codex",
          session_id: "codex-session-1",
          target_ref: "codex:codex-session-1",
          label: "Codex",
          reason: "",
        },
      })
      .mockResolvedValue({
        revision_id: "rrv-1",
        annotations: [{
          ...base,
          author_response: "addressed",
          author_response_source_id: "codex-session-1",
          author_response_at: "2026-09-17T18:01:00+00:00",
          updated_at: "2026-09-17T18:01:00+00:00",
        }],
        counts: {},
      });

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Send 1 to Codex…" })).toBeTruthy();
    await waitFor(() => expect(api.fetchReviewLiveState).toHaveBeenCalledTimes(1));
    await act(async () => {
      firstLive.resolve({
        cursor: "live-before-addressed", changed: true, revision_id: "rrv-1", packet_cursor: "packet-1",
        annotations_cursor: "annotations-before", journal_cursor: "journal-1",
      });
      await Promise.resolve();
    });
    await waitFor(() => expect(api.fetchReviewLiveState).toHaveBeenCalledTimes(2));
    await act(async () => {
      addressedLive.resolve({
        cursor: "live-addressed", changed: true, revision_id: "rrv-1", packet_cursor: "packet-1",
        annotations_cursor: "annotations-addressed", journal_cursor: "journal-1",
      });
      await Promise.resolve();
    });
    await waitFor(() => expect(stream.lastProps.annotations[0]?.author_response).toBe("addressed"), { timeout: 3000 });
    expect(stream.lastProps.annotations[0].author_response_source_id).toBe("codex-session-1");
    expect(api.postRefresh).not.toHaveBeenCalled();
  });

  it("blocks marking when a refresh advances the revision but its targets never load", async () => {
    await strandReaderOnAdvancedRevision();

    expect(screen.getByText(/· Snapshot 1/)).toBeTruthy();
    expect(stream.lastProps.details).toEqual({});

    await userEvent.keyboard("r");
    expect(api.postMark).not.toHaveBeenCalled();
  });

  it("says nothing about the revision while a refresh that will succeed is still loading", async () => {
    let releaseTargets: (value: ReviewTargetList) => void = () => {};
    api.postRefresh.mockResolvedValueOnce(ADVANCED_OVERVIEW);
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockImplementationOnce(() => new Promise<ReviewTargetList>((resolve) => { releaseTargets = resolve; }));

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Update from current source" }));

    // The overview is committed the moment postRefresh resolves, but the
    // reviewer hears nothing while the dependent reads are still out: `busy`
    // is already holding every judgment, and the refresh may yet succeed.
    expect(await screen.findByText(/· Snapshot 1/)).toBeTruthy();
    expect(screen.queryByText("Snapshot updated elsewhere")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();

    await act(async () => { releaseTargets(TARGET_LIST); });
    await waitFor(() => expect(stream.lastProps.busy).toBe(false));
    expect(screen.queryByText("Snapshot updated elsewhere")).toBeNull();
  });
  it("keeps appearance controls in the actions menu while Review next stays primary", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    expect(screen.getByRole("button", { name: "Review next area" })).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    const theme = screen.getByRole("combobox", { name: "Code theme" }) as HTMLSelectElement;
    expect([...theme.options].map((option) => option.textContent)).toEqual([
      "LemonCrow",
      "LemonCrow Dark",
      "GitHub Dark",
      "GitHub Light",
    ]);
    expect(theme.value).toBe("lemoncrow");

    await userEvent.selectOptions(theme, "lemoncrow-dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(window.localStorage.getItem("lemoncrow-review-code-theme")).toBe("lemoncrow-dark");
    expect(stream.lastProps.codeTheme).toBe("lemoncrow-dark");

    await userEvent.selectOptions(theme, "github-light");
    expect(document.documentElement.classList.contains("light")).toBe(true);
    expect(window.localStorage.getItem("lemoncrow-review-code-theme")).toBe("github-light");
    expect(stream.lastProps.chromeTheme).toBe("light");

    await userEvent.selectOptions(theme, "github-dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);

    // Plain LemonCrow is the light/default LemonCrow preset; switching from
    // LemonCrow Dark must visibly transition both syntax and Reader chrome.
    await userEvent.selectOptions(theme, "lemoncrow");
    expect(document.documentElement.classList.contains("light")).toBe(true);
    expect(stream.lastProps.codeTheme).toBe("lemoncrow");
    expect(stream.lastProps.chromeTheme).toBe("light");
  });

  it("toggles and persists long-line wrapping with the actions menu and w shortcut", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(stream.lastProps.diffOverflow).toBe("scroll");

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: /Code lines/i }));
    expect(stream.lastProps.diffOverflow).toBe("wrap");
    expect(window.localStorage.getItem("lemoncrow.review.reader.diffOverflow")).toBe("wrap");

    await userEvent.keyboard("w");
    expect(stream.lastProps.diffOverflow).toBe("scroll");
    expect(window.localStorage.getItem("lemoncrow.review.reader.diffOverflow")).toBe("scroll");
  });

  it("records the reviewer outcome before finishing the review lifecycle", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Record review outcome…" }));
    expect(await screen.findByRole("dialog", { name: "Record review outcome" })).toBeTruthy();

    await userEvent.click(screen.getByRole("radio", { name: "LGTM" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Review outcome summary" }), "Ready from my side.");
    await userEvent.click(screen.getByRole("button", { name: "Record outcome anyway" }));

    await waitFor(() => expect(api.postReviewOutcome).toHaveBeenCalledWith(
      "review-1",
      "lgtm",
      "Ready from my side.",
      "rrv-1",
    ));
    await waitFor(() => expect(api.finishReview).toHaveBeenCalledWith("review-1", "finished", "rrv-1"));
    expect(api.postReviewOutcome.mock.invocationCallOrder[0]).toBeLessThan(api.finishReview.mock.invocationCallOrder[0]);
  });

  it("opens quick product feedback with optional contact email", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Send LemonCrow feedback" }));
    expect(screen.getByRole("dialog", { name: "LemonCrow feedback" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "Feedback" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "Feedback contact email" })).toBeTruthy();
    expect(screen.getByText(/okay with the developer contacting you/i)).toBeTruthy();
  });

  it("makes Reload review the primary recovery action for a stranded reader", async () => {
    await strandReaderOnAdvancedRevision();
    expect(api.postRefresh).toHaveBeenCalledTimes(1);

    const reload = screen.getByRole("button", { name: "Reload review" });
    expect(reload).not.toBeDisabled();
    expect(screen.queryByRole("button", { name: "Record review outcome" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Record review outcome…" })).toBeNull();

    // Judgment shortcuts remain blocked until the reader actually catches up.
    await userEvent.keyboard("r");
    expect(api.postMark).not.toHaveBeenCalled();

    // The command palette mirrors the same recovery transition.
    await userEvent.keyboard("p");
    const palette = await screen.findByRole("dialog", { name: "Review actions" });
    expect(within(palette).getByText("Reload review")).toBeTruthy();
    await userEvent.keyboard("{Escape}");
  });

  it("hands judgments back when Reload review catches the reader up", async () => {
    await strandReaderOnAdvancedRevision();
    api.fetchOverview.mockResolvedValueOnce(ADVANCED_OVERVIEW);

    await userEvent.click(screen.getByRole("button", { name: "Reload review" }));
    await waitFor(() => expect(screen.queryByText("Snapshot updated elsewhere")).toBeNull());

    await userEvent.keyboard("r");
    await waitFor(() => expect(api.postMark).toHaveBeenCalledTimes(1));
  });

  it("keeps judgments blocked when Reload review fails too", async () => {
    api.fetchOverview
      .mockResolvedValueOnce(OVERVIEW)
      .mockRejectedValueOnce(new Error("gateway still down"));
    await strandReaderOnAdvancedRevision();

    await userEvent.click(screen.getByRole("button", { name: "Reload review" }));
    await waitFor(() => expect(api.fetchOverview).toHaveBeenCalledTimes(2));

    expect(screen.getByText("Snapshot updated elsewhere")).toBeTruthy();
    await userEvent.keyboard("r");
    expect(api.postMark).not.toHaveBeenCalled();
  });

  it("keeps a drafted file in the stream when the search stops matching it", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("c");
    await waitFor(() => expect(stream.lastProps.draft?.path).toBe("src/file-0.py"));

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "fn_25");

    expect(await screen.findByText("1 review area · 1 file")).toBeTruthy();
    await waitFor(() => expect(
      stream.lastProps.targets.some((target: ReviewTarget) => target.path === "src/file-0.py"),
    ).toBe(true));
    expect(stream.lastProps.draft?.path).toBe("src/file-0.py");
  });

  it("steps to the file the search matched, not the file a draft pinned into the stream", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("c");
    await waitFor(() => expect(stream.lastProps.draft?.path).toBe("src/file-0.py"));

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "fn_25");
    expect(await screen.findByText("1 review area · 1 file")).toBeTruthy();
    expect(stream.lastProps.activeTargetId).toBe("target-0");

    // The one match lives in src/file-2.py; src/file-0.py is only in the stream
    // because it holds the draft, and the counter does not count it.
    await userEvent.click(screen.getByRole("button", { name: "Next search match" }));
    await waitFor(() => expect(stream.lastProps.activeTargetId).toBe("target-25"));
  });

  it("lists only the searched files in the outline, not the file a draft pinned into the stream", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("c");
    await waitFor(() => expect(stream.lastProps.draft?.path).toBe("src/file-0.py"));

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "fn_25");
    expect(await screen.findByText("1 review area · 1 file")).toBeTruthy();

    // src/file-0.py is in the stream only because it holds the draft. An
    // outline row for it under a counter that says one file would read
    // "file-0.py · 0 matches" and contradict the line right above it.
    const outlinePanel = screen.getByRole("complementary", { name: "Review outline" });
    expect(within(outlinePanel).getByText("file-2.py")).toBeTruthy();
    expect(within(outlinePanel).queryByText("file-0.py")).toBeNull();
    expect(within(outlinePanel).queryByText("0 matches")).toBeNull();
  });

  it("keeps an open composer when refresh proves its file bytes were preserved", async () => {
    const nextTargets = TARGETS.map((target, index) => ({
      ...target,
      state: index < 20 ? "reviewed" as const : "changed_since_review" as const,
      changed_since_mark: index >= 20,
      reviewed_revision_id: "rrv-1",
    }));
    // The whole delta lives in src/file-2.py; the draft sits on src/file-0.py.
    const active = nextTargets.slice(20).map(deltaRef);
    const nextOverview: ReviewOverview = {
      ...ADVANCED_OVERVIEW,
      refreshed: {
        created: true,
        revision_number: 1,
        previous_revision_number: 0,
        reopened: 10,
        carried: 20,
        added: 0,
        removed: [],
        discarded: [],
        notes: [],
        target_delta: { preserved: nextTargets.slice(0, 20).map(deltaRef), reopened: active, added: [], removed: [], active },
        changed_paths: ["src/file-2.py"],
        added_paths: [],
        removed_paths: [],
        renamed_paths: [],
        preserved_paths: ["src/file-0.py", "src/file-1.py"],
      },
    };
    api.postRefresh.mockResolvedValueOnce(nextOverview);
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockResolvedValueOnce({ ...TARGET_LIST, revision_id: "rrv-2", targets: nextTargets });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.keyboard("c");
    await waitFor(() => expect(stream.lastProps.draft?.path).toBe("src/file-0.py"));

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Update from current source" }));

    expect(await screen.findByText("20 streamed targets")).toBeTruthy();
    expect(stream.lastProps.draft?.path).toBe("src/file-0.py");
    expect(new Set(stream.lastProps.targets.map((target: ReviewTarget) => target.path))).toEqual(
      new Set(["src/file-0.py", "src/file-2.py"]),
    );
    expect(screen.queryByText(/Unsaved comment on src\/file-0\.py was discarded/)).toBeNull();
  });

  it("preserves an open composer as a detached recovered draft when refresh changes its anchor", async () => {
    const nextOverview: ReviewOverview = {
      ...ADVANCED_OVERVIEW,
      refreshed: {
        created: true,
        revision_number: 1,
        previous_revision_number: 0,
        reopened: 10,
        carried: 20,
        added: 0,
        removed: [],
        discarded: [],
        notes: [],
        changed_paths: ["src/file-0.py"],
        added_paths: [],
        removed_paths: [],
        renamed_paths: [],
        preserved_paths: ["src/file-1.py", "src/file-2.py"],
      },
    };
    api.postRefresh.mockResolvedValueOnce(nextOverview);
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockResolvedValueOnce({ ...TARGET_LIST, revision_id: "rrv-2" });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.keyboard("c");
    await waitFor(() => expect(stream.lastProps.draft?.path).toBe("src/file-0.py"));
    await act(async () => {
      stream.lastProps.onDraft({
        ...stream.lastProps.draft,
        body: "Keep this correction request",
        kind: "request_change",
        markTarget: true,
      });
    });

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Update from current source" }));

    await waitFor(() => expect(stream.lastProps.draft?.recovered).toBe(true));
    expect(stream.lastProps.draft.body).toBe("Keep this correction request");
    expect(stream.lastProps.draft.startLine).toBe(0);
    expect(stream.lastProps.draft.targetUnitKey).toBeUndefined();
    expect(screen.getByText("Draft preserved")).toBeTruthy();
    expect(screen.getByText(/Keep this correction request/)).toBeTruthy();
    expect(screen.getByText(/select a new line to reattach/)).toBeTruthy();
  });

  it("refuses both evidence writes while another action still holds the busy lock", async () => {
    let releaseBulk: () => void = () => {};
    api.postBulkReviewed.mockImplementationOnce(() => new Promise((resolve) => {
      releaseBulk = () => resolve({
        marked: [],
        skipped: [],
        progress: TARGET_LIST.progress,
        outline_updates: [],
        group_counts: OVERVIEW.group_counts,
      });
    }));

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await act(async () => stream.lastProps.onContext(TARGETS[0], "evidence"));
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();

    act(() => { void stream.lastProps.onBulkReview("src/file-0.py"); });
    await waitFor(() => expect(api.postBulkReviewed).toHaveBeenCalledTimes(1));

    // Both controls sit side by side in the evidence tab and are gated only by
    // readOnly there, so both are reachable mid-bulk.
    await act(async () => drawer.lastProps.onUploadEvidence([new File(["x"], "shot.png", { type: "image/png" })]));
    expect(api.uploadReviewEvidence).not.toHaveBeenCalled();

    await act(async () => drawer.lastProps.onAddPreview("https://example.test/shot.png", "Shot"));
    expect(api.linkReviewEvidence).not.toHaveBeenCalled();

    // The bulk POST still owns the lock, so marking is still refused.
    await userEvent.keyboard("r");
    expect(api.postMark).not.toHaveBeenCalled();
    await act(async () => { releaseBulk(); });
    await waitFor(() => expect(stream.lastProps.busy).toBe(false));
  });

  it("leaves focus in the dialog a palette action opens instead of the button behind it", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const primary = screen.getByRole("button", { name: "Review next area" });
    primary.focus();

    await userEvent.keyboard("p");
    const palette = await screen.findByRole("dialog", { name: "Review actions" });
    await userEvent.type(within(palette).getByRole("textbox", { name: "Filter review actions" }), "change overview");
    await userEvent.keyboard("{Enter}");

    const dialog = await screen.findByRole("dialog", { name: "Change overview" });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 80)); });
    expect(document.activeElement).not.toBe(primary);
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it("closes the topmost dialog on Escape after focus has escaped its overlay", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Open change overview" }));
    expect(await screen.findByRole("dialog", { name: "Change overview" })).toBeTruthy();

    const primary = screen.getByRole("button", { name: "Review next area" });
    act(() => primary.focus());
    await userEvent.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Change overview" })).toBeNull());
  });

  it("never offers a revision update on an archived review", async () => {
    api.fetchOverview.mockResolvedValue({ ...OVERVIEW, session: { ...OVERVIEW.session, status: "archived" } });
    api.fetchSourceState.mockResolvedValue({ supported: true, changed: true, fingerprint: "wt-1", path_count: 2, paths: [], reason: "" });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Restore review" })).toBeTruthy();

    expect(api.fetchSourceState).not.toHaveBeenCalled();
    expect(screen.queryByText("New Snapshot available")).toBeNull();
  });

  it("turns a selected range into a durable proposal, applies it, then asks for an explicit revision update", async () => {
    api.fetchProposalSelection.mockResolvedValue({
      revision_id: "rrv-1",
      path: "src/file-0.py",
      start_line: 1,
      end_line: 1,
      side: "additions",
      text: "old_line\n",
    });
    const proposed = changeProposal();
    api.createChangeProposal.mockResolvedValue({ proposal: proposed });
    api.applyChangeProposal.mockResolvedValue({
      proposal: changeProposal({
        state: "applied",
        can_apply: false,
        applied_at: "2026-09-22T17:02:00Z",
      }),
      source_state: {
        supported: true,
        changed: true,
        fingerprint: "wt-after-reviewer-edit",
        path_count: 1,
        paths: ["src/file-0.py"],
        reason: "",
      },
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    act(() => {
      stream.lastProps.onSuggestEdit({
        path: "src/file-0.py",
        startLine: 1,
        endLine: 1,
        side: "additions",
        targetUnitKey: "sym-0",
      });
    });
    await waitFor(() => expect(api.fetchProposalSelection).toHaveBeenCalledWith(
      "review-1",
      "src/file-0.py",
      1,
      1,
      "additions",
    ));
    await waitFor(() => expect(stream.lastProps.draft?.mode).toBe("proposal"));
    expect(stream.lastProps.proposalSelection?.text).toBe("old_line\n");

    act(() => {
      stream.lastProps.onDraft({
        ...stream.lastProps.draft,
        replacementText: "new_line\n",
        proposalIntent: "Use the corrected value.",
      });
    });
    await waitFor(() => expect(stream.lastProps.draft?.replacementText).toBe("new_line\n"));

    act(() => { stream.lastProps.onCreateProposal(); });
    await waitFor(() => expect(api.createChangeProposal).toHaveBeenCalledWith("review-1", expect.objectContaining({
      expected_revision_id: "rrv-1",
      path: "src/file-0.py",
      start_line: 1,
      end_line: 1,
      replacement_text: "new_line\n",
      target_unit_key: "sym-0",
      intent: "Use the corrected value.",
    })));
    await waitFor(() => expect(stream.lastProps.activeProposal?.id).toBe("rcp-1"));
    expect(await screen.findByTestId("review-proposals")).toHaveTextContent("1 ready");

    act(() => { stream.lastProps.onApplyProposal(); });
    await waitFor(() => expect(api.applyChangeProposal).toHaveBeenCalledWith("rcp-1"));
    expect(await screen.findByText("New Snapshot available")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Capture current source" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Update review" })).toBeNull();
    expect(screen.getByTestId("review-proposals")).toHaveTextContent("1 awaiting update");
    expect(api.postRefresh).not.toHaveBeenCalled();
  });

  it("applies Edit source in one guarded action while still creating a durable proposal", async () => {
    api.fetchProposalSelection.mockResolvedValue({
      revision_id: "rrv-1",
      path: "src/file-0.py",
      start_line: 1,
      end_line: 1,
      side: "additions",
      text: "old_line\n",
    });
    const proposed = changeProposal();
    api.createChangeProposal.mockResolvedValue({ proposal: proposed });
    api.applyChangeProposal.mockResolvedValue({
      proposal: changeProposal({
        state: "applied",
        can_apply: false,
        applied_at: "2026-09-22T17:03:00Z",
      }),
      source_state: {
        supported: true,
        changed: true,
        fingerprint: "wt-after-direct-edit",
        path_count: 1,
        paths: ["src/file-0.py"],
        reason: "",
      },
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(stream.lastProps.sourceMutationSupported).toBe(true));

    act(() => {
      stream.lastProps.onEditSource({
        path: "src/file-0.py",
        startLine: 1,
        endLine: 1,
        side: "additions",
        targetUnitKey: "sym-0",
      });
    });
    await waitFor(() => expect(stream.lastProps.draft?.sourceAction).toBe("edit"));
    act(() => {
      stream.lastProps.onDraft({
        ...stream.lastProps.draft,
        replacementText: "new_line\n",
      });
    });
    await waitFor(() => expect(stream.lastProps.draft?.replacementText).toBe("new_line\n"));

    act(() => { stream.lastProps.onCreateAndApplyProposal(); });
    await waitFor(() => expect(api.createChangeProposal).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.applyChangeProposal).toHaveBeenCalledWith("rcp-1"));
    expect(await screen.findByText("New Snapshot available")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Capture current source" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Update review" })).toBeNull();
    expect(screen.getByTestId("review-proposals")).toHaveTextContent("1 awaiting update");
    expect(api.postRefresh).not.toHaveBeenCalled();
  });

  it("keeps captured proposals out of persistent source-edit chrome", async () => {
    api.fetchChangeProposals.mockResolvedValue({
      revision_id: "rrv-2",
      source_mutation_supported: true,
      proposals: [
        changeProposal({
          state: "applied",
          can_apply: false,
          result_revision_id: "rrv-2",
          result_target_ids: ["target-0"],
        }),
      ],
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchChangeProposals).toHaveBeenCalled());
    expect(screen.queryByTestId("review-proposals")).toBeNull();
  });

  it("shows exact-session agent work without auto-capturing a half-fix", async () => {
    const probes: Array<(state: SourceState) => void> = [];
    api.fetchSourceState.mockImplementation(() => new Promise<SourceState>((resolve) => { probes.push(resolve); }));
    const annotation = handoffAnnotation();
    const deliveryCapability = {
      supported: true,
      host: "claude",
      session_id: "session-1",
      target_ref: "claude:session-1",
      label: "Claude",
      reason: "",
    };
    api.fetchAnnotations
      .mockResolvedValueOnce({
        revision_id: "rrv-1",
        annotations: [annotation],
        counts: {},
        feedback: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
        delivery: deliveryCapability,
      })
      .mockResolvedValue({
        revision_id: "rrv-1",
        annotations: [annotation],
        counts: {},
        feedback: { open_total: 1, unpublished: 0, published: 0, in_flight: 1, addressed: 0 },
        delivery: deliveryCapability,
      });
    api.exportFeedback.mockResolvedValueOnce({
      markdown: "## Review feedback\n",
      open: 1,
      orphaned: 0,
      resolved: 0,
      revision_id: "rrv-1",
      feedback_hash: "feedback-live-1",
      operation_id: "fop-live-1",
      annotation_versions: { "ann-1": 1 },
      delivery: deliveryCapability,
      status: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
    });
    api.deliverFeedback.mockResolvedValueOnce({
      state: "queued",
      target_ref: "claude:session-1",
      remote_ref: "inbox:msg-1",
      message: "queued",
      annotation_count: 1,
      operation_id: "fop-live-1",
    });

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Send 1 to Claude…" }));
    await userEvent.click(await screen.findByRole("button", { name: "Send 1 to Claude" }));
    expect(await screen.findByText("Queued to Claude")).toBeTruthy();

    const changed: SourceState = {
      supported: true,
      changed: true,
      fingerprint: "wt-agent-working",
      path_count: 2,
      paths: ["src/file-0.py", "src/file-1.py"],
      reason: "",
    };
    await waitFor(() => expect(probes.length).toBeGreaterThanOrEqual(1));
    await act(async () => { probes[0](changed); });
    await waitFor(() => expect(probes.length).toBeGreaterThanOrEqual(2));
    await act(async () => { probes[1](changed); });

    expect(await screen.findByText("Claude is working")).toBeTruthy();
    expect(screen.getByText(/capture automatically after every submitted comment is marked addressed/i)).toBeTruthy();
    expect(api.postRefresh).not.toHaveBeenCalled();
  });

  it("auto-captures the settled source after the exact feedback handoff is fully addressed", async () => {
    const probes: Array<(state: SourceState) => void> = [];
    api.fetchSourceState.mockImplementation(() => new Promise<SourceState>((resolve) => { probes.push(resolve); }));
    const annotation = handoffAnnotation();
    const addressed = handoffAnnotation("ann-1", true);
    const deliveryCapability = {
      supported: true,
      host: "claude",
      session_id: "session-1",
      target_ref: "claude:session-1",
      label: "Claude",
      reason: "",
    };
    api.fetchAnnotations
      .mockResolvedValueOnce({
        revision_id: "rrv-1",
        annotations: [annotation],
        counts: {},
        feedback: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
        delivery: deliveryCapability,
      })
      .mockResolvedValueOnce({
        revision_id: "rrv-1",
        annotations: [addressed],
        counts: {},
        feedback: { open_total: 1, unpublished: 0, published: 0, in_flight: 0, addressed: 1 },
        delivery: deliveryCapability,
      })
      .mockResolvedValue({
        revision_id: "rrv-2",
        annotations: [addressed],
        counts: {},
        feedback: { open_total: 1, unpublished: 0, published: 0, in_flight: 0, addressed: 1 },
        delivery: deliveryCapability,
      });
    api.exportFeedback.mockResolvedValueOnce({
      markdown: "## Review feedback\n",
      open: 1,
      orphaned: 0,
      resolved: 0,
      revision_id: "rrv-1",
      feedback_hash: "feedback-live-2",
      operation_id: "fop-live-2",
      annotation_versions: { "ann-1": 1 },
      delivery: deliveryCapability,
      status: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
    });
    api.deliverFeedback.mockResolvedValueOnce({
      state: "queued",
      target_ref: "claude:session-1",
      remote_ref: "inbox:msg-2",
      message: "queued",
      annotation_count: 1,
      operation_id: "fop-live-2",
    });
    api.postRefresh.mockResolvedValueOnce({
      ...ADVANCED_OVERVIEW,
      refreshed: {
        created: true,
        revision_number: 1,
        previous_revision_number: 0,
        reopened: 1,
        carried: 29,
        added: 0,
        removed: [],
        discarded: [],
        notes: [],
        changed_paths: ["src/file-0.py"],
        added_paths: [],
        removed_paths: [],
        renamed_paths: [],
        preserved_paths: ["src/file-1.py", "src/file-2.py"],
      },
    });
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockResolvedValue({ ...TARGET_LIST, revision_id: "rrv-2" });
    api.fetchReviewEvidence.mockResolvedValue({ revision_id: "rrv-2", evidence: [] });

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Send 1 to Claude…" }));
    await userEvent.click(await screen.findByRole("button", { name: "Send 1 to Claude" }));
    expect(await screen.findByText("Queued to Claude")).toBeTruthy();

    const changed: SourceState = {
      supported: true,
      changed: true,
      fingerprint: "wt-agent-done",
      path_count: 1,
      paths: ["src/file-0.py"],
      reason: "",
    };
    await waitFor(() => expect(probes.length).toBeGreaterThanOrEqual(1));
    await act(async () => { probes[0](changed); });
    await waitFor(() => expect(probes.length).toBeGreaterThanOrEqual(2));
    await act(async () => { probes[1](changed); });

    await waitFor(() => expect(api.postRefresh).toHaveBeenCalledTimes(1));
    expect(api.postRefresh).toHaveBeenCalledWith("review-1");
    await waitFor(() => expect(screen.getByText(/· Snapshot 1/)).toBeTruthy());
    expect(screen.queryByText("New Snapshot available")).toBeNull();
  });

  it("waits for the same working-tree change twice, then offers one explicit update", async () => {
    // Each probe hangs until the test releases it, so the stability gate is
    // answered at an exact number of observations rather than after a sleep.
    const probes: Array<(state: SourceState) => void> = [];
    api.fetchSourceState.mockImplementation(() => new Promise<SourceState>((resolve) => { probes.push(resolve); }));
    const changed: SourceState = { supported: true, changed: true, fingerprint: "wt-1", path_count: 2, paths: [], reason: "" };
    api.postRefresh.mockResolvedValueOnce({
      ...ADVANCED_OVERVIEW,
      refreshed: {
        created: true,
        revision_number: 1,
        previous_revision_number: 0,
        reopened: 0,
        carried: 30,
        added: 0,
        removed: [],
        discarded: [],
        notes: [],
        changed_paths: ["src/file-2.py"],
        added_paths: [],
        removed_paths: [],
        renamed_paths: [],
        preserved_paths: ["src/file-0.py", "src/file-1.py"],
      },
    });
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockResolvedValueOnce({ ...TARGET_LIST, revision_id: "rrv-2" });

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(probes).toHaveLength(1));

    await act(async () => { probes[0](changed); });
    expect(api.postRefresh).not.toHaveBeenCalled();

    await waitFor(() => expect(probes).toHaveLength(2));
    await act(async () => { probes[1](changed); });
    expect(await screen.findByText("New Snapshot available")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Update review" })).toBeNull();
    expect(api.postRefresh).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Capture current source" }));
    await waitFor(() => expect(api.postRefresh).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText(/· Snapshot 1/)).toBeTruthy());
    expect(screen.queryByText("New Snapshot available")).toBeNull();
  });

  it("keeps a settled source change explicit while a draft exists", async () => {
    const probes: Array<(state: SourceState) => void> = [];
    api.fetchSourceState.mockImplementation(() => new Promise<SourceState>((resolve) => { probes.push(resolve); }));
    const changed: SourceState = { supported: true, changed: true, fingerprint: "wt-draft", path_count: 1, paths: ["src/file-2.py"], reason: "" };
    api.postRefresh.mockResolvedValueOnce({
      ...ADVANCED_OVERVIEW,
      refreshed: {
        created: true,
        revision_number: 1,
        previous_revision_number: 0,
        reopened: 0,
        carried: 30,
        added: 0,
        removed: [],
        discarded: [],
        notes: [],
        changed_paths: ["src/file-2.py"],
        added_paths: [],
        removed_paths: [],
        renamed_paths: [],
        preserved_paths: ["src/file-0.py", "src/file-1.py"],
      },
    });
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockResolvedValueOnce({ ...TARGET_LIST, revision_id: "rrv-2" });

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.keyboard("c");
    await waitFor(() => expect(stream.lastProps.draft?.path).toBe("src/file-0.py"));
    await waitFor(() => expect(probes).toHaveLength(1));
    await act(async () => { probes[0](changed); });
    await waitFor(() => expect(probes).toHaveLength(2));
    await act(async () => { probes[1](changed); });

    expect(await screen.findByText("New Snapshot available")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Capture current source" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Update review" })).toBeNull();
    expect(api.postRefresh).not.toHaveBeenCalled();

    await userEvent.keyboard("{Escape}");
    expect(api.postRefresh).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Capture current source" }));
    await waitFor(() => expect(api.postRefresh).toHaveBeenCalledTimes(1));
  });

  it("keeps a settled source change explicit while another mutation owns the busy lock", async () => {
    const probes: Array<(state: SourceState) => void> = [];
    api.fetchSourceState.mockImplementation(() => new Promise<SourceState>((resolve) => { probes.push(resolve); }));
    const changed: SourceState = { supported: true, changed: true, fingerprint: "wt-busy", path_count: 1, paths: ["src/file-2.py"], reason: "" };
    let releaseBulk: () => void = () => {};
    api.postBulkReviewed.mockImplementationOnce(() => new Promise((resolve) => {
      releaseBulk = () => resolve({
        marked: [],
        skipped: [],
        progress: TARGET_LIST.progress,
        outline_updates: [],
        group_counts: OVERVIEW.group_counts,
      });
    }));
    api.postRefresh.mockResolvedValueOnce({
      ...ADVANCED_OVERVIEW,
      refreshed: {
        created: true,
        revision_number: 1,
        previous_revision_number: 0,
        reopened: 0,
        carried: 30,
        added: 0,
        removed: [],
        discarded: [],
        notes: [],
        changed_paths: ["src/file-2.py"],
        added_paths: [],
        removed_paths: [],
        renamed_paths: [],
        preserved_paths: ["src/file-0.py", "src/file-1.py"],
      },
    });
    api.fetchTargets
      .mockResolvedValueOnce(TARGET_LIST)
      .mockResolvedValueOnce({ ...TARGET_LIST, revision_id: "rrv-2" });

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    act(() => { void stream.lastProps.onBulkReview("src/file-0.py"); });
    await waitFor(() => expect(api.postBulkReviewed).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(probes).toHaveLength(1));
    await act(async () => { probes[0](changed); });
    await waitFor(() => expect(probes).toHaveLength(2));
    await act(async () => { probes[1](changed); });

    expect(await screen.findByText("New Snapshot available")).toBeTruthy();
    const update = screen.getByRole("button", { name: "Capture current source" });
    expect(update).toBeDisabled();
    expect(api.postRefresh).not.toHaveBeenCalled();

    await act(async () => { releaseBulk(); });
    await waitFor(() => expect(update).not.toBeDisabled());
    expect(api.postRefresh).not.toHaveBeenCalled();
    await userEvent.click(update);
    await waitFor(() => expect(api.postRefresh).toHaveBeenCalledTimes(1));
  });

  it("reports evidence upload progress without erasing an unrelated banner", async () => {
    let release: () => void = () => {};
    api.uploadReviewEvidence.mockImplementation(() => new Promise<void>((resolve) => { release = () => resolve(); }));
    api.postBulkReviewed.mockResolvedValueOnce({
      marked: [],
      skipped: [{ unit_key: "sym-0", path: "src/file-0.py", reason: "still needs individual attention" }],
      progress: TARGET_LIST.progress,
      outline_updates: [],
      group_counts: OVERVIEW.group_counts,
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await act(async () => stream.lastProps.onBulkReview("src/file-0.py"));
    expect(await screen.findByText(/1 kept for individual review/)).toBeTruthy();

    await act(async () => stream.lastProps.onContext(TARGETS[0], "evidence"));
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();

    act(() => drawer.lastProps.onUploadEvidence([new File(["x"], "shot.png", { type: "image/png" })]));
    await waitFor(() => expect(drawer.lastProps.uploading).toBe(true));

    await act(async () => { release(); await Promise.resolve(); });
    await waitFor(() => expect(drawer.lastProps.uploading).toBe(false));
    expect(screen.getByText(/1 kept for individual review/)).toBeTruthy();
  });

  it("clears a zero-match search with Escape after focus has left the search box", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "zzz-no-such-target");
    expect(await screen.findByText("0 review areas · 0 files")).toBeTruthy();
    expect(screen.getByText("No matches.")).toBeTruthy();

    act(() => search.blur());
    await userEvent.keyboard("{Escape}");

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
  });
});
