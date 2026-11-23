import userEvent from "@testing-library/user-event";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ReviewFlowRoute } from "./ReviewFlowRoute";
import { LOCAL_BROWSER_RECONNECT_MESSAGE } from "./localBrowser";
import ReviewReader, { type ReviewReaderExtensions } from "./ReviewReader";
import { stateLine, stateLineSignals, type StateLineFacts } from "./reviewStateLineModel";

const flags = vi.hoisted(() => ({ brief: false }));

vi.mock("./reviewEnvironment", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./reviewEnvironment")>();
  const local = actual.fallbackReviewEnvironment("local");
  return {
    ...actual,
    useReviewEnvironment: () => ({
      ...local,
      discovered: true,
      review: { ...local.review, brief: flags.brief },
    }),
  };
});

const api = vi.hoisted(() => ({
  fetchProjection: vi.fn(),
  fetchHistoricalOverview: vi.fn(),
  fetchHistoricalTargets: vi.fn(),
  fetchHistoricalAnnotations: vi.fn(),
  fetchHistoricalFile: vi.fn(),
  fetchHistoricalReviewJournal: vi.fn(),
  fetchOverview: vi.fn(),
  fetchStartupOverview: vi.fn(),
  fetchReviewJournal: vi.fn(),
  fetchReviewDevelopment: vi.fn(),
  fetchHistoricalReviewDevelopment: vi.fn(),
  nextLiveState: vi.fn(),
  fetchTargets: vi.fn(),
  fetchStartupTargets: vi.fn(),
  fetchTargetsForPath: vi.fn(),
  fetchAnnotations: vi.fn(),
  fetchFile: vi.fn(),
  fetchRevisionLocator: vi.fn(),
  fetchRevisions: vi.fn(),
  fetchRevisionComparison: vi.fn(),
  fetchSourceComparison: vi.fn(),
  fetchSourceState: vi.fn(),
  fetchReviewEvidence: vi.fn(),
  fetchReviewReadiness: vi.fn(),
  armReviewLanding: vi.fn(),
  cancelReviewLanding: vi.fn(),
  fetchReviewOutcome: vi.fn(),
  fetchReviewSurfaces: vi.fn(),
  fetchChangeProposals: vi.fn(),
  fetchProposalSelection: vi.fn(),
  fetchReviewPreparation: vi.fn(),
  fetchReviewPatch: vi.fn(),
  deliverFeedback: vi.fn(),
  createChangeProposal: vi.fn(),
  applyChangeProposal: vi.fn(),
  postMark: vi.fn(),
  fetchFixLines: vi.fn(),
  runReviewSurface: vi.fn(),
  finishReview: vi.fn(),
  postRefresh: vi.fn(),
  publishReviewSnapshot: vi.fn(),
  postReviewOutcome: vi.fn(),
  fetchFinishReview: vi.fn(),
  exportFeedback: vi.fn(),
  fetchHostedAttachment: vi.fn(),
}));

vi.mock("./ReviewStream", async () => {
  const React = await import("react");
  return {
    default: React.forwardRef((props: any, ref: any) => {
      React.useImperativeHandle(ref, () => ({
        scrollToTarget: vi.fn(),
        scrollToFile: vi.fn(),
        scrollToSurface: vi.fn(),
      }));
      return <div data-testid="historical-stream">{props.targets.length} historical part</div>;
    }),
  };
});

vi.mock("./reviewApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./reviewApi")>()),
  authenticatedFetch: vi.fn(async () => Response.json({ notes: [] })),
  adoptBootstrapFragment: () => ({ token: "t", reviewId: "rev-history" }),
  selectReviewId: vi.fn(),
  reviewDirectoryHref: () => "/reviews#t=t",
  reviewHref: (id: string) => `/r/${id.startsWith("r/") ? id.slice(2) : id}`,
  historicalReviewHref: (id: string, revisionNumber?: number) => revisionNumber === undefined
    ? `/rr/${id.startsWith("rr/") ? id.slice(3) : id}`
    : `/r/${id.startsWith("r/") ? id.slice(2) : id}?base=${revisionNumber}&target=${revisionNumber}`,
  reviewCompareHref: (id: string, base: number, target: number) => `/r/${id.startsWith("r/") ? id.slice(2) : id}?base=${base}&target=${target}`,
  sourceCompareHref: (from: string, to: string) => `/r/x/${from}..${to}`,
  fetchHistoricalOverview: api.fetchHistoricalOverview,
  fetchHistoricalTargets: api.fetchHistoricalTargets,
  fetchHistoricalAnnotations: api.fetchHistoricalAnnotations,
  fetchHistoricalFile: api.fetchHistoricalFile,
  fetchHistoricalReviewJournal: api.fetchHistoricalReviewJournal,
  fetchOverview: api.fetchOverview,
  fetchProjection: api.fetchProjection,
  fetchStartupOverview: api.fetchStartupOverview,
  fetchReviewJournal: api.fetchReviewJournal,
  fetchReviewDevelopment: api.fetchReviewDevelopment,
  fetchHistoricalReviewDevelopment: api.fetchHistoricalReviewDevelopment,
  fetchReviewDevelopmentEvents: vi.fn(async () => ({ events: [], next_before: null })),
  nextLiveState: api.nextLiveState,
  subscribeLiveState: (_id: string, onState: (state: any) => Promise<void>) => {
    let live = true;
    const feed = async () => {
      while (live) {
        const state = await api.nextLiveState();
        if (!live) return;
        try { await onState(state); } catch { /* reconnect from fresh authoritative state */ }
      }
    };
    void feed();
    return () => { live = false; };
  },
  fetchTargets: api.fetchTargets,
  fetchStartupTargets: api.fetchStartupTargets,
  fetchTargetsForPath: api.fetchTargetsForPath,
  fetchAnnotations: api.fetchAnnotations,
  fetchFile: api.fetchFile,
  fetchRevisionLocator: api.fetchRevisionLocator,
  fetchRevisions: api.fetchRevisions,
  fetchRevisionComparison: api.fetchRevisionComparison,
  fetchSourceComparison: api.fetchSourceComparison,
  fetchReviewEvidence: api.fetchReviewEvidence,
  fetchReviewReadiness: api.fetchReviewReadiness,
  armReviewLanding: api.armReviewLanding,
  cancelReviewLanding: api.cancelReviewLanding,
  fetchReviewOutcome: api.fetchReviewOutcome,
  fetchReviewSurfaces: api.fetchReviewSurfaces,
  fetchSourceState: api.fetchSourceState,
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
  fetchReviewPreparation: api.fetchReviewPreparation,
  fetchReviewPatch: api.fetchReviewPatch,
  fetchFinishReview: api.fetchFinishReview,
  fetchRelatedSource: vi.fn(),
  fetchHistoricalRelatedSource: vi.fn(),
  postMark: api.postMark,
  postBulkReviewed: vi.fn(),
  undoBulkReviewed: vi.fn(),
  postRefresh: api.postRefresh,
  publishReviewSnapshot: api.publishReviewSnapshot,
  postReviewOutcome: api.postReviewOutcome,
  postAnnotation: vi.fn(),
  patchAnnotation: vi.fn(),
  finishReview: api.finishReview,
  exportFeedback: api.exportFeedback,
  fetchHostedAttachment: api.fetchHostedAttachment,
  deliverFeedback: api.deliverFeedback,
  createChangeProposal: api.createChangeProposal,
  applyChangeProposal: api.applyChangeProposal,
  uploadReviewEvidence: vi.fn(),
  linkReviewEvidence: vi.fn(),
  runReviewSurface: api.runReviewSurface,
  fetchFixLines: api.fetchFixLines,
  requestBrowserJson: vi.fn(async () => ({})),
  fetchReviewSeenCoverage: vi.fn(() => new Promise(() => {})),
  fetchReviewProjection: vi.fn(() => new Promise(() => {})),
}));

