import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  DiscoveredProductCapabilitiesProvider,
  LOCAL_DASHBOARD_PRODUCT_CAPABILITIES,
  LOCAL_PRODUCT_CAPABILITIES,
  ProductCapabilitiesProvider,
  REVIEW_ONLY_PRODUCT_CAPABILITIES,
  type LemonCrowCapabilities,
} from "../lib/RuntimeCapabilities";
import ContextDrawer from "./ContextDrawer";
import { development, event, projection } from "./developmentTestFixtures";
import type { Annotation, FileDetail, ReviewEvidence, ReviewTarget } from "./types";

const reviewApi = vi.hoisted(() => ({
  fetchTargetHistory: vi.fn(),
  fetchReviewEvidenceContent: vi.fn(),
  fetchReviewDevelopmentEvents: vi.fn(),
}));
vi.mock("./reviewApi", () => ({
  fetchReviewEvidenceContent: reviewApi.fetchReviewEvidenceContent,
  fetchTargetHistory: reviewApi.fetchTargetHistory,
  fetchReviewDevelopmentEvents: reviewApi.fetchReviewDevelopmentEvents,
}));
const codeMap = vi.hoisted(() => ({ codeMapSearch: vi.fn(), codeMapReferences: vi.fn() }));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: { ...actual.api, ...codeMap } };
});
// The run-plan panel has its own tests; here its fetch stays pending so it never
// updates state outside act().
vi.mock("./runPlanApi", () => ({
  fetchRunPlan: vi.fn(() => new Promise(() => {})),
  postRunSpecific: vi.fn(),
  startRunSpecific: vi.fn(),
}));
vi.mock("./checkRecipesApi", () => ({
  fetchCheckRecipes: vi.fn(() => new Promise(() => {})),
  postCheckRecipesRun: vi.fn(),
  postCheckRequest: vi.fn(),
}));
// The interactive preview panel has its own tests; resolved where the mount is asserted.
const previewApi = vi.hoisted(() => ({
  fetchInteractivePreview: vi.fn(() => new Promise(() => {})),
  startInteractivePreview: vi.fn(),
  enterInteractivePreview: vi.fn(),
  stopInteractivePreview: vi.fn(),
}));
vi.mock("./interactivePreviewApi", () => previewApi);
vi.mock("./artifactAnalysesApi", () => ({
  fetchArtifactAnalyses: vi.fn(() => new Promise(() => {})),
  postArtifactAnalysisRun: vi.fn(),
  fetchAnalysisArtifact: vi.fn(),
}));
vi.mock("./runtimeObservationApi", () => ({
  fetchRuntimeObservation: vi.fn(() => new Promise(() => {})),
  postRuntimeObservationRun: vi.fn(),
  fetchRuntimeArtifact: vi.fn(),
}));
// Static findings have their own panel tests; pending by default, resolved where the mount is asserted.
const staticApi = vi.hoisted(() => ({
  fetchStaticFindings: vi.fn(() => new Promise(() => {})),
  postStaticDisposition: vi.fn(),
}));
vi.mock("./staticFindingsApi", () => staticApi);

const TARGET: ReviewTarget = {
  target_id: "target:checkout",
  unit_key: "sym:checkout",
  kind: "symbol",
  path: "src/checkout.tsx",
  label: "src/checkout.tsx::Checkout",
  symbol: "Checkout",
  start_line: 10,
  end_line: 80,
  hunk_ordinals: [0],
  spans: [{ side: "new", start_line: 20, end_line: 24, hunk_ordinal: 0 }],
  state: "unreviewed",
  changed_since_mark: false,
  reviewed_revision_id: "",
  attention_rank: 2,
  attention_level: "high",
  reasons: ["7 known callers", "1 untouched impacted site"],
  additions: 5,
  deletions: 1,
  fingerprint_method: "symbol_body_sha256",
  verification: { pass: 1, fail: 1, unknown: 1 },
  annotation_counts: { open: 1, orphaned: 0, addressed_needs_rereview: 0 },
};

const DETAIL: FileDetail = {
  path: "src/checkout.tsx",
  status: "modified",
  language: "tsx",
  additions: 42,
  deletions: 8,
  patch: "",
  renderable: true,
  refusal: "",
  detail: "",
  degraded: [],
  impact: [
    {
      kind: "untouched_caller",
      path: "src/cart.ts:L19",
      old: "checkout",
      new: null,
      snippet: "checkout(cart)",
      in_patch: false,
      inspected_by_agent: false,
      source_path: "src/checkout.tsx",
      uncertainty: "",
    },
  ],
  symbols: [
    {
      symbol_name: "Checkout",
      qualified_name: "Checkout",
      kind: "function",
      change: "modified",
      start_line: 10,
      end_line: 80,
      caller_count: 7,
      centrality_rank: 4,
      source: "tree_sitter",
    },
  ],
  evidence: [
    { name: "Full suite", status: "PASS", detail: "624 passed", source: "test", scope: "review" },
    { name: "Lint", status: "FAIL", detail: "1 failure", source: "ruff", scope: "file" },
    { name: "Focused tests", status: "UNKNOWN", detail: "not captured", source: "none", scope: "file" },
  ],
  provenance: {
    status: "matched",
    host: "claude",
    model: "claude-opus",
    session_id: "session-1",
    task: "checkout refactor",
    certainty: "exact",
    match_confidence: 1,
    match_reason: "recorded at authoring time",
    commands_run: ["pytest tests/checkout"],
    subagents: [],
    reads_recorded: true,
    inspected: true,
    uninspected_impacted: [],
  },
};

const AUTHOR: Annotation = {
  id: "author-1",
  review_id: "review-1",
  revision_id: "rrv-1",
  parent_id: "",
  kind: "comment",
  state: "open",
  body: "Validation moved before payment creation.",
  created_by: "claude",
  created_by_actor: "agent",
  source: "author",
  source_id: "session-1",
  title: "Why validation moved",
  evidence: [],
  confidence: null,
  author_response: "none",
  created_at: "",
  updated_at: "",
  anchor_method: "identical_blob",
  anchor_method_label: "file unchanged since the comment",
  anchor_exact: true,
  anchor_detail: "",
  anchored: true,
  path: "src/checkout.tsx",
  side: "new",
  start_line: 24,
  end_line: 24,
  unit_key: "sym:checkout",
  symbol: "Checkout",
  origin_symbol: "Checkout",
};

