import { Button as UiButton } from "../components/ui/button";
import { afterEach, describe, expect, it, beforeEach, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import {
  LOCAL_DASHBOARD_PRODUCT_CAPABILITIES,
  LOCAL_PRODUCT_CAPABILITIES,
  ProductCapabilitiesProvider,
  REVIEW_ONLY_PRODUCT_CAPABILITIES,
} from "../lib/RuntimeCapabilities";
import ReviewReader from "./ReviewReader";
import { readReviewBacktrail, writeReviewBacktrail } from "./reviewBacktrail";
import { commentDraftKey } from "./reviewDraftPersistence";
import { REVIEW_MAX_PERSISTENT_VIEWS, REVIEW_PERSISTENT_VIEW_LABELS, requiredPaletteCoverage } from "./reviewUxGate";
import type { Annotation, FileDetail, ReviewChangeProposal, RevisionTargetRef, ReviewOutlineItem, ReviewOverview, ReviewProjection, ReviewTarget, ReviewTargetList, ReviewTriage, SourceState } from "./types";

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
const environment = vi.hoisted(() => ({ brief: true, unsupported: [] as string[] }));
vi.mock("./reviewEnvironment", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./reviewEnvironment")>();
  return {
    ...actual,
    useReviewEnvironment: (mode: Parameters<typeof actual.fallbackReviewEnvironment>[0] = "local") => {
      const fallback = actual.fallbackReviewEnvironment(mode);
      return {
        ...fallback,
        unsupported: environment.unsupported,
        review: {
          ...fallback.review,
          brief: environment.brief,
          ask: !environment.unsupported.includes("Ask"),
        },
      };
    },
  };
});
vi.mock("./ContextDrawer", () => ({
  default: (props: any) => {
    drawer.lastProps = props;
    return (
      <div data-testid="context-drawer-mock">
        <span>{props.tab} · {props.pinned ? "pinned" : "overlay"}</span>
        <UiButton unstyled type="button" onClick={props.onClose}>Close context mock</UiButton>
      </div>
    );
  },
}));
vi.mock("./ReviewMapSheet", () => ({
  default: (props: any) => {
    mapSheet.lastProps = props;
    return (
      <div role="dialog" aria-label="Review Map mock">
        <UiButton unstyled
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
        </UiButton>
        <UiButton unstyled
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
        </UiButton>
        <UiButton unstyled
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
        </UiButton>
        {props.askPanel}
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

const unmockedBrowserJson = async (path: string): Promise<unknown> => {
  throw new Error(`${path} not mocked`);
};

const api = vi.hoisted(() => ({
  requestBrowserJson: vi.fn(async (path: string): Promise<unknown> => {
    throw new Error(`${path} not mocked`);
  }),
  adoptBootstrapFragment: vi.fn(),
  deliverFeedback: vi.fn(),
  fetchOverview: vi.fn(),
  fetchStartupOverview: vi.fn(),
  fetchTargets: vi.fn(),
  fetchStartupTargets: vi.fn(),
  fetchTargetsForPath: vi.fn(),
  fetchAnnotations: vi.fn(),
  fetchReviewEvidence: vi.fn(),
  fetchReviewReadiness: vi.fn(),
  armReviewLanding: vi.fn(),
  cancelReviewLanding: vi.fn(),
  fetchReviewVerification: vi.fn(),
  fetchReviewDependencies: vi.fn(),
  fetchHistoricalReviewMap: vi.fn(),
  fetchReviewProjectContext: vi.fn(),
  fetchReviewJournal: vi.fn(),
  fetchReviewDevelopment: vi.fn(),
  fetchHistoricalReviewDevelopment: vi.fn(),
  fetchReviewAsk: vi.fn(),
  postReviewAsk: vi.fn(),
  sendReviewAskHandoff: vi.fn(),
  nextLiveState: vi.fn(),
  liveTerminal: null as null | ((status: number) => void),
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
  undoBulkReviewed: vi.fn(),
  postMark: vi.fn(),
  postRefresh: vi.fn(),
  publishReviewSnapshot: vi.fn(),
  postReviewOutcome: vi.fn(),
  postAnnotation: vi.fn(),
  postAiReview: vi.fn(),
  cancelAiReview: vi.fn(),
  postDecisionEvent: vi.fn(),
  postSynthesis: vi.fn(),
  patchAnnotation: vi.fn(),
  fetchRelatedSource: vi.fn(),
  finishReview: vi.fn(),
  exportFeedback: vi.fn(),
  uploadReviewEvidence: vi.fn(),
  linkReviewEvidence: vi.fn(),
  fetchProjection: vi.fn(),
  fetchBriefPreparation: vi.fn(),
  fetchHistoricalProjection: vi.fn(),
  previewGitHubPublish: vi.fn(),
  publishGitHubReview: vi.fn(),
  fetchReviewDestinationOptions: vi.fn(),
  putReviewDestination: vi.fn(),
  fetchFixLines: vi.fn(),
  fetchHostedAttachment: vi.fn(),
  resolveHostedAction: vi.fn(),
  detachHostedAttachment: vi.fn(),
}));

vi.mock("./reviewApi", () => ({
  authenticatedFetch: vi.fn(async () => Response.json({ notes: [] })),
  requestBrowserJson: api.requestBrowserJson,
  adoptBootstrapFragment: api.adoptBootstrapFragment,
  selectReviewId: vi.fn(),
  reviewDirectoryHref: () => "/reviews#t=t",
  reviewHref: (reviewId: string) => `/r/${reviewId}`,
  historicalReviewHref: (reviewId: string, revisionNumber: number) => `/r/${reviewId}?base=${revisionNumber}&target=${revisionNumber}`,
  reviewCompareHref: (reviewId: string, base: number, target: number) => `/r/${reviewId}?base=${base}&target=${target}`,
  sourceCompareHref: (fromRef: string, toRef: string) => `/r/x/${encodeURIComponent(fromRef)}..${encodeURIComponent(toRef)}`,
  deliverFeedback: api.deliverFeedback,
  fetchOverview: api.fetchOverview,
  fetchStartupOverview: api.fetchStartupOverview,
  fetchTargets: api.fetchTargets,
  fetchStartupTargets: api.fetchStartupTargets,
  fetchTargetsForPath: api.fetchTargetsForPath,
  fetchAnnotations: api.fetchAnnotations,
  fetchReviewEvidence: api.fetchReviewEvidence,
  fetchReviewReadiness: api.fetchReviewReadiness,
  armReviewLanding: api.armReviewLanding,
  cancelReviewLanding: api.cancelReviewLanding,
  fetchReviewVerification: api.fetchReviewVerification,
  fetchReviewDependencies: api.fetchReviewDependencies,
  fetchHistoricalReviewMap: api.fetchHistoricalReviewMap,
  fetchReviewProjectContext: api.fetchReviewProjectContext,
  fetchReviewJournal: api.fetchReviewJournal,
  fetchReviewDevelopment: api.fetchReviewDevelopment,
  fetchHistoricalReviewDevelopment: api.fetchHistoricalReviewDevelopment,
  fetchReviewDevelopmentEvents: vi.fn(async () => ({ events: [], next_before: null })),
  fetchReviewAsk: api.fetchReviewAsk,
  postReviewAsk: api.postReviewAsk,
  sendReviewAskHandoff: api.sendReviewAskHandoff,
  // Feed deferred observations through the subscription callback, as SSE does.
  subscribeLiveState: (_id: string, onState: (state: any) => Promise<void>, _retryMs?: number, onTerminal?: (status: number) => void) => {
    let live = true;
    api.liveTerminal = onTerminal ?? null;
    const feed = async () => {
      while (live) {
        const state = await api.nextLiveState();
        if (live) {
          try { await onState(state); } catch { /* A failed event reconnects with fresh state. */ }
        }
      }
    };
    void feed();
    return () => { live = false; };
  },
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
  subscribeSourceState: (_id: string, onState: (state: any) => void, retryMs = 2000) => {
    let live = true;
    let timer = 0;
    const feed = async () => {
      const state = await api.fetchSourceState();
      if (!live) return;
      onState(state);
      timer = window.setTimeout(() => void feed(), retryMs);
    };
    void feed();
    return () => { live = false; window.clearTimeout(timer); };
  },
  fetchChangeProposals: api.fetchChangeProposals,
  fetchProposalSelection: api.fetchProposalSelection,
  createChangeProposal: api.createChangeProposal,
  applyChangeProposal: api.applyChangeProposal,
  postBulkReviewed: api.postBulkReviewed,
  undoBulkReviewed: api.undoBulkReviewed,
  postMark: api.postMark,
  postRefresh: api.postRefresh,
  publishReviewSnapshot: api.publishReviewSnapshot,
  postReviewOutcome: api.postReviewOutcome,
  postAnnotation: api.postAnnotation,
  postAiReview: api.postAiReview,
  cancelAiReview: api.cancelAiReview,
  postDecisionEvent: api.postDecisionEvent,
  postSynthesis: api.postSynthesis,
  patchAnnotation: api.patchAnnotation,
  fetchRelatedSource: api.fetchRelatedSource,
  finishReview: api.finishReview,
  exportFeedback: api.exportFeedback,
  uploadReviewEvidence: api.uploadReviewEvidence,
  linkReviewEvidence: api.linkReviewEvidence,
  fetchProjection: api.fetchProjection,
  fetchBriefPreparation: api.fetchBriefPreparation,
  fetchHistoricalProjection: api.fetchHistoricalProjection,
  previewGitHubPublish: api.previewGitHubPublish,
  publishGitHubReview: api.publishGitHubReview,
  fetchReviewDestinationOptions: api.fetchReviewDestinationOptions,
  putReviewDestination: api.putReviewDestination,
  fetchFixLines: api.fetchFixLines,
  fetchHostedAttachment: api.fetchHostedAttachment,
  resolveHostedAction: api.resolveHostedAction,
  detachHostedAttachment: api.detachHostedAttachment,
}));

const correctionsApi = vi.hoisted(() => ({
  fetchReviewActions: vi.fn(),
  previewReviewAction: vi.fn(),
  startReviewAction: vi.fn(),
  cancelReviewAction: vi.fn(),
}));
vi.mock("./reviewActionsApi", () => correctionsApi);

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
  const revision = { ...OVERVIEW.revision, id: "rrv-large" };
  return {
    overview: { ...OVERVIEW, revision, latest_revision: revision, unit_count: 3200, target_count: 1500, progress, outline: rows },
    targets: { revision_id: revision.id, order: "recommended", targets, progress, outline: rows },
  };
}

function compactLargeStartup(full: { overview: ReviewOverview; targets: ReviewTargetList }) {
  return {
    overview: {
      ...full.overview,
      startup_compact: true,
      brief: null,
      groups: [],
      group_counts: {} as ReviewOverview["group_counts"],
      chapters: {} as ReviewOverview["chapters"],
      outline: [],
    } satisfies ReviewOverview,
    targets: {
      ...full.targets,
      targets: full.targets.targets.slice(0, 50),
      outline: full.targets.outline.map((row) => ({
        ...row,
        reasons: [],
        changed_lines: 3,
      })),
      complete: false,
    } satisfies ReviewTargetList,
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
  await userEvent.click(screen.getByRole("menuitem", { name: "Update from current source" }));
  expect(await screen.findByText("Snapshot superseded elsewhere")).toBeTruthy();
}

beforeEach(() => {
  vi.clearAllMocks();
  api.fetchOverview.mockReset();
  api.fetchTargets.mockReset();
  api.fetchStartupOverview.mockReset();
  api.fetchStartupTargets.mockReset();
  api.fetchTargetsForPath.mockReset();
  api.fetchProjection.mockReset();
  api.fetchHistoricalProjection.mockReset();
  api.fetchReviewSurfaces.mockReset();
  api.fetchFile.mockReset();
  window.sessionStorage.clear();
  window.localStorage.clear();
  window.history.replaceState(null, "", "/review");
  document.documentElement.classList.remove("light", "dark");
  stream.lastProps = null;
  drawer.lastProps = null;
  mapSheet.lastProps = null;
  api.adoptBootstrapFragment.mockReturnValue({ token: "token", reviewId: "review-1" });
  api.fetchReviewPreparation.mockResolvedValue({ state: "ready", detail: "" });
  environment.brief = true;
  environment.unsupported = [];
  const unavailableProjection = Object.assign(new Error("projection unavailable"), { status: 422 });
  api.fetchProjection.mockRejectedValue(unavailableProjection);
  api.fetchHistoricalProjection.mockRejectedValue(unavailableProjection);
  api.fetchOverview.mockResolvedValue(OVERVIEW);
  api.fetchTargets.mockResolvedValue(TARGET_LIST);
  api.fetchStartupOverview.mockImplementation((id: string) => api.fetchOverview(id));
  api.fetchStartupTargets.mockImplementation((id: string, order?: "recommended" | "file") => api.fetchTargets(id, order));
  api.fetchTargetsForPath.mockImplementation((id: string, _path: string, order?: "recommended" | "file") => api.fetchTargets(id, order));
  api.fetchFixLines.mockResolvedValue({ fix_lines: [], fix_of: null });
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
  api.fetchHistoricalReviewMap.mockRejectedValue(new Error("architecture delta unavailable"));
  // PK2: no Project Knowledge configured unless a test says otherwise.
  api.fetchReviewProjectContext.mockResolvedValue({
    review_id: "review-1",
    revision_id: "revision-1",
    revision_number: 0,
    project_context: { available: false, reason: "not_configured", entries: [], narration: [], omitted: 0 },
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
  api.nextLiveState.mockImplementation(() => new Promise(() => {}));
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

function briefProjection(overrides: Partial<ReviewProjection> = {}): ReviewProjection {
  return {
    projection_version: 1,
    review_id: OVERVIEW.session.id,
    revision_id: OVERVIEW.revision.id,
    snapshot_number: 0,
    historical: false,
    plan: { schema_version: 1, input_digest: "digest", intents: [{ producer: "claude:s", intent: "Sessions report invalid tokens." }], limitations: [] },
    actions: [],
    questions: [],
    items: [{
      id: "item:first",
      key: "first",
      kind: "behavior",
      title: "First behavior",
      before: "",
      after: "Invalid tokens are reported.",
      rationale: "",
      rationale_origin: "author",
      source_label: "Author's agent note",
      producer: "claude:s",
      stale: false,
      attention: "understand",
      tier: 4,
      position: 0,
      primary_target_ids: ["target-3"],
      related_target_ids: [],
      claim_ids: [],
      question_ids: [],
      concerns: [],
      unresolved_refs: [],
    }],
    claims: [],
    decisions: [],
    coverage: { target_count: 30, author_explained: 1, fallback_grouped: 29, other: 0, routine: 0, unresolved_refs: 0, unexplained_targets: 29, file_count: 3 },
    counts: { review_first: 0, understand: 1, routine: 0, done: 0, decisions: 0 },
    queue: TARGETS.map((target) => target.target_id),
    ...overrides,
  };
}

function mapProjection(): ReviewProjection {
  const base = briefProjection();
  return {
    ...base,
    items: [
      ...base.items,
      {
        ...base.items[0],
        id: "item:architecture",
        key: "architecture",
        title: "Architecture boundary",
        primary_target_ids: ["target-4"],
        position: 1,
      },
    ],
    coverage: { ...base.coverage, file_count: 4 },
  };
}

function enableMap(): void {
  window.localStorage.setItem(`lc.review.view.${OVERVIEW.session.id}`, "implementation");
  api.fetchProjection.mockResolvedValue(mapProjection());
}

function viewButton(name: "Brief" | "Code" | "Map"): HTMLElement {
  return within(screen.getByRole("group", { name: "Review views" })).getByRole("button", { name });
}

describe("ReviewReader Brief", () => {
  it("opens a new review on the Brief and jumps to the exact implementation target", async () => {
    api.fetchProjection.mockResolvedValue(briefProjection());
    render(<ReviewReader />);
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();
    expect(viewButton("Brief").getAttribute("aria-pressed")).toBe("true");
    expect(new URLSearchParams(window.location.search).get("view")).toBe("brief");
    expect(screen.getByText("Sessions report invalid tokens.")).toBeInTheDocument();

    await userEvent.click(within(screen.getByTestId("brief-item-item:first")).getByRole("button", { name: "Open code" }));
    await waitFor(() => expect(screen.queryByTestId("review-brief")).toBeNull());
    expect(viewButton("Code").getAttribute("aria-pressed")).toBe("true");
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalledWith(expect.objectContaining({ target_id: "target-3" }), expect.anything()));
    expect(window.localStorage.getItem(`lc.review.view.${OVERVIEW.session.id}`)).toBe("implementation");
  });

  it("searches Snapshot concepts from the Brief and resolves them to exact Review targets", async () => {
    const base = briefProjection();
    api.fetchProjection.mockResolvedValue({
      ...base,
      items: [{ ...base.items[0], title: "Privilege boundary rotation", rationale: "Refresh session authority after escalation" }],
    });

    render(<ReviewReader />);
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();
    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "privilege rotation");
    expect(await screen.findByText("1 review area · 1 file")).toBeInTheDocument();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-3"));
  });

  it("keeps the startup screen until the Brief a new review lands on is ready, then opens on it", async () => {
    const reply = deferred<ReviewProjection>();
    api.fetchProjection.mockReturnValue(reply.promise);
    // Every DOM state the reviewer could see: the Reader never appears without its Brief.
    const shellWithoutBrief: string[] = [];
    const observer = new MutationObserver(() => {
      if (screen.queryByTestId("review-reader") && !screen.queryByTestId("review-brief")) {
        shellWithoutBrief.push(new URLSearchParams(window.location.search).get("view") ?? "implementation");
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    try {
      render(<ReviewReader />);
      // The first diff is ready; only the Brief is pending.
      expect(await screen.findByRole("status")).toHaveTextContent("Preparing Brief");
      expect(screen.getByTestId("review-loading-screen")).toBeInTheDocument();
      expect(screen.queryByTestId("review-reader")).toBeNull();

      await act(async () => { reply.resolve(briefProjection()); await reply.promise; });
      expect(await screen.findByTestId("review-brief")).toBeInTheDocument();
      expect(viewButton("Brief").getAttribute("aria-pressed")).toBe("true");
      expect(screen.queryByTestId("review-loading-screen")).toBeNull();
      expect(shellWithoutBrief).toEqual([]);
    } finally {
      observer.disconnect();
    }
  });

  it("retries a transient Brief projection failure and still lands on the Brief", async () => {
    api.fetchProjection
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(briefProjection());
    render(<ReviewReader />);
    expect(await screen.findByRole("status")).toHaveTextContent("Preparing Brief");
    expect(await screen.findByTestId("review-brief", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(api.fetchProjection).toHaveBeenCalledTimes(2);
    expect(viewButton("Brief").getAttribute("aria-pressed")).toBe("true");
  });

  it("opens Implementation when the Brief a new review would land on cannot load", async () => {
    const permanent = Object.assign(new Error("projection unavailable"), { status: 422 });
    api.fetchProjection.mockRejectedValue(permanent);
    render(<ReviewReader />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Preparing Brief"));
    expect(await screen.findByTestId("reader-stream-mock")).toBeInTheDocument();
    expect(api.fetchProjection).toHaveBeenCalledTimes(1);
    expect(viewButton("Code").getAttribute("aria-pressed")).toBe("true");
  });

  it("opens Ask beside the Brief without leaving it", async () => {
    const [item] = briefProjection().items;
    api.fetchProjection.mockResolvedValue(briefProjection({ items: [{ ...item, attention: "review_first", tier: 3 }] }));
    render(<ReviewReader />);
    const card = await screen.findByTestId("brief-item-item:first");
    await userEvent.click(within(card).getByRole("button", { name: "Ask" }));
    const view = screen.getByTestId("review-brief-view");
    expect(await within(view).findByText("Ask, challenge, or request a change")).toBeTruthy();
    expect(screen.getByTestId("review-brief")).toBeInTheDocument();
  });

  it("request change files the feedback and the decision in one write bound to the shown scope", async () => {
    const decision = {
      key: "retention",
      producer: "claude:s",
      question: "How long should checkpoints be retained?",
      options: [{ key: "indefinite", label: "Retain indefinitely" }, { key: "ninety", label: "90 days" }],
      selected_option: "indefinite",
      reason_human_required: "Unspecified.",
      assumptions: [],
      contract_scope: [],
      relied_checks: [],
      target_ids: ["target-3"],
      stale: false,
      read_only: false,
      conflict: false,
      positions: [],
      actions: ["accept", "request_change", "ask", "defer"] as const,
      state: {
        disposition: "open" as const,
        validity: null,
        changed_inputs: [],
        outstanding: true,
        navigable: true,
        withdrawn: false,
        trigger: null,
        trigger_fired: false,
        accepted_option: "",
        latest_event_id: "",
        latest_action: "",
        latest_reason: "",
        semantic_fingerprint: "sha256:scope-seen",
        event_snapshot_number: null,
        reason: "decision_open",
        detail: "Needs your decision",
        tier: 3,
        limitations: [],
      },
    };
    api.fetchProjection.mockResolvedValue(
      briefProjection({ revision_id: "rev-shown", decisions: [{ ...decision, actions: [...decision.actions] }] }),
    );
    api.postDecisionEvent.mockResolvedValueOnce({ event: { id: "dec-1" }, decision });
    render(<ReviewReader />);
    const card = await screen.findByTestId("brief-decision-retention");
    await userEvent.click(within(card).getByRole("button", { name: "Request change" }));
    await userEvent.type(within(card).getByRole("textbox", { name: "Requested change" }), "Expire after 90 days.");
    await userEvent.click(within(card).getByRole("button", { name: "Send request" }));
    await waitFor(() => expect(api.postDecisionEvent).toHaveBeenCalled());
    // The server files the request and records the decision together, so a
    // refused decision can never leave a stray request-change behind.
    expect(api.postAnnotation).not.toHaveBeenCalled();
    expect(api.postDecisionEvent).toHaveBeenCalledWith(
      OVERVIEW.session.id,
      "retention",
      expect.objectContaining({
        action: "request_change",
        feedback_body: "Expire after 90 days.",
        feedback_path: TARGETS[3].path,
        expected_revision_id: "rev-shown",
        expected_event_id: "",
        expected_semantic_fingerprint: "sha256:scope-seen",
      }),
    );
  });

  it("a refused decision write still refreshes what the Brief shows", async () => {
    const base = briefProjection();
    const decision = {
      key: "retention",
      producer: "claude:s",
      question: "How long should checkpoints be retained?",
      options: [{ key: "indefinite", label: "Retain indefinitely" }],
      selected_option: "indefinite",
      reason_human_required: "Unspecified.",
      assumptions: [],
      contract_scope: [],
      relied_checks: [],
      target_ids: ["target-3"],
      stale: false,
      read_only: false,
      conflict: false,
      positions: [],
      actions: ["accept", "ask"],
      state: {
        disposition: "open" as const,
        validity: null,
        changed_inputs: [],
        outstanding: true,
        navigable: true,
        withdrawn: false,
        trigger: null,
        trigger_fired: false,
        accepted_option: "",
        latest_event_id: "",
        latest_action: "",
        latest_reason: "",
        semantic_fingerprint: "sha256:scope-seen",
        event_snapshot_number: null,
        reason: "decision_open",
        detail: "Needs your decision",
        tier: 3,
        limitations: [],
      },
    };
    api.fetchProjection.mockResolvedValue({ ...base, decisions: [decision] });
    api.postDecisionEvent.mockRejectedValueOnce(new Error("the decision changed since you loaded it; reload before deciding"));
    render(<ReviewReader />);
    const card = await screen.findByTestId("brief-decision-retention");
    const loads = api.fetchProjection.mock.calls.length;
    await userEvent.click(within(card).getByRole("button", { name: "Accept decision" }));
    await waitFor(() => expect(api.fetchProjection.mock.calls.length).toBeGreaterThan(loads));
    expect(await screen.findByText(/changed since you loaded it/)).toBeInTheDocument();
  });

  it("a decision refused for a newer Snapshot never shows that Snapshot's targets or comments here", async () => {
    const base = briefProjection();
    const decision = {
      key: "retention", producer: "claude:s", question: "How long should checkpoints be retained?",
      options: [{ key: "indefinite", label: "Retain indefinitely" }], selected_option: "indefinite",
      reason_human_required: "Unspecified.", assumptions: [], contract_scope: [], relied_checks: [],
      target_ids: ["target-3"], stale: false, read_only: false, conflict: false, positions: [], actions: ["accept", "ask"],
      state: {
        disposition: "open" as const, validity: null, changed_inputs: [], outstanding: true, navigable: true,
        withdrawn: false, trigger: null, trigger_fired: false, accepted_option: "", latest_event_id: "",
        latest_action: "", latest_reason: "", semantic_fingerprint: "sha256:scope-seen", event_snapshot_number: null,
        reason: "decision_open", detail: "Needs your decision", tier: 3, limitations: [],
      },
    };
    api.fetchProjection.mockResolvedValue({ ...base, decisions: [decision] });
    api.postDecisionEvent.mockRejectedValueOnce(Object.assign(
      new Error("the Review advanced to a newer Snapshot; reload before deciding"),
      { status: 409 },
    ));
    render(<ReviewReader />);
    const card = await screen.findByTestId("brief-decision-retention");
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, revision_id: "rrv-2", targets: TARGETS.slice(0, 4) });
    api.fetchAnnotations.mockResolvedValue({ revision_id: "rrv-2", annotations: [handoffAnnotation("ann-next")], counts: {} });
    const targetReads = api.fetchTargets.mock.calls.length;

    await userEvent.click(within(card).getByRole("button", { name: "Accept decision" }));

    await waitFor(() => expect(api.fetchTargets.mock.calls.length).toBeGreaterThan(targetReads));
    expect((await screen.findAllByText(/Review advanced to a newer Snapshot/)).length).toBeGreaterThan(0);
    await userEvent.click(viewButton("Code"));
    expect(stream.lastProps.targets).toHaveLength(30);
    expect(stream.lastProps.annotations).toHaveLength(0);
    expect(screen.getAllByText("Snapshot superseded elsewhere").length).toBeGreaterThan(0);
  });

  it("opening or remounting Brief never originates automatic AI Review", async () => {
    const aiReview = {
      status: "none" as const,
      effort: "auto" as const,
      candidates: 1,
      findings: 0,
      auto_eligible: true,
      budget: { calls_used: 0, max_calls: 20 },
    };
    api.fetchProjection.mockResolvedValue(briefProjection({ revision_id: "rev-ai", ai_review: aiReview }));

    const first = render(<ReviewReader />);
    await screen.findByTestId("review-brief");
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(api.postAiReview).not.toHaveBeenCalled();

    first.unmount();
    render(<ReviewReader />);
    await screen.findByTestId("review-brief");
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(api.postAiReview).not.toHaveBeenCalled();
    expect(api.fetchProjection.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("does not call AI review when deterministic triage already proved the Snapshot unnecessary", async () => {
    api.fetchProjection.mockResolvedValue(briefProjection({
      ai_review: {
        status: "skipped_unnecessary",
        effort: "auto",
        candidates: 0,
        findings: 0,
        budget: { calls_used: 0, max_calls: 20 },
      },
    }));
    render(<ReviewReader />);
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(api.postAiReview).not.toHaveBeenCalled();
  });

  it("explains unexplained changes for the Snapshot on screen, then shows the result", async () => {
    const synthesis = { status: "none" as const, model: "openai:m", error: "", candidates: 1, rejected_count: 0, unsynthesized: 0 };
    api.fetchProjection.mockResolvedValue(briefProjection({ revision_id: "rev-shown", synthesis }));
    api.postSynthesis.mockResolvedValueOnce({ synthesis: { ...synthesis, status: "ready" } });
    render(<ReviewReader />);
    const button = await screen.findByRole("button", { name: "Explain unexplained changes" });
    const loads = api.fetchProjection.mock.calls.length;
    const targetLoads = api.fetchTargets.mock.calls.length;
    await userEvent.click(button);
    await waitFor(() => expect(api.postSynthesis).toHaveBeenCalledWith(OVERVIEW.session.id, "rev-shown"));
    // AI questions join the queue: both the plan and the targets refresh.
    await waitFor(() => expect(api.fetchProjection.mock.calls.length).toBeGreaterThan(loads));
    expect(api.fetchTargets.mock.calls.length).toBeGreaterThan(targetLoads);
  });

  it("keeps AI review polling off the diff navigation path and hydrates completed findings", async () => {
    const pending = briefProjection({ ai_review: {
      status: "pending", effort: "auto", candidates: 1, findings: 0,
      budget: { calls_used: 0, max_calls: 8 },
      execution: { status: "queued", job_id: "job-p4", revision_id: OVERVIEW.revision.id },
    } });
    const ready = briefProjection({ ai_review: {
      status: "ready", effort: "auto", candidates: 1, findings: 1,
      budget: { calls_used: 2, max_calls: 8 },
      execution: { status: "succeeded", job_id: "job-p4", revision_id: OVERVIEW.revision.id },
    } });
    api.fetchProjection.mockResolvedValueOnce(pending).mockResolvedValue(ready);
    render(<ReviewReader />);
    await screen.findByTestId("review-brief");
    await userEvent.click(viewButton("Code"));
    const loads = api.fetchProjection.mock.calls.length;
    const annotations = api.fetchAnnotations.mock.calls.length;
    await waitFor(() => expect(api.fetchProjection.mock.calls.length).toBeGreaterThan(loads), { timeout: 5500 });
    await waitFor(() => expect(api.fetchAnnotations.mock.calls.length).toBeGreaterThan(annotations));
    expect(viewButton("Code").getAttribute("aria-pressed")).toBe("true");
    expect(screen.queryByTestId("review-loading-screen")).toBeNull();
    expect(api.postAiReview).not.toHaveBeenCalled();
  });

  it("stops the current durable AI job without triggering a new model run", async () => {
    api.fetchProjection.mockResolvedValue(briefProjection({ ai_review: {
      status: "pending", effort: "auto", candidates: 1, findings: 0,
      budget: { calls_used: 0, max_calls: 8 },
      execution: { status: "queued", job_id: "job-42", revision_id: OVERVIEW.revision.id },
    } }));
    api.cancelAiReview.mockResolvedValue({ ai_review: { status: "cancel_requested", job_id: "job-42" } });
    render(<ReviewReader />);
    await screen.findByTestId("review-brief");
    await userEvent.click(screen.getByRole("button", { name: "Stop AI review" }));
    await waitFor(() => expect(api.cancelAiReview).toHaveBeenCalledWith(
      OVERVIEW.session.id, OVERVIEW.revision.id, "job-42",
    ));
    expect(api.postAiReview).not.toHaveBeenCalled();
  });

  it("keeps checking while another request is preparing explanations", async () => {
    const pending = { status: "pending" as const, model: "openai:m", error: "", candidates: 1, rejected_count: 0, unsynthesized: 0 };
    api.fetchProjection.mockResolvedValue(briefProjection({ synthesis: pending }));
    render(<ReviewReader />);
    expect(await screen.findByText("Preparing explanations…")).toBeInTheDocument();
    const loads = api.fetchProjection.mock.calls.length;
    await waitFor(() => expect(api.fetchProjection.mock.calls.length).toBeGreaterThan(loads), { timeout: 5000 });
  });

  it("an outline decision action opens the Brief on that decision", async () => {
    window.localStorage.setItem(`lc.review.view.${OVERVIEW.session.id}`, "implementation");
    const base = briefProjection();
    api.fetchProjection.mockResolvedValue({
      ...base,
      decisions: [
        {
          key: "retention",
          producer: "claude:s",
          question: "How long should checkpoints be retained?",
          options: [{ key: "indefinite", label: "Retain indefinitely" }],
          selected_option: "indefinite",
          reason_human_required: "Unspecified.",
          assumptions: [],
          contract_scope: [],
          relied_checks: [],
          target_ids: ["target-3"],
          stale: false,
          read_only: true,
        },
      ],
      actions: [
        {
          id: "decision:retention",
          reason: "decision_open",
          title: "How long should checkpoints be retained?",
          detail: "Needs your decision",
          item_ids: [],
          target_ids: ["target-3"],
          actions: ["accept"],
          tier: 3,
        },
      ],
    });
    render(<ReviewReader />);
    const outline = await screen.findByRole("complementary", { name: "Review outline" });
    expect(screen.queryByTestId("review-brief")).toBeNull();
    await userEvent.click(await within(outline).findByRole("button", { name: /How long should checkpoints be retained/ }));
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();
    expect(screen.getByTestId("brief-decision-retention")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /readiness/i })).toBeNull();
  });

  it("keeps the palette and review navigation live on the Brief", async () => {
    api.fetchProjection.mockResolvedValue(briefProjection());
    render(<ReviewReader />);
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();

    await userEvent.keyboard("p");
    expect(await screen.findByRole("dialog", { name: "Review actions" })).toBeTruthy();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Review actions" })).toBeNull());

    // Marks act on the current target, which is hidden under the Brief.
    await userEvent.keyboard("r");
    expect(api.postMark).not.toHaveBeenCalled();
    expect(screen.getByTestId("review-brief")).toBeInTheDocument();

    // Review navigation leaves the Brief for the code it moves to.
    await userEvent.keyboard("]");
    await waitFor(() => expect(screen.queryByTestId("review-brief")).toBeNull());
    expect(viewButton("Code").getAttribute("aria-pressed")).toBe("true");
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
  });

  it("keeps Implementation mounted but hidden under the Brief", async () => {
    api.fetchProjection.mockResolvedValue(briefProjection());
    render(<ReviewReader />);
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();
    const outline = screen.getByRole("complementary", { name: "Review outline" });
    expect(outline.closest(".invisible")).not.toBeNull();
    await userEvent.click(viewButton("Code"));
    await waitFor(() => expect(screen.queryByTestId("review-brief")).toBeNull());
    expect(outline.closest(".invisible")).toBeNull();
    expect(screen.getByTestId("reader-stream-mock")).toBeInTheDocument();
  });

  it("resumes the view the reviewer last chose", async () => {
    window.localStorage.setItem(`lc.review.view.${OVERVIEW.session.id}`, "implementation");
    api.fetchProjection.mockResolvedValue(briefProjection());
    render(<ReviewReader />);
    await waitFor(() => expect(api.fetchProjection).toHaveBeenCalled());
    expect(viewButton("Code").getAttribute("aria-pressed")).toBe("true");
    expect(screen.queryByTestId("review-brief")).toBeNull();
    await userEvent.click(viewButton("Brief"));
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();
  });

  it("names a feature the server declares off instead of silently hiding it", async () => {
    environment.unsupported = ["Ask"];
    render(<ReviewReader />);
    expect(await screen.findByTestId("reader-stream-mock")).toBeInTheDocument();
    expect((await screen.findAllByText(/does not support Ask/)).length).toBeGreaterThan(0);
  });

  it("says nothing about unsupported features when the server supports them all", async () => {
    render(<ReviewReader />);
    expect(await screen.findByTestId("reader-stream-mock")).toBeInTheDocument();
    expect(screen.queryByText(/does not support/)).toBeNull();
  });

  it("keeps Code as the only persistent Review category when Brief capability is off", async () => {
    environment.brief = false;
    api.fetchProjection.mockResolvedValue(briefProjection());
    render(<ReviewReader />);
    expect(await screen.findByTestId("reader-stream-mock")).toBeInTheDocument();
    const views = screen.getByRole("group", { name: "Review views" });
    expect(within(views).getByRole("button", { name: "Code" })).toBeTruthy();
    expect(within(views).queryByRole("button", { name: "Brief" })).toBeNull();
    expect(within(views).queryByRole("button", { name: "Map" })).toBeNull();
    expect(api.fetchProjection).not.toHaveBeenCalled();
  });

  it("shows a failing required check in the outline while reading implementation", async () => {
    window.localStorage.setItem(`lc.review.view.${OVERVIEW.session.id}`, "implementation");
    api.fetchProjection.mockResolvedValue(briefProjection({
      actions: [{
        id: "q:required:browser:checkout",
        reason: "required_browser_scenario_failed",
        title: "Required check failing",
        detail: "browser:checkout failed on Snapshot 0. No specific change item association is known.",
        item_ids: [],
        target_ids: [],
        actions: ["inspect_evidence"],
        tier: 1,
      }],
    }));
    render(<ReviewReader />);
    const outline = await screen.findByRole("complementary", { name: "Review outline" });
    expect(await within(outline).findByRole("button", { name: /Required check failing/ })).toBeInTheDocument();
  });

  it.each([
    ["a dedicated file tab on its target", "/review?review-item=file&review-path=src%2Ffile-3.py&review-target=target-3"],
    ["a dedicated surface tab", "/review?review-item=surface&review-provider=web&review-surface=%2Fstandalone"],
    ["a return to a file position", "/review?review-return-path=src%2Ffile-0.py"],
    ["a return to a surface position", "/review?review-return-surface=web%3A%2Fstandalone"],
    ["a Snapshot link", "/review?base=0&target=0"],
  ])("opens Implementation, never the Brief, for %s", async (_name, url) => {
    window.history.replaceState(null, "", url);
    const reply = deferred<ReviewProjection>();
    api.fetchProjection.mockReturnValue(reply.promise);
    render(<ReviewReader />);
    await waitFor(() => expect(api.fetchProjection).toHaveBeenCalled());
    // The Brief is ready: only now could a new review switch to it.
    await act(async () => { reply.resolve(briefProjection()); await reply.promise; });

    expect(viewButton("Code").getAttribute("aria-pressed")).toBe("true");
    expect(screen.queryByTestId("review-brief")).toBeNull();
    expect(new URLSearchParams(window.location.search).get("view")).toBeNull();
  });

  it("reopens a review on the Brief when the reviewer last left it there", async () => {
    window.localStorage.setItem(`lc.review.view.${OVERVIEW.session.id}`, "brief");
    api.fetchProjection.mockResolvedValue(briefProjection());
    render(<ReviewReader />);
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();
    expect(viewButton("Brief").getAttribute("aria-pressed")).toBe("true");
  });

  it("keeps the selected target, file, Snapshot and scroll across Brief · Map · Code", async () => {
    enableMap();
    window.history.replaceState(null, "", "/review?base=0&target=0");
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const streamNode = screen.getByTestId("reader-stream-mock");
    // The stream reports where the reviewer has scrolled to.
    act(() => stream.lastProps.onActiveTarget("target-12"));
    await waitFor(() => expect(stream.lastProps.activeTargetId).toBe("target-12"));
    stream.scrollToTarget.mockClear();
    stream.scrollToFile.mockClear();
    const overviewLoads = api.fetchOverview.mock.calls.length;
    const targetLoads = api.fetchTargets.mock.calls.length;

    await userEvent.click(viewButton("Brief"));
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();
    await userEvent.click(viewButton("Map"));
    expect(await screen.findByRole("dialog", { name: "Review Map mock" })).toBeTruthy();
    // The Map opens on the selected file.
    expect(mapSheet.lastProps.location.path).toBe("src/file-1.py");
    await userEvent.click(viewButton("Code"));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Review Map mock" })).toBeNull());

    // The same mounted stream, target and Snapshot; nothing re-scrolled or reloaded.
    expect(screen.getByTestId("reader-stream-mock")).toBe(streamNode);
    expect(stream.lastProps.activeTargetId).toBe("target-12");
    expect(stream.scrollToTarget).not.toHaveBeenCalled();
    expect(stream.scrollToFile).not.toHaveBeenCalled();
    const params = new URLSearchParams(window.location.search);
    expect([params.get("base"), params.get("target")]).toEqual(["0", "0"]);
    expect(api.fetchOverview.mock.calls.length).toBe(overviewLoads);
    expect(api.fetchTargets.mock.calls.length).toBe(targetLoads);
  });

  it("resume, the Next card, ] from the Brief and the primary action all follow one queue", async () => {
    // Source order puts target-0 first; the server's queue puts target-25 then target-7.
    const queueOrder = [25, 7, ...TARGETS.map((_, index) => index).filter((index) => index !== 25 && index !== 7)];
    const queued: ReviewTarget[] = TARGETS.map((target, index) => ({
      ...target,
      priority: {
        version: 1,
        tier: index === 25 ? 1 : 4,
        attention: index === 25 ? ("review_first" as const) : ("understand" as const),
        outstanding: true,
        position: queueOrder.indexOf(index),
        reasons: index === 25
          ? [{ code: "request_change_open", detail: "Requested change is still open" }]
          : [{ code: "unreviewed", detail: "Not reviewed yet" }],
      },
    }));
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, targets: queued });
    api.fetchProjection.mockResolvedValue(briefProjection({ queue: queueOrder.map((index) => `target-${index}`) }));

    render(<ReviewReader />);
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();
    // Resume: the selection and the Next card are the queue's first item.
    expect(stream.lastProps.activeTargetId).toBe("target-25");
    expect(within(screen.getByTestId("review-guide")).getByText("Requested change is still open")).toBeTruthy();

    // ] from the Brief opens that item instead of skipping past it.
    await userEvent.keyboard("]");
    await waitFor(() => expect(screen.queryByTestId("review-brief")).toBeNull());
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-25"));

    // The existing Next card leaves the Brief for the same item.
    await userEvent.click(viewButton("Brief"));
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();
    stream.scrollToTarget.mockClear();
    await userEvent.click(within(screen.getByTestId("review-guide")).getByRole("button", { name: "Review first: fn_25" }));
    await waitFor(() => expect(screen.queryByTestId("review-brief")).toBeNull());
    expect(viewButton("Code").getAttribute("aria-pressed")).toBe("true");
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-25"));

    // In Implementation, ] continues down the same queue.
    await userEvent.keyboard("]");
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-7"));
  });
});

describe("ReviewReader scoped request", () => {
  const scope = { kind: "targets" as const, values: ["sym-12", "sym-13", "sym-14"], requestedBy: "author_a" };
  const streamedIds = () => stream.lastProps.targets.map((target: ReviewTarget) => target.target_id);

  it("a scoped request is the default queue and every outside target stays one click away", async () => {
    render(<ReviewReader extensions={{ requestScope: scope }} />);
    expect(await screen.findByText("3 streamed targets")).toBeTruthy();
    expect(streamedIds()).toEqual(["target-12", "target-13", "target-14"]);
    await waitFor(() => expect(stream.lastProps.activeTargetId).toBe("target-12"));
    const bar = screen.getByTestId("request-scope-bar");
    expect(bar.textContent).toContain("Your request · 3 of 30 targets · requested by author_a");

    // ] walks only the request.
    await userEvent.keyboard("]");
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-13"));

    // The outside targets are never hidden: one visible toggle shows all of them.
    const toggle = within(bar).getByRole("button", { name: "Show 27 outside your request" });
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    await userEvent.click(toggle);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(streamedIds()).toContain("target-0");
    expect(bar.textContent).toContain("All 30 targets · 3 in your request");

    await userEvent.click(within(bar).getByRole("button", { name: "Only your request" }));
    expect(await screen.findByText("3 streamed targets")).toBeTruthy();
  });

  it("search reaches an outside target while the request is focused", async () => {
    render(<ReviewReader extensions={{ requestScope: scope }} />);
    expect(await screen.findByText("3 streamed targets")).toBeTruthy();

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "fn_25");
    expect(await screen.findByText("1 streamed targets")).toBeTruthy();
    expect(streamedIds()).toEqual(["target-25"]);
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-25"));
  });

  it("does not narrow when the request covers every target or none of the current ones", async () => {
    const first = render(<ReviewReader extensions={{ requestScope: { kind: "paths", values: ["src/file-0.py", "src/file-1.py", "src/file-2.py"] } }} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(screen.queryByTestId("request-scope-bar")).toBeNull();
    first.unmount();

    render(<ReviewReader extensions={{ requestScope: { kind: "paths", values: ["src/renamed.py"] } }} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(screen.queryByTestId("request-scope-bar")).toBeNull();
  });
});

describe("ReviewReader feedback comparisons", () => {
  it("never shows Full review code while a Snapshot comparison is still loading", async () => {
    window.localStorage.setItem(`lc.review.view.${OVERVIEW.session.id}`, "implementation");
    window.history.replaceState(null, "", "/review?base=0&target=1");
    const current = {
      ...ADVANCED_OVERVIEW,
      latest_revision_number: 1,
      default_base_revision_number: 0,
      default_target_revision_number: 1,
    };
    const originalComparison = await api.fetchRevisionComparison();
    const pending = deferred<any>();
    api.fetchOverview.mockResolvedValue(current);
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, revision_id: current.revision.id });
    api.fetchRevisionComparison.mockReturnValue(pending.promise);

    render(<ReviewReader />);

    expect(await screen.findByTestId("review-comparison-placeholder")).toHaveTextContent(
      "Preparing Snapshot 0 → Snapshot 1 diff…",
    );
    expect(screen.queryByTestId("reader-stream-mock")).toBeNull();
    await waitFor(() => expect(api.fetchRevisionComparison).toHaveBeenCalledWith("review-1", 0, 1));

    await act(async () => {
      pending.resolve({
        ...originalComparison,
        from_revision: OVERVIEW.revision,
        to_revision: current.revision,
        code_files: [{
          path: "src/file-0.py",
          status: "changed",
          added_lines: 1,
          removed_lines: 1,
          patch: "--- rev-0/src/file-0.py\n+++ rev-1/src/file-0.py\n@@ -1 +1 @@\n-old\n+new\n",
        }],
      });
      await pending.promise;
    });

    expect(await screen.findByTestId("reader-stream-mock")).toBeTruthy();
    expect(screen.queryByTestId("review-comparison-placeholder")).toBeNull();
    expect(stream.lastProps.snapshotComparison).toEqual({
      fromRevision: 0,
      toRevision: 1,
      fromRevisionId: OVERVIEW.revision.id,
      toRevisionId: current.revision.id,
    });
  });

  it("opens on feedback delta and keeps Full review through capture and reload", async () => {
    const current = {
      ...ADVANCED_OVERVIEW, latest_revision_number: 1,
      default_base_revision_number: 0, default_target_revision_number: 1,
    };
    api.fetchOverview.mockResolvedValue(current);
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, revision_id: current.revision.id });
    api.fetchRevisionComparison.mockResolvedValue({
      ...(await api.fetchRevisionComparison()),
      from_revision: OVERVIEW.revision,
      to_revision: current.revision,
      code_files: [{
        path: "src/file-0.py",
        status: "changed",
        added_lines: 1,
        removed_lines: 1,
        patch: "--- rev-0/src/file-0.py\n+++ rev-1/src/file-0.py\n@@ -1 +1 @@\n-old\n+new\n",
      }],
    });
    const mounted = render(<ReviewReader />);
    expect(await screen.findByRole("button", { name: "Show full review" }, { timeout: 3000 })).toBeTruthy();
    const comparisonOutline = screen.getByRole("complementary", { name: "Review outline" });
    expect(within(comparisonOutline).queryByText("Change story")).toBeNull();
    expect(within(comparisonOutline).getByText("file-0.py")).toBeTruthy();
    expect(within(comparisonOutline).queryByText("file-1.py")).toBeNull();
    expect(new URLSearchParams(window.location.search).get("compare")).toBe("auto");
    expect(new URLSearchParams(window.location.search).get("base")).toBe("0");
    expect(new URLSearchParams(window.location.search).get("target")).toBe("1");
    expect(stream.lastProps.comparisonMode).toBe(false);
    expect(stream.lastProps.details).toBeTruthy();
    await waitFor(() => expect(api.fetchRevisionComparison).toHaveBeenCalledWith("review-1", 0, 1));
    expect(api.fetchRevisionComparison).toHaveBeenCalled();
    await waitFor(() => expect(stream.lastProps.snapshotComparison).toEqual({
      fromRevision: 0,
      toRevision: 1,
      fromRevisionId: OVERVIEW.revision.id,
      toRevisionId: current.revision.id,
    }));
    await userEvent.click(screen.getByRole("button", { name: "Show full review" }));
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(new URLSearchParams(window.location.search).get("compare")).toBe("full");
    expect(screen.queryByTestId("revision-compare-bar")).toBeNull();
    await waitFor(() => expect(stream.lastProps.snapshotComparison).toBeNull());

    const next = {
      ...current, revision: { ...current.revision, id: "rrv-3", revision_number: 2 },
      latest_revision_number: 2, default_base_revision_number: 1, default_target_revision_number: 2,
    };
    api.postRefresh.mockResolvedValueOnce(next);
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, revision_id: next.revision.id });
    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Update from current source" }));
    await waitFor(() => expect(api.postRefresh).toHaveBeenCalled());
    await waitFor(() => expect(api.fetchTargets.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(new URLSearchParams(window.location.search).get("compare")).toBe("full");
    expect(new URLSearchParams(window.location.search).has("base")).toBe(false);
    mounted.unmount();
    api.fetchOverview.mockResolvedValue(next);
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(screen.queryByTestId("revision-compare-bar")).toBeNull();
    expect(api.postMark).not.toHaveBeenCalled();
    expect(api.finishReview).not.toHaveBeenCalled();
  });

  it("advances the automatic feedback delta when a newer Snapshot arrives", async () => {
    const current = {
      ...ADVANCED_OVERVIEW, latest_revision_number: 1,
      default_base_revision_number: 0, default_target_revision_number: 1,
    };
    api.fetchOverview.mockResolvedValue(current);
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, revision_id: current.revision.id });
    api.fetchRevisionComparison.mockResolvedValue({
      ...(await api.fetchRevisionComparison()),
      from_revision: OVERVIEW.revision, to_revision: current.revision,
    });

    render(<ReviewReader />);
    expect(await screen.findByRole("button", { name: "Show full review" })).toBeTruthy();
    expect(new URLSearchParams(window.location.search).get("compare")).toBe("auto");

    const next = {
      ...current, revision: { ...current.revision, id: "rrv-3", revision_number: 2 },
      latest_revision_number: 2, default_base_revision_number: 1, default_target_revision_number: 2,
    };
    api.postRefresh.mockResolvedValueOnce(next);
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, revision_id: next.revision.id });
    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Update from current source" }));
    await waitFor(() => expect(api.postRefresh).toHaveBeenCalled());

    await waitFor(() => {
      const params = new URLSearchParams(window.location.search);
      expect(params.get("compare")).toBe("auto");
      expect(params.get("base")).toBe("1");
      expect(params.get("target")).toBe("2");
    });
  });
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

    // Comparison selection now lives under revision history rather than in a
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

  it("compares two Snapshots over only the review areas whose content changed between them", async () => {
    const rev0 = { ...OVERVIEW.revision, revision_number: 0 };
    const rev1 = { ...OVERVIEW.revision, id: "rrv-2", ref: "rr/rrv-2", revision_number: 1 };
    api.fetchOverview.mockResolvedValue({ ...OVERVIEW, revision: rev1, revision_count: 2, latest_revision_number: 1, latest_revision: rev1 });
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, revision_id: rev1.id });
    api.fetchRevisionComparison.mockResolvedValue({
      review_id: OVERVIEW.session.id,
      reviewer_id: OVERVIEW.session.reviewer_id,
      from_revision: rev0,
      to_revision: rev1,
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
      summary: { added: 0, changed: 2, removed: 0, unchanged: 28 },
      files: [],
      code_available: true,
      code_unavailable_reason: "",
      code_summary: { files: 2, added_lines: 2, removed_lines: 2 },
      code_files: ["src/file-0.py", "src/file-1.py"].map((path) => ({
        path,
        status: "changed",
        added_lines: 1,
        removed_lines: 1,
        patch: `--- rev-0/${path}\n+++ rev-1/${path}\n@@ -1 +1 @@\n-old\n+new\n`,
      })),
      // The agent rewrote target-3 only; file-1 changed outside every area.
      units: [
        { unit_key: "sym-3", path: "src/file-0.py", kind: "symbol", symbol: "f3", status: "changed", from_state: "needs_changes", to_state: "needs_changes", from_start_line: 3, to_start_line: 3 },
        { unit_key: "file:src/file-1.py", path: "src/file-1.py", kind: "file", symbol: "", status: "changed", from_state: "unreviewed", to_state: "unreviewed", from_start_line: 0, to_start_line: 0 },
      ],
    });
    window.history.replaceState(null, "", "/review?base=0&target=1");

    render(<ReviewReader />);

    expect(await screen.findByText("Comparing Snapshot 0 → Snapshot 1")).toBeTruthy();
    // Never-judged, unchanged areas of the same files are not in the delta; a
    // changed file no area covers still shows as one file row.
    await waitFor(() => expect(stream.lastProps.targets.map((target: ReviewTarget) => target.target_id)).toEqual([
      "target-3",
      "revision-compare:src/file-1.py",
    ]));
  });

  it("resolving inside a comparison up to the latest Snapshot shows the resolve, not the comparison's copy", async () => {
    const rev0 = { ...OVERVIEW.revision, revision_number: 0 };
    const rev1 = { ...OVERVIEW.revision, id: "rrv-2", ref: "rr/rrv-2", revision_number: 1 };
    api.fetchOverview.mockResolvedValue({ ...OVERVIEW, revision: rev1, revision_count: 2, latest_revision_number: 1, latest_revision: rev1 });
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, revision_id: rev1.id });
    const addressed = {
      id: "ann-1", review_id: "review-1", revision_id: "rrv-1", parent_id: "", kind: "request_change", state: "open",
      body: "Guard it", title: "", unit_key: "sym-0", path: "src/file-0.py", side: "new", start_line: 1, end_line: 1,
      author_response: "addressed", origin_revision_number: 0, relation_to_revision: "inherited", author_addressed_revision_number: 1,
    };
    api.fetchAnnotations.mockResolvedValue({ revision_id: rev1.id, annotations: [addressed], counts: {} });
    api.fetchRevisionComparison.mockResolvedValue({
      review_id: OVERVIEW.session.id,
      reviewer_id: OVERVIEW.session.reviewer_id,
      from_revision: rev0,
      to_revision: rev1,
      suggested_from_revision_number: 0,
      revision_state: {
        latest_revision_number: 1,
        last_reviewed_revision_number: 0,
        last_commented_revision_number: 0,
        default_base_revision_number: 0,
        default_target_revision_number: 1,
      },
      annotations: [{ ...addressed, addressed_by_revision: true }],
      annotation_groups: { originating: [], inherited: ["ann-1"], addressed_by_target: ["ann-1"] },
      summary: { added: 0, changed: 1, removed: 0, unchanged: 29 },
      files: [],
      code_available: true,
      code_unavailable_reason: "",
      code_summary: { files: 1, added_lines: 1, removed_lines: 1 },
      code_files: [{
        path: "src/file-0.py",
        status: "changed",
        added_lines: 1,
        removed_lines: 1,
        patch: "--- rev-0/src/file-0.py\n+++ rev-1/src/file-0.py\n@@ -1 +1 @@\n-old\n+new\n",
      }],
      units: [],
    });
    window.history.replaceState(null, "", "/review?base=0&target=1");
    const stateOf = () => stream.lastProps.annotations.find((item: { id: string }) => item.id === "ann-1")?.state;

    render(<ReviewReader />);
    expect(await screen.findByText("Comparing Snapshot 0 → Snapshot 1")).toBeTruthy();
    await waitFor(() => expect(stateOf()).toBe("open"));

    api.fetchAnnotations.mockResolvedValue({ revision_id: rev1.id, annotations: [{ ...addressed, state: "resolved" }], counts: {} });
    act(() => { stream.lastProps.onSetAnnotationState("ann-1", "resolved"); });
    await waitFor(() => expect(api.patchAnnotation).toHaveBeenCalledWith("ann-1", { state: "resolved" }, rev1.id));
    await waitFor(() => expect(stream.lastProps.busy).toBe(false));
    expect(stateOf()).toBe("resolved");
    // Still told apart as a comment carried in from Snapshot 0.
    expect(stream.lastProps.annotations.find((item: { id: string }) => item.id === "ann-1")?.relation_to_revision).toBe("inherited");
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
    expect(stream.lastProps.comparisonMode).toBe(true);
    expect(stream.lastProps.snapshotComparison).toBeNull();
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

  it("does not restore a tab preparation job when opening a saved short Review link", async () => {
    render(<MemoryRouter initialEntries={["/r/5f5be0d3"]}>
      <Routes><Route path="/r/:reviewRef" element={<ReviewReader />} /></Routes>
    </MemoryRouter>);

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(api.adoptBootstrapFragment).toHaveBeenCalledWith({ restorePreparation: false });
    expect(api.fetchReviewPreparation).not.toHaveBeenCalled();
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

  it("a 403 on load says access was lost and offers All reviews instead of a Retry that cannot work", async () => {
    api.fetchOverview.mockRejectedValue(Object.assign(new Error("Forbidden"), { status: 403 }));

    render(<ReviewReader />);

    expect(await screen.findByText("You no longer have access to this review")).toBeTruthy();
    expect(screen.getByText(/Ask your administrator/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "All reviews" }).getAttribute("href")).toBe("/reviews");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.queryByText("Review couldn't load")).toBeNull();
  });

  it.each([
    [403, "Access to this review was removed"],
    [404, "This review is no longer available"],
  ])("a terminal %i on the live stream makes the Reader read-only and says why", async (status, label) => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.liveTerminal).toBeTypeOf("function"));

    act(() => { api.liveTerminal!(status); });

    expect((await screen.findAllByText(label)).length).toBeGreaterThan(0);
    // The stream is locked the same way every read-only Reader locks it.
    await waitFor(() => expect(stream.lastProps.busy).toBe(true));
    expect(screen.getAllByRole("link", { name: "All reviews" }).length).toBeGreaterThan(0);
  });

  it("waits for a real diff before showing the Reader shell, then opens automatically", async () => {
    const pending = new Map([
      ["src/file-0.py", deferred<FileDetail>()],
      ["src/file-1.py", deferred<FileDetail>()],
      ["src/file-2.py", deferred<FileDetail>()],
    ]);
    api.fetchFile.mockImplementation(async (_id: string, path: string) => pending.get(path)!.promise);

    render(<ReviewReader />);

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Preparing first visual diff"));
    const loadingScreen = screen.getByTestId("review-loading-screen");
    expect(loadingScreen).toHaveClass("min-h-screen");
    expect(loadingScreen).not.toHaveClass("fixed", "h-dvh", "overflow-hidden", "overscroll-none");
    expect(within(loadingScreen).getByRole("list")).not.toHaveClass("overflow-y-auto", "max-h-[60vh]");
    expect(screen.getAllByRole("region", { name: "Review preparation" })).toHaveLength(1);
    expect(screen.getByRole("progressbar", { name: "Review preparation steps" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Preparing your review" })).toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: /Review Status:/i }).getAttribute("aria-label")).toContain("Review ready");
    await userEvent.click(screen.getByRole("button", { name: /Some context unavailable/ }));
    expect(await screen.findByText(/Review discussions unavailable: comments offline/)).toBeTruthy();
    expect(screen.getByText(/Review evidence unavailable: evidence offline/)).toBeTruthy();
    expect(screen.getByText(/Review outcome unavailable: outcome offline/)).toBeTruthy();
    expect(screen.getByText(/Rendered review surfaces unavailable: surface offline/)).toBeTruthy();
  });

  it("enables provider merge only with the exact readiness snapshot and condition hash", async () => {
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
    await userEvent.click(screen.getByRole("button", { name: /Review Status:/i }));
    expect(await screen.findByText("Not ready · 1 condition")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Merge when ready" }));

    await waitFor(() => expect(api.armReviewLanding).toHaveBeenCalledWith("review-1", {
      expected_revision_id: "rrv-1",
      expected_tree_fingerprint: "tree-1",
      expected_conditions_hash: "conditions-1",
    }));
    expect(await screen.findByText("enabled")).toBeTruthy();
  });

  describe("reviewer suggestions", () => {
    const suggestions = {
      advisory: true,
      revision_id: "rrv-1",
      suggestions: [{
        kind: "user", key: "handle:@sec-lead", label: "@sec-lead", principal_id: "", handle: "sec-lead", provider: "", durable_id: "",
        score: 400, explanation: "Reviewer rule auth-rule · src/file-0.py",
        reasons: [{ signal: "rule", paths: ["src/file-0.py"], path_count: 1, sources: ["trusted base"], detail: "Reviewer rule auth-rule · src/file-0.py", rule_ids: ["auth-rule"], count: 1 }],
        paths: ["src/file-0.py"], path_count: 1, critical_paths: [], access: "not_applicable", status: "suggested", automatic_rules: [],
      }],
      excluded: {}, truncated: 0, signals: [], head_changes: [], automatic_routing: false, errors: [],
    };
    beforeEach(() => {
      // Other Reader panels share requestBrowserJson; answer only the suggestions route.
      api.requestBrowserJson.mockImplementation(async (path: string) => (
        path.includes("/reviewer-suggestions") ? suggestions : unmockedBrowserJson(path)
      ));
    });
    afterEach(() => {
      api.requestBrowserJson.mockImplementation(unmockedBrowserJson);
    });

    it("lives inside Review Status as advisory text, without a new top-level control", async () => {
      render(<ReviewReader />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();
      expect(screen.queryByText("Suggested reviewers")).toBeNull();
      expect(api.requestBrowserJson).not.toHaveBeenCalledWith(expect.stringContaining("/reviewer-suggestions"));

      await userEvent.click(screen.getByRole("button", { name: /Review Status:/i }));
      const sheet = await screen.findByRole("dialog");
      expect(await within(sheet).findByText("@sec-lead")).toBeTruthy();
      expect(within(sheet).getByText("Suggested reviewers")).toBeTruthy();
      expect(within(sheet).getByText("Reviewer rule auth-rule · src/file-0.py")).toBeTruthy();
      expect(api.requestBrowserJson).toHaveBeenCalledWith("/api/reviews/review-1/reviewer-suggestions?revision_id=rrv-1");
      // The local Reader offers no request action: suggestions stay advisory.
      expect(within(sheet).queryByRole("button", { name: /Request review/ })).toBeNull();
    });

    it("leaves reviewer suggestions to a host that shows them elsewhere", async () => {
      render(<ReviewReader extensions={{ reviewerSuggestions: "external" }} />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();
      await userEvent.click(screen.getByRole("button", { name: /Review Status:/i }));
      const sheet = await screen.findByRole("dialog");
      expect(within(sheet).queryByText("Suggested reviewers")).toBeNull();
      expect(api.requestBrowserJson).not.toHaveBeenCalledWith(expect.stringContaining("/reviewer-suggestions"));
    });
  });

  it("keeps Reader shortcuts off the stream while the readiness sheet is open", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: /Review Status:/i }));
    await screen.findByRole("dialog");
    stream.scrollToTarget.mockClear();

    await userEvent.keyboard("j");
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 120)); });

    expect(stream.scrollToTarget).not.toHaveBeenCalled();
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
    const readinessButton = await screen.findByRole("button", { name: /Review Status:/i });
    await waitFor(() => expect(readinessButton).toHaveTextContent("0/30 reviewed · 1 blocker"));
    await userEvent.hover(readinessButton);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Required checks");
  });

  it("fails readiness closed when the server-authoritative landing projection is unavailable", async () => {
    api.fetchReviewReadiness.mockRejectedValue(new Error("readiness offline"));

    render(<ReviewReader />);

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => {
      const readinessButton = screen.getByRole("button", { name: /Review Status:/i });
      expect(readinessButton.getAttribute("aria-label")).toContain("Merge readiness unavailable");
    });
    await userEvent.click(screen.getByRole("button", { name: /Review Status:/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Merge readiness unavailable: readiness offline");
    expect(screen.queryByRole("button", { name: "Merge when ready" })).toBeNull();
  });

  it("re-reads landing readiness once a mark commits, and a reply from before it never wins", async () => {
    const landing = (ready: boolean) => ({
      review_id: "review-1", snapshot_id: "rrv-1", snapshot_number: 0, tree_fingerprint: "tree-1", source_fingerprint: "source-1",
      conditions: ready ? [] : [{
        id: "latest-snapshot-reviewed", label: "Latest revision reviewed", state: "pending" as const,
        detail: "1 review area(s) still need human judgment or re-review.", owner: "lemoncrow" as const, category: "human_review", url: "",
      }],
      ready, armable: false, blocker_count: ready ? 0 : 1, local_blocker_count: ready ? 0 : 1,
      conditions_hash: "conditions-1", work_items: [],
      landing: { supported: false, provider: "", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "" },
    });
    // Each readiness read waits for the test to answer it.
    const pending: ((ready: boolean) => void)[] = [];
    api.fetchReviewReadiness.mockImplementation(() => new Promise((resolve) => {
      pending.push((ready) => resolve(landing(ready)));
    }));
    const answerAll = (ready: boolean) => { for (const answer of pending.splice(0)) answer(ready); };
    const recordMark = api.postMark.getMockImplementation()!;
    let commit: () => void = () => {};
    api.postMark.mockImplementationOnce((...args: Parameters<typeof recordMark>) => new Promise((resolve) => {
      commit = () => resolve(recordMark(...args));
    }));
    const readinessLabel = () => screen.getByRole("button", { name: /Review Status:/i }).getAttribute("aria-label");

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => {
      answerAll(false);
      expect(readinessLabel()).toContain("1 merge requirement unresolved");
    });

    // The mark shows at once, and readiness is read again before the server has it.
    act(() => { stream.lastProps.onMark(TARGETS[0], "reviewed"); });
    await waitFor(() => expect(pending).toHaveLength(1));
    const beforeCommit = pending.splice(0);

    // Committed: read again. That answer counts the mark; the earlier one,
    // arriving after it, predates the mark and is dropped.
    await act(async () => { commit(); });
    await waitFor(() => expect(pending).toHaveLength(1));
    await act(async () => { answerAll(true); });
    await waitFor(() => expect(readinessLabel()).toContain("Review ready"));
    await act(async () => { for (const answer of beforeCommit) answer(false); });
    expect(readinessLabel()).toContain("Review ready");
  });

  describe("landing arm/cancel failure", () => {
    function landingReadiness(armed: boolean) {
      return {
        review_id: "review-1", snapshot_id: "rrv-1", snapshot_number: 0, tree_fingerprint: "tree-1", source_fingerprint: "source-1",
        conditions: [], ready: true, armable: true, blocker_count: 0, local_blocker_count: 0, conditions_hash: "conditions-1", work_items: [],
        landing: {
          supported: true, provider: "github", state: armed ? "armed" : "not_armed", armed, semantic_stale: false,
          revision_id: armed ? "rrv-1" : "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "",
        },
      };
    }

    it("names a failed merge-when-ready request, refetches authoritative state, and lets the reviewer retry", async () => {
      api.fetchReviewReadiness.mockResolvedValue(landingReadiness(false));
      api.armReviewLanding.mockRejectedValueOnce(new Error("GitHub refused"));
      render(<ReviewReader />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();
      await userEvent.click(screen.getByRole("button", { name: /Review Status:/i }));
      const enable = await screen.findByRole("button", { name: "Merge when ready" });
      const fetchesBefore = api.fetchReviewReadiness.mock.calls.length;
      await userEvent.click(enable);

      expect(await screen.findByText("Could not arm landing: GitHub refused")).toBeTruthy();
      await waitFor(() => expect(api.fetchReviewReadiness.mock.calls.length).toBeGreaterThan(fetchesBefore));
      const retry = screen.getByRole("button", { name: "Merge when ready" });
      expect(retry).not.toBeDisabled();
      await userEvent.click(retry);
      await waitFor(() => expect(api.armReviewLanding).toHaveBeenCalledTimes(2));
    });

    it("fails closed when the refetch after a failed merge-when-ready request also fails", async () => {
      api.fetchReviewReadiness.mockResolvedValue(landingReadiness(false));
      api.armReviewLanding.mockImplementationOnce(async () => {
        api.fetchReviewReadiness.mockRejectedValue(new Error("offline"));
        throw new Error("GitHub refused");
      });
      render(<ReviewReader />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();
      await userEvent.click(screen.getByRole("button", { name: /Review Status:/i }));
      await userEvent.click(await screen.findByRole("button", { name: "Merge when ready" }));

      expect(await screen.findByRole("alert")).toHaveTextContent("Could not arm landing: GitHub refused");
      expect(screen.queryByRole("button", { name: "Merge when ready" })).toBeNull();
    });

    it("names a failed cancel and keeps Cancel available once the state is refetched", async () => {
      api.fetchReviewReadiness.mockResolvedValue(landingReadiness(true));
      api.cancelReviewLanding.mockRejectedValueOnce(new Error("timeout"));
      render(<ReviewReader />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();
      await userEvent.click(screen.getByRole("button", { name: /Review Status:/i }));
      const cancel = await screen.findByRole("button", { name: "Cancel merge when ready" });
      const fetchesBefore = api.fetchReviewReadiness.mock.calls.length;
      await userEvent.click(cancel);

      expect(await screen.findByText("Could not cancel landing: timeout")).toBeTruthy();
      await waitFor(() => expect(api.fetchReviewReadiness.mock.calls.length).toBeGreaterThan(fetchesBefore));
      expect(screen.getByRole("button", { name: "Cancel merge when ready" })).not.toBeDisabled();
      expect(api.cancelReviewLanding).toHaveBeenCalledWith("review-1", "rrv-1");
    });
  });

  describe.each([["result", false], ["error", true]])("surface run %s bound to its Snapshot", (_name, fails) => {
    it("is discarded when the Snapshot changed before the run returned", async () => {
      const surfaces = [{
        id: "app-stack:api", provider: "service", kind: "service", title: "api", locator: "api", runtime: "app-stack",
        affected_paths: ["src/file-0.py"], capabilities: ["source", "execute", "results", "compare"],
        metadata: { surface_id: "app-stack", service: "api", runner: "docker-compose" },
      }];
      api.fetchReviewSurfaces.mockResolvedValue({ revision_id: "rrv-1", surfaces });
      const original = api.runReviewSurface.getMockImplementation()!;
      let settleOld: () => void = () => {};
      api.runReviewSurface.mockImplementationOnce((...args: Parameters<typeof original>) => new Promise((resolve, reject) => {
        settleOld = () => (fails
          ? reject(new Error("old snapshot run failed"))
          : resolve({ ...({} as object), revision_id: "rrv-1", result: { surface_id: "x", provider: "service", revision_id: "rrv-1", side: "new", status: "passed", summary: "OLD RESULT", runner: "r", runtime: "app-stack", exit_code: 0, duration_ms: 1, output: "", data: {} } } as never));
        void args;
      }));
      api.postRefresh.mockResolvedValueOnce({
        ...OVERVIEW,
        revision: { ...OVERVIEW.revision, id: "rrv-2", revision_number: 1 },
        revision_count: 2,
      });

      render(<ReviewReader />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();
      await waitFor(() => expect(api.runReviewSurface).toHaveBeenCalledTimes(1));
      await userEvent.click(screen.getByLabelText("Review actions"));
      await userEvent.click(screen.getByRole("menuitem", { name: "Update from current source" }));
      await waitFor(() => expect(api.postRefresh).toHaveBeenCalled());
      // The old run no longer holds the new Snapshot's run back: it may
      // already have committed by now.
      await waitFor(() => expect(stream.lastProps.revisionId).toBe("rrv-2"));

      await act(async () => { settleOld(); });
      // The old run may not appear in, or fail, the new Snapshot's state.
      const run = stream.lastProps.surfaceRuns["service:app-stack:api"];
      expect(run?.new?.summary).not.toBe("OLD RESULT");
      expect(run?.error).toBeUndefined();
      // The new Snapshot runs afresh and commits its own result.
      await waitFor(() => expect(stream.lastProps.surfaceRuns["service:app-stack:api"]?.new?.summary).toBe("check passed"));
    });
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

  it("retries a file whose source request failed", async () => {
    const loaded = api.fetchFile.getMockImplementation()!;
    api.fetchFile.mockImplementation(async (id: string, path: string) => {
      if (path === "src/file-0.py" && api.fetchFile.mock.calls.filter(([, item]) => item === path).length === 1) {
        throw Object.assign(new Error("file gone"), { status: 404 });
      }
      return loaded(id, path);
    });
    render(<ReviewReader />);
    await waitFor(() => expect(stream.lastProps?.errors["src/file-0.py"]).toBe("file gone"));

    let recovered = false;
    await act(async () => { recovered = await stream.lastProps.onRetryPath("src/file-0.py"); });
    expect(recovered).toBe(true);
    expect(stream.lastProps.errors["src/file-0.py"]).toBeUndefined();
    expect(stream.lastProps.details["src/file-0.py"]).toBeTruthy();
  });

  it("lands a dedicated file tab opened from Review Map on the mapped target", async () => {
    window.history.replaceState(null, "", "/review?review-item=file&review-path=src%2Ffile-3.py&review-target=target-3#r=review-1");

    render(<ReviewReader />);

    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalledWith(expect.objectContaining({ target_id: "target-3" }), "instant"));
    expect(stream.lastProps.dedicatedItem).toEqual({ kind: "file", path: "src/file-3.py", targetId: "target-3" });
    const scope = screen.getByTestId("dedicated-review-scope");
    expect(within(scope).getByText("File view")).toBeTruthy();
    expect(within(scope).getByText("src/file-3.py")).toBeTruthy();
    expect(within(scope).getByText("Showing 1 of 3 changed files")).toBeTruthy();
    expect(within(scope).getByRole("link", { name: "View full change" }).getAttribute("href")).toBe("/r/r/review-1");
    expect(screen.getByRole("link", { name: "Back to full review" }).getAttribute("href")).toBe("/r/r/review-1");
    // The canonical Reader URL keeps the dedicated item, so a reload stays on it.
    expect(new URLSearchParams(window.location.search).get("review-target")).toBe("target-3");
  });


  it("fetches one far-file target slice for a dedicated large-review link before full hydration", async () => {
    const full = largeReviewFixture();
    const startup = compactLargeStartup(full);
    const path = "src/large-499.py";
    const pathTargets = full.targets.targets.filter((target) => target.path === path);
    api.fetchStartupOverview.mockResolvedValueOnce(startup.overview);
    api.fetchStartupTargets.mockResolvedValueOnce(startup.targets);
    api.fetchTargetsForPath.mockResolvedValueOnce({
      ...full.targets,
      targets: pathTargets,
      outline: full.targets.outline.filter((row) => row.path === path),
      complete: false,
    });
    window.history.replaceState(
      null,
      "",
      "/review?review-item=file&review-path=src%2Flarge-499.py&review-target=large-target-1499#r=review-1",
    );

    render(<ReviewReader />);

    await waitFor(() => expect(api.fetchTargetsForPath).toHaveBeenCalledWith("review-1", path, "recommended"));
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledWith("review-1", path));
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalledWith(
      expect.objectContaining({ target_id: "large-target-1499", path }),
      "instant",
    ));
    expect(stream.lastProps.dedicatedItem).toEqual({ kind: "file", path, targetId: "large-target-1499" });
  });
  it("returns from a dedicated file to its position in the full review", async () => {
    window.history.replaceState(null, "", "/review?review-return-path=src%2Ffile-0.py#r=review-1");
    render(<ReviewReader />);
    await waitFor(() => expect(stream.scrollToFile).toHaveBeenCalledWith("src/file-0.py", "instant"));
    expect(screen.getByRole("button", { name: "Back to reviews" })).toBeTruthy();
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
    const scope = screen.getByTestId("dedicated-review-scope");
    expect(within(scope).getByText("Component view")).toBeTruthy();
    expect(within(scope).getByText("/standalone")).toBeTruthy();
    expect(within(scope).getByRole("link", { name: "View full change" }).getAttribute("href")).toBe("/r/r/review-1");
    expect(screen.getByRole("link", { name: "Back to full review" }).getAttribute("href")).toBe("/r/r/review-1");
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

it("says which file is opening and lets only the latest navigation scroll", async () => {
    const small = smallManyFileReviewFixture();
    api.fetchOverview.mockResolvedValueOnce(small.overview);
    api.fetchTargets.mockResolvedValueOnce(small.targets);
    const loaded = api.fetchFile.getMockImplementation()!;
    const slow = new Map([["src/small-15.py", deferred<FileDetail>()], ["src/small-16.py", deferred<FileDetail>()]]);
    api.fetchFile.mockImplementation(async (id: string, path: string) => slow.get(path)?.promise ?? loaded(id, path));
    const fileDetail = (path: string): FileDetail => ({
      path, status: "modified", additions: 1, deletions: 0, patch: "patch", renderable: true, refusal: "", detail: "", degraded: [],
    });

    render(<ReviewReader />);
    expect(await screen.findByText("20 streamed targets")).toBeTruthy();
    stream.scrollToTarget.mockClear();
    const outlinePanel = screen.getByRole("complementary", { name: "Review outline" });

    await userEvent.click(within(outlinePanel).getByText("small-15.py"));
    expect(await screen.findByText("small-15.py", { selector: "main [role=status] span" })).toBeTruthy();
    await userEvent.click(within(outlinePanel).getByText("small-16.py"));
    expect(await screen.findByText("small-16.py", { selector: "main [role=status] span" })).toBeTruthy();
    expect(stream.scrollToTarget).not.toHaveBeenCalled();

    await act(async () => { slow.get("src/small-15.py")!.resolve(fileDetail("src/small-15.py")); });
    expect(screen.getByText("small-16.py", { selector: "main [role=status] span" })).toBeTruthy();
    expect(stream.scrollToTarget).not.toHaveBeenCalled();
    await act(async () => { slow.get("src/small-16.py")!.resolve(fileDetail("src/small-16.py")); });
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalledTimes(1));
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].path).toBe("src/small-16.py");
    expect(document.querySelector("main [role=status]")).toBeNull();
  });
  it("keeps the large startup model bounded through first Code paint and hydrates on rich-view demand", async () => {
    const full = largeReviewFixture();
    const startup = compactLargeStartup(full);
    api.fetchStartupOverview.mockResolvedValueOnce(startup.overview);
    api.fetchStartupTargets.mockResolvedValueOnce(startup.targets);
    api.fetchOverview.mockResolvedValue(full.overview);
    api.fetchTargets.mockResolvedValue({ ...full.targets, complete: true });

    render(<ReviewReader />);

    expect(await screen.findByText("50 streamed targets")).toBeTruthy();
    expect(screen.getByTestId("review-reader").getAttribute("data-review-targets-complete")).toBe("false");
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(1));
    act(() => stream.lastProps.onFirstContentReady());
    await act(async () => { await Promise.resolve(); });
    expect(api.fetchOverview).not.toHaveBeenCalled();
    expect(api.fetchTargets).not.toHaveBeenCalled();
    expect(api.fetchReviewReadiness).not.toHaveBeenCalled();
    expect(api.fetchReviewDevelopment).not.toHaveBeenCalled();
    expect(api.fetchReviewEvidence).not.toHaveBeenCalled();
    expect(api.fetchReviewSurfaces).not.toHaveBeenCalled();
    expect(api.fetchRevisions).not.toHaveBeenCalled();
    expect(api.fetchFile).toHaveBeenCalledTimes(1);
    expect(api.fetchRevisions).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Brief" }));
    await userEvent.click(screen.getByRole("button", { name: "Brief" }));
    await waitFor(() => expect(api.fetchOverview).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.fetchTargets).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId("review-reader").getAttribute("data-review-targets-complete")).toBe("true"));
    expect(api.fetchOverview).toHaveBeenCalledTimes(1);
    expect(api.fetchTargets).toHaveBeenCalledTimes(1);
  });


  it("starts far-file target and source reads together without poisoning stable slots", async () => {
    const full = largeReviewFixture();
    const startup = compactLargeStartup(full);
    const path = "src/large-499.py";
    const pathTargets = full.targets.targets.filter((target) => target.path === path);
    const targetSlice = deferred<ReviewTargetList>();
    const source = deferred<FileDetail>();
    const originalFetchFile = api.fetchFile.getMockImplementation()!;
    api.fetchStartupOverview.mockResolvedValueOnce(startup.overview);
    api.fetchStartupTargets.mockResolvedValueOnce(startup.targets);
    api.fetchTargetsForPath.mockReturnValueOnce(targetSlice.promise);
    api.fetchOverview.mockResolvedValue(full.overview);
    api.fetchTargets.mockResolvedValue({ ...full.targets, complete: true });
    api.fetchFile.mockImplementation(async (id: string, requestedPath: string) => (
      requestedPath === path ? source.promise : originalFetchFile(id, requestedPath)
    ));

    render(<ReviewReader />);
    expect(await screen.findByText("50 streamed targets")).toBeTruthy();
    const outline = screen.getByTestId("review-outline-virtualized");
    act(() => {
      outline.scrollTop = 499 * 36;
      outline.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    await userEvent.click(await screen.findByText("large-499.py"));

    await waitFor(() => expect(api.fetchTargetsForPath).toHaveBeenCalledWith("review-1", path, "recommended"));
    expect(api.fetchFile).toHaveBeenCalledWith("review-1", path);
    expect(stream.scrollToTarget).not.toHaveBeenCalledWith(expect.objectContaining({ path }), "instant");

    await act(async () => {
      targetSlice.resolve({
        ...full.targets,
        targets: pathTargets,
        outline: full.targets.outline.filter((row) => row.path === path),
        complete: false,
      });
      source.resolve({
        path,
        status: "modified",
        additions: 3,
        deletions: 0,
        patch: "patch",
        renderable: true,
        refusal: "",
        detail: "",
        degraded: [],
      });
      await Promise.resolve();
    });
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalledWith(
      expect.objectContaining({ path }),
      "instant",
    ));

    await userEvent.click(screen.getByRole("button", { name: "Brief" }));
    await waitFor(() => expect(screen.getByTestId("review-reader").getAttribute("data-review-targets-complete")).toBe("true"));
    expect(stream.lastProps.targets[50].target_id).toBe(full.targets.targets[50].target_id);
    expect(stream.lastProps.targets.findIndex((target: ReviewTarget) => target.target_id === pathTargets[0].target_id))
      .toBe(full.targets.targets.findIndex((target) => target.target_id === pathTargets[0].target_id));
  });

  it("does not commit partial full-model hydration across the compact startup boundary", async () => {
    const full = largeReviewFixture();
    const startup = compactLargeStartup(full);
    const hydratedOverview = deferred<ReviewOverview>();
    const hydratedTargets = deferred<ReviewTargetList>();
    api.fetchStartupOverview.mockResolvedValueOnce(startup.overview);
    api.fetchStartupTargets.mockResolvedValueOnce(startup.targets);
    api.fetchOverview.mockReturnValueOnce(hydratedOverview.promise);
    api.fetchTargets.mockReturnValueOnce(hydratedTargets.promise);

    render(<ReviewReader />);

    expect(await screen.findByText("50 streamed targets")).toBeTruthy();
    act(() => stream.lastProps.onFirstContentReady());
    expect(api.fetchOverview).not.toHaveBeenCalled();
    expect(api.fetchTargets).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Brief" }));
    await waitFor(() => expect(api.fetchOverview).toHaveBeenCalledTimes(1));
    expect(api.fetchTargets).toHaveBeenCalledTimes(1);
    expect(screen.getByText("50 streamed targets")).toBeTruthy();
    expect(screen.getByTestId("review-reader").getAttribute("data-review-targets-complete")).toBe("false");

    await act(async () => {
      hydratedOverview.resolve(full.overview);
      hydratedTargets.resolve({ ...full.targets, complete: true });
      await Promise.resolve();
    });
    await waitFor(() => expect(screen.getByTestId("review-reader").getAttribute("data-review-targets-complete")).toBe("true"));
  });

  it("defers rendered-surface placement prefetch until rich-view demand without expanding the visible diff page", async () => {
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
    act(() => stream.lastProps.onFirstContentReady());
    await act(async () => { await Promise.resolve(); });
    expect(api.fetchReviewSurfaces).not.toHaveBeenCalled();
    for (let index = 0; index < 5; index += 1) {
      expect(api.fetchFile).toHaveBeenCalledWith("review-1", `src/large-${index}.py`);
    }

    await userEvent.click(screen.getByRole("button", { name: "Brief" }));
    await waitFor(() => expect(api.fetchReviewSurfaces).toHaveBeenCalledWith("review-1", "rrv-large"));
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledWith("review-1", "src/large-499.py"));
    expect(stream.lastProps.remainingFileCount).toBe(495);
  });

  it("keeps large Auto Code free of Brief projection work until the reviewer asks for it", async () => {
    const large = largeReviewFixture();
    const projection = deferred<ReviewProjection>();
    api.fetchOverview.mockResolvedValueOnce(large.overview);
    api.fetchTargets.mockResolvedValueOnce(large.targets);
    api.fetchProjection.mockReturnValueOnce(projection.promise);

    render(<ReviewReader />);

    expect(await screen.findByText("1500 streamed targets")).toBeTruthy();
    expect(screen.queryByTestId("review-brief")).toBeNull();
    expect(new URLSearchParams(window.location.search).get("view")).toBeNull();
    act(() => stream.lastProps.onFirstContentReady());
    await act(async () => { await Promise.resolve(); });
    expect(api.fetchProjection).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Brief" }));
    await waitFor(() => expect(api.fetchProjection).toHaveBeenCalledTimes(1));
    await act(async () => {
      projection.resolve(briefProjection({ revision_id: "rrv-large" }));
      await Promise.resolve();
    });
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

    const outline = screen.getByTestId("review-outline-virtualized");
    act(() => {
      outline.scrollTop = 499 * 36;
      outline.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    await userEvent.click(await screen.findByText("large-499.py"));

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

  it("keeps scrolling within a changed-line page, then changes the URL and mounted page", async () => {
    const fixture = smallManyFileReviewFixture(21);
    const targets = fixture.targets.targets.map((target) => ({ ...target, additions: 120, deletions: 80 }));
    api.fetchOverview.mockResolvedValueOnce(fixture.overview);
    api.fetchTargets.mockResolvedValueOnce({ ...fixture.targets, targets });
    render(<ReviewReader />);
    await waitFor(() => expect(stream.lastProps?.pagination?.pageCount).toBe(3));
    expect(stream.lastProps.targets).toHaveLength(10);
    expect(stream.lastProps.pagination.changedLines).toBe(2000);
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(6));
    act(() => stream.lastProps.onLoadMore());
    await waitFor(() => expect(stream.lastProps.readyHiddenFileCount).toBe(4));
    act(() => stream.lastProps.onLoadMore());
    await waitFor(() => expect(stream.lastProps.remainingFileCount).toBe(0));
    expect(api.fetchFile.mock.calls.some((call) => call[1] === "src/small-10.py")).toBe(false);

    act(() => stream.lastProps.pagination.onChange(2));
    await waitFor(() => expect(new URLSearchParams(window.location.search).get("p")).toBe("2"));
    await waitFor(() => expect(stream.lastProps.targets[0].path).toBe("src/small-10.py"));
    await waitFor(() => expect(Object.keys(stream.lastProps.details)).toContain("src/small-10.py"));
    expect(Object.keys(stream.lastProps.details)).not.toContain("src/small-0.py");

    act(() => {
      const url = new URL(window.location.href);
      url.searchParams.set("p", "1");
      window.history.replaceState(null, "", url);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await waitFor(() => expect(stream.lastProps.pagination.page).toBe(1));
    await waitFor(() => expect(Object.keys(stream.lastProps.details)).toContain("src/small-0.py"));
    expect(Object.keys(stream.lastProps.details)).not.toContain("src/small-10.py");
  });

  it("opens review previews on page 1 from a later page without dropping file preview descriptors", async () => {
    const fixture = smallManyFileReviewFixture(21);
    const surface = {
      id: "/", provider: "web", kind: "web.route", title: "/", locator: "/", runtime: "",
      affected_paths: ["src/small-0.py"], capabilities: ["preview", "compare"], metadata: {},
    };
    api.fetchOverview.mockResolvedValueOnce(fixture.overview);
    api.fetchTargets.mockResolvedValueOnce({
      ...fixture.targets,
      targets: fixture.targets.targets.map((target) => ({ ...target, additions: 200 })),
    });
    api.fetchReviewSurfaces.mockResolvedValue({ revision_id: "rrv-1", surfaces: [surface] });
    window.history.replaceState(null, "", "/review?p=2");
    render(<ReviewReader />);
    await waitFor(() => expect(stream.lastProps?.pagination?.page).toBe(2));
    await waitFor(() => expect(stream.lastProps.surfaces).toEqual([surface]));
    expect(stream.lastProps.showReviewPreviews).toBe(false);

    act(() => stream.lastProps.onOpenPreviews());
    await waitFor(() => expect(new URLSearchParams(window.location.search).get("p")).toBe("1"));
    await waitFor(() => expect(stream.lastProps.showReviewPreviews).toBe(true));
    await waitFor(() => expect(stream.scrollToSurface).toHaveBeenCalledWith("web:/"));
    expect(stream.lastProps.surfaces).toEqual([surface]);
  });

  it("maps outline files to pages and loads then scrolls to a cross-page destination", async () => {
    const fixture = smallManyFileReviewFixture(21);
    api.fetchOverview.mockResolvedValueOnce(fixture.overview);
    api.fetchTargets.mockResolvedValueOnce({
      ...fixture.targets,
      targets: fixture.targets.targets.map((target) => ({ ...target, additions: 200 })),
    });
    render(<ReviewReader />);
    await waitFor(() => expect(stream.lastProps?.pagination?.page).toBe(1));
    const outline = screen.getByRole("complementary", { name: "Review outline" });
    const destination = within(outline).getByText("small-20.py");
    expect(destination.closest("[data-review-page]")?.getAttribute("data-review-page")).toBe("3");
    await userEvent.click(destination);
    await waitFor(() => expect(new URLSearchParams(window.location.search).get("p")).toBe("3"));
    await waitFor(() => expect(Object.keys(stream.lastProps.details)).toContain("src/small-20.py"));
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].path).toBe("src/small-20.py"));
    expect(stream.lastProps.targets.map((target: ReviewTarget) => target.path)).toEqual(["src/small-20.py"]);
  });

  it.each([["2", 2, "src/small-10.py"], ["999", 3, "src/small-20.py"], ["invalid", 1, "src/small-0.py"]])(
    "opens and canonicalizes p=%s without fetching another page",
    async (value, expected, path) => {
      const fixture = smallManyFileReviewFixture(21);
      api.fetchOverview.mockResolvedValueOnce(fixture.overview);
      api.fetchTargets.mockResolvedValueOnce({
        ...fixture.targets,
        targets: fixture.targets.targets.map((target) => ({ ...target, additions: 200 })),
      });
      window.history.replaceState(null, "", "/review?p=" + value);
      render(<ReviewReader />);
      await waitFor(() => expect(stream.lastProps?.pagination?.page).toBe(expected));
      await waitFor(() => expect(Object.keys(stream.lastProps.details)).toContain(path));
      expect(new URLSearchParams(window.location.search).get("p")).toBe(String(expected));
      const start = (Number(expected) - 1) * 10;
      expect(api.fetchFile.mock.calls.every((call) => {
        const index = Number(String(call[1]).match(/small-(\d+)/)?.[1]);
        return index >= start && index < start + 10;
      })).toBe(true);
    },
  );

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

  it("keeps every opened file when background enrichment reloads the same Snapshot", async () => {
    const large = largeReviewFixture();
    api.fetchOverview.mockResolvedValue(large.overview);
    api.fetchTargets.mockResolvedValue(large.targets);
    const priming = deferred<any>();
    const packet = deferred<any>();
    api.nextLiveState
      .mockReturnValueOnce(priming.promise)
      .mockReturnValueOnce(packet.promise)
      .mockImplementation(() => new Promise(() => {}));
    const live = {
      changed: true, revision_id: large.overview.revision.id, annotations_cursor: "annotations-1",
      journal_cursor: "journal-1", development_cursor: "1", marks_cursor: "marks-1",
    };

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("1500 streamed targets")).toBeTruthy();
    await waitFor(() => expect(stream.lastProps.remainingFileCount).toBe(495));
    act(() => stream.lastProps.onLoadMore());
    await waitFor(() => expect(stream.lastProps.remainingFileCount).toBe(485));
    await act(async () => {
      priming.resolve({ ...live, cursor: "live-1", packet_cursor: "packet-1" });
      await Promise.resolve();
    });
    await waitFor(() => expect(api.nextLiveState).toHaveBeenCalledTimes(2));
    const overviewCalls = api.fetchOverview.mock.calls.length;

    await act(async () => {
      packet.resolve({ ...live, cursor: "live-2", packet_cursor: "packet-2" });
      await Promise.resolve();
    });

    await waitFor(() => expect(api.fetchOverview.mock.calls.length).toBe(overviewCalls + 1));
    await waitFor(() => expect(api.nextLiveState).toHaveBeenCalledTimes(3));
    expect(stream.lastProps.remainingFileCount).toBe(485);
  });

  it("lets one very large file consume the next change budget by itself", async () => {
    const large = largeReviewFixture();
    const targets = large.targets.targets.map((target) => target.path === "src/large-5.py"
      ? { ...target, additions: 400, deletions: 0 }
      : target);
    api.fetchOverview.mockResolvedValueOnce(large.overview);
    api.fetchTargets.mockResolvedValueOnce({ ...large.targets, targets });

    render(<ReviewReader />);

    expect(await screen.findByText("801 streamed targets")).toBeTruthy();
    // Three 400-line targets make file 5 a ~1,200-line next chunk, so background
    // preparation stops after that one file.
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(6));
    expect(api.fetchFile.mock.calls.at(-1)?.[1]).toBe("src/large-5.py");

    act(() => stream.lastProps.onLoadMore());

    await waitFor(() => expect(stream.lastProps.remainingFileCount).toBe(261));
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
  it("leaves a key alone when a focused surface (the line-selection menu) already handled it", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    // The stream's selection menu listens in the capture phase and prevents
    // the default for its own r/x/c/s/e.
    const menuOwnsKey = (event: KeyboardEvent) => event.preventDefault();
    document.addEventListener("keydown", menuOwnsKey, true);
    try {
      await userEvent.keyboard("r");
      await userEvent.keyboard("c");
    } finally {
      document.removeEventListener("keydown", menuOwnsKey, true);
    }

    expect(api.postMark).not.toHaveBeenCalled();
    expect(stream.scrollToTarget).not.toHaveBeenCalled();
    expect(stream.lastProps.draft).toBeNull();
  });

  it("hands an auto-advance to the stream before mark layout can scroll-sync back", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    stream.scrollToTarget.mockClear();

    act(() => { stream.lastProps.onMark(TARGETS[0], "reviewed", true); });

    // The stream's navigation hold must start in the same turn as the optimistic
    // mark. The judged area can fold on that render; if scrolling starts only
    // after loadPath/RAF, scroll-sync can make the old area active again first.
    expect(stream.scrollToTarget).toHaveBeenCalledWith(
      expect.objectContaining({ target_id: "target-1" }),
      "smooth",
    );
    await waitFor(() => expect(api.postMark).toHaveBeenCalledWith("review-1", "sym-0", "reviewed", "rrv-1"));
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
  const staleError = () => Object.assign(
    new Error("the Review advanced before marking; reload the current revision and try again"),
    { status: 409 },
  );

  it("treats a stale Snapshot on mark as a first-class state with a reload path", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    api.postMark.mockRejectedValueOnce(staleError());
    act(() => { stream.lastProps.onMark(TARGETS[2], "reviewed"); });
    expect(await screen.findByText(/Review advanced to a newer Snapshot/)).toBeTruthy();
    expect(stream.lastProps.targets.find((item: ReviewTarget) => item.target_id === "target-2")?.state).toBe(TARGETS[2].state);
    expect(screen.getByRole("button", { name: "Reload review" })).toBeTruthy();
  });

  it("returns to the failed target after an auto-advanced mark fails, unless the reviewer moved on", async () => {
    let failFirst: (reason: Error) => void = () => {};
    api.postMark.mockImplementationOnce(() => new Promise((_resolve, reject) => { failFirst = reject; }));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    stream.scrollToTarget.mockClear();
    act(() => { stream.lastProps.onMark(TARGETS[0], "reviewed", true); });
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1"));
    await act(async () => { failFirst(new Error("mark write failed")); });
    expect(await screen.findByText(/mark write failed/)).toBeTruthy();
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-0"));

    // Second attempt: the reviewer scrolls elsewhere before the failure lands.
    api.postMark.mockImplementationOnce(() => new Promise((_resolve, reject) => { failFirst = reject; }));
    stream.scrollToTarget.mockClear();
    act(() => { stream.lastProps.onMark(TARGETS[0], "reviewed", true); });
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1"));
    act(() => { stream.lastProps.onActiveTarget("target-7"); });
    await waitFor(() => expect(stream.lastProps.activeTargetId).toBe("target-7"));
    stream.scrollToTarget.mockClear();
    await act(async () => { failFirst(new Error("mark write failed again")); });
    expect(await screen.findByText(/mark write failed again/)).toBeTruthy();
    expect(stream.scrollToTarget.mock.calls.some((call) => call[0].target_id === "target-0")).toBe(false);
  });

  it("keeps one mark's failure visible when a concurrent mark succeeds", async () => {
    const answer = api.postMark.getMockImplementation()!;
    let failFirst: (reason: Error) => void = () => {};
    let releaseSecond: () => void = () => {};
    api.postMark.mockImplementationOnce(() => new Promise((_resolve, reject) => { failFirst = reject; }));
    api.postMark.mockImplementationOnce((...args: Parameters<typeof answer>) => new Promise((resolve) => {
      releaseSecond = () => resolve(answer(...args));
    }));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    act(() => { stream.lastProps.onMark(TARGETS[0], "reviewed"); });
    act(() => { stream.lastProps.onMark(TARGETS[1], "reviewed"); });
    await waitFor(() => expect(api.postMark).toHaveBeenCalledTimes(2));
    await act(async () => { failFirst(new Error("first mark failed")); });
    expect(await screen.findByText(/first mark failed/)).toBeTruthy();
    await act(async () => { releaseSecond(); });
    await waitFor(() => expect(stream.lastProps.targets.find((item: ReviewTarget) => item.target_id === "target-1")?.state).toBe("reviewed"));
    expect(screen.getByText(/first mark failed/)).toBeTruthy();
  });

  it("does not paint bulk targets Reviewed before the server replies, and reports a stale Snapshot", async () => {
    let failBulk: (reason: Error) => void = () => {};
    api.postBulkReviewed.mockImplementationOnce(() => new Promise((_resolve, reject) => { failBulk = reject; }));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const before = stream.lastProps.targets.map((item: ReviewTarget) => item.state);
    act(() => { void stream.lastProps.onBulkReview("src/file-0.py"); });
    await waitFor(() => expect(api.postBulkReviewed).toHaveBeenCalledTimes(1));
    expect(stream.lastProps.targets.map((item: ReviewTarget) => item.state)).toEqual(before);
    await act(async () => { failBulk(staleError()); });
    expect(await screen.findByText(/Review advanced to a newer Snapshot/)).toBeTruthy();
    expect(stream.lastProps.targets.map((item: ReviewTarget) => item.state)).toEqual(before);
  });

  it("reports a stale Snapshot when Finish is refused", async () => {
    api.finishReview.mockRejectedValueOnce(staleError());
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Finish review…" }));
    await userEvent.click(await screen.findByRole("radio", { name: "LGTM" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Review outcome summary" }), "ok");
    await userEvent.click(screen.getByRole("button", { name: "LGTM & finish anyway" }));
    expect(await screen.findByText(/Review advanced to a newer Snapshot/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reload review" })).toBeTruthy();
  });

  it("binds an extension-recorded verdict to the Snapshot on screen and reloads on a stale refusal", async () => {
    const recordOutcome = vi.fn().mockRejectedValueOnce(Object.assign(
      new Error("the Review advanced to a newer Snapshot; reload the review before recording a verdict"),
      { status: 409 },
    ));
    render(<ReviewReader extensions={{ recordOutcome }} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Finish review…" }));
    await userEvent.click(await screen.findByRole("radio", { name: "LGTM" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Review outcome summary" }), "ok");
    await userEvent.click(screen.getByRole("button", { name: "LGTM & finish anyway" }));
    await waitFor(() => expect(recordOutcome).toHaveBeenCalledWith("lgtm", "ok", "rrv-1"));
    expect(await screen.findByText(/Review advanced to a newer Snapshot/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reload review" })).toBeTruthy();
    expect(screen.queryByText(/Nothing was recorded/)).toBeNull();
    expect(api.finishReview).not.toHaveBeenCalled();
  });

  it("shows a mark and advances before the server answers, keeps marking available, and rolls back a failure", async () => {
    const answer = api.postMark.getMockImplementation()!;
    let releaseFirst: () => void = () => {};
    api.postMark.mockImplementationOnce((...args: Parameters<typeof answer>) => new Promise((resolve) => {
      releaseFirst = () => resolve(answer(...args));
    }));
    const stateOf = (id: string) => stream.lastProps.targets.find((item: ReviewTarget) => item.target_id === id)?.state;

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    stream.scrollToTarget.mockClear();

    act(() => { stream.lastProps.onMark(TARGETS[0], "reviewed", true); });
    // Navigation ownership must reach the stream in the same event turn. The
    // optimistic mark can resize/fold the current target immediately; waiting
    // for an async path load gives scroll-sync a chance to steal focus back.
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1");
    await waitFor(() => expect(stateOf("target-0")).toBe("reviewed"));
    expect(stream.lastProps.busy).toBe(false);

    act(() => { stream.lastProps.onMark(TARGETS[1], "needs_changes"); });
    await waitFor(() => expect(api.postMark).toHaveBeenCalledWith("review-1", "sym-1", "needs_changes", "rrv-1"));
    expect(stateOf("target-1")).toBe("needs_changes");
    await act(async () => { releaseFirst(); });
    await waitFor(() => expect(stateOf("target-0")).toBe("reviewed"));

    api.postMark.mockRejectedValueOnce(new Error("mark write failed"));
    act(() => { stream.lastProps.onMark(TARGETS[2], "reviewed"); });
    expect(await screen.findByText(/mark write failed/)).toBeTruthy();
    expect(stateOf("target-2")).toBe(TARGETS[2].state);
  });

  it("shows a resolve before the server answers and puts it back when the write fails", async () => {
    const comment = {
      id: "ann-1", review_id: "review-1", revision_id: "rrv-1", parent_id: "", kind: "comment", state: "open",
      body: "Check this", title: "", unit_key: "sym-0", path: "src/file-0.py", side: "new", start_line: 1, end_line: 1,
    };
    api.fetchAnnotations.mockResolvedValue({ revision_id: "rrv-1", annotations: [comment], counts: {} });
    let failPatch: (reason: Error) => void = () => {};
    api.patchAnnotation.mockImplementationOnce(() => new Promise((_resolve, reject) => { failPatch = reject; }));
    const stateOf = () => stream.lastProps.annotations.find((item: { id: string }) => item.id === "ann-1")?.state;

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(stateOf()).toBe("open"));

    act(() => { stream.lastProps.onSetAnnotationState("ann-1", "resolved"); });
    await waitFor(() => expect(stateOf()).toBe("resolved"));
    await act(async () => { failPatch(new Error("resolve failed")); });
    expect(await screen.findByText(/resolve failed/)).toBeTruthy();
    expect(stateOf()).toBe("open");
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

  it("the Next card and ] follow server queue positions, while j keeps list order", async () => {
    // The list arrives in source order; the server's queue puts target-25 first
    // and target-7 second. Nothing in the client may re-rank that.
    const queueOrder = [25, 7, ...TARGETS.map((_, index) => index).filter((index) => index !== 25 && index !== 7)];
    const queued: ReviewTarget[] = TARGETS.map((target, index) => ({
      ...target,
      priority: {
        version: 1,
        tier: index === 25 ? 1 : 4,
        attention: index === 25 ? ("review_first" as const) : ("understand" as const),
        outstanding: true,
        position: queueOrder.indexOf(index),
        reasons: index === 25
          ? [{ code: "request_change_open", detail: "Requested change is still open" }]
          : [{ code: "unreviewed", detail: "Not reviewed yet" }],
      },
    }));
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, targets: queued });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const guide = await screen.findByTestId("review-guide");
    expect(within(guide).getByText("Requested change is still open")).toBeTruthy();
    expect(within(guide).getByText("Review first")).toBeTruthy();

    await userEvent.keyboard("]");
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-7");
  });

  it("] crosses an internal page boundary to the next queued target without page reasoning", async () => {
    // Gate G / Phase 14 item 13: the second queue position lives on page 3.
    const fixture = smallManyFileReviewFixture(21);
    const queueOrder = [0, 20, ...Array.from({ length: 19 }, (_, index) => index + 1)];
    const targets = fixture.targets.targets.map((target, index) => ({
      ...target,
      additions: 200,
      priority: {
        version: 1,
        tier: 4,
        attention: "understand" as const,
        outstanding: true,
        position: queueOrder.indexOf(index),
        reasons: [{ code: "unreviewed", detail: "Not reviewed yet" }],
      },
    }));
    api.fetchOverview.mockResolvedValueOnce(fixture.overview);
    api.fetchTargets.mockResolvedValue({ ...fixture.targets, targets });
    render(<ReviewReader />);
    await waitFor(() => expect(stream.lastProps?.pagination?.page).toBe(1));

    await userEvent.keyboard("]");

    await waitFor(() => expect(new URLSearchParams(window.location.search).get("p")).toBe("3"));
    await waitFor(() => expect(Object.keys(stream.lastProps.details)).toContain("src/small-20.py"));
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].path).toBe("src/small-20.py"));
  });

  it("j walks the list order from the canonical first item", async () => {
    const queued: ReviewTarget[] = TARGETS.map((target, index) => ({
      ...target,
      priority: {
        version: 1,
        tier: 4,
        attention: "understand" as const,
        outstanding: true,
        position: index === 25 ? 0 : index < 25 ? index + 1 : index,
        reasons: [{ code: "unreviewed", detail: "Not reviewed yet" }],
      },
    }));
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, targets: queued });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.keyboard("j");
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-26");
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

    await userEvent.click(screen.getByRole("button", { name: /Review order/ }));
    await userEvent.click(await screen.findByRole("menuitemradio", { name: /File order/ }));

    await waitFor(() => expect(api.fetchTargets).toHaveBeenLastCalledWith("review-1", "file"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Review order: File order" })).toBeTruthy());
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1"));
    expect(api.postMark).not.toHaveBeenCalled();
    expect(api.fetchFile).toHaveBeenCalledTimes(3);
  });

  it("j walks source order after the reviewer switches to File order", async () => {
    const recommended: ReviewTargetList = {
      ...TARGET_LIST,
      targets: [TARGETS[0], TARGETS[3], TARGETS[1], TARGETS[2], ...TARGETS.slice(4)],
    };
    const fileOrdered: ReviewTargetList = { ...TARGET_LIST, order: "file", targets: [...TARGETS] };
    api.fetchTargets.mockResolvedValueOnce(recommended).mockResolvedValueOnce(fileOrdered);

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: /Review order/ }));
    await userEvent.click(await screen.findByRole("menuitemradio", { name: /File order/ }));
    await waitFor(() => expect(api.fetchTargets).toHaveBeenLastCalledWith("review-1", "file"));
    await waitFor(() => expect(stream.lastProps.targets.map((t: ReviewTarget) => t.target_id).slice(0, 4)).toEqual([
      "target-0", "target-1", "target-2", "target-3",
    ]));
    stream.scrollToTarget.mockClear();
    await userEvent.keyboard("j");
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
    // A deferred aim from the reorder must not land after j and yank the reader back.
    await new Promise((resolve) => window.requestAnimationFrame(() => window.requestAnimationFrame(resolve)));
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1");
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
    await userEvent.click(screen.getByRole("button", { name: /Review order/ }));
    await userEvent.click(await screen.findByRole("menuitemradio", { name: /File order/ }));
    await waitFor(() => expect(api.fetchTargets).toHaveBeenLastCalledWith("review-1", "file"));

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Update from current source" }));

    await waitFor(() => expect(api.postRefresh).toHaveBeenCalledWith("review-1"));
    await waitFor(() => expect(api.fetchTargets).toHaveBeenLastCalledWith("review-1", "file"));
    expect(screen.getByRole("button", { name: "Review order: File order" })).toBeTruthy();
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
    const filter = within(dialog).getByRole("combobox", { name: "Filter review actions" });
    await waitFor(() => expect(document.activeElement).toBe(filter));
    await userEvent.type(filter, "send 1 to claude");
    await userEvent.keyboard("{Enter}");

    await waitFor(() => expect(api.exportFeedback).toHaveBeenCalledWith("review-1"));
    expect(screen.queryByRole("dialog", { name: "Review actions" })).toBeNull();
    expect(await screen.findByRole("region", { name: "Review feedback" })).toBeTruthy();
  });

  it("opens Review Map and routes an exact map node back to the existing target stream", async () => {
    enableMap();
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Map" }));
    expect(await screen.findByRole("dialog", { name: "Review Map mock" })).toBeTruthy();
    expect(mapSheet.lastProps).toMatchObject({ reviewId: "review-1", revisionNumber: 0, historical: false });

    await userEvent.click(screen.getByRole("button", { name: "Open mapped target" }));
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalledWith(expect.objectContaining({ target_id: "target-0" }), "smooth"));
    expect(screen.queryByRole("dialog", { name: "Review Map mock" })).toBeNull();
  });

  it("opens Ask within Review Map and keeps the mapped selection", async () => {
    enableMap();
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Map" }));
    await userEvent.click(await screen.findByRole("button", { name: "Ask mapped node" }));

    const map = screen.getByRole("dialog", { name: "Review Map mock" });
    expect(await within(map).findByText("Review Map · fn_0")).toBeTruthy();
    expect(mapSheet.lastProps.location).toMatchObject({ level: "architecture" });
    expect(api.fetchReviewAsk).toHaveBeenCalledWith("review-1");

    await userEvent.click(within(map).getByRole("button", { name: "Close Ask" }));
    expect(within(map).queryByText("Review Map · fn_0")).toBeNull();
    expect(screen.getByRole("dialog", { name: "Review Map mock" })).toBeTruthy();
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
    expect(mapSheet.lastProps.location).toEqual({
      level: "symbol",
      projection: "change_map",
      focus: "symbol:fn0",
      node: "symbol:fn0",
      edge: "",
      path: "src/file-0.py",
    });
    expect(new URLSearchParams(window.location.search).get("map")).toBe("symbol");
  });

  it("keeps Review Map in the URL and offers mapped diffs a dedicated tab as the secondary link", async () => {
    enableMap();
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Map" }));
    expect(await screen.findByRole("dialog", { name: "Review Map mock" })).toBeTruthy();
    const readerPath = window.location.pathname;
    expect(readerPath).toMatch(/^\/r\//);
    expect(new URLSearchParams(window.location.search).get("map")).toBe("architecture");

    act(() => mapSheet.lastProps.onLocationChange({ level: "file", focus: "file:0", node: "file:0", edge: "", path: "" }, "push"));
    expect(window.location.search).toBe("?map=file&map-focus=file%3A0&map-node=file%3A0");
    expect(mapSheet.lastProps.location).toMatchObject({ level: "file", focus: "file:0" });

    const mapped = { kind: "review_map_node", review_id: "review-1", revision_id: "rrv-1", exactness: "snapshot" };
    const href = new URL(mapSheet.lastProps.selectionHref({ ...mapped, path: "src/file-0.py", target_ids: ["target-0"] }));
    expect(href.pathname).toBe(readerPath);
    expect(Object.fromEntries(href.searchParams)).toEqual({
      "review-item": "file",
      "review-path": "src/file-0.py",
      "review-target": "target-0",
      "review-return": `${readerPath}?map=file&map-focus=file%3A0&map-node=file%3A0`,
    });
    expect(mapSheet.lastProps.selectionHref({ ...mapped, path: "src/unchanged-caller.py", target_ids: [] })).toBeNull();
    expect(mapSheet.lastProps.selectionHref({ ...mapped, exactness: "current_index", path: "src/file-0.py", target_ids: ["target-0"] })).toBeNull();

    // Leaving the map adds history, so Back returns to it; the Map location
    // also rides the in-Review backtrail.
    await userEvent.click(screen.getByRole("button", { name: "Open mapped impact" }));
    expect(screen.queryByRole("dialog", { name: "Review Map mock" })).toBeNull();
    const leftMap = new URLSearchParams(window.location.search);
    expect([...leftMap.keys()]).toEqual(["trail"]);
    act(() => {
      window.history.replaceState(null, "", `${readerPath}?map=file&map-focus=file%3A0`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(await screen.findByRole("dialog", { name: "Review Map mock" })).toBeTruthy();
    expect(mapSheet.lastProps.location).toMatchObject({ level: "file", focus: "file:0" });
  });

  it("opens the related-source viewer as loading before a slow source reply arrives", async () => {
    enableMap();
    let resolveSource: (value: unknown) => void = () => {};
    api.fetchRelatedSource.mockImplementationOnce(() => new Promise((resolve) => { resolveSource = resolve; }));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Map" }));
    await userEvent.click(await screen.findByRole("button", { name: "Open mapped impact" }));

    expect(screen.queryByRole("dialog", { name: "Review Map mock" })).toBeNull();
    expect(await screen.findByText("Loading source…")).toBeTruthy();
    await act(async () => {
      resolveSource({ path: "src/unchanged-caller.py", language: "python", start_line: 40, end_line: 48, focus_line: 44, text: "def caller():\n" });
    });
    expect(screen.queryByText("Loading source…")).toBeNull();
    expect(await screen.findByText(/def caller\(\):/)).toBeTruthy();
  });

  it("opens Snapshot-exact impacted source from Review Map through the existing related-source viewer", async () => {
    enableMap();
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

    await userEvent.click(screen.getByRole("button", { name: "Map" }));
    await userEvent.click(await screen.findByRole("button", { name: "Open mapped impact" }));

    await waitFor(() => expect(api.fetchRelatedSource).toHaveBeenCalledWith("review-1", "src/unchanged-caller.py", 44));
    expect(screen.queryByRole("dialog", { name: "Review Map mock" })).toBeNull();
    expect(await screen.findByText("src/unchanged-caller.py")).toBeTruthy();
  });

  it("reports Brief preparation stages and opens code before the Brief finishes", async () => {
    const pending = deferred<ReviewProjection>();
    api.fetchProjection.mockReturnValue(pending.promise);
    api.fetchBriefPreparation.mockResolvedValue({
      revision_id: "rrv-1", state: "running", completed: 5, total: 10, detail: "Preparing guided review",
    });
    render(<ReviewReader />);
    const preparation = await screen.findByRole("region", { name: "Review preparation" });
    await waitFor(() => expect(within(preparation).getByRole("status"))
      .toHaveTextContent("Preparing guided review · 5/10 Brief stages"));
    await userEvent.click(await screen.findByRole("button", { name: "Open code" }));
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(screen.queryByTestId("review-loading-screen")).toBeNull();

    await act(async () => { pending.resolve(briefProjection()); });
    expect(screen.queryByTestId("review-brief")).toBeNull();
    expect(new URLSearchParams(window.location.search).get("view")).not.toBe("brief");
  });

  it("opens the canonical Brief from the change title and jumps into exact code", async () => {
    api.fetchProjection.mockResolvedValue(briefProjection());

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Open change overview" }));

    const brief = await screen.findByTestId("review-brief");
    expect(within(brief).getByText("First behavior")).toBeTruthy();
    expect(within(brief).getByText("Sessions report invalid tokens.")).toBeTruthy();

    await userEvent.click(within(brief).getByRole("button", { name: "Open code" }));
    await waitFor(() => expect(screen.queryByTestId("review-brief")).toBeNull());
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-3"));
  });

  it("? opens shortcut help and Escape restores the reviewer's focus", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const primary = screen.getByRole("button", { name: /^Send feedback/ });
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

  it("offers feedback and outcome in one panel without duplicate next-area actions", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Review next area" })).toBeNull();
    await userEvent.click(screen.getByLabelText("Review actions"));
    expect(screen.queryByRole("menuitem", { name: "Review next area" })).toBeNull();
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("button", { name: /^Send feedback/ }));
    const feedback = await screen.findByRole("dialog", { name: "Send feedback" });
    expect(api.deliverFeedback).not.toHaveBeenCalled();
    await userEvent.click(within(feedback).getByRole("button", { name: "Finish review…" }));
    expect(await screen.findByRole("dialog", { name: "Finish review" })).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "Send feedback" })).toBeNull();
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

  it("the Next card follows the server's fresh queue after bulk review", async () => {
    const priority = (position: number, outstanding = true) => ({
      version: 1,
      tier: outstanding ? 4 : 6,
      attention: outstanding ? ("understand" as const) : ("done" as const),
      outstanding,
      position,
      reasons: [{ code: outstanding ? "unreviewed" : "reviewed", detail: outstanding ? "Not reviewed yet" : "Reviewed; content unchanged" }],
    });
    const queued: ReviewTarget[] = TARGETS.map((target, index) => ({ ...target, priority: priority(index) }));
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, targets: queued });
    const marked = TARGETS.slice(0, 10).map((target) => ({ unit_key: target.unit_key, path: target.path }));
    api.postBulkReviewed.mockResolvedValueOnce({
      marked,
      skipped: [],
      progress: { ...TARGET_LIST.progress, reviewed: 10, unreviewed: 20 },
      outline_updates: [],
      group_counts: OVERVIEW.group_counts,
      priorities: Object.fromEntries(TARGETS.map((target, index) => [
        target.target_id,
        index < 10 ? priority(20 + index, false) : priority(index - 10),
      ])),
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await act(async () => stream.lastProps.onBulkReview("src/file-0.py"));
    await waitFor(() => expect(stream.lastProps.targets[0].state).toBe("reviewed"));
    const guide = await screen.findByTestId("review-guide");
    await waitFor(() => expect(guide.textContent).toContain("file-1.py"));
    expect(guide.textContent).not.toContain("file-0.py");
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
    await userEvent.click(screen.getByRole("menuitem", { name: "Update from current source" }));

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
    await userEvent.click(screen.getByRole("menuitem", { name: "Update from current source" }));

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
    await userEvent.click(screen.getByRole("menuitem", { name: "Update from current source" }));
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

  it("names the wait on the primary button while the finish preview loads", async () => {
    let resolvePreview: (value: unknown) => void = () => {};
    const summary = await api.fetchFinishReview.getMockImplementation()?.("review-1");
    api.fetchFinishReview.mockImplementationOnce(() => new Promise((resolve) => { resolvePreview = resolve; }));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Finish review…" }));

    expect(await screen.findByText("Preparing finish…")).toBeTruthy();
    await act(async () => { resolvePreview(summary); });
    expect(await screen.findByRole("dialog", { name: "Finish review" })).toBeTruthy();
    expect(screen.queryByText("Preparing finish…")).toBeNull();
  });

  it("previews the target-based finish sheet before mutating review status", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Finish review…" }));

    await waitFor(() => expect(api.fetchFinishReview).toHaveBeenCalledWith("review-1"));
    const finishDialog = await screen.findByRole("dialog", { name: "Finish review" });
    expect(finishDialog).toBeTruthy();
    expect(api.finishReview).not.toHaveBeenCalled();
    expect(within(finishDialog).getByRole("region", { name: "Review status summary" })).toHaveTextContent("0/30 reviewed");

    await userEvent.click(screen.getByRole("button", { name: "Finish anyway" }));
    await waitFor(() => expect(api.finishReview).toHaveBeenCalledWith("review-1", "finished", "rrv-1"));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Finish review" })).toBeNull());
    expect(screen.getByRole("button", { name: "Reopen review" })).toBeTruthy();
  });

  it("keeps analysis limitations out of compact Finish and routes detail to Review Status", async () => {
    api.fetchOverview.mockResolvedValueOnce({
      ...OVERVIEW,
      degraded: [{ name: "impact_disabled", note: "Impact analysis has not run yet." }],
    });
    const freshFinish = await api.fetchFinishReview.getMockImplementation()?.("review-1");
    expect(freshFinish).toBeTruthy();
    api.fetchFinishReview.mockResolvedValueOnce({
      ...freshFinish!,
      degraded: [{ name: "centrality", note: "Centrality ranking is unavailable." }],
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Finish review…" }));

    const finishDialog = await screen.findByRole("dialog", { name: "Finish review" });
    expect(within(finishDialog).queryByText(/Analysis limitations/)).toBeNull();
    expect(within(finishDialog).queryByText("centrality")).toBeNull();
    expect(within(finishDialog).queryByText("impact_disabled")).toBeNull();
    expect(within(finishDialog).getByRole("button", { name: "Review Status" })).toBeTruthy();
  });

  it("returns focus to the primary review action when the finish sheet is dismissed", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const primaryButton = screen.getByRole("button", { name: /^Send feedback/ });

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Finish review…" }));
    const close = await screen.findByRole("button", { name: "Close review outcome" });
    await waitFor(() => expect(document.activeElement).toBe(close));
    await userEvent.click(screen.getByRole("button", { name: "Continue reviewing" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Finish review" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(primaryButton));
    expect(api.finishReview).not.toHaveBeenCalled();
  });
  it("restores focus to the Context trigger after closing the drawer", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const trigger = screen.getByRole("button", { name: "Review actions" });
    await userEvent.click(trigger);
    // The menu item unmounts with its menu, so focus returns to the menu trigger.
    await userEvent.click(screen.getByRole("menuitem", { name: "Open context" }));
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
    expect(await screen.findByText("checks · pinned")).toBeTruthy();
    expect(drawer.lastProps.target.target_id).toBe("target-0");

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("context-drawer-mock")).toBeNull());
  });

  it("uses one secondary rail and switches cleanly between Ask and Context", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Ask about this review" }));
    expect(await screen.findByText("Ask, challenge, or request a change")).toBeTruthy();
    expect(screen.queryByTestId("context-drawer-mock")).toBeNull();

    await act(async () => stream.lastProps.onContext(TARGETS[0], "context"));
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();
    expect(screen.queryByText("Ask, challenge, or request a change")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Ask about this review" }));
    expect(await screen.findByText("Ask, challenge, or request a change")).toBeTruthy();
    expect(screen.queryByTestId("context-drawer-mock")).toBeNull();
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
    await act(async () => stream.lastProps.onContext(TARGETS[0], "activity"));

    expect(await screen.findByText("activity · pinned")).toBeTruthy();
    expect(drawer.lastProps.journal.entries[0].summary).toContain("checkout reuses validate_cart");
    expect(drawer.lastProps.journal.session_id).toBe("session-1");
  });

  it("keeps frozen Development history out of the routine header", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchReviewDevelopment).toHaveBeenCalledWith("review-1"));

    // No stacked attention / development / source bars in the routine state.
    expect(document.querySelector(".review-context-strip")).toBeNull();
    expect(document.querySelector(".review-attention-bar")).toBeNull();
    expect(screen.queryByRole("button", { name: /Development: 1 contributor in Snapshot 0/ })).toBeNull();
  });

  it("collapses fix-of context into the single context strip instead of another banner", async () => {
    api.fetchFixLines.mockResolvedValue({
      fix_lines: [],
      fix_of: {
        id: "fix-1",
        review_id: "parent-review",
        snapshot_revision_id: "parent-rev-2",
        snapshot_number: 2,
        child_review_id: "review-1",
        status: "fixing",
        worktree_path: "/tmp/fix",
        branch: "fix/review",
        base_commit: "abc",
        worktree_missing: false,
        applied_revision_id: "",
        applied_files: [],
        applied_at: "",
        snapshots: [{ id: "fix-snap-1", fix_number: 1, display_number: 1, created_at: "" }],
        checklist: [],
        unreviewed: 0,
      },
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchFixLines).toHaveBeenCalledWith("review-1"));
    const strip = await screen.findByText("Fix of Snapshot 2");
    expect(strip.closest(".review-context-strip")).toBeTruthy();
    expect(screen.queryByTestId("fix-of-banner")).toBeNull();
  });

  it("promotes Activity only when work is live or newer than the Snapshot", async () => {
    api.fetchReviewDevelopment.mockResolvedValue({
      ...DEVELOPMENT,
      since: {
        ...DEVELOPMENT.snapshot,
        scope: "since",
        headline: "Agent is still working",
        contributors: [],
        event_count: 2,
        newer_paths: ["src/file-0.py"],
        active: true,
      },
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const action = await screen.findByRole("button", { name: /Activity: 1 contributor in Snapshot 0 · agents working now · 1 newer file/ });
    await userEvent.click(action);
    expect(await screen.findByRole("dialog", { name: "Activity" })).toBeTruthy();
  });

  it("refetches only Development when its live cursor advances", async () => {
    const first = deferred<any>();
    const second = deferred<any>();
    api.nextLiveState
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
      .mockImplementation(() => new Promise(() => {}));

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.nextLiveState).toHaveBeenCalledTimes(1));
    const base = {
      changed: true, revision_id: "rrv-1", packet_cursor: "packet-1",
      annotations_cursor: "annotations-1", journal_cursor: "journal-1",
    };
    await act(async () => {
      first.resolve({ ...base, cursor: "live-1", development_cursor: "3" });
      await Promise.resolve();
    });
    await waitFor(() => expect(api.nextLiveState).toHaveBeenCalledTimes(2));
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
    api.nextLiveState
      .mockReturnValueOnce(first.promise)
      .mockImplementation(() => new Promise(() => {}));

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.nextLiveState).toHaveBeenCalledTimes(1));

    await act(async () => {
      first.resolve({
        cursor: "live-newer", changed: true, revision_id: "rrv-2", packet_cursor: "packet-2",
        annotations_cursor: "annotations-2", journal_cursor: "journal-2",
      });
      await Promise.resolve();
    });

    expect(await screen.findByText("Snapshot superseded elsewhere")).toBeTruthy();
  });

  describe("Snapshot freshness", () => {
    const LIVE = {
      changed: true, packet_cursor: "packet-1", annotations_cursor: "annotations-1",
      journal_cursor: "journal-1", marks_cursor: "marks-1",
    };
    const fileOn = (patch: string) => async (_id: string, path: string) => ({
      path, status: "modified", additions: 10, deletions: 0, patch, renderable: true, refusal: "", detail: "", degraded: [],
    });

    it("Reload review onto a Snapshot captured elsewhere shows that Snapshot's files, not the cached ones", async () => {
      const first = deferred<any>();
      api.nextLiveState.mockReturnValueOnce(first.promise).mockImplementation(() => new Promise(() => {}));
      api.fetchFile.mockImplementation(fileOn("SNAPSHOT-0"));
      render(<ReviewReader sourceProbeIntervalMs={25} />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();
      await waitFor(() => expect(stream.lastProps.details["src/file-0.py"]?.patch).toBe("SNAPSHOT-0"));

      await act(async () => {
        first.resolve({ ...LIVE, cursor: "live-2", revision_id: "rrv-2" });
        await Promise.resolve();
      });
      expect(await screen.findByText("Snapshot superseded elsewhere")).toBeTruthy();
      api.fetchOverview.mockResolvedValue(ADVANCED_OVERVIEW);
      api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, revision_id: "rrv-2" });
      api.fetchFile.mockImplementation(fileOn("SNAPSHOT-1"));

      await userEvent.click(screen.getByRole("button", { name: "Reload review" }));
      await waitFor(() => expect(screen.queryByText("Snapshot superseded elsewhere")).toBeNull());
      await waitFor(() => expect(stream.lastProps.details["src/file-0.py"]?.patch).toBe("SNAPSHOT-1"));
    });

    it("Reload review onto a Snapshot captured elsewhere keeps the unsent comment, detached", async () => {
      const first = deferred<any>();
      api.nextLiveState.mockReturnValueOnce(first.promise).mockImplementation(() => new Promise(() => {}));
      render(<ReviewReader sourceProbeIntervalMs={25} />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();
      await userEvent.keyboard("c");
      await waitFor(() => expect(stream.lastProps.draft?.path).toBe("src/file-0.py"));
      await act(async () => {
        stream.lastProps.onDraft({ ...stream.lastProps.draft, body: "Keep invalid sessions visible", kind: "request_change" });
      });
      expect(window.localStorage.getItem(commentDraftKey("review-1", "rrv-1"))).toContain("Keep invalid sessions visible");

      await act(async () => {
        first.resolve({ ...LIVE, cursor: "live-2", revision_id: "rrv-2" });
        await Promise.resolve();
      });
      expect(await screen.findByText("Snapshot superseded elsewhere")).toBeTruthy();
      api.fetchOverview.mockResolvedValue(ADVANCED_OVERVIEW);
      api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, revision_id: "rrv-2" });
      api.fetchAnnotations.mockResolvedValue({ revision_id: "rrv-2", annotations: [], counts: {} });
      await userEvent.click(screen.getByRole("button", { name: "Reload review" }));

      await waitFor(() => expect(stream.lastProps.revisionId).toBe("rrv-2"));
      await waitFor(() => expect(stream.lastProps.draft?.recovered).toBe(true));
      expect(stream.lastProps.draft.body).toBe("Keep invalid sessions visible");
      expect(stream.lastProps.draft.startLine).toBe(0);
      expect(stream.lastProps.draft.targetUnitKey).toBeUndefined();
      expect(screen.getByText("Draft preserved")).toBeTruthy();
      expect(window.localStorage.getItem(commentDraftKey("review-1", "rrv-1"))).toBeNull();
      await waitFor(() => expect(window.localStorage.getItem(commentDraftKey("review-1", "rrv-2"))).toContain("Keep invalid sessions visible"));
    });

    it("a superseded load's late failure never replaces a newer successful load", async () => {
      const priming = deferred<any>();
      const packet = deferred<any>();
      api.nextLiveState
        .mockReturnValueOnce(priming.promise)
        .mockReturnValueOnce(packet.promise)
        .mockImplementation(() => new Promise(() => {}));
      render(<ReviewReader sourceProbeIntervalMs={25} />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();
      await act(async () => {
        priming.resolve({ ...LIVE, cursor: "live-1", revision_id: "rrv-1" });
        await Promise.resolve();
      });
      await waitFor(() => expect(api.nextLiveState).toHaveBeenCalledTimes(2));
      api.postMark.mockRejectedValueOnce(staleError());
      act(() => { stream.lastProps.onMark(TARGETS[2], "reviewed"); });
      expect(await screen.findByRole("button", { name: "Reload review" })).toBeTruthy();

      // Packet enrichment starts a load that answers only after a newer one.
      let rejectLate: (reason: unknown) => void = () => {};
      const late = new Promise<ReviewOverview>((_resolve, reject) => { rejectLate = reject; });
      api.fetchOverview.mockReturnValueOnce(late);
      await act(async () => {
        packet.resolve({ ...LIVE, cursor: "live-2", revision_id: "rrv-1", packet_cursor: "packet-2" });
        await Promise.resolve();
      });
      await waitFor(() => expect(api.fetchOverview).toHaveBeenCalledTimes(2));
      await userEvent.click(screen.getByRole("button", { name: "Reload review" }));
      await waitFor(() => expect(screen.queryByRole("button", { name: "Reload review" })).toBeNull());

      await act(async () => {
        rejectLate(Object.assign(new Error("superseded load refused"), { status: 404 }));
        await late.catch(() => undefined);
      });
      await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 0)); });
      expect(screen.queryByText(/superseded load refused/)).toBeNull();
    });

    it("its own capture, heard first on the live-state poll, is not a Snapshot superseded elsewhere", async () => {
      const priming = deferred<any>();
      const woken = deferred<any>();
      const refresh = deferred<ReviewOverview>();
      api.nextLiveState
        .mockReturnValueOnce(priming.promise)
        .mockReturnValueOnce(woken.promise)
        .mockImplementation(() => new Promise(() => {}));
      api.postRefresh.mockReturnValueOnce(refresh.promise);
      render(<ReviewReader sourceProbeIntervalMs={25} />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();
      await act(async () => {
        priming.resolve({ ...LIVE, cursor: "live-1", revision_id: "rrv-1" });
        await Promise.resolve();
      });
      await waitFor(() => expect(api.nextLiveState).toHaveBeenCalledTimes(2));

      await userEvent.click(screen.getByLabelText("Review actions"));
      await userEvent.click(screen.getByRole("menuitem", { name: "Update from current source" }));
      await waitFor(() => expect(api.postRefresh).toHaveBeenCalled());
      // The server committed Snapshot 1 and woke the poll before /refresh answered.
      await act(async () => {
        woken.resolve({ ...LIVE, cursor: "live-2", revision_id: "rrv-2" });
        await Promise.resolve();
      });
      api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, revision_id: "rrv-2" });
      await act(async () => { refresh.resolve(ADVANCED_OVERVIEW); await refresh.promise; });

      await waitFor(() => expect(stream.lastProps.revisionId).toBe("rrv-2"));
      await waitFor(() => expect(screen.getByRole("button", { name: /^Send feedback/ })).toBeTruthy());
      expect(screen.queryByText("Snapshot superseded elsewhere")).toBeNull();
    });

    it("a capture reads the new Snapshot's Development and surfaces, and drops Snapshot 0's late answers", async () => {
      const late = deferred<any>();
      api.fetchReviewDevelopment
        .mockReturnValueOnce(late.promise)
        .mockResolvedValue({ ...DEVELOPMENT, revision_id: "rrv-2", revision_number: 1, snapshot: { ...DEVELOPMENT.snapshot, contributors: [] } });
      api.postRefresh.mockResolvedValueOnce(ADVANCED_OVERVIEW);
      api.fetchTargets.mockResolvedValueOnce(TARGET_LIST).mockResolvedValue({ ...TARGET_LIST, revision_id: "rrv-2" });
      render(<ReviewReader />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();
      await waitFor(() => expect(api.fetchReviewDevelopment).toHaveBeenCalledTimes(1));

      await userEvent.click(screen.getByLabelText("Review actions"));
      await userEvent.click(screen.getByRole("menuitem", { name: "Update from current source" }));
      await waitFor(() => expect(api.fetchReviewSurfaces).toHaveBeenCalledWith("review-1", "rrv-2"));
      await waitFor(() => expect(api.fetchReviewDevelopment).toHaveBeenCalledTimes(2));
      expect(screen.queryByRole("button", { name: /Development:/ })).toBeNull();

      await act(async () => { late.resolve(DEVELOPMENT); await late.promise; });
      expect(screen.queryByRole("button", { name: /Development:/ })).toBeNull();
    });

    it("takes a mark recorded in another tab from the live-state marks cursor", async () => {
      const priming = deferred<any>();
      const marked = deferred<any>();
      api.nextLiveState
        .mockReturnValueOnce(priming.promise)
        .mockReturnValueOnce(marked.promise)
        .mockImplementation(() => new Promise(() => {}));
      render(<ReviewReader sourceProbeIntervalMs={25} />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();
      await act(async () => {
        priming.resolve({ ...LIVE, cursor: "live-1", revision_id: "rrv-1" });
        await Promise.resolve();
      });
      await waitFor(() => expect(api.nextLiveState).toHaveBeenCalledTimes(2));
      const reviewed = TARGET_LIST.targets.map((target) => target.target_id === "target-3" ? { ...target, state: "reviewed" as const } : target);
      api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, targets: reviewed });

      await act(async () => {
        marked.resolve({ ...LIVE, cursor: "live-2", revision_id: "rrv-1", marks_cursor: "marks-2" });
        await Promise.resolve();
      });
      await waitFor(() => expect(stream.lastProps.targets.find((target: ReviewTarget) => target.target_id === "target-3")?.state).toBe("reviewed"));
    });

    it("runs a surface clicked while another surface's run is still in flight", async () => {
      const surfaces = ["api", "app"].map((service) => ({
        id: `app-stack:${service}`, provider: "service", kind: "service", title: service, locator: service, runtime: "app-stack",
        affected_paths: ["src/file-0.py"], capabilities: ["source", "execute", "results", "compare"],
        metadata: { surface_id: "app-stack", service, runner: "docker-compose" },
      }));
      api.fetchReviewSurfaces.mockResolvedValue({ revision_id: "rrv-1", surfaces });
      const original = api.runReviewSurface.getMockImplementation()!;
      api.runReviewSurface.mockImplementationOnce(() => new Promise(() => {}));
      render(<ReviewReader />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();
      // The automatic run of the first surface never returns.
      await waitFor(() => expect(api.runReviewSurface).toHaveBeenCalledTimes(1));
      api.runReviewSurface.mockImplementation(original);

      await act(async () => stream.lastProps.onRunSurface(surfaces[1], false));
      await waitFor(() => expect(api.runReviewSurface).toHaveBeenCalledWith("review-1", "service", "app-stack:app", "new", "rrv-1"));
      await waitFor(() => expect(stream.lastProps.surfaceRuns["service:app-stack:app"]?.new?.summary).toBe("check passed"));
      expect(stream.lastProps.surfaceBusy.has("service:app-stack:api")).toBe(true);
    });
  });

  it("defers same-Snapshot packet reloads while a large Code review stays compact", async () => {
    const full = largeReviewFixture();
    const startup = compactLargeStartup(full);
    const first = deferred<any>();
    const second = deferred<any>();
    api.fetchStartupOverview.mockResolvedValueOnce(startup.overview);
    api.fetchStartupTargets.mockResolvedValueOnce(startup.targets);
    api.fetchOverview.mockResolvedValue(full.overview);
    api.fetchTargets.mockResolvedValue({ ...full.targets, complete: true });
    api.nextLiveState
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
      .mockImplementation(() => new Promise(() => {}));

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("50 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(1));
    act(() => stream.lastProps.onFirstContentReady());
    const startupOverviewCalls = api.fetchStartupOverview.mock.calls.length;
    const startupTargetCalls = api.fetchStartupTargets.mock.calls.length;
    const sourceCalls = api.fetchFile.mock.calls.length;

    await act(async () => {
      first.resolve({
        cursor: "live-1", changed: true, revision_id: "rrv-1", packet_cursor: "packet-1",
        annotations_cursor: "annotations-1", journal_cursor: "journal-1", development_cursor: "1", marks_cursor: "marks-1",
      });
      await Promise.resolve();
    });
    await waitFor(() => expect(api.nextLiveState).toHaveBeenCalledTimes(2));

    await act(async () => {
      second.resolve({
        cursor: "live-2", changed: true, revision_id: "rrv-1", packet_cursor: "packet-2",
        annotations_cursor: "annotations-1", journal_cursor: "journal-1", development_cursor: "1", marks_cursor: "marks-1",
      });
      await Promise.resolve();
    });
    await Promise.resolve();

    expect(api.fetchStartupOverview.mock.calls.length).toBe(startupOverviewCalls);
    expect(api.fetchStartupTargets.mock.calls.length).toBe(startupTargetCalls);
    expect(api.fetchFile.mock.calls.length).toBe(sourceCalls);
    expect(api.fetchOverview).not.toHaveBeenCalled();
    expect(api.fetchTargets).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Brief" }));
    await waitFor(() => expect(api.fetchOverview).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.fetchTargets).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId("review-reader").getAttribute("data-review-targets-complete")).toBe("true"));
  });

  it("uses one live-state cursor for metadata and same-revision projection updates", async () => {
    const first = deferred<any>();
    const second = deferred<any>();
    const third = deferred<any>();
    api.nextLiveState
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
      .mockReturnValueOnce(third.promise)
      .mockImplementation(() => new Promise(() => {}));

    render(<ReviewReader sourceProbeIntervalMs={25} />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.nextLiveState).toHaveBeenCalledTimes(1));

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
    await waitFor(() => expect(api.nextLiveState).toHaveBeenCalledTimes(2));

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
    await waitFor(() => expect(api.nextLiveState).toHaveBeenCalledTimes(3));

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

  it("docks Context by default and remembers an explicit overlay choice for the tab", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await act(async () => stream.lastProps.onContext(TARGETS[0], "context"));
    expect(await screen.findByText("context · pinned")).toBeTruthy();

    act(() => drawer.lastProps.onTogglePin());
    await waitFor(() => expect(screen.getByTestId("context-drawer-mock").textContent).toContain("context · overlay"));
    expect(window.sessionStorage.getItem("lemoncrow.review.reader.contextPinned")).toBe("0");
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

  it("a saved comment never pulls in the next Snapshot's discussion list", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    api.postAnnotation.mockResolvedValueOnce({ annotation: { id: "ann-1" } });
    api.fetchAnnotations.mockResolvedValue({ revision_id: "rrv-2", annotations: [handoffAnnotation("ann-next")], counts: {} });

    act(() => stream.lastProps.onCreate({
      path: TARGETS[0].path,
      start_line: TARGETS[0].start_line,
      end_line: TARGETS[0].start_line,
      side: "additions",
      body: "Guard this path.",
      kind: "comment",
      target_unit_key: TARGETS[0].unit_key,
      mark_target: false,
    }));

    await waitFor(() => expect(api.fetchAnnotations.mock.calls.length).toBeGreaterThan(1));
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 0)); });
    expect(stream.lastProps.annotations).toHaveLength(0);
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

    await userEvent.click(screen.getByRole("button", { name: "Send feedback · 1" }));
    expect(await screen.findByRole("button", { name: "Send 1 to Codex" })).toBeTruthy();
    expect(api.deliverFeedback).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Send 1 to Codex" }));
    await waitFor(() => expect(api.deliverFeedback).toHaveBeenCalledWith(
      "review-1",
      expect.objectContaining({ operation_id: "fop-codex", feedback_hash: "codex-feedback-hash" }),
    ));
    expect(await screen.findByText("Sent to Codex")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Close send feedback" }));
    const waiting = await screen.findByRole("button", { name: "Send feedback" });
    expect(screen.queryByRole("button", { name: "Send feedback · 1" })).toBeNull();
    expect(waiting).toBeTruthy();
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

    await userEvent.click(screen.getByRole("button", { name: "Send feedback · 1" }));
    await userEvent.click(await screen.findByRole("button", { name: "Send 1 to Codex" }));

    expect(await screen.findByText("Sent to Codex")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Close send feedback" }));
    const waiting = await screen.findByRole("button", { name: "Send feedback" });
    expect(waiting).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send feedback · 1" })).toBeNull();
    expect(await screen.findByText(/Feedback was sent, but the review state could not refresh: comments offline/)).toBeTruthy();
  });

  it("surfaces an addressed response even when source bytes do not change", async () => {
    const firstLive = deferred<any>();
    const addressedLive = deferred<any>();
    api.nextLiveState
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
    expect(screen.getByRole("button", { name: "Send feedback · 1" })).toBeTruthy();
    await waitFor(() => expect(api.nextLiveState).toHaveBeenCalledTimes(1));
    await act(async () => {
      firstLive.resolve({
        cursor: "live-before-addressed", changed: true, revision_id: "rrv-1", packet_cursor: "packet-1",
        annotations_cursor: "annotations-before", journal_cursor: "journal-1",
      });
      await Promise.resolve();
    });
    await waitFor(() => expect(api.nextLiveState).toHaveBeenCalledTimes(2));
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
    await userEvent.click(screen.getByRole("menuitem", { name: "Update from current source" }));

    // The overview is committed the moment postRefresh resolves, but the
    // reviewer hears nothing while the dependent reads are still out: `busy`
    // is already holding every judgment, and the refresh may yet succeed.
    expect(await screen.findByText(/· Snapshot 1/)).toBeTruthy();
    expect(screen.queryByText("Snapshot superseded elsewhere")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();

    await act(async () => { releaseTargets(TARGET_LIST); });
    await waitFor(() => expect(stream.lastProps.busy).toBe(false));
    expect(screen.queryByText("Snapshot superseded elsewhere")).toBeNull();
  });
  it("inherits the root dark theme when opening Review", async () => {
    window.localStorage.setItem("lemoncrow-theme", "dark");
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(stream.lastProps.codeTheme).toBe("lemoncrow-dark");
    expect(stream.lastProps.chromeTheme).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("keeps the saved syntax family when the root theme changes", async () => {
    window.localStorage.setItem("lemoncrow-theme", "dark");
    window.localStorage.setItem("lemoncrow-review-code-theme", "github-light");
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(stream.lastProps.codeTheme).toBe("github-dark");
    expect(window.localStorage.getItem("lemoncrow-review-code-theme")).toBe("github-dark");
  });

  it("keeps appearance controls in the actions menu while Review next stays primary", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    expect(screen.getByRole("button", { name: /^Send feedback/ })).toBeTruthy();

    async function openAppearance() {
      await userEvent.click(screen.getByLabelText("Review actions"));
      await userEvent.click(screen.getByRole("menuitem", { name: /Appearance/ }));
    }

    await openAppearance();
    expect(screen.getByRole("menuitemradio", { name: "LemonCrow" })).toHaveAttribute("data-state", "checked");
    expect(screen.getAllByRole("menuitemradio").map((option) => option.textContent?.trim())).toEqual([
      "LemonCrow",
      "LemonCrow Dark",
      "GitHub Dark",
      "GitHub Light",
    ]);
    await userEvent.click(screen.getByRole("menuitemradio", { name: "LemonCrow Dark" }));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(window.localStorage.getItem("lemoncrow-review-code-theme")).toBe("lemoncrow-dark");
    expect(stream.lastProps.codeTheme).toBe("lemoncrow-dark");

    await openAppearance();
    await userEvent.click(screen.getByRole("menuitemradio", { name: "GitHub Light" }));
    expect(document.documentElement.classList.contains("light")).toBe(true);
    expect(window.localStorage.getItem("lemoncrow-review-code-theme")).toBe("github-light");
    expect(stream.lastProps.chromeTheme).toBe("light");

    await openAppearance();
    await userEvent.click(screen.getByRole("menuitemradio", { name: "GitHub Dark" }));
    expect(document.documentElement.classList.contains("dark")).toBe(true);

    // Plain LemonCrow is the light/default LemonCrow preset; switching from
    // LemonCrow Dark must visibly transition both syntax and Reader chrome.
    await openAppearance();
    await userEvent.click(screen.getByRole("menuitemradio", { name: "LemonCrow" }));
    expect(document.documentElement.classList.contains("light")).toBe(true);
    expect(stream.lastProps.codeTheme).toBe("lemoncrow");
    expect(stream.lastProps.chromeTheme).toBe("light");
  });

  it("toggles and persists long-line wrapping with the actions menu and w shortcut", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(stream.lastProps.diffOverflow).toBe("scroll");

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: /Code lines/i }));
    expect(stream.lastProps.diffOverflow).toBe("wrap");
    expect(window.localStorage.getItem("lemoncrow.review.reader.diffOverflow")).toBe("wrap");

    await userEvent.keyboard("w");
    expect(stream.lastProps.diffOverflow).toBe("scroll");
    expect(window.localStorage.getItem("lemoncrow.review.reader.diffOverflow")).toBe("scroll");
  });

  it("requests changes by sending the comments to the exact agent session and leaves the Review open", async () => {
    api.fetchAnnotations.mockResolvedValue({
      revision_id: "rrv-1",
      annotations: [handoffAnnotation()],
      counts: {},
      feedback: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
      delivery: { supported: true, host: "claude", session_id: "session-1", target_ref: "claude:session-1", label: "Claude", reason: "" },
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    const sheet = await openFinish();
    await userEvent.click(within(sheet).getByRole("radio", { name: "Request changes" }));
    await userEvent.click(within(sheet).getByRole("button", { name: "Request changes & send 1" }));

    // The verdict button is the explicit send: outcome, then the prepared bundle, then delivery.
    await waitFor(() => expect(api.deliverFeedback).toHaveBeenCalledWith(
      "review-1",
      expect.objectContaining({ operation_id: "fop-1" }),
    ));
    expect(api.postReviewOutcome).toHaveBeenCalledWith("review-1", "changes_requested", "", "rrv-1");
    expect(api.postReviewOutcome.mock.invocationCallOrder[0]).toBeLessThan(api.exportFeedback.mock.invocationCallOrder[0]);
    // Request changes never finishes: the Review waits on the fix.
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Finish review" })).toBeNull());
    expect(api.finishReview).not.toHaveBeenCalled();
  });

  it("sends the leftover comments with an LGTM, then finishes", async () => {
    api.fetchAnnotations.mockResolvedValue({
      revision_id: "rrv-1",
      annotations: [handoffAnnotation()],
      counts: {},
      feedback: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
      delivery: { supported: true, host: "claude", session_id: "session-1", target_ref: "claude:session-1", label: "Claude", reason: "" },
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    const sheet = await openFinish();
    await userEvent.click(within(sheet).getByRole("radio", { name: "LGTM" }));
    expect(within(sheet).getByRole("checkbox", { name: "Also send 1 to Claude" })).toBeChecked();
    await userEvent.click(within(sheet).getByRole("button", { name: "LGTM & finish anyway" }));

    await waitFor(() => expect(api.finishReview).toHaveBeenCalledWith("review-1", "finished", "rrv-1"));
    expect(api.deliverFeedback).toHaveBeenCalledTimes(1);
    expect(api.deliverFeedback.mock.invocationCallOrder[0]).toBeLessThan(api.finishReview.mock.invocationCallOrder[0]);
  });

  it("does not finish when the send before it fails, and offers to finish without sending", async () => {
    api.fetchAnnotations.mockResolvedValue({
      revision_id: "rrv-1",
      annotations: [handoffAnnotation()],
      counts: {},
      feedback: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
      delivery: { supported: true, host: "claude", session_id: "session-1", target_ref: "claude:session-1", label: "Claude", reason: "" },
    });
    api.deliverFeedback.mockResolvedValueOnce({
      state: "failed",
      target_ref: "claude:session-1",
      remote_ref: "",
      message: "claude exited before accepting the handoff",
      annotation_count: 1,
      operation_id: "fop-1",
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    const sheet = await openFinish();
    await userEvent.click(within(sheet).getByRole("button", { name: "Finish anyway" }));
    expect(await within(sheet).findByRole("alert")).toHaveTextContent("claude exited before accepting the handoff");
    expect(api.finishReview).not.toHaveBeenCalled();
    expect(within(sheet).getByRole("button", { name: "Retry" })).toBeTruthy();

    await userEvent.click(within(sheet).getByRole("button", { name: "Finish without sending" }));
    await waitFor(() => expect(api.finishReview).toHaveBeenCalledWith("review-1", "finished", "rrv-1"));
    expect(api.deliverFeedback).toHaveBeenCalledTimes(1);
  });

  it("copies the comments for the author and records the handoff only once they are copied", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const toAuthor = {
      supported: true, host: "", session_id: "", target_ref: "clipboard:author", label: "author", reason: "no exact author session",
      kind: "export" as const, destination: "auto" as const, fallback: true,
    };
    api.fetchAnnotations.mockResolvedValue({
      revision_id: "rrv-1",
      annotations: [handoffAnnotation()],
      counts: {},
      feedback: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
      delivery: toAuthor,
    });
    api.exportFeedback.mockResolvedValueOnce({
      markdown: "## Review feedback\n\nFix it.",
      open: 1,
      orphaned: 0,
      resolved: 0,
      revision_id: "rrv-1",
      feedback_hash: "feedback-copy",
      operation_id: "fop-copy",
      annotation_versions: { "ann-1": 1 },
      delivery: toAuthor,
      status: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
    });
    try {
      render(<ReviewReader />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();

      const sheet = await openFinish();
      await userEvent.click(within(sheet).getByRole("radio", { name: "Request changes" }));
      await userEvent.click(within(sheet).getByRole("button", { name: "Request changes & copy 1" }));

      await waitFor(() => expect(api.deliverFeedback).toHaveBeenCalledWith(
        "review-1",
        expect.objectContaining({ operation_id: "fop-copy" }),
        { copied: true },
      ));
      expect(writeText).toHaveBeenCalledWith("## Review feedback\n\nFix it.");
      expect(writeText.mock.invocationCallOrder[0]).toBeLessThan(api.deliverFeedback.mock.invocationCallOrder[0]);
      expect(api.finishReview).not.toHaveBeenCalled();
    } finally {
      Reflect.deleteProperty(navigator, "clipboard");
    }
  });

  it("starts the copy for the author inside the click, before the verdict round trip", async () => {
    const copied: string[] = [];
    const write = vi.fn(async (items: { items: Record<string, Promise<Blob>> }[]) => {
      const blob = await items[0].items["text/plain"];
      copied.push(await new Promise<string>((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.readAsText(blob);
      }));
    });
    class FakeClipboardItem {
      constructor(public items: Record<string, Promise<Blob>>) {}
    }
    vi.stubGlobal("ClipboardItem", FakeClipboardItem);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { write, writeText: vi.fn() } });
    const toAuthor = {
      supported: true, host: "", session_id: "", target_ref: "clipboard:author", label: "author", reason: "no exact author session",
      kind: "export" as const, destination: "auto" as const, fallback: true,
    };
    api.fetchAnnotations.mockResolvedValue({
      revision_id: "rrv-1",
      annotations: [handoffAnnotation()],
      counts: {},
      feedback: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
      delivery: toAuthor,
    });
    api.exportFeedback.mockResolvedValueOnce({
      markdown: "## Review feedback\n\nFix it.",
      open: 1,
      orphaned: 0,
      resolved: 0,
      revision_id: "rrv-1",
      feedback_hash: "feedback-copy",
      operation_id: "fop-copy",
      annotation_versions: { "ann-1": 1 },
      delivery: toAuthor,
      status: { open_total: 1, unpublished: 1, published: 0, in_flight: 0, addressed: 0 },
    });
    try {
      render(<ReviewReader />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();

      const sheet = await openFinish();
      await userEvent.click(within(sheet).getByRole("radio", { name: "Request changes" }));
      await userEvent.click(within(sheet).getByRole("button", { name: "Request changes & copy 1" }));

      await waitFor(() => expect(api.deliverFeedback).toHaveBeenCalledWith(
        "review-1",
        expect.objectContaining({ operation_id: "fop-copy" }),
        { copied: true },
      ));
      expect(write).toHaveBeenCalledTimes(1);
      expect(write.mock.invocationCallOrder[0]).toBeLessThan(api.postReviewOutcome.mock.invocationCallOrder[0]);
      expect(copied).toEqual(["## Review feedback\n\nFix it."]);
    } finally {
      vi.unstubAllGlobals();
      Reflect.deleteProperty(navigator, "clipboard");
    }
  });

  it("records the reviewer outcome before finishing the review lifecycle", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Finish review…" }));
    expect(await screen.findByRole("dialog", { name: "Finish review" })).toBeTruthy();

    await userEvent.click(screen.getByRole("radio", { name: "LGTM" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Review outcome summary" }), "Ready from my side.");
    await userEvent.click(screen.getByRole("button", { name: "LGTM & finish anyway" }));

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
    await userEvent.click(screen.getByRole("menuitem", { name: "Send LemonCrow feedback" }));
    expect(screen.getByRole("dialog", { name: "Make a wish" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "Feedback" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "Feedback contact email" })).toBeTruthy();
    expect(screen.getByText(/okay with the developer contacting you/i)).toBeTruthy();
  });

  it("makes Reload review the primary recovery action for a stranded reader", async () => {
    await strandReaderOnAdvancedRevision();
    expect(api.postRefresh).toHaveBeenCalledTimes(1);

    const reload = screen.getByRole("button", { name: "Reload review" });
    expect(reload).not.toBeDisabled();
    expect(screen.queryByRole("button", { name: "Finish review" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Finish review…" })).toBeNull();

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
    await waitFor(() => expect(screen.queryByText("Snapshot superseded elsewhere")).toBeNull());

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

    expect(screen.getByText("Snapshot superseded elsewhere")).toBeTruthy();
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
    await userEvent.click(screen.getByRole("menuitem", { name: "Update from current source" }));

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
    await userEvent.click(screen.getByRole("menuitem", { name: "Update from current source" }));

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
    await act(async () => stream.lastProps.onContext(TARGETS[0], "checks"));
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

  it("moves focus into the view a palette action opens instead of leaving it on the button behind", async () => {
    api.fetchProjection.mockResolvedValue(briefProjection());
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const primary = screen.getByRole("button", { name: /^Send feedback/ });
    primary.focus();

    await userEvent.keyboard("p");
    const palette = await screen.findByRole("dialog", { name: "Review actions" });
    await userEvent.type(within(palette).getByRole("combobox", { name: "Filter review actions" }), "show brief");
    await userEvent.keyboard("{Enter}");

    const brief = await screen.findByTestId("review-brief");
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 80)); });
    expect(document.activeElement).not.toBe(primary);
    expect(brief.contains(document.activeElement)).toBe(true);
  });

  it("keeps focus inside an open dialog, closes it on Escape and returns focus to its opener", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    const opener = screen.getByRole("button", { name: /^Review comments/ });
    await userEvent.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "Comments" });
    // The page behind a modal is hidden from assistive tech and unreachable by Tab.
    expect(screen.queryByRole("button", { name: /^Send feedback/ })).toBeNull();
    await userEvent.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Comments" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });

  it("switches Brief · Code · Map in the URL and lands on the Map's selection", async () => {
    enableMap();
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Code" }).getAttribute("aria-pressed")).toBe("true");

    await userEvent.click(screen.getByRole("button", { name: "Brief" }));
    expect(await screen.findByTestId("review-brief")).toBeTruthy();
    expect(new URLSearchParams(window.location.search).get("view")).toBe("brief");
    expect(screen.getByRole("button", { name: "Brief" }).getAttribute("aria-pressed")).toBe("true");

    await userEvent.click(screen.getByRole("button", { name: "Map" }));
    expect(await screen.findByRole("dialog", { name: "Review Map mock" })).toBeTruthy();
    expect(screen.queryByTestId("review-brief")).toBeNull();
    expect(new URLSearchParams(window.location.search).get("view")).toBeNull();
    expect(new URLSearchParams(window.location.search).get("map")).toBe("architecture");
    // The stream stays mounted under the Map, so its scroll position survives.
    expect(screen.getByTestId("reader-stream-mock")).toBeTruthy();

    act(() => mapSheet.lastProps.onSelectionChange({
      kind: "review_map_node",
      review_id: "review-1",
      revision_id: "rrv-1",
      exactness: "snapshot",
      path: "src/file-1.py",
      target_ids: ["target-12"],
    }));
    await userEvent.click(screen.getByRole("button", { name: "Code" }));
    expect(screen.queryByRole("dialog", { name: "Review Map mock" })).toBeNull();
    expect(window.location.search).toBe("");
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-12"));
  });

  it("offers Open in Code only with a local checkout, and returns to Code it was opened from", async () => {
    enableMap();
    window.history.replaceState(null, "", "/review?from=%2Fcode%3Frepo%3Dproj%26file%3Dsrc%252Fa.ts");
    const { unmount } = render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(stream.lastProps.onOpenInCode).toBeUndefined();
    expect(screen.getByRole("link", { name: "Back to Code" }).getAttribute("href")).toBe("/code?repo=proj&file=src%2Fa.ts");
    unmount();

    api.fetchOverview.mockResolvedValue({ ...OVERVIEW, workspace_root: "/repo" });
    window.history.replaceState(null, "", "/review?from=%2F%2Fevil.example");
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(stream.lastProps.onOpenInCode).toEqual(expect.any(Function));
    await userEvent.click(screen.getByRole("button", { name: "Map" }));
    expect(mapSheet.lastProps.onOpenInCode).toBe(stream.lastProps.onOpenInCode);
    // A Back target that leaves the app is ignored.
    expect(screen.queryByRole("link", { name: "Back" })).toBeNull();
  });

  it("never offers a revision update on an archived review", async () => {
    api.fetchOverview.mockResolvedValue({ ...OVERVIEW, session: { ...OVERVIEW.session, status: "archived" } });
    api.fetchSourceState.mockResolvedValue({ supported: true, changed: true, fingerprint: "wt-1", path_count: 2, paths: [], reason: "" });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Restore review" })).toBeTruthy();

    expect(api.fetchSourceState).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Capture current source" })).toBeNull();
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
    expect(await screen.findByRole("button", { name: "Capture current source" })).toBeTruthy();
    expect(screen.getByTestId("source-snapshot-control")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Capture current source" })).toBeTruthy();
    expect(screen.getByTestId("review-proposals")).toHaveTextContent("1 awaiting update");
    expect(api.postRefresh).not.toHaveBeenCalled();
  });

  it("opens a proposal immediately from the loaded revision diff", async () => {
    api.fetchFile.mockImplementation(async (_id: string, path: string) => ({
      path,
      revision_id: "rrv-1",
      status: "modified",
      additions: 1,
      deletions: 1,
      patch: "--- a/src/file-0.py\n+++ b/src/file-0.py\n@@ -1 +1 @@\n-old_line\n+new_line\n",
      renderable: true,
      refusal: "",
      detail: "",
      degraded: [],
    }));

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(stream.lastProps.details["src/file-0.py"]?.revision_id).toBe("rrv-1"));

    act(() => { stream.lastProps.onSuggestEdit({
      path: "src/file-0.py", startLine: 1, endLine: 1, side: "additions", targetUnitKey: "sym-0",
    }); });
    expect(stream.lastProps.proposalSelection?.text).toBe("new_line\n");
    expect(stream.lastProps.draft?.replacementText).toBe("new_line\n");
    expect(stream.lastProps.proposalBusy).toBe(false);
    expect(api.fetchProposalSelection).not.toHaveBeenCalled();
  });

  it("uses source already loaded for expanded diff context", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    act(() => { stream.lastProps.onSuggestEdit({
      path: "src/file-0.py", startLine: 7, endLine: 8, side: "additions",
      selectedText: "visible one\nvisible two\n",
      selectedRevisionId: "rrv-1",
    }); });
    expect(stream.lastProps.proposalSelection?.text).toBe("visible one\nvisible two\n");
    expect(stream.lastProps.proposalBusy).toBe(false);
    expect(api.fetchProposalSelection).not.toHaveBeenCalled();
  });

  it("opens the proposal composer while the exact source loads and drops a reply after it closes", async () => {
    let resolveSelection: (value: unknown) => void = () => {};
    api.fetchProposalSelection.mockImplementation(() => new Promise((resolve) => { resolveSelection = resolve; }));
    const selectionDraft = { path: "src/file-0.py", startLine: 1, endLine: 1, side: "additions" as const, targetUnitKey: "sym-0" };
    const selection = { revision_id: "rrv-1", path: "src/file-0.py", start_line: 1, end_line: 1, side: "additions", text: "old_line\n" };

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    act(() => { stream.lastProps.onSuggestEdit(selectionDraft); });
    await waitFor(() => expect(stream.lastProps.draft?.mode).toBe("proposal"));
    expect(stream.lastProps.proposalBusy).toBe(true);
    expect(stream.lastProps.proposalSelection).toBeNull();

    await act(async () => { resolveSelection(selection); });
    await waitFor(() => expect(stream.lastProps.proposalSelection?.text).toBe("old_line\n"));
    expect(stream.lastProps.draft?.replacementText).toBe("old_line\n");
    expect(stream.lastProps.proposalBusy).toBe(false);

    act(() => { stream.lastProps.onCloseProposal(); });
    act(() => { stream.lastProps.onSuggestEdit(selectionDraft); });
    await waitFor(() => expect(stream.lastProps.draft?.mode).toBe("proposal"));
    act(() => { stream.lastProps.onCloseProposal(); });
    await act(async () => { resolveSelection(selection); });
    await waitFor(() => expect(stream.lastProps.proposalBusy).toBe(false));
    expect(stream.lastProps.draft).toBeNull();
    expect(stream.lastProps.proposalSelection).toBeNull();
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
    expect(await screen.findByRole("button", { name: "Capture current source" })).toBeTruthy();
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

  it("counts every live source edit by status in the proposal bar and its details", async () => {
    api.fetchChangeProposals.mockResolvedValue({
      revision_id: "rrv-1",
      source_mutation_supported: true,
      proposals: [
        changeProposal({ id: "rcp-ready", start_line: 1, end_line: 1 }),
        changeProposal({ id: "rcp-saved", start_line: 2, end_line: 2, can_apply: false }),
        changeProposal({ id: "rcp-applied", start_line: 3, end_line: 3, state: "applied", can_apply: false }),
        changeProposal({ id: "rcp-stale", start_line: 4, end_line: 4, base_revision_id: "rrv-0", can_apply: false }),
        changeProposal({ id: "rcp-conflict", start_line: 5, end_line: 5, state: "conflicted", can_apply: false, conflict_reason: "source moved" }),
        changeProposal({ id: "rcp-captured", start_line: 6, end_line: 6, state: "applied", can_apply: false, result_revision_id: "rrv-1" }),
        changeProposal({ id: "rcp-dismissed", start_line: 7, end_line: 7, state: "dismissed", can_apply: false }),
        changeProposal({ id: "rcp-superseded", start_line: 8, end_line: 8, state: "superseded", can_apply: false }),
      ],
    });

    render(<ReviewReader />);
    const bar = await screen.findByTestId("review-proposals");
    expect(bar).toHaveTextContent("1 ready");
    expect(bar).toHaveTextContent("1 saved");
    expect(bar).toHaveTextContent("1 awaiting update");
    expect(bar).toHaveTextContent("1 from older Snapshot");
    expect(bar).toHaveTextContent("1 need attention");
    // Anything stale or conflicted outranks the calmer states in the bar's tone.
    expect(bar.className).toContain("border-amber-900/45");

    await userEvent.click(within(bar).getByRole("button", { name: "Details" }));
    const details = await screen.findByLabelText("Source edit details");
    const status = (line: number) => within(details).getByText(`src/file-0.py:${line}`).parentElement?.parentElement?.textContent ?? "";
    expect(status(1)).toContain("Ready to apply");
    expect(status(2)).toContain("Saved proposal");
    expect(status(3)).toContain("Awaiting review update");
    expect(status(4)).toContain("Review advanced");
    expect(status(5)).toContain("Source moved");
    // Captured, dismissed, and superseded edits are not live source-edit chrome.
    expect(details.textContent).not.toMatch(/src\/file-0\.py:[678]/);
    // Proposed and conflicted edits can be reopened; applied ones wait for capture.
    expect(within(details).getAllByRole("button", { name: "Open" })).toHaveLength(4);
  });

  it("tones the proposal bar by its most urgent live state", async () => {
    api.fetchChangeProposals.mockResolvedValue({
      revision_id: "rrv-1",
      source_mutation_supported: true,
      proposals: [
        changeProposal({ id: "rcp-ready" }),
        changeProposal({ id: "rcp-applied", start_line: 3, end_line: 3, state: "applied", can_apply: false }),
      ],
    });
    render(<ReviewReader />);
    const bar = await screen.findByTestId("review-proposals");
    expect(bar).toHaveTextContent("1 ready");
    expect(bar).toHaveTextContent("1 awaiting update");
    expect(bar.className).toContain("border-sky-900/45");
    expect(bar).not.toHaveTextContent("need attention");
    expect(bar).not.toHaveTextContent("from older Snapshot");
  });

  it("reopens a proposal whose apply hit a source conflict as the refreshed conflicted proposal", async () => {
    const conflictReason = "source changed since this proposal was prepared";
    api.fetchChangeProposals.mockResolvedValue({ revision_id: "rrv-1", source_mutation_supported: true, proposals: [changeProposal()] });
    api.applyChangeProposal.mockRejectedValue(Object.assign(new Error(conflictReason), { status: 409 }));

    render(<ReviewReader />);
    const bar = await screen.findByTestId("review-proposals");
    expect(bar).toHaveTextContent("1 ready");
    await userEvent.click(within(bar).getByRole("button", { name: "Details" }));
    await userEvent.click(within(await screen.findByLabelText("Source edit details")).getByRole("button", { name: "Open" }));
    await waitFor(() => expect(stream.lastProps.activeProposal?.id).toBe("rcp-1"));
    expect(stream.lastProps.draft?.proposalId).toBe("rcp-1");

    api.fetchChangeProposals.mockResolvedValue({
      revision_id: "rrv-1",
      source_mutation_supported: true,
      proposals: [changeProposal({ state: "conflicted", can_apply: false, conflict_reason: conflictReason })],
    });
    act(() => { stream.lastProps.onApplyProposal(); });
    await waitFor(() => expect(api.applyChangeProposal).toHaveBeenCalledWith("rcp-1"));
    expect(await screen.findByText(conflictReason)).toBeTruthy();
    await waitFor(() => expect(stream.lastProps.activeProposal?.state).toBe("conflicted"));
    expect(stream.lastProps.activeProposal?.conflict_reason).toBe(conflictReason);
    expect(screen.getByTestId("review-proposals")).toHaveTextContent("1 need attention");
    expect(screen.getByTestId("review-proposals")).not.toHaveTextContent("ready");
    expect(api.postRefresh).not.toHaveBeenCalled();
  });

  it("saves a Quick Edit from the loaded Snapshot source through the durable proposal path", async () => {
    api.fetchFile.mockImplementation(async (_id: string, path: string) => ({
      path,
      revision_id: "rrv-1",
      status: "modified",
      additions: 1,
      deletions: 1,
      patch: "--- a/src/file-0.py\n+++ b/src/file-0.py\n@@ -1 +1 @@\n-old_line\n+new_line\n",
      renderable: true,
      refusal: "",
      detail: "",
      degraded: [],
    }));
    api.createChangeProposal.mockResolvedValue({ proposal: changeProposal({ original_text: "new_line\n", replacement_text: "newer_line\n" }) });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(stream.lastProps.details["src/file-0.py"]?.revision_id).toBe("rrv-1"));

    act(() => { stream.lastProps.onQuickEdit({
      path: "src/file-0.py", startLine: 1, endLine: 1, side: "additions", targetUnitKey: "sym-0",
    }); });
    expect(stream.lastProps.draft?.sourceAction).toBe("quick");
    expect(stream.lastProps.proposalSelection).toEqual(expect.objectContaining({ revision_id: "rrv-1", text: "new_line\n" }));
    expect(api.fetchProposalSelection).not.toHaveBeenCalled();

    act(() => { stream.lastProps.onDraft({ ...stream.lastProps.draft, replacementText: "newer_line\n" }); });
    await waitFor(() => expect(stream.lastProps.draft?.replacementText).toBe("newer_line\n"));
    act(() => { stream.lastProps.onCreateProposal(); });
    await waitFor(() => expect(api.createChangeProposal).toHaveBeenCalledWith("review-1", expect.objectContaining({
      expected_revision_id: "rrv-1",
      path: "src/file-0.py",
      start_line: 1,
      end_line: 1,
      side: "additions",
      replacement_text: "newer_line\n",
      target_unit_key: "sym-0",
    })));
    await waitFor(() => expect(stream.lastProps.activeProposal?.id).toBe("rcp-1"));
    expect(api.applyChangeProposal).not.toHaveBeenCalled();
    expect(await screen.findByTestId("review-proposals")).toHaveTextContent("1 ready");
  });

  it("keeps a create-and-apply that hit a source conflict on its one durable proposal instead of offering a second create", async () => {
    const conflictReason = "source changed since this proposal was prepared";
    api.fetchProposalSelection.mockResolvedValue({
      revision_id: "rrv-1", path: "src/file-0.py", start_line: 1, end_line: 1, side: "additions", text: "old_line\n",
    });
    api.createChangeProposal.mockResolvedValue({ proposal: changeProposal() });
    api.applyChangeProposal.mockRejectedValue(Object.assign(new Error(conflictReason), { status: 409 }));

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(stream.lastProps.sourceMutationSupported).toBe(true));
    api.fetchChangeProposals.mockResolvedValue({
      revision_id: "rrv-1",
      source_mutation_supported: true,
      proposals: [changeProposal({ state: "conflicted", can_apply: false, conflict_reason: conflictReason })],
    });

    act(() => { stream.lastProps.onQuickEdit({
      path: "src/file-0.py", startLine: 1, endLine: 1, side: "additions", targetUnitKey: "sym-0",
    }); });
    await waitFor(() => expect(stream.lastProps.proposalSelection?.text).toBe("old_line\n"));
    act(() => { stream.lastProps.onDraft({ ...stream.lastProps.draft, replacementText: "new_line\n" }); });
    await waitFor(() => expect(stream.lastProps.draft?.replacementText).toBe("new_line\n"));

    act(() => { stream.lastProps.onCreateAndApplyProposal(); });
    await waitFor(() => expect(api.applyChangeProposal).toHaveBeenCalledWith("rcp-1"));
    expect(await screen.findByText(conflictReason)).toBeTruthy();
    // The composer now holds the created proposal (refreshed as conflicted), so it
    // shows the stale patch rather than an editable form whose Apply would create
    // a second durable proposal for the same edit.
    await waitFor(() => expect(stream.lastProps.activeProposal?.state).toBe("conflicted"));
    expect(stream.lastProps.activeProposal?.id).toBe("rcp-1");
    expect(stream.lastProps.draft?.proposalId).toBe("rcp-1");
    expect(stream.lastProps.proposalBusy).toBe(false);
    expect(screen.getByTestId("review-proposals")).toHaveTextContent("1 need attention");
    expect(api.createChangeProposal).toHaveBeenCalledTimes(1);
  });

  it("files a chosen evidence file and a preview URL against the active target's Snapshot and refreshes evidence", async () => {
    const uploaded = {
      id: "evd-shot", review_id: "review-1", revision_id: "rrv-1", revision_number: 0, kind: "screenshot",
      title: "shot.png", path: "src/file-0.py", url: "", content_hash: "h", mime_type: "image/png", bytes: 1,
      source: "human", source_ref: "shot.png", verification_status: "", detail: "", created_at: "", status: "current",
      content_url: "/api/evidence/evd-shot/content",
    };
    api.uploadReviewEvidence.mockResolvedValue({ evidence: uploaded });
    api.linkReviewEvidence.mockResolvedValue({ evidence: { ...uploaded, id: "evd-preview", kind: "live_preview", url: "http://localhost:3000/", content_url: "" } });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await act(async () => stream.lastProps.onContext(TARGETS[0], "checks"));
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();
    await waitFor(() => expect(api.fetchReviewEvidence).toHaveBeenCalled());
    const evidenceLoads = api.fetchReviewEvidence.mock.calls.length;

    api.fetchReviewEvidence.mockResolvedValue({ revision_id: "rrv-1", evidence: [uploaded] });
    const file = new File(["x"], "shot.png", { type: "image/png" });
    await act(async () => drawer.lastProps.onUploadEvidence([file]));
    await waitFor(() => expect(api.uploadReviewEvidence).toHaveBeenCalledWith("review-1", file, "src/file-0.py", "rrv-1"));
    await waitFor(() => expect(drawer.lastProps.evidence.map((item: { id: string }) => item.id)).toEqual(["evd-shot"]));
    expect(api.fetchReviewEvidence).toHaveBeenCalledTimes(evidenceLoads + 1);
    await waitFor(() => expect(drawer.lastProps.uploading).toBe(false));

    await act(async () => drawer.lastProps.onAddPreview("http://localhost:3000/", "Live preview"));
    await waitFor(() => expect(api.linkReviewEvidence).toHaveBeenCalledWith(
      "review-1", "http://localhost:3000/", "Live preview", "src/file-0.py", "rrv-1",
    ));
    await waitFor(() => expect(api.fetchReviewEvidence).toHaveBeenCalledTimes(evidenceLoads + 2));
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
    await userEvent.click(screen.getByRole("button", { name: "Send feedback · 1" }));
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

    await userEvent.click(screen.getByRole("button", { name: "Close send feedback" }));
    await userEvent.click(await screen.findByRole("button", { name: "New Snapshot options" }));
    expect(screen.getByText(/Claude is working\./)).toBeTruthy();
    expect(screen.getByText(/capture a read-only draft after every submitted comment is marked addressed/i)).toBeTruthy();
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
    await userEvent.click(screen.getByRole("button", { name: "Send feedback · 1" }));
    await userEvent.click(await screen.findByRole("button", { name: "Send 1 to Claude" }));
    // The comments refreshed after Send already carry the agent's addressed
    // claim, so the handoff reads as over although its delivery record from
    // Send says queued (the queued-and-unaddressed case is covered above).
    expect(await screen.findByText("Re-review addressed feedback")).toBeTruthy();
    expect(screen.queryByText("Queued to Claude")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Close send feedback" }));

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
    expect(api.postRefresh).toHaveBeenCalledWith("review-1", false);
    await waitFor(() => expect(screen.getByText(/· Snapshot 1/)).toBeTruthy());
    expect(screen.queryByRole("button", { name: "Capture current source" })).toBeNull();

    // The handoff is over once every comment it carried holds the agent's
    // addressed claim, even though the delivery record from Send still says queued.
    expect(screen.queryByText("Queued to Claude")).toBeNull();
    expect(screen.queryByRole("button", { name: /Waiting on Claude/ })).toBeNull();
    // "Re-review 1" opens the Comments sheet on the comments it counts.
    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Re-review 1" }));
    expect(await screen.findByRole("tab", { name: "Re-review" })).toHaveAttribute("aria-selected", "true");
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
    expect(await screen.findByRole("button", { name: "Capture current source" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Update review" })).toBeNull();
    expect(api.postRefresh).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Capture current source" }));
    await waitFor(() => expect(api.postRefresh).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText(/· Snapshot 1/)).toBeTruthy());
    expect(screen.queryByRole("button", { name: "Capture current source" })).toBeNull();
  });

  it("keeps New Snapshot visible as source keeps changing and clears it when changes are reverted", async () => {
    const probes: Array<(state: SourceState) => void> = [];
    api.fetchSourceState.mockImplementation(() => new Promise<SourceState>((resolve) => { probes.push(resolve); }));
    const changed: SourceState = { supported: true, changed: true, fingerprint: "wt-1", path_count: 2, paths: [], reason: "" };
    render(<ReviewReader sourceProbeIntervalMs={25} />);
    await screen.findByText("30 streamed targets");
    const answer = async (index: number, state: SourceState) => {
      await waitFor(() => expect(probes.length).toBeGreaterThan(index));
      await act(async () => { probes[index](state); });
    };
    await answer(0, changed);
    expect(screen.queryByRole("button", { name: "Capture current source" })).toBeNull();
    await answer(1, changed);
    const control = await screen.findByTestId("source-snapshot-control");
    await answer(2, { ...changed, fingerprint: "wt-2", path_count: 3 });
    expect(control).toHaveTextContent("New Snapshot3");
    await answer(3, { ...changed, fingerprint: "wt-3", path_count: 4 });
    expect(control).toHaveTextContent("New Snapshot4");
    expect(api.postRefresh).not.toHaveBeenCalled();
    await answer(4, { ...changed, changed: false });
    expect(screen.queryByTestId("source-snapshot-control")).toBeNull();
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

    expect(await screen.findByRole("button", { name: "Capture current source" })).toBeTruthy();
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

    expect(await screen.findByRole("button", { name: "Capture current source" })).toBeTruthy();
    const update = screen.getByRole("button", { name: "Capture current source" });
    expect(update).toBeDisabled();
    expect(api.postRefresh).not.toHaveBeenCalled();

    await act(async () => { releaseBulk(); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Capture current source" })).not.toBeDisabled());
    expect(api.postRefresh).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Capture current source" }));
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

    await act(async () => stream.lastProps.onContext(TARGETS[0], "checks"));
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

function inboxComment(overrides: Record<string, unknown> = {}) {
  return { ...handoffAnnotation(), kind: "comment", body: "Check this", ...overrides };
}

async function openComments() {
  await userEvent.click(await screen.findByRole("button", { name: /^Review comments: \d+ open/ }));
  return screen.findByRole("dialog", { name: "Comments" });
}

describe("ReviewReader comments inbox", () => {
  it("jumps to the comment's target, closing the sheet and leaving the Brief for Implementation", async () => {
    api.fetchProjection.mockResolvedValue(briefProjection());
    api.fetchAnnotations.mockResolvedValue({
      revision_id: "rrv-1",
      annotations: [inboxComment({ unit_key: "sym-12", path: "src/file-1.py", start_line: 13, end_line: 13 })],
      counts: {},
    });
    render(<ReviewReader />);
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();

    const sheet = await openComments();
    await userEvent.click(within(sheet).getByRole("button", { name: "Show in review" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Comments" })).toBeNull());
    await waitFor(() => expect(screen.queryByTestId("review-brief")).toBeNull());
    expect(viewButton("Code").getAttribute("aria-pressed")).toBe("true");
    await waitFor(() => expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-12"));
  });

  it("jumps to the file when a current comment has no review target", async () => {
    api.fetchAnnotations.mockResolvedValue({
      revision_id: "rrv-1",
      annotations: [inboxComment({ unit_key: "", path: "src/unchanged.py", start_line: 4, end_line: 4 })],
      counts: {},
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const sheet = await openComments();
    await userEvent.click(within(sheet).getByRole("button", { name: "Show in review" }));
    await waitFor(() => expect(stream.scrollToFile).toHaveBeenCalledWith("src/unchanged.py", undefined));
    expect(stream.scrollToTarget).not.toHaveBeenCalled();
  });

  it.each([
    ["resolve", "orphaned", "Resolve", "Reopen"],
    ["reopen", "resolved", "Reopen", "Resolve"],
  ])("shows an orphan %s at once and puts it back when the write fails", async (_action, state, before, after) => {
    const orphan = inboxComment({ state, anchored: false, body: "Orphaned concern" });
    api.fetchAnnotations.mockResolvedValue({ revision_id: "rrv-1", annotations: [orphan], counts: {} });
    let failPatch: (reason: Error) => void = () => {};
    api.patchAnnotation.mockImplementationOnce(() => new Promise((_resolve, reject) => { failPatch = reject; }));

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const sheet = await openComments();
    await userEvent.click(within(sheet).getByRole("tab", { name: "Orphaned" }));
    await userEvent.click(within(sheet).getByRole("button", { name: `${before} unanchored comment` }));

    expect(api.patchAnnotation).toHaveBeenCalledWith("ann-1", { state: state === "resolved" ? "open" : "resolved" }, "rrv-1");
    expect(await within(sheet).findByRole("button", { name: `${after} unanchored comment` })).toBeTruthy();
    await act(async () => { failPatch(new Error("comment write failed")); });

    expect(await screen.findByText(/comment write failed/)).toBeTruthy();
    expect(within(sheet).getByRole("button", { name: `${before} unanchored comment` })).toBeTruthy();
    expect(within(sheet).getByText("Orphaned concern")).toBeTruthy();
  });
});

describe("ReviewReader feedback handoff after a reload", () => {
  const claude = { supported: true, host: "claude", session_id: "session-1", target_ref: "claude:session-1", label: "Claude", reason: "" };

  it("restores an in-flight handoff from the server and offers no second send", async () => {
    // A fresh page load knows nothing locally; the server's durable delivery
    // records say the handoff is still in flight.
    api.fetchAnnotations.mockResolvedValue({
      revision_id: "rrv-1",
      annotations: [handoffAnnotation()],
      counts: {},
      feedback: { open_total: 1, unpublished: 0, published: 0, in_flight: 1, addressed: 0 },
      delivery: claude,
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    const pending = await screen.findByRole("button", { name: "Send feedback" });
    expect(pending).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Send \d+ to Claude…/ })).toBeNull();
    const sheet = await openComments();
    expect(within(sheet).getByText("1 delivery pending")).toBeTruthy();
    expect(api.exportFeedback).not.toHaveBeenCalled();
    expect(api.deliverFeedback).not.toHaveBeenCalled();
  });

  it("sends only the comment written since, under the operation the server prepared", async () => {
    api.fetchAnnotations.mockResolvedValue({
      revision_id: "rrv-1",
      annotations: [handoffAnnotation(), handoffAnnotation("ann-2")],
      counts: {},
      feedback: { open_total: 2, unpublished: 1, published: 0, in_flight: 1, addressed: 0 },
      delivery: claude,
    });
    api.exportFeedback.mockResolvedValueOnce({
      markdown: "## Review feedback\n",
      open: 1,
      orphaned: 0,
      resolved: 0,
      revision_id: "rrv-1",
      feedback_hash: "feedback-new-only",
      operation_id: "fop-new-only",
      annotation_versions: { "ann-2": 1 },
      delivery: claude,
      status: { open_total: 2, unpublished: 1, published: 0, in_flight: 1, addressed: 0 },
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(await screen.findByRole("button", { name: "Send feedback · 1" }));
    await userEvent.click(await screen.findByRole("button", { name: "Send 1 to Claude" }));
    await waitFor(() => expect(api.deliverFeedback).toHaveBeenCalledTimes(1));
    expect(api.deliverFeedback).toHaveBeenCalledWith(
      "review-1",
      expect.objectContaining({ operation_id: "fop-new-only", annotation_versions: { "ann-2": 1 } }),
    );
  });
});

async function openFinish() {
  const primary = screen.queryByRole("button", { name: "Finish review" });
  if (primary) {
    await userEvent.click(primary);
  } else {
    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Finish review…" }));
  }
  return screen.findByRole("dialog", { name: "Finish review" });
}

describe("ReviewReader Finish", () => {
  it("stays non-optimistic: nothing reads finished until the server answers, and a failure changes nothing", async () => {
    let failFinish: (reason: Error) => void = () => {};
    api.finishReview.mockImplementationOnce(() => new Promise((_resolve, reject) => { failFinish = reject; }));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const sheet = await openFinish();
    expect(api.finishReview).not.toHaveBeenCalled();
    expect(api.postReviewOutcome).not.toHaveBeenCalled();

    const finish = within(sheet).getByText("Finish anyway").closest("button");
    expect(finish).not.toBeNull();
    expect(finish).toBeEnabled();
    await userEvent.click(finish!);
    await waitFor(() => expect(api.finishReview).toHaveBeenCalledTimes(1));
    expect(within(sheet).getByRole("button", { name: "Recording…" })).toBeDisabled();
    expect(screen.getByRole("dialog", { name: "Finish review" })).toBe(sheet);
    expect(screen.queryByRole("button", { name: "Reopen review" })).toBeNull();

    await act(async () => { failFinish(new Error("finish write failed")); });
    expect(await screen.findByText(/finish write failed/)).toBeTruthy();
    expect(screen.getByRole("dialog", { name: "Finish review" })).toBeTruthy();
    expect(within(sheet).getByRole("button", { name: "Finish anyway" })).not.toBeDisabled();
    expect(screen.queryByRole("button", { name: "Reopen review" })).toBeNull();
  });

  it("records the verdict once when Finish is retried after the status write failed", async () => {
    api.finishReview.mockRejectedValueOnce(new Error("network dropped"));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const sheet = await openFinish();
    await userEvent.click(within(sheet).getByRole("radio", { name: "LGTM" }));
    await userEvent.type(within(sheet).getByRole("textbox", { name: "Review outcome summary" }), "Fine once retried.");

    await userEvent.click(within(sheet).getByRole("button", { name: "LGTM & finish anyway" }));
    expect(await screen.findByText(/network dropped/)).toBeTruthy();
    expect(api.postReviewOutcome).toHaveBeenCalledTimes(1);

    await userEvent.click(within(sheet).getByRole("button", { name: "LGTM & finish anyway" }));
    await waitFor(() => expect(api.finishReview).toHaveBeenCalledTimes(2));
    expect(api.postReviewOutcome).toHaveBeenCalledTimes(1);
    expect(api.postReviewOutcome).toHaveBeenCalledWith("review-1", "lgtm", "Fine once retried.", "rrv-1");
    expect(await screen.findByRole("button", { name: "Reopen review" })).toBeTruthy();
  });

  it("reports a failed finish inside the open sheet, which covers the page's alert strip", async () => {
    api.finishReview.mockRejectedValueOnce(new Error("finish store unavailable"));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const sheet = await openFinish();
    await userEvent.click(within(sheet).getByRole("radio", { name: "LGTM" }));
    await userEvent.click(within(sheet).getByRole("button", { name: "LGTM & finish anyway" }));

    expect(await within(sheet).findByRole("alert")).toHaveTextContent(
      "Your verdict is recorded, but the Review was not finished: finish store unavailable",
    );
    // Said once, where the reviewer is looking.
    expect(screen.getAllByText(/finish store unavailable/)).toHaveLength(1);

    await userEvent.click(within(sheet).getByRole("button", { name: "LGTM & finish anyway" }));
    expect(await screen.findByRole("button", { name: "Reopen review" })).toBeTruthy();
    expect(api.postReviewOutcome).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/finish store unavailable/)).toBeNull();
  });

  it("reports a failed verdict inside the open sheet: nothing was recorded", async () => {
    api.postReviewOutcome.mockRejectedValueOnce(new Error("outcome store unavailable"));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const sheet = await openFinish();
    await userEvent.click(within(sheet).getByRole("button", { name: "Finish anyway" }));

    expect(await within(sheet).findByRole("alert")).toHaveTextContent("Nothing was recorded: outcome store unavailable");
    expect(api.finishReview).not.toHaveBeenCalled();
    expect(screen.getAllByText(/outcome store unavailable/)).toHaveLength(1);
  });

  it("keeps analysis limitations in Review Status without blocking Finish", async () => {
    api.fetchOverview.mockResolvedValue({
      ...OVERVIEW,
      degraded: [{ name: "impact", note: "The code index was unavailable; callers are unknown." }],
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const sheet = await openFinish();
    expect(within(sheet).queryByText(/Analysis limitations/)).toBeNull();
    expect(within(sheet).queryByText(/callers are unknown/)).toBeNull();
    expect(within(sheet).getByRole("button", { name: "Review Status" })).toBeTruthy();

    const finish = await within(sheet).findByRole("button", { name: "Finish anyway" });
    expect(finish).toBeEnabled();
    await userEvent.click(finish);
    await waitFor(() => expect(api.finishReview).toHaveBeenCalledWith("review-1", "finished", "rrv-1"));
    expect(await screen.findByRole("button", { name: "Reopen review" })).toBeTruthy();
  });

  it("publishes a pull-request Review's verdict through the GitHub preview before finishing, and posts nothing", async () => {
    api.fetchOverview.mockResolvedValue({ ...OVERVIEW, session: { ...OVERVIEW.session, source_ref: "github:acme/app#7" } });
    api.previewGitHubPublish.mockImplementation(() => new Promise(() => {}));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const sheet = await openFinish();

    expect(within(sheet).getByRole("checkbox", { name: "Also publish 0 to the pull request" })).toBeChecked();
    await userEvent.click(within(sheet).getByRole("button", { name: "Finish anyway" }));
    expect(await screen.findByRole("dialog", { name: /Publish to GitHub/ })).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "Finish review" })).toBeNull();
    expect(api.postReviewOutcome).toHaveBeenCalledWith("review-1", "comment", "", "rrv-1");
    expect(api.previewGitHubPublish).toHaveBeenCalledWith("review-1");
    expect(api.publishGitHubReview).not.toHaveBeenCalled();
    // The Review finishes once GitHub has the review, not before.
    expect(api.finishReview).not.toHaveBeenCalled();
  });

  it("opens the GitHub publish preview from Review actions for a pull-request Review, and posts nothing", async () => {
    api.fetchOverview.mockResolvedValue({ ...OVERVIEW, session: { ...OVERVIEW.session, source_ref: "github:acme/app#7" } });
    api.previewGitHubPublish.mockImplementation(() => new Promise(() => {}));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(await screen.findByRole("menuitem", { name: /Publish to GitHub/ }));
    expect(await screen.findByRole("dialog", { name: /Publish to GitHub/ })).toBeTruthy();
    expect(api.previewGitHubPublish).toHaveBeenCalledWith("review-1");
    expect(api.publishGitHubReview).not.toHaveBeenCalled();
  });

  it("has no GitHub publish entry for a Review that is not a pull request", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.click(screen.getByLabelText("Review actions"));
    expect(await screen.findByRole("menuitem", { name: /Download Snapshot patch/ })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: /Publish to GitHub/ })).toBeNull();
  });
});

describe("running draft lifecycle", () => {
  it("does not advertise Snapshot creation when the host forces the Review read-only", async () => {
    const draftOverview = { ...OVERVIEW, revision: { ...OVERVIEW.revision, capture_kind: "draft" as const, is_published_snapshot: false } };
    api.fetchOverview.mockResolvedValue(draftOverview);

    render(<ReviewReader extensions={{ forceReadOnly: true, hidePrimaryAction: true, readOnlyReason: "This demo workspace is read-only." }} />);
    await screen.findByText("30 streamed targets");

    expect(screen.queryByRole("button", { name: "Create Snapshot" })).toBeNull();
    expect(screen.getByText("This demo workspace is read-only.")).toBeTruthy();
    expect(api.publishReviewSnapshot).not.toHaveBeenCalled();
  });

  it("explains Snapshot-gated features from the draft and explicitly saves the displayed bytes", async () => {
    const draftOverview = { ...OVERVIEW, revision: { ...OVERVIEW.revision, capture_kind: "draft" as const, is_published_snapshot: false } };
    api.fetchOverview.mockResolvedValue(draftOverview);
    api.publishReviewSnapshot.mockImplementation(async () => {
      api.fetchOverview.mockResolvedValue({ ...draftOverview, revision: { ...draftOverview.revision, is_published_snapshot: true } });
      return { revision_id: "rrv-1", revision_number: 0 };
    });
    render(<ReviewReader />);
    await screen.findByText("30 streamed targets");
    expect(stream.lastProps.busy).toBe(true);
    expect(screen.getByText(/unlocks Snapshot-exact Brief, Map, and architecture analysis/)).toBeTruthy();
    expect(api.postRefresh).not.toHaveBeenCalled();
    expect(api.publishReviewSnapshot).not.toHaveBeenCalled();

    await userEvent.click(screen.getByLabelText(/Review comments/));
    expect(await screen.findByText(/Create a Snapshot first/)).toBeTruthy();

    await act(async () => { await stream.lastProps.onMark(TARGETS[0], "reviewed"); });
    expect(api.postMark).not.toHaveBeenCalled();
    await userEvent.click(screen.getAllByRole("button", { name: "Create Snapshot" })[0]);
    await waitFor(() => expect(stream.lastProps.busy).toBe(false));
    expect(api.publishReviewSnapshot).toHaveBeenCalledWith("review-1", 0);
    expect(api.postRefresh).not.toHaveBeenCalled();
  });

  it("keeps a draft read-only after Snapshot creation fails", async () => {
    api.fetchOverview.mockResolvedValue({ ...OVERVIEW, revision: { ...OVERVIEW.revision, capture_kind: "draft", is_published_snapshot: false } });
    api.publishReviewSnapshot.mockRejectedValueOnce(new Error("Snapshot could not be saved"));
    render(<ReviewReader />);
    await screen.findByText("30 streamed targets");
    await userEvent.click(screen.getAllByRole("button", { name: "Create Snapshot" })[0]);
    await screen.findByText("Snapshot could not be saved");
    expect(stream.lastProps.busy).toBe(true);
    expect(api.postAnnotation).not.toHaveBeenCalled();
  });
  it("keeps newer edits locked when the draft advances during Snapshot creation", async () => {
    api.fetchOverview.mockResolvedValue({ ...OVERVIEW, revision: { ...OVERVIEW.revision, capture_kind: "draft", is_published_snapshot: false } });
    api.publishReviewSnapshot.mockImplementationOnce(async () => {
      api.fetchOverview.mockResolvedValue({
        ...ADVANCED_OVERVIEW,
        revision: { ...ADVANCED_OVERVIEW.revision, capture_kind: "draft", is_published_snapshot: false },
      });
      return { revision_id: "rrv-1", revision_number: 0 };
    });
    render(<ReviewReader />);
    await screen.findByText("30 streamed targets");
    await userEvent.click(screen.getAllByRole("button", { name: "Create Snapshot" })[0]);
    await waitFor(() => expect(stream.lastProps.revisionId).toBe("rrv-2"));
    expect(stream.lastProps.busy).toBe(true);
    expect(api.publishReviewSnapshot).toHaveBeenCalledWith("review-1", 0);
    expect(api.postAnnotation).not.toHaveBeenCalled();
  });
});

describe("ReviewReader correction actions", () => {
  const listing = {
    revision_id: "rrv-1",
    actions: [],
    fix_all_limit: 20,
    catalog: [
      { kind: "fix_all", label: "Fix all", available: true, reason: "", setup: "", subject_ids: ["ann-a", "ann-b"], code_changing: true },
      {
        kind: "resolve_merge_conflicts",
        label: "Resolve merge conflicts",
        available: false,
        reason: "LemonCrow does not observe this Change's mergeability yet",
        setup: "Resolve conflicts in your agent session, then capture a Snapshot",
        subject_ids: [],
        code_changing: true,
      },
    ],
  };

  it("puts Fix all and the explicit actions in the palette and More menu, never in the header or primary CTA", async () => {
    correctionsApi.fetchReviewActions.mockResolvedValue(listing);
    correctionsApi.previewReviewAction.mockReturnValue(new Promise(() => {}));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(correctionsApi.fetchReviewActions).toHaveBeenCalledWith("review-1"));
    // The primary CTA is unchanged and no correction action takes a header slot.
    expect(screen.getByRole("button", { name: /^Send feedback/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Fix all|Resolve merge/ })).toBeNull();

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(await screen.findByRole("menuitem", { name: /Fix & corrections/ }));
    const merge = await screen.findByRole("menuitem", { name: /Resolve merge conflicts/ });
    expect(merge.getAttribute("aria-disabled")).toBe("true");
    await userEvent.keyboard("{Escape}");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());

    await userEvent.keyboard("p");
    const dialog = await screen.findByRole("dialog", { name: "Review actions" });
    const unavailable = within(dialog).getByRole("option", { name: /Resolve merge conflicts/ });
    expect(unavailable.getAttribute("aria-disabled")).toBe("true");
    expect(unavailable.textContent).toContain("does not observe this Change's mergeability");
    await userEvent.type(within(dialog).getByRole("combobox", { name: "Filter review actions" }), "fix all open");
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(correctionsApi.previewReviewAction).toHaveBeenCalledWith(
      "review-1",
      "rrv-1",
      { kind: "fix_all", subject_ids: ["ann-a", "ann-b"], route: undefined, directive: "" },
    ));
  });

  it("fails closed when the action listing is unavailable", async () => {
    correctionsApi.fetchReviewActions.mockRejectedValue(new Error("gateway restarted"));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(correctionsApi.fetchReviewActions).toHaveBeenCalled());
    await userEvent.keyboard("p");
    const dialog = await screen.findByRole("dialog", { name: "Review actions" });
    expect(within(dialog).queryByRole("option", { name: /Fix all/ })).toBeNull();
  });
});

function architecturePayload(material: boolean) {
  return {
    review_id: "review-1",
    revision_id: "rrv-1",
    revision_number: 0,
    level: "architecture",
    projection: "architecture_delta",
    architecture_delta: {
      version: 1,
      digest: "delta-1",
      status: "ready",
      reason: "",
      base_identity: "base",
      snapshot_identity: "snapshot",
      grouping: { strategy: "directory", version: 1, stable_for_identical_input: true, components: [] },
      summary: {
        material,
        status: material ? "changed" : "unchanged",
        changed_components: material ? 2 : 0,
        changed_nodes: material ? 2 : 0,
        changed_edges: material ? 1 : 0,
        added_edges: material ? 1 : 0,
        removed_edges: 0,
        modified_edges: 0,
        context_only_edges: 0,
        unknown_edges: 0,
        important_concerns: material ? ["auth now depends on routes"] : [],
        exactness: "snapshot",
        degraded: [],
      },
      flow: { applicable: false, kind: "", reason: "", steps: [] },
      graph: {
        level: "architecture",
        focus: "component:docs",
        nodes: [
          { id: "component:docs", exactness: "snapshot", delta_status: "unchanged" },
          { id: "component:auth", exactness: "snapshot", delta_status: material ? "changed" : "unchanged" },
        ],
        edges: [],
        truncated: false,
      },
    },
  };
}

function tourStep(key: string, title: string, targetId: string, position: number) {
  return {
    id: `step:${key}`, semantic_key: key, title, why: `Why ${title}`, why_origin: "deterministic" as const, target_ids: [targetId],
    stale_target_ids: [], change_type: "current" as const, signals: [], claim_ids: [], decision_refs: [], evidence_refs: [],
    stale: false, synthesis_needed: false, position, implementation: null,
  };
}

function tourProjection(): ReviewProjection {
  return {
    ...mapProjection(),
    tour: { schema_version: 1, step_count: 2, steps: [tourStep("step-a", "Auth step", "target-3", 0), tourStep("step-b", "Routes step", "target-14", 1)] },
  };
}

function briefScrollRoot(): HTMLElement {
  return screen.getByTestId("review-brief").closest<HTMLElement>("[data-review-scroll-root='brief']")!;
}

function headerButtonCount(): number {
  return within(document.querySelector("header")!).getAllByRole("button").length;
}

describe("ReviewReader 8F in-Review backtrail and residual presentation", () => {
  it("Code Peek → definition → usage → Back → Back restores each exact location", async () => {
    api.fetchRelatedSource.mockResolvedValue({ path: "src/caller.py", language: "python", start_line: 40, end_line: 48, focus_line: 44, text: "def caller():\n    return fn_12()\n" });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await act(async () => stream.lastProps.onContext(TARGETS[0], "context"));
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Back to reviews" })).toBeTruthy();
    const headerControls = headerButtonCount();

    // Definition: target-12 covers src/file-1.py:L13 in this Snapshot's patch.
    stream.scrollToTarget.mockClear();
    await act(async () => drawer.lastProps.onJumpToCode({ path: "src/file-1.py", line: 13, endLine: 14 }));
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenLastCalledWith(expect.objectContaining({ target_id: "target-12" }), expect.anything()));
    await waitFor(() => expect(stream.lastProps.activeTargetId).toBe("target-12"));
    // Back takes the existing slot: no extra header control.
    expect(screen.getByRole("button", { name: "Back to src/file-0.py:L1" })).toBeTruthy();
    expect(headerButtonCount()).toBe(headerControls);

    // Usage outside the patch: the Snapshot source at that line.
    await act(async () => drawer.lastProps.onJumpToCode({ path: "src/caller.py", line: 44, endLine: 44 }));
    expect(await screen.findByText(/return fn_12\(\)/)).toBeTruthy();
    expect(api.fetchRelatedSource).toHaveBeenLastCalledWith("review-1", "src/caller.py", 44);
    const back = screen.getByRole("button", { name: "Back to src/file-1.py:L13" });

    // Reload keeps the trail: it lives in the URL.
    expect(readTrail()).toHaveLength(2);

    stream.scrollToTarget.mockClear();
    back.focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(screen.queryByText(/return fn_12\(\)/)).toBeNull());
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenLastCalledWith(expect.objectContaining({ target_id: "target-12" }), expect.anything()));
    expect(stream.lastProps.activeTargetId).toBe("target-12");
    expect(await screen.findByRole("button", { name: "Back to src/file-0.py:L1" })).toBeTruthy();

    stream.scrollToTarget.mockClear();
    await userEvent.click(screen.getByRole("button", { name: "Back to src/file-0.py:L1" }));
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenLastCalledWith(expect.objectContaining({ target_id: "target-0" }), expect.anything()));
    await waitFor(() => expect(stream.lastProps.activeTargetId).toBe("target-0"));
    expect(await screen.findByRole("button", { name: "Back to reviews" })).toBeTruthy();
    expect(new URLSearchParams(window.location.search).has("trail")).toBe(false);
  });

  it("a Map node jump stays in the Review and Back returns to the exact Map location", async () => {
    enableMap();
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(viewButton("Map"));
    expect(await screen.findByRole("dialog", { name: "Review Map mock" })).toBeTruthy();
    act(() => mapSheet.lastProps.onLocationChange({ level: "file", projection: "change_map", focus: "file:0", node: "symbol:fn0", edge: "", path: "" }, "push"));
    const mapQuery = window.location.search;

    await userEvent.click(screen.getByRole("button", { name: "Open mapped target" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Review Map mock" })).toBeNull());
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalledWith(expect.objectContaining({ target_id: "target-0" }), expect.anything()));

    await userEvent.click(screen.getByRole("button", { name: "Back to Map" }));
    expect(await screen.findByRole("dialog", { name: "Review Map mock" })).toBeTruthy();
    expect(window.location.search).toBe(mapQuery);
    expect(mapSheet.lastProps.location).toMatchObject({ level: "file", focus: "file:0", node: "symbol:fn0" });
    expect(screen.getByRole("button", { name: "Back to reviews" })).toBeTruthy();
  });

  it("an Ask citation jump stays in the Review and Back returns to the cited-from target", async () => {
    const selection = {
      kind: "review_map_node", review_id: "review-1", revision_id: "rrv-1", level: "symbol", focus_node_id: "symbol:fn0",
      node_id: "symbol:fn0", exactness: "snapshot", path: "src/file-0.py", line: 1, end_line: 2, symbol: "fn_0", target_ids: ["target-0"],
    };
    api.fetchReviewAsk.mockResolvedValueOnce({
      review_id: "review-1",
      latest_revision_number: 0,
      messages: [{
        id: "ram-map-answer", review_id: "review-1", revision_id: "rrv-1", revision_number: 0, role: "assistant", intent: "question",
        body: "Map answer", scope: { target_unit_key: "", path: "src/file-0.py", symbol: "fn_0", side: "new", start_line: 1, end_line: 2 },
        context: { selection, label: "fn_0" }, references: [{ kind: "review_map_node", label: "fn_0", map_selection: selection }],
        action: {}, created_at: "2026-09-24T00:00:00+00:00",
      }],
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    act(() => stream.lastProps.onActiveTarget("target-7"));
    await waitFor(() => expect(stream.lastProps.activeTargetId).toBe("target-7"));

    await userEvent.click(screen.getByRole("button", { name: "Ask about this review" }));
    await userEvent.click(await screen.findByRole("button", { name: "Map · fn_0" }));
    expect(await screen.findByRole("dialog", { name: "Review Map mock" })).toBeTruthy();

    stream.scrollToTarget.mockClear();
    await userEvent.click(screen.getByRole("button", { name: "Back to src/file-0.py:L8" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Review Map mock" })).toBeNull());
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenLastCalledWith(expect.objectContaining({ target_id: "target-7" }), expect.anything()));
    expect(new URLSearchParams(window.location.search).get("map")).toBeNull();
  });

  it("a Guided-review target jump returns to the exact Tour step and scroll", async () => {
    api.fetchProjection.mockResolvedValue(tourProjection());
    window.history.replaceState(null, "", "/review?view=tour&guided=step-b");
    render(<ReviewReader />);
    expect(await screen.findByText("Routes step")).toBeTruthy();
    const tourRoot = () => document.querySelector<HTMLElement>("[data-review-scroll-root='tour']")!;
    tourRoot().scrollTop = 120;

    await userEvent.click(screen.getByRole("button", { name: "src/file-1.py · fn_14" }));
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenLastCalledWith(expect.objectContaining({ target_id: "target-14" }), expect.anything()));
    await waitFor(() => expect(screen.queryByText("Routes step")).toBeNull());

    await userEvent.click(screen.getByRole("button", { name: "Back to Guided review" }));
    expect(await screen.findByText("Routes step")).toBeTruthy();
    const params = new URLSearchParams(window.location.search);
    expect([params.get("view"), params.get("guided")]).toEqual(["tour", "step-b"]);
    await waitFor(() => expect(tourRoot().scrollTop).toBe(120));
  });

  it("mounts from a reloaded URL carrying a trail and guided step, and Back restores each encoded location", async () => {
    api.fetchProjection.mockResolvedValue(tourProjection());
    api.fetchRelatedSource.mockResolvedValue({ path: "src/caller.py", language: "python", start_line: 40, end_line: 48, focus_line: 44, text: "def caller():\n    return fn_12()\n" });
    // A reload of Guided step-a, reached from Code (target-12 with the caller's
    // Snapshot source open), itself reached from Guided step-b scrolled to 120.
    const trail = writeReviewBacktrail(new URLSearchParams("view=tour&guided=step-a"), [
      { query: "view=tour&guided=step-b", view: "tour", target: "target-14", scroll: 120, related: null, label: "Guided review" },
      { query: "", view: "implementation", target: "target-12", scroll: 0, related: { path: "src/caller.py", line: 44 }, label: "src/caller.py:L44" },
    ]);
    window.history.replaceState(null, "", `/review?${trail.toString()}`);
    render(<ReviewReader />);

    expect(await screen.findByText("Auth step")).toBeTruthy();
    // The canonical rewrite keeps the Guided step and the trail.
    await waitFor(() => expect(window.location.pathname).toMatch(/^\/r\//));
    expect(new URLSearchParams(window.location.search).get("guided")).toBe("step-a");
    expect(readTrail()).toHaveLength(2);

    stream.scrollToTarget.mockClear();
    await userEvent.click(await screen.findByRole("button", { name: "Back to src/caller.py:L44" }));
    await waitFor(() => expect(screen.queryByText("Auth step")).toBeNull());
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenLastCalledWith(expect.objectContaining({ target_id: "target-12" }), expect.anything()));
    expect(api.fetchRelatedSource).toHaveBeenLastCalledWith("review-1", "src/caller.py", 44);
    expect(await screen.findByText(/return fn_12\(\)/)).toBeTruthy();
    expect(new URLSearchParams(window.location.search).has("view")).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "Back to Guided review" }));
    expect(await screen.findByText("Routes step")).toBeTruthy();
    const params = new URLSearchParams(window.location.search);
    expect([params.get("view"), params.get("guided"), params.get("trail")]).toEqual(["tour", "step-b", null]);
    await waitFor(() => expect(document.querySelector<HTMLElement>("[data-review-scroll-root='tour']")!.scrollTop).toBe(120));
    await waitFor(() => expect(stream.lastProps.activeTargetId).toBe("target-14"));
    expect(screen.queryByText(/return fn_12\(\)/)).toBeNull();
    expect(screen.getByRole("button", { name: "Back to reviews" })).toBeTruthy();
  });

  it("shows the Architecture Delta card on material movement, opens Map at the node, and Back returns to the Brief", async () => {
    api.fetchProjection.mockResolvedValue({
      ...mapProjection(),
      summary: {
        paragraphs: ["Sessions now reject expired tokens."],
        origin: "author",
        scope: "3 files",
        verification: "Tests ran.",
        attention: "",
        additional_explanations: 0,
      },
    });
    api.fetchHistoricalReviewMap.mockResolvedValue(architecturePayload(true));
    render(<ReviewReader />);
    const brief = await screen.findByTestId("review-brief");
    const card = await within(brief).findByTestId("brief-architecture-delta");
    expect(api.fetchHistoricalReviewMap).toHaveBeenCalledWith("review-1", 0, "architecture", "", "architecture_delta");
    expect(within(card).getByText("2 components · 1 relationship changed (1 added)")).toBeTruthy();
    expect(within(card).getByText("auth now depends on routes")).toBeTruthy();
    expect(within(card).getByTestId("brief-architecture-exactness")).toHaveTextContent("Snapshot-exact · base ↔ Snapshot");
    // §5.1 item 2: directly after "What changed".
    const sections = [...brief.querySelectorAll("section[aria-label]")].map((section) => section.getAttribute("aria-label"));
    expect(sections.slice(0, 2)).toEqual(["Change summary", "Architecture delta"]);
    // Still one primary CTA in the Brief: the summary's own.
    expect([...brief.querySelectorAll("button.bg-primary")].map((button) => button.textContent)).toEqual(["Open code"]);
    expect(within(card).getByRole("button", { name: "Open architecture view" }).className).not.toContain("bg-primary");

    briefScrollRoot().scrollTop = 240;
    await userEvent.click(within(card).getByRole("button", { name: "Open architecture view" }));
    expect(await screen.findByRole("dialog", { name: "Review Map mock" })).toBeTruthy();
    expect(mapSheet.lastProps.location).toEqual({ level: "architecture", projection: "architecture_delta", focus: "", node: "component:auth", edge: "", path: "" });
    // No new view: the views group is still Brief · Code · Map.
    expect(within(screen.getByRole("group", { name: "Review views" })).getAllByRole("button").map((button) => button.textContent)).toEqual(["Brief", "Code", "Map"]);

    await userEvent.click(screen.getByRole("button", { name: "Back to Brief" }));
    expect(await screen.findByTestId("review-brief")).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "Review Map mock" })).toBeNull();
    expect(new URLSearchParams(window.location.search).get("view")).toBe("brief");
    await waitFor(() => expect(briefScrollRoot().scrollTop).toBe(240));
    expect(screen.getByRole("button", { name: "Back to reviews" })).toBeTruthy();
  });

  it("PK2: without Project Knowledge the Brief and Map are unchanged", async () => {
    api.fetchProjection.mockResolvedValue(mapProjection());
    api.fetchHistoricalReviewMap.mockResolvedValue(architecturePayload(true));
    render(<ReviewReader />);
    const brief = await screen.findByTestId("review-brief");
    await waitFor(() => expect(api.fetchReviewProjectContext).toHaveBeenCalledWith("review-1", 0, "brief"));
    await act(async () => { await Promise.resolve(); });
    // Not configured: the Brief is exactly as before and the Map gets no history loader.
    expect(within(brief).queryByTestId("brief-project-context")).toBeNull();
    await userEvent.click(within(await within(brief).findByTestId("brief-architecture-delta")).getByRole("button", { name: "Open architecture view" }));
    expect(await screen.findByRole("dialog", { name: "Review Map mock" })).toBeTruthy();
    expect(mapSheet.lastProps.loadProjectHistory).toBeUndefined();
  });

  it("PK2: shows landed history in the Brief and hands the Map a history loader when available", async () => {
    api.fetchProjection.mockResolvedValue(mapProjection());
    api.fetchHistoricalReviewMap.mockResolvedValue(architecturePayload(true));
    api.fetchReviewProjectContext.mockResolvedValue({
      review_id: "review-1",
      revision_id: "revision-1",
      revision_number: 0,
      project_context: {
        purpose: "brief",
        available: true,
        reason: "",
        context_class: "project_knowledge",
        authority: "advisory",
        authority_note: "",
        current_generation: "generation:2",
        entries: [{
          entry_id: "knowledge:1",
          context_class: "project_knowledge",
          subject: { kind: "path", key: "src/session.py" },
          knowledge_kind: "responsibility",
          summary: "Session refresh rejects revoked tokens.",
          summary_truncated: false,
          state: "current",
          state_label: "Landed on main",
          freshness_label: "Landed on main · predates this Change, which edits src/session.py",
          stale_for_snapshot: true,
          support: "deterministic",
          canonical_ref: "refs/heads/main",
          relevance: "changed_path",
          source_paths: ["src/session.py"],
          observed_at: "2026-10-02T10:00:00Z",
          history: { origin: null, superseded_by: null },
          citations: [],
        }],
        narration: [],
        omitted: 0,
        truncated: false,
        filtered_by_content_access: 0,
      },
    });
    render(<ReviewReader />);
    const brief = await screen.findByTestId("review-brief");
    const history = await within(brief).findByTestId("brief-project-context");
    expect(within(history).getByText("Session refresh rejects revoked tokens.")).toBeTruthy();
    expect(within(history).getByTestId("project-history-freshness")).toHaveTextContent("predates this Change");
    // No new top-level Review view.
    expect(within(screen.getByRole("group", { name: "Review views" })).getAllByRole("button").map((button) => button.textContent)).toEqual(["Brief", "Code", "Map"]);
    await userEvent.click(within(await within(brief).findByTestId("brief-architecture-delta")).getByRole("button", { name: "Open architecture view" }));
    expect(await screen.findByRole("dialog", { name: "Review Map mock" })).toBeTruthy();
    expect(typeof mapSheet.lastProps.loadProjectHistory).toBe("function");
    // One read per Snapshot across Brief and Map.
    expect(api.fetchReviewProjectContext).toHaveBeenCalledTimes(1);
  });

  it("renders no Architecture Delta card when the delta is not material", async () => {
    api.fetchProjection.mockResolvedValue(mapProjection());
    api.fetchHistoricalReviewMap.mockResolvedValue(architecturePayload(false));
    render(<ReviewReader />);
    const brief = await screen.findByTestId("review-brief");
    await waitFor(() => expect(api.fetchHistoricalReviewMap).toHaveBeenCalled());
    await act(async () => { await Promise.resolve(); });
    expect(within(brief).queryByTestId("brief-architecture-delta")).toBeNull();
    expect(within(brief).queryByText("Open architecture view")).toBeNull();
  });

  it("does not even load the architecture delta for a tiny mechanical change", async () => {
    const mechanical = TARGETS.slice(0, 2).map((target) => ({ ...target, attention_level: "mechanical" as const }));
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, targets: mechanical });
    api.fetchProjection.mockResolvedValue(briefProjection({
      items: [],
      coverage: { target_count: 2, author_explained: 0, fallback_grouped: 2, other: 0, routine: 2, unresolved_refs: 0, unexplained_targets: 0, file_count: 1 },
      queue: mechanical.map((target) => target.target_id),
    }));
    api.fetchHistoricalReviewMap.mockResolvedValue(architecturePayload(true));
    window.history.replaceState(null, "", "/review?view=brief");
    render(<ReviewReader />);
    expect(await screen.findByTestId("review-brief")).toBeTruthy();
    await act(async () => { await Promise.resolve(); });
    expect(api.fetchHistoricalReviewMap).not.toHaveBeenCalled();
    expect(screen.queryByTestId("brief-architecture-delta")).toBeNull();
  });

  it("shows package semantics in the Brief and hands them to Code before raw lockfile lines", async () => {
    api.fetchProjection.mockResolvedValue(mapProjection());
    const dependencies = {
      review_id: "review-1", snapshot_id: "rrv-1", snapshot_number: 0, supported: true,
      lockfiles: [{ path: "package-lock.json", manifest: "package.json", ecosystem: "npm", semantic_changes: 1, direct_changes: 1, transitive_changes: 0 }],
      changes: [{
        ecosystem: "npm", identity: "npm:left-pad", name: "left-pad", direct: true, scope: "runtime", changes: ["version"],
        before_requirement: "^1.0.0", after_requirement: "^1.3.0", before_version: "1.0.0", after_version: "1.3.0",
        before_source: "registry", after_source: "registry", before_integrity: "sha512-old", after_integrity: "sha512-new",
        license: "MIT", license_source: "lockfile", manifest: "package.json", lockfile: "package-lock.json",
        dependency_paths: [["app", "left-pad"]], transitive_changes: 0, artifacts: [], vulnerabilities: [],
        target_unit_key: "sym-4", target_id: "target-4",
      }],
      summary: { semantic_changes: 1, direct_changes: 1, transitive_changes: 0, added: 0, removed: 0, version_changed: 1, source_changed: 0, integrity_changed: 1 },
      supply_chain: { sboms: [], provenance: [], vulnerability_scans: [] },
      degraded: [],
    };
    api.fetchReviewDependencies.mockResolvedValue(dependencies);
    render(<ReviewReader />);
    const brief = await screen.findByTestId("review-brief");
    const section = await within(brief).findByRole("region", { name: "Dependencies · 1" });
    expect(within(section).getByText("left-pad")).toBeTruthy();
    expect(within(section).getByText("1.0.0 → 1.3.0")).toBeTruthy();
    expect(api.fetchReviewDependencies).toHaveBeenCalledWith("review-1", "rrv-1");
    await waitFor(() => expect(stream.lastProps.fileSemantics.dependencies).toEqual(dependencies));

    // The dependency target jump is an in-Review jump onto the backtrail.
    await userEvent.click(within(section).getByRole("button", { name: "Open dependency target" }));
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenLastCalledWith(expect.objectContaining({ target_id: "target-4" }), expect.anything()));
    expect(await screen.findByRole("button", { name: "Back to Brief" })).toBeTruthy();
  });

  it("offers no Code Peek or Code entry point in a Review-only install", async () => {
    api.fetchOverview.mockResolvedValue({ ...OVERVIEW, workspace_root: "/repo" });
    render(
      <ProductCapabilitiesProvider value={REVIEW_ONLY_PRODUCT_CAPABILITIES}>
        <ReviewReader />
      </ProductCapabilitiesProvider>,
    );
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await act(async () => stream.lastProps.onContext(TARGETS[0], "context"));
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();
    expect(drawer.lastProps.codeRoot).toBe("");
    expect(drawer.lastProps.onOpenInCode).toBeUndefined();
    expect(stream.lastProps.onOpenInCode).toBeUndefined();
  });

  it("opens Code on a local Dashboard that serves only the deterministic code map", async () => {
    api.fetchOverview.mockResolvedValue({ ...OVERVIEW, workspace_root: "/repo" });
    render(
      <ProductCapabilitiesProvider value={LOCAL_DASHBOARD_PRODUCT_CAPABILITIES}>
        <ReviewReader />
      </ProductCapabilitiesProvider>,
    );
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    expect(stream.lastProps.onOpenInCode).toEqual(expect.any(Function));
    await act(async () => stream.lastProps.onContext(TARGETS[0], "context"));
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();
    // Code Peek itself stays gated on runtime.code inside ContextDrawer.
    expect(drawer.lastProps.codeRoot).toBe("/repo");
  });

  it("keeps Code Peek and Code with the Runtime declared present", async () => {
    api.fetchOverview.mockResolvedValue({ ...OVERVIEW, workspace_root: "/repo" });
    render(
      <ProductCapabilitiesProvider value={LOCAL_PRODUCT_CAPABILITIES}>
        <ReviewReader />
      </ProductCapabilitiesProvider>,
    );
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await act(async () => stream.lastProps.onContext(TARGETS[0], "context"));
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();
    expect(drawer.lastProps.codeRoot).toBe("/repo");
    expect(typeof drawer.lastProps.onOpenInCode).toBe("function");
    expect(typeof drawer.lastProps.onJumpToCode).toBe("function");
  });
});

function readTrail() {
  return readReviewBacktrail(new URLSearchParams(window.location.search));
}

describe("ReviewReader finding threshold", () => {
  const origin = { layer: "trusted_base_repository", source: "base .lemoncrow/review.yaml", commit: "c".repeat(40) };

  function thresholdTriage(hidden: string[]): ReviewTriage {
    return {
      version: 1,
      review_id: "review-1",
      revision_id: "rrv-1",
      priority: "normal",
      computed_priority: "normal",
      criticality: "normal",
      severity_summary: { p0: 1, p1: 0, p2: 0, suggestion: 1, unrated: 0, stale: 0, below_threshold: hidden.length, highest: "p0" },
      finding_threshold: { value: hidden.length ? "critical" : "suggestions", label: hidden.length ? "Critical only" : "Suggestions", origin },
      evidence_state: "current",
      review_state: "not_reviewed",
      evidence_label: "Not reviewed · current evidence",
      release_urgency: "unknown",
      dependency_changes: 0,
      required_failures: 0,
      override: null,
      explanation: [],
      hidden_finding_ids: hidden,
      policy_state: "final",
      policy_errors: [],
      computed: true,
    };
  }

  async function renderAt(hidden: string[]) {
    api.fetchProjection.mockResolvedValue(briefProjection({ triage: thresholdTriage(hidden) }));
    api.fetchAnnotations.mockResolvedValue({
      revision_id: "rrv-1",
      annotations: [
        inboxComment({ id: "ann-human", body: "Human concern" }),
        inboxComment({ id: "ann-p0", source: "ai_review", created_by_actor: "agent", title: "Auth bypass", body: "P0 finding" }),
        inboxComment({ id: "ann-low", source: "ai_review", created_by_actor: "agent", title: "Rename helper", body: "Suggestion finding" }),
      ],
      counts: {},
    });
    const view = render(<ReviewReader />);
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();
    const comments = await screen.findByRole("button", { name: /^Review comments: \d+ open/ });
    const status = await screen.findByRole("button", { name: /Review Status:/i });
    await waitFor(() => expect(stream.lastProps.annotations).toHaveLength(3));
    return { view, comments: comments.getAttribute("aria-label"), status: status.textContent };
  }

  it("hides below-threshold findings behind one disclosure without changing counts or readiness", async () => {
    const all = await renderAt([]);
    expect(stream.lastProps.annotations.some((item: Annotation) => item.below_threshold)).toBe(false);
    all.view.unmount();

    const critical = await renderAt(["ann-low"]);
    // Readiness and the open-comment count read every annotation at every threshold.
    expect(critical.comments).toBe(all.comments);
    expect(critical.status).toBe(all.status);
    expect(stream.lastProps.annotations.map((item: Annotation) => [item.id, Boolean(item.below_threshold)])).toEqual([
      ["ann-human", false],
      ["ann-p0", false],
      ["ann-low", true],
    ]);

    const sheet = await openComments();
    expect(within(sheet).getByText("Human concern")).toBeVisible();
    expect(within(sheet).getByText("Auth bypass")).toBeVisible();
    expect(within(sheet).getByText("Rename helper")).not.toBeVisible();
    await userEvent.click(within(sheet).getByText("1 below threshold"));
    expect(within(sheet).getByText("Rename helper")).toBeVisible();
  });
});

/** Rails the reviewer can see: the implementation rail under a Brief/Tour overlay is hidden. */
function visibleRails(): HTMLElement[] {
  // CSS visibility inherits, and projected views re-show themselves with `.visible` inside the
  // invisible Code layout: the nearest `.visible` / `.invisible` ancestor decides.
  return [...document.querySelectorAll<HTMLElement>("[data-review-rail]")]
    .filter((rail) => !rail.closest(".invisible, .visible")?.classList.contains("invisible"));
}

/** Phase 14 item 8: the shell's complexity budget, asserted on every surface. */
function expectComplexityBudget(): void {
  const views = within(screen.getByRole("group", { name: "Review views" })).getAllByRole("button");
  expect(views.length).toBeLessThanOrEqual(REVIEW_MAX_PERSISTENT_VIEWS);
  expect(views.map((button) => button.getAttribute("aria-label"))).toEqual(
    REVIEW_PERSISTENT_VIEW_LABELS.filter((label) => views.some((button) => button.getAttribute("aria-label") === label)),
  );
  expect(document.querySelectorAll(".review-reader-primary")).toHaveLength(1);
  expect(document.querySelectorAll("[data-review-status-authority]")).toHaveLength(1);
  // Progress has exactly one home: the Status control. No second counter.
  expect(document.querySelectorAll(".review-reader-progress, .review-reader-footer-progress")).toHaveLength(0);
  expect(visibleRails().length).toBeLessThanOrEqual(1);
  expect(screen.queryAllByTestId("review-blocking-notice").length).toBeLessThanOrEqual(1);
}

describe("ReviewReader Phase 14 complexity invariants and UX gate", () => {
  it("keeps three persistent views, one primary action and one status authority on Code, Brief and Guided review", async () => {
    api.fetchProjection.mockResolvedValue(tourProjection());
    window.localStorage.setItem(`lc.review.view.${OVERVIEW.session.id}`, "implementation");
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(within(screen.getByRole("group", { name: "Review views" })).getAllByRole("button")).toHaveLength(3));
    expectComplexityBudget();

    await userEvent.click(viewButton("Brief"));
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();
    expectComplexityBudget();

    await userEvent.keyboard("p");
    await userEvent.click(await screen.findByRole("option", { name: /Start guided review/ }));
    expect(await screen.findByText("Auth step")).toBeTruthy();
    // Guided review is transient: it never becomes a fourth persistent view.
    expect(viewButton("Brief").getAttribute("aria-pressed")).toBe("true");
    expectComplexityBudget();
  });

  it("shows at most one right rail across Context, Ask and the Brief", async () => {
    api.fetchProjection.mockResolvedValue(briefProjection());
    window.localStorage.setItem(`lc.review.view.${OVERVIEW.session.id}`, "implementation");
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await act(async () => stream.lastProps.onContext(TARGETS[0], "context"));
    expect(await screen.findByTestId("context-drawer-mock")).toBeTruthy();
    expect(visibleRails().map((rail) => rail.dataset.reviewRail)).toEqual(["context"]);

    await userEvent.click(screen.getByRole("button", { name: "Ask about this review" }));
    expect(await screen.findByText("Ask, challenge, or request a change")).toBeTruthy();
    expect(visibleRails().map((rail) => rail.dataset.reviewRail)).toEqual(["ask"]);
    expectComplexityBudget();

    await userEvent.click(viewButton("Brief"));
    expect(await screen.findByTestId("review-brief")).toBeInTheDocument();
    expect(visibleRails().length).toBeLessThanOrEqual(1);
    expectComplexityBudget();
  });

  it("arbitrates a request error over a Snapshot comparison into one blocking notice", async () => {
    const current = { ...ADVANCED_OVERVIEW, latest_revision_number: 1, default_base_revision_number: 0, default_target_revision_number: 1 };
    api.fetchOverview.mockResolvedValue(current);
    api.fetchTargets.mockResolvedValue({ ...TARGET_LIST, revision_id: current.revision.id });
    api.fetchRevisionComparison.mockResolvedValue({
      ...(await api.fetchRevisionComparison()),
      from_revision: OVERVIEW.revision, to_revision: current.revision,
      code_files: [{
        path: "src/file-0.py", status: "changed", added_lines: 1, removed_lines: 1,
        patch: "--- rev-0/src/file-0.py\n+++ rev-1/src/file-0.py\n@@ -1 +1 @@\n-old\n+new\n",
      }],
    });
    render(<ReviewReader />);
    expect(await screen.findByRole("button", { name: "Show full review" }, { timeout: 3000 })).toBeTruthy();
    expect(screen.getAllByTestId("review-blocking-notice").map((notice) => notice.dataset.notice)).toEqual(["comparison"]);
    await waitFor(() => expect(stream.lastProps?.snapshotComparison).toBeTruthy());

    api.postMark.mockRejectedValueOnce(new Error("mark write failed"));
    act(() => { stream.lastProps.onMark(TARGETS[2], "reviewed"); });
    expect(await screen.findByText(/mark write failed/)).toBeTruthy();
    expect(screen.getAllByTestId("review-blocking-notice").map((notice) => notice.dataset.notice)).toEqual(["error"]);
    expectComplexityBudget();

    await userEvent.click(within(screen.getByTestId("review-blocking-notice")).getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.getAllByTestId("review-blocking-notice").map((notice) => notice.dataset.notice)).toEqual(["comparison"]));
  });

  it("reaches every action Phase 8 moved out of the header from the palette", async () => {
    api.fetchProjection.mockResolvedValue(tourProjection());
    window.localStorage.setItem(`lc.review.view.${OVERVIEW.session.id}`, "implementation");
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(within(screen.getByRole("group", { name: "Review views" })).getAllByRole("button")).toHaveLength(3));
    await waitFor(() => expect(api.fetchReviewDevelopment).toHaveBeenCalled());

    await userEvent.keyboard("p");
    const palette = await screen.findByRole("listbox", { name: "Review actions" });
    const coverage = requiredPaletteCoverage();
    expect(coverage.map((entry) => entry.paletteId).sort()).toEqual(["development", "guided-review", "map", "status"]);
    await waitFor(() => {
      for (const { paletteId, paletteLabel } of coverage) {
        const option = palette.querySelector<HTMLElement>(`[role="option"][id$="-${paletteId}"]`);
        expect(option, paletteId).not.toBeNull();
        expect(option!.querySelector("div > div")?.textContent).toMatch(paletteLabel);
        expect(option!.hasAttribute("disabled"), paletteId).toBe(false);
      }
    });
  });

  it("each palette action that opens a sheet leaves exactly one dialog and Status returns focus to its opener", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    await userEvent.keyboard("p");
    await userEvent.click(await screen.findByRole("option", { name: /Open Review Status/ }));
    expect(await screen.findByRole("dialog", { name: "Review Status" })).toBeTruthy();
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    const status = screen.getByRole("button", { name: /^Review Status:/ });
    await userEvent.click(status);
    expect(await screen.findByRole("dialog", { name: "Review Status" })).toBeTruthy();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(status));

    await userEvent.keyboard("p");
    await userEvent.click(await screen.findByRole("option", { name: /Open comments/ }));
    expect(await screen.findByRole("dialog", { name: "Comments" })).toBeTruthy();
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
  });
});