const target = {
  target_id: "target-1",
  unit_key: "sym:app:run",
  kind: "symbol",
  path: "src/app.py",
  label: "src/app.py::run",
  symbol: "run",
  start_line: 1,
  end_line: 2,
  hunk_ordinals: [0],
  spans: [{ side: "new", start_line: 1, end_line: 2, hunk_ordinal: 0 }],
  state: "unreviewed",
  changed_since_mark: false,
  reviewed_revision_id: "",
  attention_rank: 1,
  attention_level: "normal",
  reasons: ["implementation change"],
  additions: 1,
  deletions: 0,
  fingerprint_method: "symbol_body_sha256",
  verification: { pass: 0, fail: 0, unknown: 0 },
  annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
};

const overview = {
  session: { id: "rev-history", title: "Historical fixture", subject_type: "local_change", range_mode: "working_tree", source_ref: "", status: "open", actor_type: "agent", reviewer_id: "local", repo_root: "/repo", updated_at: "2026-09-15T20:00:00Z" },
  revision: { id: "11111111111171118111111111111111", ref: "rr/11111111111171118111111111111111", revision_number: 0, range_mode: "working_tree", base_sha: "base", head_sha: "", dirty: true, degraded: [], provenance_host: "", provenance_model: "", provenance_session_id: "", provenance_certainty: "none", created_at: "2026-09-15T19:00:00Z" },
  revision_count: 2,
  historical: true,
  latest_revision_number: 1,
  latest_revision: { id: "22222222222272228222222222222222", ref: "rr/22222222222272228222222222222222", revision_number: 1, range_mode: "working_tree", base_sha: "base", head_sha: "", dirty: true, degraded: [], provenance_host: "", provenance_model: "", provenance_session_id: "", provenance_certainty: "none", created_at: "2026-09-15T20:00:00Z" },
  historical_judgment_complete: false,
  packet_available: true,
  stats: {},
  title: "Historical fixture",
  provenance: { status: "unknown", host: "", model: "", session_id: "", task: "", certainty: "none", match_confidence: 0, match_reason: "", commands_run: [], subagents: [], reads_recorded: false, inspected: null, uninspected_impacted: [] },
  evidence: [],
  degraded: [],
  groups: [],
  group_counts: { needs_attention: 0, changed: 0, unreviewed: 1, reviewed: 0, mechanical: 0 },
  unit_count: 1,
  target_count: 1,
  progress: { target_count: 1, reviewed: 0, unreviewed: 1, needs_changes: 0, changed_since_review: 0, unknown: 0 },
  outline: [{ path: "src/app.py", reviewed: 0, target_count: 1, state: "unreviewed", attention_level: "normal", reasons: ["implementation change"], additions: 1, deletions: 0 }],
  mark_count: 0,
  discarded: [],
} as any;

