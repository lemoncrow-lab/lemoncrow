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

const api = vi.hoisted(() => ({
  adoptBootstrapFragment: vi.fn(),
  deliverFeedback: vi.fn(),
  fetchOverview: vi.fn(),
  fetchTargets: vi.fn(),
  fetchAnnotations: vi.fn(),
  fetchReviewEvidence: vi.fn(),
  fetchReviewOutcome: vi.fn(),
  fetchReviewPreparation: vi.fn(),
  fetchReviewPatch: vi.fn(),
  fetchReviewSurfaces: vi.fn(),
  runReviewSurface: vi.fn(),
  fetchFile: vi.fn(),
  fetchFinishReview: vi.fn(),
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
  historicalReviewHref: (reviewId: string, revisionNumber: number) => `/r/${reviewId}/rev/${revisionNumber}`,
  sourceCompareHref: (fromRef: string, toRef: string) => `/r/x/${encodeURIComponent(fromRef)}..${encodeURIComponent(toRef)}`,
  deliverFeedback: api.deliverFeedback,
  fetchOverview: api.fetchOverview,
  fetchTargets: api.fetchTargets,
  fetchAnnotations: api.fetchAnnotations,
  fetchReviewEvidence: api.fetchReviewEvidence,
  fetchReviewOutcome: api.fetchReviewOutcome,
  fetchReviewPreparation: api.fetchReviewPreparation,
  fetchReviewPatch: api.fetchReviewPatch,
  fetchReviewSurfaces: api.fetchReviewSurfaces,
  runReviewSurface: api.runReviewSurface,
  fetchFile: api.fetchFile,
  fetchFinishReview: api.fetchFinishReview,
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
    base_revision_number: 1,
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
    revision_number: 1,
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

const ADVANCED_OVERVIEW: ReviewOverview = {
  ...OVERVIEW,
  revision: { ...OVERVIEW.revision, id: "rrv-2", revision_number: 2 },
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
  await userEvent.click(screen.getByRole("button", { name: "Refresh local revision" }));
  expect(await screen.findByText("Review updated elsewhere")).toBeTruthy();
}

beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
  window.localStorage.clear();
  window.history.replaceState(null, "", "/review");
  document.documentElement.classList.remove("light", "dark");
  stream.lastProps = null;
  drawer.lastProps = null;
  api.adoptBootstrapFragment.mockReturnValue({ token: "t", reviewId: "review-1" });
  api.fetchReviewPreparation.mockResolvedValue({ state: "ready", detail: "" });
  api.fetchOverview.mockResolvedValue(OVERVIEW);
  api.fetchTargets.mockResolvedValue(TARGET_LIST);
  api.fetchAnnotations.mockResolvedValue({ revision_id: "rrv-1", annotations: [], counts: {} });
  api.fetchReviewEvidence.mockResolvedValue({ revision_id: "rrv-1", evidence: [] });
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
    expect(screen.getByText((_text, element) => element?.textContent === "30 targets")).toBeTruthy();
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
    expect(screen.getByText("Revision")).toBeTruthy();
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
    });
    api.fetchReviewPreparation
      .mockReturnValueOnce(preparation.promise)
      .mockResolvedValue({ state: "ready", detail: "" });

    render(<ReviewReader />);

    expect(await screen.findByText("Preparing review")).toBeTruthy();
    expect(api.fetchOverview).not.toHaveBeenCalled();

    await act(async () => {
      preparation.resolve({ state: "impact_background", detail: "" });
      await Promise.resolve();
    });

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
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
    expect(screen.getByRole("button", { name: /Review readiness:/i }).getAttribute("aria-label")).toContain("Not ready");
    expect(await screen.findByText(/Review discussions unavailable: comments offline/)).toBeTruthy();
    expect(screen.getByText(/Review evidence unavailable: evidence offline/)).toBeTruthy();
    expect(screen.getByText(/Review outcome unavailable: outcome offline/)).toBeTruthy();
    expect(screen.getByText(/Rendered review surfaces unavailable: surface offline/)).toBeTruthy();
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
    expect(api.runReviewSurface).toHaveBeenCalledWith("review-1", "service", "app-stack:api", "new");
    expect(api.runReviewSurface).toHaveBeenCalledWith("review-1", "service", "app-stack:app", "new");
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
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(35));
    expect(new Set(api.fetchFile.mock.calls.map((call) => call[1]))).toEqual(
      new Set(Array.from({ length: 35 }, (_, index) => `src/large-${index}.py`)),
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
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(35));
    expect(stream.lastProps.remainingFileCount).toBe(495);

    act(() => stream.lastProps.onLoadMore());

    // The next 30-file page was already prepared, so visibility changes without
    // refetching it. Preparation immediately moves on to the following page.
    await waitFor(() => expect(stream.lastProps.remainingFileCount).toBe(465));
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(65));
    expect(new Set(api.fetchFile.mock.calls.map((call) => call[1]))).toEqual(
      new Set(Array.from({ length: 65 }, (_, index) => `src/large-${index}.py`)),
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

    await waitFor(() => expect(api.postMark).toHaveBeenCalledWith("review-1", "sym-0", "reviewed"));
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

    await waitFor(() => expect(api.postMark).toHaveBeenCalledWith("review-1", "sym-0", "reviewed"));
    await waitFor(() => expect(stream.scrollToTarget).toHaveBeenCalled());
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].target_id).toBe("target-1");
    expect(stream.scrollToTarget.mock.calls.at(-1)?.[0].path).toBe("src/file-0.py");
  });

  it("review-and-advance stops at the end instead of cycling to earlier targets", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    stream.scrollToTarget.mockClear();

    await act(async () => stream.lastProps.onMark(TARGETS.at(-1), "reviewed", true));

    await waitFor(() => expect(api.postMark).toHaveBeenCalledWith("review-1", "sym-29", "reviewed"));
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
      revision: { ...OVERVIEW.revision, id: "rrv-2", revision_number: 2 },
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

    await userEvent.click(screen.getByRole("button", { name: "Refresh local revision" }));

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

  it("opens the compact change overview and jumps from a major change into the stream", async () => {
    api.fetchOverview.mockResolvedValueOnce({
      ...OVERVIEW,
      brief: {
        summary: "3 files across Review reader and target progress; 1 currently deserves focused attention.",
        themes: ["Review reader", "Target progress"],
        major_changes: [{
          key: "commit:abc",
          label: "feat(review): target-aware continuous reader",
          file_count: 2,
          target_count: 15,
          attention_count: 1,
          first_path: "src/file-1.py",
        }],
        review_first: [],
        verification: { pass: 3, fail: 1, not_run: 0, unknown: 1 },
        annotations: { human: 1, author: 0, lemoncrow: 2, ai_review: 0 },
        artifacts: { current: 2, stale: 1 },
      },
    });

    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Open change overview" }));

    const dialog = await screen.findByRole("dialog", { name: "Change overview" });
    expect(within(dialog).getByText("feat(review): target-aware continuous reader")).toBeTruthy();
    expect(within(dialog).getByText("2 files · 15 targets")).toBeTruthy();
    expect(within(dialog).getByText("1 elevated")).toBeTruthy();
    expect(within(dialog).getByText("1 failed")).toBeTruthy();
    expect(within(dialog).getByText("1 previous revision")).toBeTruthy();

    await userEvent.click(within(dialog).getByRole("button", { name: /target-aware continuous reader/i }));
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
    await waitFor(() => expect(api.fetchFile).toHaveBeenCalledTimes(35));

    await userEvent.keyboard("/");
    const search = await screen.findByRole("textbox", { name: "Search review" });
    await userEvent.type(search, "large-499 fn_1499");
    expect(await screen.findByText("1 target · 1 file")).toBeTruthy();
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
    expect(await screen.findByText(/10 targets · 1 file/)).toBeTruthy();

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
    expect(await screen.findByText("1 target · 1 file")).toBeTruthy();
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
        revision_number: 2,
        previous_revision_number: 1,
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
    await userEvent.click(screen.getByRole("button", { name: "Refresh local revision" }));

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
      revision: { ...OVERVIEW.revision, id: "rrv-2", revision_number: 2 },
      revision_count: 2,
      progress: { ...OVERVIEW.progress!, reviewed: 24, changed_since_review: 6, unreviewed: 0 },
      refreshed: {
        created: true,
        revision_number: 2,
        previous_revision_number: 1,
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
    await userEvent.click(screen.getByRole("button", { name: "Refresh local revision" }));

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
      revision: { ...OVERVIEW.revision, id: "rrv-2", revision_number: 2 },
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
    await userEvent.click(screen.getByRole("button", { name: "Refresh local revision" }));
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
    await userEvent.click(screen.getByRole("button", { name: "Finish review…" }));

    await waitFor(() => expect(api.fetchFinishReview).toHaveBeenCalledWith("review-1"));
    const finishDialog = await screen.findByRole("dialog", { name: "Finish review" });
    expect(finishDialog).toBeTruthy();
    expect(api.finishReview).not.toHaveBeenCalled();
    expect(within(finishDialog).getByText("0/30")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Finish anyway" }));
    await waitFor(() => expect(api.finishReview).toHaveBeenCalledWith("review-1", "finished"));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Finish review" })).toBeNull());
    expect(screen.getByRole("button", { name: "Reopen review" })).toBeTruthy();
  });

  it("returns focus to the primary review action when the finish sheet is dismissed", async () => {
    render(<ReviewReader />);
    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
    const primaryButton = screen.getByRole("button", { name: "Review next area" });

    await userEvent.click(screen.getByLabelText("Review actions"));
    await userEvent.click(screen.getByRole("button", { name: "Finish review…" }));
    const close = await screen.findByRole("button", { name: "Close finish review" });
    await waitFor(() => expect(document.activeElement).toBe(close));
    await userEvent.click(screen.getByRole("button", { name: "Continue reviewing" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Finish review" })).toBeNull());
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
    await waitFor(() => expect(stream.lastProps.annotations[0]?.author_response).toBe("addressed"), { timeout: 3000 });
    expect(stream.lastProps.annotations[0].author_response_source_id).toBe("codex-session-1");
    expect(api.postRefresh).not.toHaveBeenCalled();
  });

  it("blocks marking when a refresh advances the revision but its targets never load", async () => {
    await strandReaderOnAdvancedRevision();

    expect(screen.getByText(/· rev 2/)).toBeTruthy();
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
    await userEvent.click(screen.getByRole("button", { name: "Refresh local revision" }));

    // The overview is committed the moment postRefresh resolves, but the
    // reviewer hears nothing while the dependent reads are still out: `busy`
    // is already holding every judgment, and the refresh may yet succeed.
    expect(await screen.findByText(/· rev 2/)).toBeTruthy();
    expect(screen.queryByText("Review updated elsewhere")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();

    await act(async () => { releaseTargets(TARGET_LIST); });
    await waitFor(() => expect(stream.lastProps.busy).toBe(false));
    expect(screen.queryByText("Review updated elsewhere")).toBeNull();
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
    await userEvent.click(screen.getByRole("button", { name: "Finish review…" }));
    expect(await screen.findByRole("dialog", { name: "Finish review" })).toBeTruthy();

    await userEvent.click(screen.getByRole("radio", { name: "LGTM" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Review outcome summary" }), "Ready from my side.");
    await userEvent.click(screen.getByRole("button", { name: "Finish anyway" }));

    await waitFor(() => expect(api.postReviewOutcome).toHaveBeenCalledWith(
      "review-1",
      "lgtm",
      "Ready from my side.",
    ));
    await waitFor(() => expect(api.finishReview).toHaveBeenCalledWith("review-1", "finished"));
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

    await userEvent.click(screen.getByRole("button", { name: "Reload review" }));
    await waitFor(() => expect(screen.queryByText("Review updated elsewhere")).toBeNull());

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

    expect(screen.getByText("Review updated elsewhere")).toBeTruthy();
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

    expect(await screen.findByText("1 target · 1 file")).toBeTruthy();
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
    expect(await screen.findByText("1 target · 1 file")).toBeTruthy();
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
    expect(await screen.findByText("1 target · 1 file")).toBeTruthy();

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
        revision_number: 2,
        previous_revision_number: 1,
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
    await userEvent.click(screen.getByRole("button", { name: "Refresh local revision" }));

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
        revision_number: 2,
        previous_revision_number: 1,
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
    await userEvent.click(screen.getByRole("button", { name: "Refresh local revision" }));

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
    expect(screen.queryByText("New revision available")).toBeNull();
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
    expect(await screen.findByText("New revision available")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Capture new revision" })).toBeTruthy();
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
    expect(await screen.findByText("New revision available")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Capture new revision" })).toBeTruthy();
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
        revision_number: 2,
        previous_revision_number: 1,
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
    expect(await screen.findByText("New revision available")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Update review" })).toBeNull();
    expect(api.postRefresh).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Capture new revision" }));
    await waitFor(() => expect(api.postRefresh).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText(/· rev 2/)).toBeTruthy());
    expect(screen.queryByText("New revision available")).toBeNull();
  });

  it("keeps a settled source change explicit while a draft exists", async () => {
    const probes: Array<(state: SourceState) => void> = [];
    api.fetchSourceState.mockImplementation(() => new Promise<SourceState>((resolve) => { probes.push(resolve); }));
    const changed: SourceState = { supported: true, changed: true, fingerprint: "wt-draft", path_count: 1, paths: ["src/file-2.py"], reason: "" };
    api.postRefresh.mockResolvedValueOnce({
      ...ADVANCED_OVERVIEW,
      refreshed: {
        created: true,
        revision_number: 2,
        previous_revision_number: 1,
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

    expect(await screen.findByText("New revision available")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Capture new revision" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Update review" })).toBeNull();
    expect(api.postRefresh).not.toHaveBeenCalled();

    await userEvent.keyboard("{Escape}");
    expect(api.postRefresh).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Capture new revision" }));
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
        revision_number: 2,
        previous_revision_number: 1,
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

    expect(await screen.findByText("New revision available")).toBeTruthy();
    const update = screen.getByRole("button", { name: "Capture new revision" });
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
    expect(await screen.findByText("0 targets · 0 files")).toBeTruthy();
    expect(screen.getByText("No matches.")).toBeTruthy();

    act(() => search.blur());
    await userEvent.keyboard("{Escape}");

    expect(await screen.findByText("30 streamed targets")).toBeTruthy();
  });
});