/** Every argument any mocked network boundary received, serialized. */
function everyNetworkPayload(fetchSpy: { mock: { calls: unknown[][] } }): string {
  const calls = [
    ...Object.values(api).flatMap((fn) => (fn as { mock?: { calls: unknown[][] } }).mock?.calls ?? []),
    ...fetchSpy.mock.calls,
  ];
  return calls.map((args) => {
    try {
      return JSON.stringify(args);
    } catch {
      return String(args);
    }
  }).join("\n");
}

describe("ReviewReader Phase 14 interruption and resume", () => {
  it("an unsent comment, an Ask draft and the Guided position survive a reload without being uploaded", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("{}", { status: 404 }));
    const comment = "UNSENT-COMMENT-7731";
    const question = "UNSENT-ASK-4419";
    try {
      api.fetchProjection.mockResolvedValue(tourProjection());
      window.localStorage.setItem(`lc.review.view.${OVERVIEW.session.id}`, "implementation");
      const first = render(<ReviewReader />);
      expect(await screen.findByText("30 streamed targets")).toBeTruthy();

      await userEvent.keyboard("c");
      await waitFor(() => expect(stream.lastProps.draft?.path).toBe("src/file-0.py"));
      act(() => stream.lastProps.onDraft({ ...stream.lastProps.draft, body: comment }));
      await waitFor(() => expect(stream.lastProps.draft?.body).toBe(comment));

      await userEvent.click(screen.getByRole("button", { name: "Ask about this review" }));
      await userEvent.type(await screen.findByLabelText("Ask Review"), question);
      await userEvent.click(screen.getByRole("button", { name: "Close Ask" }));

      await userEvent.keyboard("p");
      await userEvent.click(await screen.findByRole("option", { name: /Start guided review/ }));
      expect(await screen.findByText("Auth step")).toBeTruthy();
      await userEvent.click(within(screen.getByTestId("review-tour")).getByRole("button", { name: "Next" }));
      expect(await screen.findByText("Routes step")).toBeTruthy();
      const reloadUrl = window.location.pathname + window.location.search;

      // Reload: a fresh mount on the same URL with the same browser storage.
      first.unmount();
      window.history.replaceState(null, "", reloadUrl);
      render(<ReviewReader />);
      expect(await screen.findByText("Routes step")).toBeTruthy();

      await userEvent.click(screen.getByRole("button", { name: "Exit guided review" }));
      await waitFor(() => expect(stream.lastProps?.draft?.body).toBe(comment));
      await userEvent.click(screen.getByRole("button", { name: "Ask about this review" }));
      expect(await screen.findByLabelText("Ask Review")).toHaveValue(question);

      const sent = everyNetworkPayload(fetchSpy);
      expect(sent).not.toContain(comment);
      expect(sent).not.toContain(question);
      expect(api.postAnnotation).not.toHaveBeenCalled();
    } finally {
      fetchSpy.mockRestore();
    }
  });
});