describe("ReviewReader mounted through the redesigned flow routes", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    flags.brief = false;


    window.history.replaceState(null, "", "/");
    api.fetchHistoricalOverview.mockResolvedValue(overview);
    api.fetchStartupOverview.mockImplementation((id: string) => api.fetchOverview(id));
    api.fetchHistoricalTargets.mockResolvedValue({ revision_id: "11111111111171118111111111111111", historical: true, order: "recommended", targets: [target], progress: overview.progress, outline: overview.outline });
    api.fetchStartupTargets.mockImplementation((id: string, order?: "recommended" | "file") => api.fetchTargets(id, order));
    api.fetchTargetsForPath.mockImplementation((id: string, _path: string, order?: "recommended" | "file") => api.fetchTargets(id, order));
    api.fetchHistoricalAnnotations.mockResolvedValue({ revision_id: "11111111111171118111111111111111", historical: true, annotations: [], counts: {} });
    api.fetchHistoricalFile.mockResolvedValue({ path: "src/app.py", status: "modified", language: "py", additions: 1, deletions: 0, patch: "diff --git a/src/app.py b/src/app.py\n@@ -1 +1 @@\n-old\n+historical\n", renderable: true, refusal: "", detail: "", symbols: [], provenance: overview.provenance, evidence: [], degraded: [] });
    api.fetchHistoricalReviewJournal.mockResolvedValue({ available: false, reason: "", review_id: overview.session.id, revision_id: overview.revision.id, revision_number: overview.revision.revision_number, session_id: "", host: "", entries: [] });
    api.fetchHistoricalReviewDevelopment.mockResolvedValue(null);
    api.fetchReviewDevelopment.mockResolvedValue(null);
    api.fetchReviewJournal.mockResolvedValue({ available: false, reason: "", review_id: overview.session.id, revision_id: overview.latest_revision.id, revision_number: overview.latest_revision.revision_number, session_id: "", host: "", entries: [] });
    api.fetchFixLines.mockResolvedValue({ fix_lines: [], fix_of: null });
    api.fetchReviewReadiness.mockImplementation(() => new Promise(() => {}));
    api.fetchReviewOutcome.mockResolvedValue({ current: null, history: [] });
    api.fetchChangeProposals.mockResolvedValue({ revision_id: "x", source_mutation_supported: false, proposals: [] });
    api.nextLiveState.mockImplementation(() => new Promise(() => {}));
    api.fetchReviewPreparation.mockResolvedValue({ state: "ready", detail: "" });
    api.fetchReviewEvidence.mockResolvedValue({ evidence: [] });
    api.fetchReviewSurfaces.mockResolvedValue({ surfaces: [] });
    api.fetchFile.mockResolvedValue({ path: "src/app.py", status: "modified", language: "py", additions: 1, deletions: 0, patch: "", revision_id: overview.latest_revision.id });
    api.fetchRevisions.mockResolvedValue({
      revisions: [
        { ...overview.revision, is_last_reviewed: true },
        { ...overview.latest_revision, is_latest: true },
      ],
      latest_revision_number: overview.latest_revision_number,
      last_reviewed_revision_number: overview.revision.revision_number,
      last_commented_revision_number: overview.revision.revision_number,
      default_base_revision_number: overview.revision.revision_number,
      default_target_revision_number: overview.latest_revision_number,
    });
    api.fetchRevisionComparison.mockImplementation(async (_id: string, base?: number, targetRevision?: number) => ({
      review_id: overview.session.id,
      reviewer_id: "local",
      from_revision: base === overview.latest_revision_number ? overview.latest_revision : overview.revision,
      to_revision: targetRevision === overview.latest_revision_number ? overview.latest_revision : overview.revision,
      suggested_from_revision_number: overview.revision.revision_number,
      revision_state: {
        latest_revision_number: overview.latest_revision_number,
        last_reviewed_revision_number: overview.revision.revision_number,
        last_commented_revision_number: overview.revision.revision_number,
        default_base_revision_number: overview.revision.revision_number,
        default_target_revision_number: overview.latest_revision_number,
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
    }));
  });

  function closureOf(overrides: Record<string, unknown> = {}) {
    return { status: "open", target_count: 1, reviewed_targets: 1, unreviewed_targets: 0, changed_since_review: 0, needs_changes: 0, unknown_targets: 0, later_targets: 0, open_comments: 0, orphaned_comments: 0, failed_verification: 0, unresolved_verification: 0, previous_revision_evidence: 0, discarded_verdicts: 0, ...overrides } as any;
  }

  function live() {
    api.fetchFinishReview.mockResolvedValue(closureOf());
    api.fetchOverview.mockResolvedValue({ ...overview, historical: false, revision: overview.latest_revision });
    api.fetchTargets.mockResolvedValue({ revision_id: overview.latest_revision.id, historical: false, order: "recommended", targets: [target], progress: overview.progress, outline: overview.outline });
    api.fetchAnnotations.mockResolvedValue({ annotations: [], counts: {} });
    api.fetchReviewOutcome.mockResolvedValue({ current: null });
    api.fetchChangeProposals.mockResolvedValue({ revision_id: overview.latest_revision.id, source_mutation_supported: false, proposals: [] });
    api.fetchSourceState.mockResolvedValue({ supported: false, changed: false, settled: true, fingerprint: "", path_count: 0, paths: [], reason: "" });
  }

  // The routes of App.tsx that mount the Reader, in the same shape: the old addresses mount it bare, /r/<review>/* inside the flow layer.
  function mount(address: string, extensions?: ReviewReaderExtensions) {
    window.history.replaceState(null, "", address);
    return render(
      <BrowserRouter>
        <Routes>
          <Route path="/r/:reviewRef/*" element={<ReviewFlowRoute><ReviewReader extensions={extensions} /></ReviewFlowRoute>} />
          <Route path="/rr/:revisionRef" element={<ReviewReader />} />
          <Route path="/reviews/:reviewId/revisions/:revisionNumber" element={<ReviewReader />} />
          <Route path="/reviews/:reviewId" element={<ReviewReader />} />
          <Route path="/review" element={<ReviewReader />} />
        </Routes>
      </BrowserRouter>,
    );
  }

  async function open(address: string, extensions?: ReviewReaderExtensions) {
    mount(address, extensions);
    return screen.findByTestId("review-reader", {}, { timeout: 8000 });
  }

  it.each([
    ["/reviews/rev-history", /^\/r\/rev-history(\/|$)/],
    ["/reviews/rev-history/revisions/0", /^\/r\/rev-history(\/|$)/],
    ["/rr/11111111111171118111111111111111", /^\/r\/rev-history(\/|$)/],
    ["/review", /^\/r\/rev-history(\/|$)/],
  ])("%s lands on the review's Overview, not a blank page", async (address, expected) => {
    live();
    api.fetchRevisionLocator.mockResolvedValue({ review: { id: "rev-history", ref: "rev-history" }, revision: { ...overview.revision, revision_number: 0 } });
    if (address === "/review") window.sessionStorage.setItem("lc.review.id", "rev-history");
    const reader = await open(address);
    await waitFor(() => expect(window.location.pathname).toMatch(expected));
    const root = reader.getAttribute("data-review-view-root");
    expect(["overview", "focus", "all-files"]).toContain(root);
    // The screen has its content, not just its frame.
    if (root === "overview") expect(await screen.findByTestId("overview-headline")).toHaveTextContent(/changed \d+ parts? in \d+ files?/);
    else expect(await screen.findByTestId(root === "focus" ? "focus-card" : "all-files-part")).toBeInTheDocument();
  });

  it("an old address never starts an automatic surface run", async () => {
    live();
    api.fetchReviewSurfaces.mockResolvedValue({ surfaces: [{ id: "svc", provider: "runtime", label: "svc", kind: "service", safe_to_auto_run: true, paths: ["src/app.py"] }] });
    await open("/reviews/rev-history");
    await new Promise((resolve) => setTimeout(resolve, 600));
    expect(api.runReviewSurface).not.toHaveBeenCalled();
  });

  // ---- ported from the retired Classic ReviewReader.test.tsx (P8-3): logic the flow still uses ----

  it("a review the server does not know says it was not found and offers All reviews", async () => {
    api.fetchStartupOverview.mockRejectedValue(Object.assign(new Error("not found"), { status: 404 }));
    api.fetchOverview.mockRejectedValue(Object.assign(new Error("not found"), { status: 404 }));
    mount("/r/rev-gone");
    expect(await screen.findByText("Review not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "All reviews" })).toHaveAttribute("href", "/reviews");
  });

  it("an account without access is told so, not that the review is gone", async () => {
    api.fetchStartupOverview.mockRejectedValue(Object.assign(new Error("forbidden"), { status: 403 }));
    api.fetchOverview.mockRejectedValue(Object.assign(new Error("forbidden"), { status: 403 }));
    mount("/r/rev-locked");
    expect(await screen.findByText("You no longer have access to this review")).toBeInTheDocument();
  });

  it("a browser the terminal never paired is sent to lc review, with a Retry", async () => {
    const unpaired = Object.assign(new Error(LOCAL_BROWSER_RECONNECT_MESSAGE), { status: 401 });
    api.fetchStartupOverview.mockRejectedValue(unpaired);
    api.fetchOverview.mockRejectedValue(unpaired);
    mount("/r/rev-unpaired");
    expect(await screen.findByText("This browser isn't connected to your local review")).toBeInTheDocument();
    // The command reopens this review; `lc review --open` would capture a different one.
    expect(screen.getByText("lc review open r/rev-unpaired")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
  });

  it("a failed load says the review couldn't load, and Retry loads it", async () => {
    live();
    api.fetchStartupOverview.mockRejectedValueOnce(new Error("boom"));
    api.fetchOverview.mockRejectedValueOnce(new Error("boom"));
    mount("/r/rev-history");
    expect(await screen.findByText("Review couldn't load", {}, { timeout: 12000 })).toBeInTheDocument();
    api.fetchStartupOverview.mockImplementation((id: string) => api.fetchOverview(id));
    live();
    const before = api.fetchStartupOverview.mock.calls.length + api.fetchOverview.mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(api.fetchStartupOverview.mock.calls.length + api.fetchOverview.mock.calls.length).toBeGreaterThan(before), { timeout: 8000 });
  }, 30000);

  async function openFocusPart() {
    live();
    const reader = await open("/r/rev-history/one/run");
    await waitFor(() => expect(reader).toHaveAttribute("data-review-view-root", "focus"), { timeout: 8000 });
    return reader;
  }

  it("a rejected mark rolls the part back in the same session and says it was not saved", async () => {
    api.postMark.mockRejectedValue(Object.assign(new Error("mark store unavailable"), { status: 500 }));
    await openFocusPart();
    const pips = () => screen.getByTestId("flow-top-bar").querySelector('[role="img"]')?.getAttribute("aria-label") ?? "";
    expect(pips()).toMatch(/^0 of \d+ decisions? judged$/);
    await userEvent.click((await screen.findAllByRole("button", { name: "Looks good" }))[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("The answer was not saved. This part has not advanced.");
    expect(api.postMark).toHaveBeenCalledTimes(1);
    expect(pips()).toMatch(/^0 of \d+ decisions? judged$/);
    expect(window.location.pathname).toBe("/r/rev-history/one/run");
  });

  it("an accepted mark advances the count of judged parts", async () => {
    api.postMark.mockResolvedValue({ unit_key: target.unit_key, requested: "reviewed", recorded: "reviewed", downgraded: false, note: "", target: { ...target, state: "reviewed" }, progress: { ...overview.progress, reviewed: 1, unreviewed: 0 }, groups: [], group_counts: {} });
    await openFocusPart();
    await userEvent.click((await screen.findAllByRole("button", { name: "Looks good" }))[0]);
    await waitFor(() => expect(api.postMark).toHaveBeenCalledWith("rev-history", target.unit_key, "reviewed", expect.any(String)));
    await waitFor(() => {
      const pips = screen.getByTestId("flow-top-bar").querySelector('[role="img"]')?.getAttribute("aria-label") ?? "";
      expect(pips).toMatch(/^1 of \d+ decisions? judged$/);
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a mark the hosted server only queued leaves the part as it was and says it is queued", async () => {
    api.postMark.mockResolvedValue({ queued: true, reference: "acme/shop#12", actions: [{ id: "a1", sequence: 1, kind: "mark", snapshot_id: "s", supersede_key: "k", payload: { state: "reviewed", unit_key: target.unit_key }, preconditions: {}, state: "pending", reason: "", result: {}, created_at: "", resolved_at: "" }] });
    await openFocusPart();
    await userEvent.click((await screen.findAllByRole("button", { name: "Looks good" }))[0]);
    expect(await screen.findByTestId("hosted-queued-notice")).toHaveTextContent(/Queued for hosted acme\/shop#12/);
    const pips = screen.getByTestId("flow-top-bar").querySelector('[role="img"]')?.getAttribute("aria-label") ?? "";
    expect(pips).toMatch(/^0 of \d+ decisions? judged$/);
  });

  it("a read-only host shows its reason, header actions and overlays, and no answer can be given", async () => {
    live();
    const reader = await open("/r/rev-history/one/run", {
      forceReadOnly: true,
      readOnlyReason: "This organization is read-only.",
      context: { label: "acme/shop", title: "Repository acme/shop" },
      authorship: { prefix: "by", label: "Dana" },
      renderHeaderActions: () => <button type="button">People</button>,
      renderOverlays: () => <div data-testid="host-overlay">Provider sheet</div>,
    });
    await waitFor(() => expect(reader).toHaveAttribute("data-review-view-root", "focus"), { timeout: 8000 });
    expect(await screen.findByTestId("host-read-only-reason")).toHaveTextContent("This organization is read-only.");
    expect(screen.getByTestId("host-chrome")).toHaveTextContent("acme/shop");
    expect(screen.getByTestId("host-chrome")).toHaveTextContent("by Dana");
    expect(screen.getByRole("button", { name: "People" })).toBeInTheDocument();
    expect(screen.getByTestId("host-overlay")).toBeInTheDocument();
    for (const button of screen.queryAllByRole("button", { name: "Looks good" })) expect(button).toBeDisabled();
    expect(api.postMark).not.toHaveBeenCalled();
  });

  it("a local Reader draws no host chrome", async () => {
    live();
    await open("/r/rev-history");
    expect(screen.queryByTestId("host-chrome")).toBeNull();
  });

  // ---- ported (P8-3 round 3): stale revisions, live mark sync, finishing, historical revisions ----

  it("a mark refused because the review advanced says so and turns every answer off", async () => {
    api.postMark.mockRejectedValue(Object.assign(new Error("the Review advanced to revision 2"), { status: 409 }));
    await openFocusPart();
    await userEvent.click((await screen.findAllByRole("button", { name: "Looks good" }))[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent(/advanced to a newer revision|not saved/i);
    await waitFor(() => {
      for (const button of screen.queryAllByRole("button", { name: "Looks good" })) expect(button).toBeDisabled();
    });
  });

  it("the queued-for-hosted notice goes when another review opens", async () => {
    api.postMark.mockResolvedValue({ queued: true, reference: "acme/shop#12", actions: [{ id: "a1", sequence: 1, kind: "mark", snapshot_id: "s", supersede_key: "k", payload: { state: "reviewed", unit_key: target.unit_key }, preconditions: {}, state: "pending", reason: "", result: {}, created_at: "", resolved_at: "" }] });
    api.fetchOverview.mockImplementation(async (id: string) => ({ ...overview, historical: false, revision: overview.latest_revision, session: { ...overview.session, id } }));
    api.fetchStartupOverview.mockImplementation((id: string) => api.fetchOverview(id));
    await openFocusPart();
    await userEvent.click((await screen.findAllByRole("button", { name: "Looks good" }))[0]);
    expect(await screen.findByTestId("hosted-queued-notice")).toBeInTheDocument();
    await act(async () => {
      window.history.pushState(null, "", "/r/rev-b");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await waitFor(() => expect(screen.queryByTestId("hosted-queued-notice")).toBeNull(), { timeout: 8000 });
  });

  it("the queued-for-hosted notice goes when the hosted queue reports nothing pending", async () => {
    live();
    const pending = { id: "a1", sequence: 1, kind: "mark", snapshot_id: "s", supersede_key: "k", payload: { state: "reviewed", unit_key: target.unit_key }, preconditions: {}, state: "pending", reason: "", result: {}, created_at: "", resolved_at: "" };
    api.postMark.mockResolvedValue({ queued: true, reference: "acme/shop#12", actions: [pending] });
    api.fetchOverview.mockImplementation(async (id: string) => ({ ...overview, historical: false, revision: overview.latest_revision, session: { ...overview.session, id, hosted: { reference: "acme/shop#12" } } }));
    api.fetchStartupOverview.mockImplementation((id: string) => api.fetchOverview(id));
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let reads = 0;
    api.fetchHostedAttachment.mockImplementation(async () => (++reads === 1
      ? { reference: "acme/shop#12", actions: [pending] }
      : gate.then(() => ({ reference: "acme/shop#12", actions: [{ ...pending, state: "applied" }] }))));
    const reader = await open("/r/rev-history/one/run");
    await waitFor(() => expect(reader).toHaveAttribute("data-review-view-root", "focus"), { timeout: 8000 });
    await userEvent.click((await screen.findAllByRole("button", { name: "Looks good" }))[0]);
    expect(await screen.findByTestId("hosted-queued-notice")).toBeInTheDocument();
    // While the queue still holds the action, the notice stays.
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(screen.getByTestId("hosted-queued-notice")).toBeInTheDocument();
    release();
    await waitFor(() => expect(screen.queryByTestId("hosted-queued-notice")).toBeNull(), { timeout: 8000 });
  }, 30_000);

  it("/walkthrough mounts the walkthrough (and /tour is replaced with it); its parts open in Focus, and with no Brief capability it says there is none", async () => {
    live();
    const tourStep = { id: "tour:run", semantic_key: "run", title: "The runner", why: "Review the changed implementation.", why_origin: "deterministic", target_ids: [target.target_id], stale_target_ids: [], change_type: "changed", signals: [], claim_ids: [], decision_refs: [], evidence_refs: [], stale: false, synthesis_needed: false, position: 0, implementation: { target_id: target.target_id, path: target.path, symbol: target.symbol, start_line: 1, end_line: 2 } };
    const plan = { projection_version: 1, review_id: overview.session.id, revision_id: overview.latest_revision.id, snapshot_number: 0, historical: false, items: [], claims: [], decisions: [], queue: [], questions: [], actions: [], plan: { schema_version: 1, input_digest: "d", intents: [], limitations: [] }, coverage: { target_count: 1, author_explained: 0, fallback_grouped: 0, other: 1, routine: 0, unresolved_refs: 0, unexplained_targets: 1, file_count: 1 }, counts: { review_first: 0, understand: 1, routine: 0, done: 0, decisions: 0 }, tour: { schema_version: 1, step_count: 1, synthesis: { status: "none", mode: "deterministic_only", candidates: 0 }, steps: [tourStep] } } as any;
    api.fetchProjection.mockReset().mockResolvedValue(plan);

    const reader = await open("/r/rev-history/walkthrough");
    await waitFor(() => expect(reader).toHaveAttribute("data-review-view-root", "walkthrough"), { timeout: 8000 });
    expect(await screen.findByText("No walkthrough is available for this revision.", {}, { timeout: 8000 })).toBeInTheDocument();
    cleanup();

    flags.brief = true;
    const gated = await open("/r/rev-history/tour");
    await waitFor(() => expect(gated).toHaveAttribute("data-review-view-root", "walkthrough"), { timeout: 8000 });
    expect(window.location.pathname).toBe("/r/rev-history/walkthrough");
    expect(await screen.findByText(/Walkthrough · step 1 of 1/, {}, { timeout: 8000 })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open this part" }));
    await waitFor(() => expect(window.location.pathname).toBe("/r/rev-history/one/run"), { timeout: 8000 });
  }, 30_000);
  // The plan: the Overview asks for it, and a change to the parts (here another tab's mark, through the live feed) starts a
  // background refresh behind that first ask. Both are heavy on the server (P9-6).
  function planWithARefreshBehindIt() {
    flags.brief = true;
    window.localStorage.clear();
    window.sessionStorage.clear();
    live();
    const state = (notes: string) => ({ revision_id: overview.latest_revision.id, packet_cursor: "p", annotations_cursor: notes, journal_cursor: "j", development_cursor: "", marks_cursor: "m" });
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let feeds = 0;
    api.nextLiveState.mockImplementation(() => (++feeds === 1 ? Promise.resolve(state("n1")) : feeds === 2 ? gate.then(() => state("n2")) : new Promise(() => {})));
    api.fetchTargets.mockImplementation(async () => ({ revision_id: overview.latest_revision.id, historical: false, order: "recommended", targets: [target], progress: overview.progress, outline: overview.outline }));
    api.fetchAnnotations.mockImplementation(async () => ({ revision_id: overview.latest_revision.id, annotations: [], counts: {} }));
    let answerFirst!: (value: unknown) => void;
    let failFirst!: (reason: unknown) => void;
    api.fetchProjection
      .mockReturnValueOnce(new Promise((resolve, reject) => { answerFirst = resolve; failFirst = reject; }))
      .mockReturnValue(new Promise(() => {}));
    const plan = { projection_version: 1, review_id: overview.session.id, revision_id: overview.latest_revision.id, snapshot_number: 0, historical: false, items: [], claims: [], decisions: [], queue: [], questions: [], actions: [], plan: { schema_version: 1, input_digest: "d", intents: [], limitations: [] }, coverage: { target_count: 1, author_explained: 0, fallback_grouped: 0, other: 1, routine: 0, unresolved_refs: 0, unexplained_targets: 1, file_count: 1 }, counts: { review_first: 0, understand: 1, routine: 0, done: 0, decisions: 0 } } as any;
    return { release, answerFirst: () => answerFirst(plan), failFirst: () => failFirst(Object.assign(new Error("projection unavailable"), { status: 422 })) };
  }

  it("the Overview is ready from the first /projection answer, though a refresh began behind it (one answer, not two)", async () => {
    const plan = planWithARefreshBehindIt();
    const reader = await open("/r/rev-history");
    await waitFor(() => expect(api.fetchProjection.mock.calls.length).toBeGreaterThanOrEqual(1), { timeout: 8000 });
    expect(reader).toHaveAttribute("data-review-view-root", "overview");
    plan.release(); // the notes change: the Overview refreshes its plan while the first ask is still being computed
    await waitFor(() => expect(api.fetchProjection.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 8000 });
    const asked = api.fetchProjection.mock.calls.length;
    expect(document.querySelector("[data-overview-state]")).toHaveAttribute("data-overview-state", "preparing");
    await act(async () => { plan.answerFirst(); });
    // Ready on the first answer, while the second ask is still pending; nothing asks again.
    await waitFor(() => expect(document.querySelector("[data-overview-state]")).toHaveAttribute("data-overview-state", "ready"), { timeout: 8000 });
    expect(api.fetchProjection).toHaveBeenCalledTimes(asked);
  }, 30_000);

  it("a plan that cannot load ends the wait on the Overview even though a refresh began behind it", async () => {
    const plan = planWithARefreshBehindIt();
    await open("/r/rev-history");
    await waitFor(() => expect(api.fetchProjection.mock.calls.length).toBeGreaterThanOrEqual(1), { timeout: 8000 });
    plan.release();
    await waitFor(() => expect(api.fetchProjection.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 8000 });
    await act(async () => { plan.failFirst(); });
    await waitFor(() => expect(document.querySelector("[data-overview-state]")).toHaveAttribute("data-overview-state", "ready"), { timeout: 8000 });
  }, 30_000);

  it("a mark another tab made arrives through the live feed and the count of judged parts follows", async () => {
    live();
    const state = (marks: string) => ({ revision_id: overview.latest_revision.id, packet_cursor: "p", annotations_cursor: "a", journal_cursor: "j", development_cursor: "", marks_cursor: marks });
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let calls = 0;
    api.nextLiveState.mockImplementation(() => (++calls === 1 ? Promise.resolve(state("m1")) : calls === 2 ? gate.then(() => state("m2")) : new Promise(() => {})));
    let otherTab = false;
    api.fetchTargets.mockImplementation(async () => ({ revision_id: overview.latest_revision.id, historical: false, order: "recommended", targets: [{ ...target, state: otherTab ? "reviewed" : "unreviewed" }], progress: { ...overview.progress, reviewed: otherTab ? 1 : 0, unreviewed: otherTab ? 0 : 1 }, outline: overview.outline }));
    const reader = await open("/r/rev-history/one/run");
    await waitFor(() => expect(reader).toHaveAttribute("data-review-view-root", "focus"), { timeout: 8000 });
    const pips = () => screen.getByTestId("flow-top-bar").querySelector('[role="img"]')?.getAttribute("aria-label") ?? "";
    // Before the other tab marks anything the count is 0, and it stays 0 until the feed says so.
    expect(pips()).toMatch(/^0 of \d+ decisions? judged$/);
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(pips()).toMatch(/^0 of \d+ decisions? judged$/);
    otherTab = true;
    release();
    await waitFor(() => expect(pips()).toMatch(/^1 of \d+ decisions? judged$/), { timeout: 8000 });
  });

  it("an enrichment that lands between the Reader's read and the live feed's first event still names the agent", async () => {
    live();
    let enriched = false;
    api.fetchOverview.mockImplementation(async () => ({
      ...overview, historical: false, packet_cursor: enriched ? "p1" : "p0",
      revision: { ...overview.latest_revision, provenance_host: enriched ? "codex" : "" },
    }));
    const state = { revision_id: overview.latest_revision.id, packet_cursor: "p1", annotations_cursor: "a", journal_cursor: "j", development_cursor: "", marks_cursor: "m" };
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let calls = 0;
    // The feed's first event is the only one: it already carries the enriched packet.
    api.nextLiveState.mockImplementation(() => (++calls === 1 ? gate.then(() => state) : new Promise(() => {})));
    const reader = await open("/r/rev-history/one/run");
    await waitFor(() => expect(reader).toHaveAttribute("data-review-view-root", "focus"), { timeout: 8000 });
    const reads = api.fetchOverview.mock.calls.length;
    enriched = true;
    release();
    await waitFor(() => expect(api.fetchOverview.mock.calls.length).toBeGreaterThan(reads), { timeout: 8000 });
  }, 20_000);

  it("the automatic comparison does not narrow the Overview to the parts that moved", async () => {
    live();
    const kept = { ...target, target_id: "target:kept", unit_key: "hun:kept", state: "reviewed", changed_since_mark: false, reviewed_revision_id: overview.revision.id };
    api.fetchTargets.mockResolvedValue({ revision_id: overview.latest_revision.id, historical: false, order: "recommended", targets: [target, kept], progress: overview.progress });
    // A comment on the earlier revision is what makes the server default to comparing against it.
    const from = overview.revision.revision_number;
    api.fetchOverview.mockResolvedValue({ ...overview, historical: false, revision: overview.latest_revision, default_base_revision_number: from, default_target_revision_number: overview.latest_revision_number });
    await open("/r/rev-history");
    await waitFor(() => expect(api.fetchRevisionComparison).toHaveBeenCalled(), { timeout: 8000 });
    await screen.findByTestId("overview-queue-line", {}, { timeout: 8000 });
    // The comparison lands after the first paint: the line must still count the whole revision then.
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 300)); });
    expect(screen.getByTestId("overview-queue-line").textContent).toContain("1 unchanged since your review");
  });

  it("moving between flow screens keeps the automatic comparison automatic", async () => {
    live();
    api.fetchOverview.mockResolvedValue({ ...overview, historical: false, revision: overview.latest_revision, default_base_revision_number: overview.revision.revision_number, default_target_revision_number: overview.latest_revision_number });
    await open("/r/rev-history");
    await waitFor(() => expect(window.location.search).toContain("compare=auto"), { timeout: 8000 });
    await userEvent.click(await screen.findByRole("button", { name: "All files" }, { timeout: 8000 }));
    await waitFor(() => expect(window.location.pathname).toBe("/r/rev-history/all"), { timeout: 8000 });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 300)); });
    const params = new URLSearchParams(window.location.search);
    expect(params.has("base") && params.get("compare") !== "auto").toBe(false);
  }, 20_000);

  it("a revision captured while Wrap up is open stops a send that was ready", async () => {
    live();
    api.fetchTargets.mockResolvedValue({ revision_id: overview.latest_revision.id, historical: false, order: "recommended", targets: [{ ...target, state: "reviewed" }], progress: { ...overview.progress, reviewed: 1, unreviewed: 0 }, outline: overview.outline });
    const state = (revision: string) => ({ revision_id: revision, packet_cursor: "p", annotations_cursor: "a", journal_cursor: "j", development_cursor: "", marks_cursor: "m" });
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let calls = 0;
    api.nextLiveState.mockImplementation(() => (++calls === 1 ? Promise.resolve(state(overview.latest_revision.id)) : calls === 2 ? gate.then(() => state("33333333333373338333333333333333")) : new Promise(() => {})));
    api.fetchRevisions.mockResolvedValue({ revisions: [overview.latest_revision, { ...overview.latest_revision, id: "33333333333373338333333333333333", revision_number: 2 }], latest_revision_number: 2, last_reviewed_revision_number: 0, last_commented_revision_number: 0, default_base_revision_number: 1, default_target_revision_number: 2 });
    await open("/r/rev-history/wrap", { recordOutcome: vi.fn() });
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }, { timeout: 8000 }));
    await waitFor(() => expect(screen.getByTestId("wrap-send")).toBeEnabled(), { timeout: 8000 });
    release();
    await waitFor(() => expect(screen.getByTestId("wrap-send")).toBeDisabled(), { timeout: 8000 });
  });

  describe("finishing from Wrap up when the host records the verdict", () => {
    const reviewed = () => api.fetchTargets.mockResolvedValue({ revision_id: overview.latest_revision.id, historical: false, order: "recommended", targets: [{ ...target, state: "reviewed" }], progress: { ...overview.progress, reviewed: 1, unreviewed: 0 }, outline: overview.outline });
    async function approve(extensions: ReviewReaderExtensions) {
      live();
      reviewed();
      await open("/r/rev-history/wrap", extensions);
      await userEvent.click(await screen.findByRole("button", { name: "Approve" }, { timeout: 8000 }));
      await waitFor(() => expect(screen.getByTestId("wrap-send")).toBeEnabled(), { timeout: 8000 });
      await userEvent.click(screen.getByTestId("wrap-send"));
    }

    it("records the verdict for the revision on screen, then finishes the review", async () => {
      const recordOutcome = vi.fn(async (outcome: string, summary: string, revision: string) => ({ outcome, summary, revision_id: revision, stale: false, reviewer_id: "local" }) as any);
      api.finishReview.mockResolvedValue({ status: "finished" });
      await approve({ recordOutcome });
      await waitFor(() => expect(recordOutcome).toHaveBeenCalledWith("lgtm", expect.any(String), overview.latest_revision.id));
      await waitFor(() => expect(api.finishReview).toHaveBeenCalledWith("rev-history", "finished", overview.latest_revision.id));
    });

    it("a verdict that could not be recorded finishes nothing", async () => {
      const recordOutcome = vi.fn(async () => { throw new Error("disk full"); });
      await approve({ recordOutcome });
      await waitFor(() => expect(recordOutcome).toHaveBeenCalledTimes(1));
      await new Promise((resolve) => setTimeout(resolve, 300));
      expect(api.finishReview).not.toHaveBeenCalled();
    });

    it("a verdict already on record is not recorded twice, and the review still finishes", async () => {
      const recordOutcome = vi.fn();
      api.finishReview.mockResolvedValue({ status: "finished" });
      await approve({ recordOutcome, outcome: { outcome: "lgtm", summary: "", revision_id: overview.latest_revision.id, stale: false, reviewer_id: "local" } as any });
      await waitFor(() => expect(api.finishReview).toHaveBeenCalledTimes(1));
      expect(recordOutcome).not.toHaveBeenCalled();
    });
  });

  it("a historical revision is read from its own endpoints and takes no answer", async () => {
    window.sessionStorage.setItem("lc.review.id", "rev-history");
    live();
    api.fetchRevisionLocator.mockResolvedValue({ review: { id: "rev-history", ref: "rev-history" }, revision: { ...overview.revision, revision_number: 0 } });
    const reader = await open("/rr/11111111111171118111111111111111");
    await waitFor(() => expect(api.fetchHistoricalOverview).toHaveBeenCalled(), { timeout: 8000 });
    await waitFor(() => expect(reader).toHaveAttribute("data-overview-state", "ready"), { timeout: 8000 });
    expect(api.fetchHistoricalTargets).toHaveBeenCalled();
    // Open the part: its answers are off on a past revision.
    await act(async () => {
      window.history.pushState(null, "", "/r/rev-history/one/run?base=0&target=0");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    const answers = await screen.findAllByRole("button", { name: "Looks good" }, { timeout: 8000 });
    for (const button of answers) expect(button).toBeDisabled();
    await userEvent.click(answers[0]);
    expect(api.postMark).not.toHaveBeenCalled();
  });

  it("hosted merge requirements show on Wrap up as information and never stop my own Approve", async () => {
    live();
    api.fetchTargets.mockResolvedValue({ revision_id: overview.latest_revision.id, historical: false, order: "recommended", targets: [{ ...target, state: "reviewed" }], progress: { ...overview.progress, reviewed: 1, unreviewed: 0 }, outline: overview.outline });
    // minimum_reviewers: 1, required_reviewers: [me]: the policy lists my own review as not completed, and the server accepts it.
    await open("/r/rev-history/wrap", {
      recordOutcome: vi.fn(),
      readinessBlockers: [{ id: "reviewers", label: "Reviewer local has not completed", detail: "", tone: "amber" }, { id: "min", label: "1 more completed reviewer required", detail: "", tone: "amber" }] as any,
      readinessSections: <p>Completed reviewers 0/1</p>,
    });
    const section = await screen.findByTestId("wrap-host-sections", {}, { timeout: 8000 });
    expect(section).toHaveTextContent("Completed reviewers 0/1");
    expect(screen.getByTestId("wrap-host-requirements")).toHaveTextContent("Reviewer local has not completed");
    expect(screen.getByTestId("wrap-host-requirements")).toHaveTextContent("1 more completed reviewer required");
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.getByTestId("wrap-send")).toBeEnabled(), { timeout: 8000 });
    expect(screen.getByTestId("wrap-footer")).not.toHaveTextContent("has not completed");
  });

  it("with no agent session the verdict copies the notes for the author and records the handoff as copied", async () => {
    live();
    const note = { id: "ann-1", review_id: "rev-history", revision_id: overview.latest_revision.id, parent_id: "", kind: "request_change", state: "open", body: "Guard the zero case", created_by: "local", created_by_actor: "human", source: "human", source_id: "local", created_at: "", updated_at: "", anchor_method: "identical_blob", anchor_method_label: "", anchor_exact: true, anchor_detail: "", anchored: true, path: "src/app.py", side: "new", start_line: 1, end_line: 1, unit_key: target.unit_key, symbol: "run", origin_symbol: "" };
    api.fetchAnnotations.mockResolvedValue({ revision_id: overview.latest_revision.id, annotations: [note], counts: {}, feedback: { published: 0, addressed: 0, unpublished: 1 } });
    api.fetchTargets.mockResolvedValue({ revision_id: overview.latest_revision.id, historical: false, order: "recommended", targets: [{ ...target, state: "needs_changes" }], progress: { ...overview.progress, needs_changes: 1, unreviewed: 0 }, outline: overview.outline });
    const exported = { markdown: "Please guard the zero case", open: 1, orphaned: 0, resolved: 0, revision_id: overview.latest_revision.id, feedback_hash: "hash-1", operation_id: "op-1", annotation_versions: { "ann-1": 1 }, delivery: { supported: false, reason: "no agent session", route: "export", label: "" }, status: { unpublished: 1, published: 0, addressed: 0 } };
    api.exportFeedback.mockResolvedValue(exported);
    api.deliverFeedback.mockResolvedValue({ state: "consumed", message: "", target_ref: "", remote_ref: "", annotation_count: 1, operation_id: "op-1" });
    api.postReviewOutcome.mockResolvedValue({ current: { outcome: "changes_requested", summary: "", revision_id: overview.latest_revision.id, stale: false, reviewer_id: "local" }, history: [] });
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await open("/r/rev-history/wrap");
    await userEvent.click(await screen.findByRole("button", { name: "Request changes" }, { timeout: 8000 }));
    await userEvent.click(await screen.findByRole("button", { name: "Must fix" }, { timeout: 8000 }));
    await waitFor(() => expect(screen.getByTestId("wrap-send")).toBeEnabled(), { timeout: 8000 });
    expect(screen.getByTestId("wrap-send")).toHaveTextContent(/Copy/);
    await userEvent.click(screen.getByTestId("wrap-send"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("Please guard the zero case"), { timeout: 8000 });
    await waitFor(() => expect(api.deliverFeedback).toHaveBeenCalled(), { timeout: 8000 });
    expect(JSON.stringify(api.deliverFeedback.mock.calls[0])).toContain('"copied":true');
  }, 30000);

  it("moving from one review to another keeps the address it was given", async () => {
    live();
    // The second review's overview is slow, so for a while the Reader still holds the first review's.
    let releaseB!: () => void;
    const slowB = new Promise<void>((resolve) => { releaseB = resolve; });
    api.fetchOverview.mockImplementation(async (id: string) => {
      if (id === "rev-b") await slowB;
      return { ...overview, historical: false, revision: overview.latest_revision, session: { ...overview.session, id } };
    });
    api.fetchStartupOverview.mockImplementation((id: string) => api.fetchOverview(id));
    const reader = await open("/r/rev-history");
    await waitFor(() => expect(reader).toHaveAttribute("data-overview-state", "ready"), { timeout: 8000 });
    const go = async (address: string) => {
      await act(async () => {
        window.history.pushState(null, "", address);
        window.dispatchEvent(new PopStateEvent("popstate"));
      });
      await new Promise((resolve) => setTimeout(resolve, 800));
    };
    await go("/r/rev-b");
    // While rev-b is still loading the stale overview must not pull the address back to rev-history.
    expect(window.location.pathname).toMatch(/^\/r\/rev-b(\/|$)/);
    releaseB();
    await new Promise((resolve) => setTimeout(resolve, 600));
    // The reading view the reviewer chose may take /r/rev-b to its first part; it never goes back to the review left.
    expect(window.location.pathname).toMatch(/^\/r\/rev-b(\/|$)/);
    await go("/r/rev-b/all");
    expect(window.location.pathname).toBe("/r/rev-b/all");
    expect(api.fetchStartupOverview.mock.calls.filter(([id]) => id === "rev-b").length).toBeLessThanOrEqual(1);
  });

  it.each([["id equal to ref", "rev-history"], ["id different from ref", "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"]])("opening a review loads its overview once (%s)", async (_name, sessionId) => {
    live();
    api.fetchOverview.mockImplementation(async () => ({ ...overview, historical: false, revision: overview.latest_revision, session: { ...overview.session, id: sessionId, ref: "rev-history" } }));
    api.fetchStartupOverview.mockImplementation((id: string) => api.fetchOverview(id));
    const reader = await open("/r/rev-history");
    await waitFor(() => expect(reader).toHaveAttribute("data-overview-state", "ready"), { timeout: 8000 });
    await new Promise((resolve) => setTimeout(resolve, 600));
    expect(api.fetchStartupOverview.mock.calls.length).toBe(1);
    expect(window.location.pathname).toMatch(/^\/r\/rev-history(\/|$)/);
  });

  // ---- the flow's one state line (reviewStateLineModel.ts): every read-only or degraded state says why and offers its action ----

  const latest = () => ({ ...overview, historical: false, revision: overview.latest_revision });
  const stateLineRow = () => screen.findByTestId("review-state-line", {}, { timeout: 8000 });

  it("a running draft says it is read-only until it is a Snapshot, and Make it a Snapshot publishes it", async () => {
    live();
    api.fetchOverview.mockResolvedValue({ ...latest(), revision: { ...overview.latest_revision, capture_kind: "draft", is_published_snapshot: false } });
    api.publishReviewSnapshot.mockImplementation(async () => {
      api.fetchOverview.mockResolvedValue(latest());
      return { revision_id: overview.latest_revision.id, revision_number: 1 };
    });
    await open("/r/rev-history");
    const line = await stateLineRow();
    expect(line).toHaveTextContent("Running draft · read-only until it is a Snapshot");
    await userEvent.click(within(line).getByRole("button", { name: "Make it a Snapshot" }));
    await waitFor(() => expect(api.publishReviewSnapshot).toHaveBeenCalledWith("rev-history", 1));
    await waitFor(() => expect(screen.queryByTestId("review-state-line")).toBeNull(), { timeout: 8000 });
  });

  it("a finished review says so, and Reopen reopens it", async () => {
    live();
    api.fetchOverview.mockResolvedValue({ ...latest(), session: { ...overview.session, status: "finished" } });
    api.finishReview.mockImplementation(async () => {
      api.fetchOverview.mockResolvedValue(latest());
      return {};
    });
    await open("/r/rev-history");
    const line = await stateLineRow();
    expect(line).toHaveTextContent("Finished · read-only until reopened");
    await userEvent.click(within(line).getByRole("button", { name: "Reopen" }));
    await waitFor(() => expect(api.finishReview).toHaveBeenCalledWith("rev-history", "open"));
    await waitFor(() => expect(screen.queryByTestId("review-state-line")).toBeNull(), { timeout: 8000 });
  });

  it("a historical revision names itself and the latest, with Back to latest and Compare with latest", async () => {
    window.sessionStorage.setItem("lc.review.id", "rev-history");
    live();
    api.fetchRevisionLocator.mockResolvedValue({ review: { id: "rev-history", ref: "rev-history" }, revision: { ...overview.revision, revision_number: 0 } });
    await open("/rr/11111111111171118111111111111111");
    const line = await stateLineRow();
    expect(line).toHaveTextContent("Revision 1 · read-only · latest is Revision 2");
    expect(within(line).getByRole("link", { name: "Back to latest" })).toHaveAttribute("href", "/r/rev-history");
    expect(within(line).getByRole("link", { name: "Compare with latest" })).toHaveAttribute("href", "/r/rev-history?base=0&target=1");
  });

  it("review context that failed to load says so, and Retry reads it again", async () => {
    live();
    api.fetchReviewJournal.mockRejectedValue(new Error("journal store down"));
    await open("/r/rev-history");
    const line = await stateLineRow();
    expect(line).toHaveTextContent("Some review context couldn't load");
    expect(line).toHaveTextContent("Couldn't load the agent's notes.");
    api.fetchReviewJournal.mockResolvedValue({ available: false, reason: "", review_id: overview.session.id, revision_id: overview.latest_revision.id, revision_number: 1, session_id: "", host: "", entries: [] });
    const before = api.fetchReviewJournal.mock.calls.length;
    await userEvent.click(within(line).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(api.fetchReviewJournal.mock.calls.length).toBeGreaterThan(before));
    await waitFor(() => expect(screen.queryByTestId("review-state-line")).toBeNull(), { timeout: 8000 });
  });

  it("a working tree that moved past this Snapshot says how many files, and Snapshot now captures and publishes", async () => {
    live();
    api.fetchSourceState.mockResolvedValue({ supported: true, changed: true, settled: true, fingerprint: "fp-2", path_count: 3, paths: ["a.py", "b.py", "c.py"], reason: "" });
    api.postRefresh.mockImplementation(async () => {
      api.fetchSourceState.mockResolvedValue({ supported: true, changed: false, settled: true, fingerprint: "fp-2", path_count: 0, paths: [], reason: "" });
      return latest();
    });
    await open("/r/rev-history");
    const line = await stateLineRow();
    expect(line).toHaveTextContent("3 files changed since this Snapshot");
    await userEvent.click(within(line).getByRole("button", { name: "Snapshot now" }));
    // Publish is postRefresh's default: the capture is a Snapshot, not a running draft.
    await waitFor(() => expect(api.postRefresh).toHaveBeenCalledWith("rev-history"));
    await waitFor(() => expect(screen.queryByTestId("review-state-line")).toBeNull(), { timeout: 8000 });
  });

  it("a Snapshot now that fails says what failed and offers Try again", async () => {
    live();
    api.fetchSourceState.mockResolvedValue({ supported: true, changed: true, settled: true, fingerprint: "fp-2", path_count: 1, paths: ["a.py"], reason: "" });
    api.postRefresh.mockRejectedValue(new Error("the source is mid-rebase"));
    await open("/r/rev-history");
    await userEvent.click(within(await stateLineRow()).getByRole("button", { name: "Snapshot now" }));
    const failed = await screen.findByRole("alert", {}, { timeout: 8000 });
    expect(failed).toHaveTextContent("Couldn't take a Snapshot · the source is mid-rebase");
    expect(within(failed).getByRole("button", { name: "Try again" })).toBeEnabled();
  });
});