/** A comment whose anchor could not be re-found: listed, never drawn on a line. */
const ORPHAN: Annotation = {
  ...AUTHOR,
  id: "orphan-1",
  source: "human",
  created_by: "reviewer",
  created_by_actor: "human",
  title: "",
  body: "Does this still handle the empty cart?",
  state: "orphaned",
  anchor_method: "unique_text",
  anchor_method_label: "matched by its text alone",
  anchor_exact: false,
  anchor_detail: "more than one place fit",
  anchored: false,
  start_line: 41,
  end_line: 41,
};

const OTHER_TARGET: ReviewTarget = {
  ...TARGET,
  target_id: "target:cart",
  unit_key: "sym:cart",
  path: "src/cart.tsx",
  label: "src/cart.tsx::Cart",
  symbol: "Cart",
};

function drawerFor(
  target: ReviewTarget,
  tab: "context" | "checks" | "discussion" | "activity",
  onAddPreview = vi.fn(),
) {
  return (
    <ContextDrawer
      reviewId="review-1"
      target={target}
      detail={DETAIL}
      degraded={[]}
      annotations={[AUTHOR]}
      evidence={[]}
      tab={tab}
      pinned
      onTab={vi.fn()}
      onClose={vi.fn()}
      onTogglePin={vi.fn()}
      onOpenImpact={vi.fn()}
      onUploadEvidence={vi.fn()}
      onAddPreview={onAddPreview}
    />
  );
}

function renderDrawer(
  tab: "context" | "checks" | "discussion" | "activity" = "context",
  detail: FileDetail = DETAIL,
  annotations: Annotation[] = [AUTHOR],
) {
  const onTab = vi.fn();
  const onClose = vi.fn();
  const onTogglePin = vi.fn();
  const onOpenImpact = vi.fn();
  render(
    <ContextDrawer
      reviewId="review-1"
      target={TARGET}
      detail={detail}
      degraded={[]}
      annotations={annotations}
      evidence={[]}
      tab={tab}
      pinned={false}
      onTab={onTab}
      onClose={onClose}
      onTogglePin={onTogglePin}
      onOpenImpact={onOpenImpact}
      onUploadEvidence={vi.fn()}
      onAddPreview={vi.fn()}
    />,
  );
  return { onTab, onClose, onTogglePin, onOpenImpact };
}