describe("ReviewReader attached to hosted", () => {
  const HOSTED = {
    reference: "r/9",
    origin: "https://hosted.example",
    url: "https://hosted.example/r/9",
    local_review_id: "local-1",
  };
  const queuedAction = (kind: string, payload: Record<string, unknown>) => ({
    id: "rha-1",
    sequence: 1,
    kind,
    snapshot_id: "rrv-1",
    supersede_key: "",
    payload,
    preconditions: {},
    state: "pending",
    reason: "",
    result: {},
    created_at: "",
    resolved_at: "",
  });
  const queued = (action: ReturnType<typeof queuedAction>) => ({
    queued: true,
    reference: "r/9",
    actions: [action],
    superseded: [],
    counts: { pending: 1, applied: 0, conflict: 0, discarded: 0 },
  });

  beforeEach(() => {
    api.fetchOverview.mockResolvedValue({ ...OVERVIEW, session: { ...OVERVIEW.session, hosted: HOSTED } });
    api.fetchHostedAttachment.mockResolvedValue({
      attached: true,
      review_id: "review-1",
      reference: "r/9",
      actions: [],
      counts: { pending: 0, applied: 0, conflict: 0, discarded: 0 },
    });
  });

  it("acknowledges a queued judgment without applying it locally", async () => {
    api.postMark.mockResolvedValueOnce(queued(queuedAction("mark", { state: "reviewed", unit_key: "sym-0" })));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchHostedAttachment).toHaveBeenCalledWith("review-1"));

    await userEvent.keyboard("r");

    await waitFor(() => expect(api.postMark).toHaveBeenCalledWith("review-1", "sym-0", "reviewed", "rrv-1"));
    const signal = await screen.findByTestId("hosted-attachment-signal");
    await waitFor(() => expect(signal.textContent).toContain(
      "Queued for hosted r/9: Mark reviewed · sym-0. It applies when you run `lc review hosted sync`.",
    ));
    // The optimistic judgment is reverted: hosted has not accepted it.
    expect(stream.lastProps.targets.find((target: ReviewTarget) => target.target_id === "target-0")?.state).toBe("unreviewed");
    expect(screen.queryByText((_text, element) => element?.textContent === "1 reviewed")).toBeNull();
    // The queue is re-read after the write.
    await waitFor(() => expect(api.fetchHostedAttachment.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("acknowledges a queued comment and clears the composer", async () => {
    api.postAnnotation.mockResolvedValueOnce(queued(queuedAction("comment", { path: TARGETS[0].path, start_line: 3, body: "Guard this." })));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    act(() => stream.lastProps.onCreate({
      path: TARGETS[0].path,
      start_line: 3,
      end_line: 3,
      side: "additions",
      body: "Guard this.",
      kind: "comment",
      target_unit_key: TARGETS[0].unit_key,
    }));

    await waitFor(() => expect(api.postAnnotation).toHaveBeenCalledTimes(1));
    expect((await screen.findByTestId("hosted-attachment-signal")).textContent).toContain(
      `Queued for hosted r/9: Comment on ${TARGETS[0].path}:L3: “Guard this.”`,
    );
    expect(stream.lastProps.draft).toBeNull();
  });

  it("queues the verdict but never finishes an attached Review locally", async () => {
    api.postReviewOutcome.mockResolvedValueOnce(queued(queuedAction("outcome", { outcome: "lgtm" })));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const sheet = await openFinish();
    await userEvent.click(within(sheet).getByRole("radio", { name: "LGTM" }));
    await userEvent.click(within(sheet).getByRole("button", { name: "LGTM & finish anyway" }));

    expect(await within(sheet).findByRole("alert")).toHaveTextContent(
      "Your verdict is queued for hosted r/9. Finishing is online-only: run `lc review hosted sync`, then finish on the hosted Review.",
    );
    expect(api.postReviewOutcome).toHaveBeenCalledTimes(1);
    expect(api.finishReview).not.toHaveBeenCalled();
  });

  const LOCAL_LGTM = {
    id: "rot-local",
    review_id: "review-1",
    revision_id: "rrv-1",
    reviewer_id: "local",
    outcome: "lgtm",
    summary: "",
    created_at: "2026-09-18T00:00:00Z",
    stale: false,
  };
  const attachedWith = (my_outcome: Record<string, unknown> | null) => ({
    attached: true,
    review_id: "review-1",
    reference: "r/9",
    projection: { snapshot: { id: "hs-1", number: 0 }, my_outcome },
    actions: [],
    counts: { pending: 0, applied: 0, conflict: 0, discarded: 0 },
  });

  it.each([
    ["no verdict on hosted", null],
    ["hosted's verdict covers an older Snapshot", { id: "rot-9", outcome: "lgtm", revision_id: "hs-0", summary: "" }],
    ["hosted's verdict differs", { id: "rot-9", outcome: "comment", revision_id: "hs-1", summary: "" }],
    ["an older hosted names no summary", { id: "rot-9", outcome: "lgtm", revision_id: "hs-1" }],
  ])("queues the verdict when a local one matches but hosted's does not (%s)", async (_case, mine) => {
    api.fetchReviewOutcome.mockResolvedValue({
      review_id: "review-1", revision_id: "rrv-1", reviewer_id: "local", current: LOCAL_LGTM, history: [LOCAL_LGTM],
    });
    api.fetchHostedAttachment.mockResolvedValue(attachedWith(mine));
    api.postReviewOutcome.mockResolvedValueOnce(queued(queuedAction("outcome", { outcome: "lgtm" })));
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchHostedAttachment).toHaveBeenCalledWith("review-1"));
    const sheet = await openFinish();
    await userEvent.click(within(sheet).getByRole("radio", { name: "LGTM" }));
    await userEvent.click(within(sheet).getByRole("button", { name: "LGTM & finish anyway" }));

    await waitFor(() => expect(api.postReviewOutcome).toHaveBeenCalledTimes(1));
    expect(await within(sheet).findByRole("alert")).toHaveTextContent("Your verdict is queued for hosted r/9.");
    expect(api.finishReview).not.toHaveBeenCalled();
  });

  it("never re-queues the verdict hosted already holds on its current Snapshot", async () => {
    api.fetchHostedAttachment.mockResolvedValue(
      attachedWith({ id: "rot-9", outcome: "lgtm", revision_id: "hs-1", summary: "" }),
    );
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await waitFor(() => expect(api.fetchHostedAttachment).toHaveBeenCalledWith("review-1"));
    const sheet = await openFinish();
    await userEvent.click(within(sheet).getByRole("radio", { name: "LGTM" }));
    await userEvent.click(within(sheet).getByRole("button", { name: "LGTM & finish anyway" }));

    expect(await within(sheet).findByRole("alert")).toHaveTextContent(
      "Your verdict is already on hosted r/9. Finishing is online-only: finish on the hosted Review.",
    );
    expect(api.postReviewOutcome).not.toHaveBeenCalled();
    expect(api.finishReview).not.toHaveBeenCalled();
  });

  it("names the hosted queue's conflicts on the attachment signal", async () => {
    api.fetchHostedAttachment.mockResolvedValue({
      attached: true,
      review_id: "review-1",
      reference: "r/9",
      actions: [{ ...queuedAction("mark", { state: "reviewed", unit_key: "sym-0" }), state: "conflict", reason: "snapshot_advanced" }],
      counts: { pending: 0, applied: 0, conflict: 1, discarded: 0 },
    });
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();

    const open = await screen.findByTestId("hosted-queue-open");
    await waitFor(() => expect(open.textContent).toBe("Queue · 1 conflict"));
    await userEvent.click(open);
    const panel = await screen.findByTestId("hosted-queue-panel");
    expect(panel.textContent).toContain("Hosted has a newer Snapshot than the one you judged.");
    expect(within(panel).getByRole("button", { name: "Re-apply" })).toBeTruthy();
  });
});