describe("the state line's one row", () => {
  const calm: StateLineFacts = {
    failure: null, accessLost: "", pendingApprovals: 0, revisionAdvanced: false, newerRevisionNumber: null, runningDraft: false,
    historical: false, revisionNumber: 0, latestRevisionNumber: 0, status: "open", contextFailures: [], sourceMoved: null,
  };

  it("shows nothing on a writable, whole Review", () => {
    expect(stateLine(calm)).toBeNull();
  });

  it("orders a failed operation first, then what changes the revision on screen, then degradation, then the routine source delta", () => {
    const everything: StateLineFacts = {
      ...calm,
      failure: { label: "take a Snapshot", message: "boom" },
      accessLost: "forbidden",
      pendingApprovals: 2,
      revisionAdvanced: true,
      newerRevisionNumber: 2,
      historical: true,
      revisionNumber: 0,
      latestRevisionNumber: 2,
      status: "finished",
      contextFailures: ["Couldn't load the agent's notes."],
      sourceMoved: { files: 4 },
    };
    expect(stateLineSignals(everything).map((signal) => signal.id)).toEqual([
      "failed", "access-lost", "agent-approvals", "revision-advanced", "historical", "finished", "context-failures", "source-moved",
    ]);
    expect(stateLine({ ...calm, runningDraft: true, sourceMoved: { files: 1 }, contextFailures: ["x"] })?.id).toBe("running-draft");
    expect(stateLine({ ...calm, contextFailures: ["x"], sourceMoved: { files: 1 } })?.id).toBe("context-failures");
  });

  it("words each state and its counts", () => {
    expect(stateLine({ ...calm, pendingApprovals: 1 })?.text).toBe("1 agent request waits for you");
    expect(stateLine({ ...calm, pendingApprovals: 3 })?.text).toBe("3 agent requests wait for you");
    expect(stateLine({ ...calm, revisionAdvanced: true, newerRevisionNumber: 1 })?.text).toBe("Revision 2 replaced this one elsewhere · read-only");
    expect(stateLine({ ...calm, revisionAdvanced: true })?.text).toBe("A newer Snapshot replaced this one elsewhere · read-only");
    expect(stateLine({ ...calm, accessLost: "gone" })?.text).toBe("This review was deleted or discarded elsewhere · read-only");
    expect(stateLine({ ...calm, sourceMoved: { files: 1 } })?.text).toBe("1 file changed since this Snapshot");
    expect(stateLine({ ...calm, sourceMoved: { files: 0 } })?.text).toBe("The source changed since this Snapshot");
    expect(stateLine({ ...calm, status: "archived" })?.text).toBe("Archived · read-only until restored");
  });
});