describe("ContextDrawer", () => {
  beforeEach(() => {
    window.localStorage.removeItem("lemoncrow.review.context.visibleTabs");
    reviewApi.fetchTargetHistory.mockReset();
    reviewApi.fetchTargetHistory.mockResolvedValue({ review_id: "review-1", reviewer_id: "local", unit_key: TARGET.unit_key, events: [] });
    reviewApi.fetchReviewEvidenceContent.mockReset();
    reviewApi.fetchReviewEvidenceContent.mockImplementation(async (id: string) => new Blob([id], { type: "image/png" }));
    let objectUrlCounter = 0;
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => `blob:evidence-${++objectUrlCounter}`) });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  });

  it("opens the target and each definition in Code when a local checkout is bound", async () => {
    const onOpenInCode = vi.fn();
    render(
      <ContextDrawer
        reviewId="review-1"
        target={TARGET}
        detail={DETAIL}
        degraded={[]}
        annotations={[]}
        evidence={[]}
        tab="context"
        pinned={false}
        onTab={vi.fn()}
        onClose={vi.fn()}
        onTogglePin={vi.fn()}
        onOpenImpact={vi.fn()}
        onUploadEvidence={vi.fn()}
        onAddPreview={vi.fn()}
        onOpenInCode={onOpenInCode}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Open in Code" }));
    expect(onOpenInCode).toHaveBeenLastCalledWith({ path: "src/checkout.tsx", line: 10, endLine: 80 });
    await userEvent.click(screen.getByText(/Definitions in this file/));
    await userEvent.click(screen.getByRole("button", { name: "Open Checkout in Code" }));
    expect(onOpenInCode).toHaveBeenLastCalledWith({ path: "src/checkout.tsx", line: 10, endLine: 80 });
  });

  it("offers no Code link without a local checkout", () => {
    renderDrawer("context");
    expect(screen.queryByRole("button", { name: "Open in Code" })).toBeNull();
  });

  function renderPeekDrawer(capabilities: LemonCrowCapabilities, onJumpToCode = vi.fn()) {
    render(
      <ProductCapabilitiesProvider value={capabilities}>
        <ContextDrawer
          reviewId="review-1"
          codeRoot="/repo"
          target={TARGET}
          detail={DETAIL}
          degraded={[]}
          annotations={[]}
          evidence={[]}
          tab="context"
          pinned={false}
          onTab={vi.fn()}
          onClose={vi.fn()}
          onTogglePin={vi.fn()}
          onOpenImpact={vi.fn()}
          onUploadEvidence={vi.fn()}
          onAddPreview={vi.fn()}
          onJumpToCode={onJumpToCode}
        />
      </ProductCapabilitiesProvider>,
    );
    return { onJumpToCode };
  }

  it("makes no code-map request for a peek opened before discovery resolves on a Review-only host", async () => {
    codeMap.codeMapSearch.mockReset();
    codeMap.codeMapReferences.mockReset();
    let answerDiscovery: (body: unknown) => void = () => undefined;
    const discovery = new Promise<Response>((resolve) => {
      answerDiscovery = (body) => resolve({ ok: true, json: async () => body } as Response);
    });
    const fetchStub = vi.fn((input: RequestInfo | URL) => (
      String(input).includes("/.well-known/lemoncrow-server.json")
        ? discovery
        : Promise.resolve({ ok: false, json: async () => ({}) } as Response)
    ));
    vi.stubGlobal("fetch", fetchStub);
    try {
      // The host's provisional fallback says "Runtime present"; only discovery may decide.
      render(
        <DiscoveredProductCapabilitiesProvider fallback={LOCAL_PRODUCT_CAPABILITIES}>
          <ContextDrawer
            reviewId="review-1"
            codeRoot="/repo"
            target={TARGET}
            detail={DETAIL}
            degraded={[]}
            annotations={[]}
            evidence={[]}
            tab="context"
            pinned={false}
            onTab={vi.fn()}
            onClose={vi.fn()}
            onTogglePin={vi.fn()}
            onOpenImpact={vi.fn()}
            onUploadEvidence={vi.fn()}
            onAddPreview={vi.fn()}
            onJumpToCode={vi.fn()}
          />
        </DiscoveredProductCapabilitiesProvider>,
      );
      expect(screen.getByText("Affected code")).toBeTruthy();
      await waitFor(() => expect(fetchStub).toHaveBeenCalledWith("/.well-known/lemoncrow-server.json", expect.anything()));
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(screen.queryByTestId("review-code-peek")).toBeNull();
      expect(codeMap.codeMapSearch).not.toHaveBeenCalled();

      await act(async () => {
        answerDiscovery({
          deployment: { mode: "hosted" },
          products: { review: { installed: true, configured: true, available: true }, runtime: { installed: false, configured: false, available: false } },
        });
        await discovery;
      });
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(screen.queryByTestId("review-code-peek")).toBeNull();
      expect(codeMap.codeMapSearch).not.toHaveBeenCalled();
      expect(codeMap.codeMapReferences).not.toHaveBeenCalled();
      expect(fetchStub.mock.calls.some(([input]) => String(input).includes("code-map"))).toBe(false);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("has no Code Peek entry point and makes no code-map request in a Review-only install", async () => {
    codeMap.codeMapSearch.mockReset();
    codeMap.codeMapReferences.mockReset();
    renderPeekDrawer(REVIEW_ONLY_PRODUCT_CAPABILITIES);
    expect(screen.getByText("Affected code")).toBeTruthy();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByTestId("review-code-peek")).toBeNull();
    expect(screen.queryByText("Code Peek")).toBeNull();
    expect(codeMap.codeMapSearch).not.toHaveBeenCalled();
    expect(codeMap.codeMapReferences).not.toHaveBeenCalled();
  });

  it("has no Code Peek on a local Dashboard, whose code map has no precise references", async () => {
    codeMap.codeMapSearch.mockReset();
    codeMap.codeMapReferences.mockReset();
    renderPeekDrawer(LOCAL_DASHBOARD_PRODUCT_CAPABILITIES);
    expect(screen.getByText("Affected code")).toBeTruthy();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByTestId("review-code-peek")).toBeNull();
    expect(codeMap.codeMapSearch).not.toHaveBeenCalled();
    expect(codeMap.codeMapReferences).not.toHaveBeenCalled();
  });

  it("peeks with the Runtime present, says when its index is down, and jumps inside the Review", async () => {
    codeMap.codeMapSearch.mockReset();
    codeMap.codeMapSearch.mockRejectedValueOnce(new Error("index offline"));
    renderPeekDrawer(LOCAL_PRODUCT_CAPABILITIES);
    expect(await screen.findByText("Current index unavailable.")).toBeTruthy();
    expect(codeMap.codeMapSearch).toHaveBeenCalledWith("Checkout", "/repo", 12);
  });

  it("routes a Code Peek definition jump to the Review, not to Code", async () => {
    codeMap.codeMapSearch.mockReset();
    codeMap.codeMapReferences.mockReset();
    codeMap.codeMapSearch.mockResolvedValue({
      query: "Checkout",
      text_results: [],
      results: [{ id: "sym:Checkout", label: "Checkout", qualified_name: "Checkout", path: "src/checkout.tsx", kind: "function", line: 10, end_line: 80, node_type: "symbol" }],
    });
    codeMap.codeMapReferences.mockResolvedValue({ symbol_id: "sym:Checkout", reference_count: 0, truncated: false, references: [] });
    const { onJumpToCode } = renderPeekDrawer(LOCAL_PRODUCT_CAPABILITIES);
    await userEvent.click(await screen.findByRole("button", { name: "Go to definition" }));
    expect(onJumpToCode).toHaveBeenCalledWith({ path: "src/checkout.tsx", line: 10, endLine: 80 });
    // Without a trusted Code handoff, Open in Code is simply absent.
    expect(screen.queryByRole("button", { name: "Open definition in Code" })).toBeNull();
  });

  it("uses task-oriented tabs and starts from the caller-selected tab", async () => {
    const { onTab } = renderDrawer("context");
    expect(screen.getByText("Affected code")).toBeTruthy();
    expect(screen.queryByText("Summary")).toBeNull();
    await userEvent.click(screen.getByRole("tab", { name: /checks/i }));
    expect(onTab).toHaveBeenCalledWith("checks");
  });

  it("renders exact-session author journal in Agent and filters unrelated file activity", () => {
    render(
      <ContextDrawer
        reviewId="review-1"
        target={TARGET}
        detail={DETAIL}
        degraded={[]}
        annotations={[AUTHOR]}
        evidence={[]}
        journal={{
          available: true,
          reason: "",
          review_id: "review-1",
          revision_id: "rrv-1",
          revision_number: 1,
          session_id: "session-1",
          host: "claude",
          entries: [
            { id: "plan", kind: "plan", at: "2026-09-23T18:00:00Z", summary: "Author plan", detail: "1. Trace checkout callers", path: "", status: "", source: "run_ledger", host: "claude", session_id: "session-1" },
            { id: "fact", kind: "fact", at: "2026-09-23T18:01:00Z", summary: "Verified: shared validator", detail: "", path: "", status: "", source: "run_ledger", host: "claude", session_id: "session-1" },
            { id: "edit", kind: "edit", at: "2026-09-23T18:02:00Z", summary: "edited src/checkout.tsx", detail: "", path: "src/checkout.tsx", status: "changed", source: "run_ledger", host: "claude", session_id: "session-1" },
            { id: "other", kind: "edit", at: "2026-09-23T18:03:00Z", summary: "edited src/unrelated.tsx", detail: "", path: "src/unrelated.tsx", status: "changed", source: "run_ledger", host: "claude", session_id: "session-1" },
          ],
        }}
        tab="activity"
        pinned={false}
        onTab={vi.fn()}
        onClose={vi.fn()}
        onTogglePin={vi.fn()}
        onOpenImpact={vi.fn()}
        onUploadEvidence={vi.fn()}
        onAddPreview={vi.fn()}
      />,
    );

    expect(screen.getByText("Author plan")).toBeTruthy();
    expect(screen.getByText("Verified: shared validator")).toBeTruthy();
    expect(screen.getByText("edited src/checkout.tsx")).toBeTruthy();
    expect(screen.queryByText("edited src/unrelated.tsx")).toBeNull();
    expect(screen.getByText(/Hidden chain-of-thought is not captured/)).toBeTruthy();
  });

  it("does not present raw AI confidence as a default reviewer truth signal", () => {
    const ai: Annotation = {
      ...AUTHOR,
      id: "ai-1",
      body: "Possible stale-cache behavior.",
      title: "Cache review",
      created_by: "correctness",
      created_by_actor: "agent",
      source: "ai_review",
      source_id: "correctness",
      confidence: 0.72,
    };

    renderDrawer("discussion", DETAIL, [AUTHOR, ai]);

    expect(screen.getByText("AI REVIEW")).toBeTruthy();
    expect(screen.queryByText("72% confidence")).toBeNull();
    expect(screen.queryByTitle("Confidence reported by the AI review source; not a human verdict")).toBeNull();
  });

  it("exposes exactly four fixed conceptual homes", () => {
    renderDrawer("context");
    expect(screen.getByRole("tablist", { name: "Review context homes" })).toBeTruthy();
    const tabs = screen.getAllByRole("tab").map((item) => item.textContent?.replace(/\d+$/, "") ?? "");
    expect(tabs).toEqual(["Context", "Checks", "Discussion", "Activity"]);
    expect(screen.queryByRole("button", { name: "Choose context tabs" })).toBeNull();
    expect(screen.queryByRole("tab", { name: /Evidence/ })).toBeNull();
    expect(screen.queryByRole("tab", { name: /Development/ })).toBeNull();
    expect(screen.queryByRole("tab", { name: /History/ })).toBeNull();
  });

  it("loads append-only judgment transitions for the active semantic target", async () => {
    reviewApi.fetchTargetHistory.mockResolvedValue({
      review_id: "review-1",
      reviewer_id: "local",
      unit_key: TARGET.unit_key,
      events: [
        {
          id: "mark-1",
          review_id: "review-1",
          reviewer_id: "local",
          unit_key: TARGET.unit_key,
          revision_id: "rrv-2",
          reviewed_revision_id: "rrv-1",
          event_kind: "reconciled",
          from_state: "reviewed",
          to_state: "changed_since_review",
          content_fingerprint: "fp",
          previous_unit_key: "",
          actor_type: "human",
          note: "",
          reason: "content changed",
          created_at: "2026-09-16T07:00:00Z",
        },
      ],
    });
    renderDrawer("activity");
    await waitFor(() => expect(screen.getByText("reviewed → changed_since_review")).toBeTruthy());
    expect(reviewApi.fetchTargetHistory).toHaveBeenCalledWith("review-1", TARGET.unit_key);
    expect(screen.getByText("content changed")).toBeTruthy();
    expect(screen.queryByText(/synthetic event/i)).toBeNull();
  });

  it("orders failed and unresolved checks ahead of review-wide passes", () => {
    renderDrawer("checks");
    const text = screen.getByText("Lint").closest("div")?.parentElement?.parentElement?.textContent ?? "";
    expect(text).toContain("Failed");
    expect(screen.getByText("Focused tests")).toBeTruthy();
    expect(screen.getByText("Full suite")).toBeTruthy();
    expect(screen.getByText(/review-wide passes are context, not proof/i)).toBeTruthy();
  });

  it("describes the single ordered list of checks it actually renders", () => {
    // Outcome is the primary sort, so a review-wide failure sits above a
    // file-scoped pass. Promising two separate sections would send the reviewer
    // hunting for a file-scoped block that is not on the panel.
    renderDrawer("checks", {
      ...DETAIL,
      evidence: [
        { name: "File lint", status: "PASS", detail: "", source: "ruff", scope: "file" },
        { name: "Packet suite", status: "FAIL", detail: "", source: "test", scope: "review" },
      ],
    });
    const panel = screen.getByRole("tabpanel");
    const order = Array.from(panel.querySelectorAll("span"))
      .map((node) => node.textContent)
      .filter((text) => text === "File lint" || text === "Packet suite");
    expect(order).toEqual(["Packet suite", "File lint"]);
    expect(panel.textContent).not.toMatch(/shown separately/i);
    expect(panel.textContent).toMatch(/review-wide passes are context, not proof/i);
  });

  it("shows where a check was verified and what that sandbox could not verify, as structured rows", () => {
    const digest = `sha256:${"ab12".repeat(16)}`;
    renderDrawer("checks", {
      ...DETAIL,
      evidence: [
        {
          name: "Signup email",
          status: "PASS",
          detail: "Signup email · PASS",
          source: "test",
          scope: "review",
          execution: {
            executor_class: "managed_sandbox",
            location: "managed sandbox",
            image_digest: digest,
            network: "denied",
            statement: `managed sandbox · image ${digest} · network denied`,
            not_verified: [
              { name: "real email delivery", reason: "SMTP is recorded, never delivered" },
              { name: "third-party webhooks", reason: "recorded by the sink" },
            ],
          },
        },
        { name: "Lint", status: "PASS", detail: "", source: "ruff", scope: "review" },
      ],
    });
    const environments = screen.getAllByTestId("execution-environment");
    // Only the typed execution result carries an environment; an untyped check gets none.
    expect(environments).toHaveLength(1);
    const environment = environments[0];
    expect(environment.textContent).toContain("sandbox · image sha256:ab12ab12ab12 · network denied");
    expect(within(environment).getByTitle(digest)).toBeTruthy();
    const unverified = within(environment).getByRole("list", { name: "Not verified" });
    expect(within(unverified).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "real email delivery",
      "third-party webhooks",
    ]);
    expect(within(unverified).getByText("real email delivery").getAttribute("title")).toBe("SMTP is recorded, never delivered");
  });

  it("labels worker and author-machine runs by where they actually executed", () => {
    const row = (name: string, executor_class: "customer_worker" | "author_machine") => ({
      name,
      status: "PASS" as const,
      detail: "",
      source: "test",
      scope: "review" as const,
      execution: { executor_class, location: "", image_digest: "", network: "denied", statement: "", not_verified: [] },
    });
    renderDrawer("checks", { ...DETAIL, evidence: [row("Worker run", "customer_worker"), row("Local run", "author_machine")] });
    const text = screen.getAllByTestId("execution-environment").map((node) => node.textContent);
    expect(text.sort()).toEqual([
      "author machine (self-attested) · image unknown · network denied",
      "worker sandbox · image unknown · network denied",
    ]);
    // No unverified list is invented when the record states none.
    expect(screen.queryByRole("list", { name: "Not verified" })).toBeNull();
  });

  it("puts a file-scoped check ahead of a review-wide check that ended the same way", () => {
    // Spec §14.4: outcome first, then "File-scoped checks before review-wide
    // checks." The names are deliberately anti-alphabetical, so a comparator
    // that falls straight from outcome to name orders these the wrong way.
    renderDrawer("checks", {
      ...DETAIL,
      evidence: [
        { name: "Alpha suite", status: "FAIL", detail: "", source: "test", scope: "review" },
        { name: "Zed lint", status: "FAIL", detail: "", source: "ruff", scope: "file" },
      ],
    });
    const panel = screen.getByRole("tabpanel");
    const order = Array.from(panel.querySelectorAll("span"))
      .map((node) => node.textContent)
      .filter((text) => text === "Alpha suite" || text === "Zed lint");
    expect(order).toEqual(["Zed lint", "Alpha suite"]);
    expect(panel.textContent).toContain("this file");
    expect(panel.textContent).toContain("review-wide");
  });

  it("pairs exact Snapshot screenshots into an inspectable before/after diff", async () => {
    const evidence = (overrides: Partial<ReviewEvidence>): ReviewEvidence => ({
      id: "evd-after",
      review_id: "review-1",
      revision_id: "rrv-1",
      revision_number: 1,
      kind: "screenshot",
      title: "checkout.png",
      path: "src/checkout.tsx",
      url: "",
      content_hash: "hash",
      mime_type: "image/png",
      bytes: 12,
      source: "agent",
      source_ref: "capture:checkout",
      verification_status: "",
      detail: "mobile checkout",
      created_at: "2026-09-24T12:01:00Z",
      status: "current",
      content_url: "/api/evidence/evd-after/content",
      ...overrides,
    });
    const before = evidence({
      id: "evd-before",
      revision_id: "rrv-0",
      revision_number: 0,
      content_hash: "old-hash",
      created_at: "2026-09-24T11:01:00Z",
      status: "stale",
      content_url: "/api/evidence/evd-before/content",
    });
    const after = evidence({});

    render(
      <ContextDrawer
        reviewId="review-1"
        target={TARGET}
        detail={{ ...DETAIL, revision_id: "rrv-1" }}
        degraded={[]}
        annotations={[]}
        evidence={[before, after]}
        tab="checks"
        pinned={false}
        onTab={vi.fn()}
        onClose={vi.fn()}
        onTogglePin={vi.fn()}
        onOpenImpact={vi.fn()}
        onUploadEvidence={vi.fn()}
        onAddPreview={vi.fn()}
      />,
    );

    expect(await screen.findByText("Screenshot diff")).toBeTruthy();
    await waitFor(() => expect(reviewApi.fetchReviewEvidenceContent).toHaveBeenCalledTimes(2));
    expect(screen.getByAltText("Before screenshot")).toBeTruthy();
    expect(screen.getByAltText("After screenshot")).toBeTruthy();
    expect(screen.getByText(/older Snapshot and is not current proof/i)).toBeTruthy();
    const divider = screen.getByRole("slider", { name: "Screenshot comparison divider" });
    expect(divider.getAttribute("aria-valuenow")).toBe("50");
    fireEvent.keyDown(divider, { key: "ArrowRight" });
    expect(divider.getAttribute("aria-valuenow")).toBe("51");
    expect((screen.getByAltText("After screenshot") as HTMLImageElement).style.clipPath).toBe("inset(0 49% 0 0)");
  });

  it("can compare a semantically-current carried screenshot against an older Snapshot", async () => {
    const base: ReviewEvidence = {
      id: "evd-carried", review_id: "review-1", revision_id: "rrv-0", revision_number: 1,
      kind: "screenshot", title: "checkout.png", path: "src/checkout.tsx", url: "",
      content_hash: "same-code", mime_type: "image/png", bytes: 12, source: "agent",
      source_ref: "capture:checkout", verification_status: "", detail: "",
      created_at: "2026-09-24T12:01:00Z", status: "current",
      content_url: "/api/evidence/evd-carried/content",
    };
    const older: ReviewEvidence = {
      ...base, id: "evd-older", revision_id: "rrv-before", revision_number: 0,
      content_hash: "older-code", created_at: "2026-09-24T11:01:00Z", status: "stale",
      content_url: "/api/evidence/evd-older/content",
    };
    // Snapshot 1 is an analysis/base-movement Snapshot with the same reviewed
    // code, so the carried artifact is still current proof even though its row
    // was captured on Snapshot 0. It should still be usable as the after image.
    const marker: ReviewEvidence = { ...base, id: "marker", revision_id: "rrv-1", revision_number: 2, kind: "document", mime_type: "text/plain", title: "marker", content_url: "" };
    render(
      <ContextDrawer
        reviewId="review-1" target={TARGET} detail={{ ...DETAIL, revision_id: "rrv-1" }} degraded={[]} annotations={[]}
        evidence={[older, base, marker]} tab="checks" pinned={false} onTab={vi.fn()} onClose={vi.fn()}
        onTogglePin={vi.fn()} onOpenImpact={vi.fn()} onUploadEvidence={vi.fn()} onAddPreview={vi.fn()}
      />,
    );
    expect(await screen.findByText("Screenshot diff")).toBeTruthy();
    expect(screen.getByAltText("Before screenshot")).toBeTruthy();
    expect(screen.getByAltText("After screenshot")).toBeTruthy();
    expect(screen.getByRole("slider", { name: "Screenshot comparison divider" })).toBeTruthy();
  });

  it("opens the evidence picker from the keyboard, not only with a pointer", async () => {
    renderDrawer("checks");
    const picker = document.querySelector('input[type="file"]') as HTMLInputElement;
    const opened = vi.fn();
    picker.addEventListener("click", opened);
    screen.getByRole("button", { name: "Choose files" }).focus();
    await userEvent.keyboard("{Enter}");
    expect(opened).toHaveBeenCalledTimes(1);
  });

  it("lists an orphaned comment in its own unresolved area, with the rung that lost it", () => {
    // §15.6: orphans belong in a dedicated unresolved area and are never drawn
    // on a guessed line. `Discussion` has no anchor filter, so an orphan handed
    // to it renders in the anchored list under a stale "L41" and the bare word
    // "orphaned", with nothing saying why the anchor was lost.
    renderDrawer("discussion", DETAIL, [AUTHOR, ORPHAN]);
    const panel = screen.getByRole("tabpanel");
    const body = screen.getByText(/Does this still handle the empty cart\?/);
    const area = screen.getByText(/1 comment lost its anchor/i).parentElement as HTMLElement;

    expect(area.contains(body)).toBe(true);
    expect(area.textContent).toContain("matched by its text alone");
    expect(area.textContent).toContain("more than one place fit");
    // Once on the panel, so it is listed in the unresolved area rather than
    // also being mixed into the anchored discussion above it.
    expect(panel.textContent?.match(/Does this still handle the empty cart\?/g)).toHaveLength(1);
    // The anchored comment still reaches the discussion list.
    expect(screen.getByText("Validation moved before payment creation.")).toBeTruthy();
  });

  it("counts the Comments card by what is inside it, not by the orphans beside it", () => {
    // The badge hangs off the card and the card holds only anchored comments,
    // so counting the orphans too printed "Discussion 2" over one comment, with
    // the other one in a sibling block outside the card entirely.
    renderDrawer("discussion", DETAIL, [AUTHOR, ORPHAN]);
    const header = screen.getByRole("heading", { name: "Comments" }).parentElement as HTMLElement;
    expect(header.textContent).toBe("Comments1");
    // The tab still counts both halves: both of them are read on this tab.
    expect(screen.getByRole("tab", { name: /discussion/i }).textContent).toBe("Discussion2");
  });

  it("drops a preview URL typed for one target when the drawer moves to the next", async () => {
    // The drawer is mounted without a key and survives j/k, so an uncommitted
    // URL left in the box would be filed against the file that happens to be on
    // screen when Add is clicked -- evidence on the wrong file, silently.
    const onAddPreview = vi.fn();
    const view = render(drawerFor(TARGET, "checks", onAddPreview));
    const field = () => screen.getByPlaceholderText("http://localhost:3000/...") as HTMLInputElement;
    await userEvent.type(field(), "http://localhost:3000/checkout");
    expect(field().value).toBe("http://localhost:3000/checkout");

    view.rerender(drawerFor(OTHER_TARGET, "checks", onAddPreview));
    expect(field().value).toBe("");
    expect((screen.getByRole("button", { name: "Add" }) as HTMLButtonElement).disabled).toBe(true);
    expect(onAddPreview).not.toHaveBeenCalled();
  });

  it("adds a typed preview URL as a live preview and clears the field", async () => {
    const onAddPreview = vi.fn();
    render(drawerFor(TARGET, "checks", onAddPreview));
    const field = screen.getByPlaceholderText("http://localhost:3000/...") as HTMLInputElement;
    const add = screen.getByRole("button", { name: "Add" }) as HTMLButtonElement;

    await userEvent.type(field, "   ");
    expect(add.disabled).toBe(true);

    await userEvent.clear(field);
    await userEvent.type(field, "  http://localhost:3000/checkout  ");
    expect(add.disabled).toBe(false);
    await userEvent.click(add);

    expect(onAddPreview).toHaveBeenCalledTimes(1);
    expect(onAddPreview).toHaveBeenCalledWith("http://localhost:3000/checkout", "Live preview");
    expect(field.value).toBe("");
    expect(add.disabled).toBe(true);
  });

  it("uploads chosen and dropped evidence files and resets the picker and drop state", () => {
    const onUploadEvidence = vi.fn();
    render(
      <ContextDrawer
        reviewId="review-1" target={TARGET} detail={DETAIL} degraded={[]} annotations={[]} evidence={[]}
        tab="checks" pinned={false} onTab={vi.fn()} onClose={vi.fn()} onTogglePin={vi.fn()} onOpenImpact={vi.fn()}
        onUploadEvidence={onUploadEvidence} onAddPreview={vi.fn()}
      />,
    );
    const shot = new File(["png"], "checkout.png", { type: "image/png" });
    const trace = new File(["zip"], "trace.zip", { type: "application/zip" });

    const picker = screen.getByLabelText("Attach evidence files") as HTMLInputElement;
    fireEvent.change(picker, { target: { files: [shot, trace] } });
    expect(onUploadEvidence).toHaveBeenLastCalledWith([shot, trace]);
    // The same file can be chosen again right away.
    expect(picker.value).toBe("");

    const zone = screen.getByText("Drop screenshot, video, trace.zip, or document").closest("label") as HTMLElement;
    fireEvent.dragEnter(zone, { dataTransfer: { files: [shot], types: ["Files"] } });
    expect(zone.className).toContain("border-sky-600");
    fireEvent.drop(zone, { dataTransfer: { files: [shot], types: ["Files"] } });
    expect(onUploadEvidence).toHaveBeenLastCalledWith([shot]);
    expect(zone.className).not.toContain("border-sky-600");

    // A drop that carries no files uploads nothing.
    fireEvent.drop(zone, { dataTransfer: { files: [], types: ["text/plain"] } });
    expect(onUploadEvidence).toHaveBeenCalledTimes(2);
  });

  it("offers no evidence writes on a read-only review", () => {
    render(
      <ContextDrawer
        reviewId="review-1" target={TARGET} detail={DETAIL} degraded={[]} annotations={[]} evidence={[]}
        tab="checks" pinned={false} readOnly onTab={vi.fn()} onClose={vi.fn()} onTogglePin={vi.fn()} onOpenImpact={vi.fn()}
        onUploadEvidence={vi.fn()} onAddPreview={vi.fn()}
      />,
    );
    expect(screen.getByText("Evidence is read-only until this review is restored.")).toBeTruthy();
    expect(screen.queryByLabelText("Attach evidence files")).toBeNull();
    expect(screen.queryByPlaceholderText("http://localhost:3000/...")).toBeNull();
  });

  it("opens stored evidence through a short-lived blob URL", async () => {
    const stored: ReviewEvidence = {
      id: "evd-trace", review_id: "review-1", revision_id: "rrv-1", revision_number: 1,
      kind: "playwright_trace", title: "trace.zip", path: "src/checkout.tsx", url: "",
      content_hash: "hash", mime_type: "application/zip", bytes: 3, source: "human",
      source_ref: "trace.zip", verification_status: "", detail: "", created_at: "2026-09-24T12:01:00Z",
      status: "current", content_url: "/api/evidence/evd-trace/content",
    };
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(
        <ContextDrawer
          reviewId="review-1" target={TARGET} detail={DETAIL} degraded={[]} annotations={[]} evidence={[stored]}
          tab="checks" pinned={false} onTab={vi.fn()} onClose={vi.fn()} onTogglePin={vi.fn()} onOpenImpact={vi.fn()}
          onUploadEvidence={vi.fn()} onAddPreview={vi.fn()}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: "Open" }));
      await waitFor(() => expect(open).toHaveBeenCalledWith("blob:evidence-1", "_blank", "noopener,noreferrer"));
      expect(reviewApi.fetchReviewEvidenceContent).toHaveBeenCalledWith("evd-trace");

      // The new tab needs the URL long enough to load it; after that it is released.
      vi.advanceTimersByTime(59_000);
      expect(URL.revokeObjectURL).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1_000);
      expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:evidence-1");
    } finally {
      vi.useRealTimers();
      open.mockRestore();
    }
  });

  it("releases screenshot-diff blob URLs when the comparison goes away", async () => {
    const screenshot = (overrides: Partial<ReviewEvidence>): ReviewEvidence => ({
      id: "evd-after", review_id: "review-1", revision_id: "rrv-1", revision_number: 1,
      kind: "screenshot", title: "checkout.png", path: "src/checkout.tsx", url: "",
      content_hash: "hash", mime_type: "image/png", bytes: 12, source: "agent",
      source_ref: "capture:checkout", verification_status: "", detail: "",
      created_at: "2026-09-24T12:01:00Z", status: "current", content_url: "/api/evidence/evd-after/content",
      ...overrides,
    });
    const before = screenshot({
      id: "evd-before", revision_id: "rrv-0", revision_number: 0, content_hash: "old-hash",
      created_at: "2026-09-24T11:01:00Z", status: "stale", content_url: "/api/evidence/evd-before/content",
    });
    const view = render(
      <ContextDrawer
        reviewId="review-1" target={TARGET} detail={{ ...DETAIL, revision_id: "rrv-1" }} degraded={[]} annotations={[]}
        evidence={[before, screenshot({})]} tab="checks" pinned={false} onTab={vi.fn()} onClose={vi.fn()}
        onTogglePin={vi.fn()} onOpenImpact={vi.fn()} onUploadEvidence={vi.fn()} onAddPreview={vi.fn()}
      />,
    );
    expect(await screen.findByAltText("After screenshot")).toBeTruthy();
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();

    view.unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:evidence-1");
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:evidence-2");
  });

  it("keeps exact author provenance separate from discussion", () => {
    renderDrawer("context");
    const panel = screen.getByRole("tabpanel");
    expect(within(panel).getByText("Authored by")).toBeTruthy();
    expect(within(panel).getByText("Why validation moved")).toBeTruthy();
    expect(within(panel).queryByRole("heading", { name: "Comments" })).toBeNull();
  });

  it("exposes explicit pin and close controls", async () => {
    const { onClose, onTogglePin } = renderDrawer("context");
    await userEvent.click(screen.getByRole("button", { name: "Pin" }));
    expect(onTogglePin).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Close review context" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps a selected expert section in the tab semantics", async () => {
    renderDrawer("activity");
    expect(screen.getByRole("tab", { name: /activity/i }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe(screen.getByRole("tab", { name: /activity/i }).id);
    await waitFor(() => expect(reviewApi.fetchTargetHistory).toHaveBeenCalledWith("review-1", TARGET.unit_key));
  });

  it("uses tab semantics and moves focus into Context when it opens", async () => {
    renderDrawer("checks");
    const close = screen.getByRole("button", { name: "Close review context" });
    await waitFor(() => expect(document.activeElement).toBe(close));
    expect(screen.getByRole("tab", { name: /checks/i }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe(screen.getByRole("tab", { name: /checks/i }).id);
  });

  it("scopes the Development stream to the file and supersedes the legacy journal", async () => {
    reviewApi.fetchReviewDevelopmentEvents.mockResolvedValue({
      events: [event("e7", { path: "src/checkout.tsx", summary: "edited src/checkout.tsx (+4 -1)", host: "codex" })],
      next_before: null,
    });
    const onOpenDevelopment = vi.fn();
    const snapshot = projection({
      changed_paths: [{ path: "src/checkout.tsx", status: "changed", sessions: ["codex:x1"], edits: 2, last_at: "", in_snapshot: true }],
    });
    render(
      <ContextDrawer
        reviewId="review-1"
        target={TARGET}
        detail={DETAIL}
        degraded={[]}
        annotations={[]}
        evidence={[]}
        journal={{ available: true, reason: "", review_id: "review-1", revision_id: "rrv-1", revision_number: 1, session_id: "s", host: "claude", entries: [] }}
        development={development({
          snapshot,
          since: projection({ scope: "since", changed_paths: [{ path: "src/checkout.tsx", status: "changed", sessions: ["claude:c1"], edits: 1, last_at: "", in_snapshot: null }] }),
        })}
        tab="activity"
        pinned={false}
        onTab={vi.fn()}
        onClose={vi.fn()}
        onTogglePin={vi.fn()}
        onOpenImpact={vi.fn()}
        onUploadEvidence={vi.fn()}
        onAddPreview={vi.fn()}
        onOpenDevelopment={onOpenDevelopment}
      />,
    );

    const panel = screen.getByTestId("context-development");
    expect(within(panel).getByText("Codex")).toBeTruthy();
    expect(within(panel).queryByText("Claude")).toBeNull();
    expect(within(panel).getByText(/2 recorded edits/)).toBeTruthy();
    expect(within(panel).getByRole("status").textContent).toContain("Changed again after this Snapshot by Claude");
    expect(await within(panel).findByText("edited src/checkout.tsx (+4 -1)")).toBeTruthy();
    expect(reviewApi.fetchReviewDevelopmentEvents).toHaveBeenCalledWith("review-1", {
      revisionNumber: 52,
      scope: "snapshot",
      path: "src/checkout.tsx",
      limit: 30,
    });
    expect(screen.getByRole("tab", { name: /activity/i })).toBeTruthy();
    await userEvent.click(within(panel).getByRole("button", { name: "Open Activity" }));
    expect(onOpenDevelopment).toHaveBeenCalledTimes(1);
  });

  it("names an unexplained file change instead of crediting an agent", () => {
    reviewApi.fetchReviewDevelopmentEvents.mockResolvedValue({ events: [], next_before: null });
    render(
      <ContextDrawer
        reviewId="review-1"
        target={TARGET}
        detail={DETAIL}
        degraded={[]}
        annotations={[]}
        evidence={[]}
        development={development({ snapshot: projection({ changed_paths: [], unattributed_paths: ["src/checkout.tsx"] }) })}
        tab="activity"
        pinned={false}
        onTab={vi.fn()}
        onClose={vi.fn()}
        onTogglePin={vi.fn()}
        onOpenImpact={vi.fn()}
        onUploadEvidence={vi.fn()}
        onAddPreview={vi.fn()}
      />,
    );
    expect(screen.getByText(/no agent recorded an exact edit to it/)).toBeTruthy();
  });
});

describe("ContextDrawer static findings", () => {
  it("mounts the file's imported findings in the Checks tab only", async () => {
    staticApi.fetchStaticFindings.mockImplementation(() =>
      Promise.resolve({
        review_id: "review-1",
        snapshot_id: "revision-1",
        path: "src/checkout.tsx",
        findings: [
          {
            id: "sf_1",
            fingerprint: "sfp_1",
            ref: "static:ev-1:sf_1",
            tool: "semgrep",
            rule_id: "py.sqli",
            severity: "p1",
            severity_label: "P1 High",
            level: "error",
            path: "src/checkout.tsx",
            start_line: 21,
            end_line: 21,
            message: "query built from input",
            message_truncated: false,
            tags: [],
            cwe: [89],
            in_snapshot: true,
            state: "current",
            evidence_id: "ev-1",
            snapshot_id: "revision-1",
            snapshot_number: 1,
            check: "semgrep",
            check_identity: "reported|semgrep",
            check_status: "FAIL",
            category: "sast",
            attestation_class: "",
            authority: "reported",
            head_sha: "a".repeat(40),
            disposition: null,
          },
        ],
        stale: [],
        checks: [],
        chains: [],
        truncation_notes: [],
        counts: { current: 1, stale: 0, dismissed: 0, chains: 0 },
        latest: true,
      }),
    );
    renderDrawer("context");
    expect(screen.queryByTestId("static-findings-panel")).toBeNull();
    cleanup();

    renderDrawer("checks");
    const panel = await screen.findByTestId("static-findings-panel");
    expect(staticApi.fetchStaticFindings).toHaveBeenLastCalledWith("review-1", DETAIL.revision_id ?? "", "src/checkout.tsx");
    expect(panel).toHaveTextContent("P1 High");
    expect(panel).toHaveTextContent("semgrep · py.sqli");
  });
});

describe("ContextDrawer interactive preview", () => {
  it("opens the side-by-side preview from the Checks rail only", async () => {
    previewApi.fetchInteractivePreview.mockImplementation(() =>
      Promise.resolve({
        state: "available",
        reason: "",
        revision_id: DETAIL.revision_id ?? "",
        run_on: "local",
        executor_class: "author_machine",
        recipe: { mode: "command", source: ".lemoncrow/review.yaml interactive_preview", framework: "" },
        budget: { remaining_minutes: null, minutes_per_minute: 2 },
        ttl: { default_seconds: 600, max_seconds: 900, min_seconds: 60 },
        opens_from: ["behavior_changes", "checks_rail"],
        observation: "human",
        sessions: [],
      }),
    );
    renderDrawer("context");
    expect(screen.queryByTestId("interactive-preview-checks_rail")).toBeNull();
    cleanup();

    renderDrawer("checks");
    const panel = await screen.findByTestId("interactive-preview-checks_rail");
    expect(previewApi.fetchInteractivePreview).toHaveBeenLastCalledWith("review-1");
    expect(within(panel).getByRole("button", { name: "Open side by side" })).toBeInTheDocument();
  });
});
