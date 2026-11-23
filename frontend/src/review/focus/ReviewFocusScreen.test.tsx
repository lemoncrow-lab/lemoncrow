import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";

import { fetchFile, fetchFileContents, fetchTargetHistory, requestBrowserJson } from "../reviewApi";
import { fetchCheckRecipes, postCheckRecipesRun, type CheckRecipesProjection, type CheckRunResponse } from "../checkRecipesApi";
import type { Annotation, MarkState, ReviewChangeProposal, ReviewOverview, ReviewProjection, ReviewTarget } from "../types";
import { fixtureDecision } from "../investigation/fixtures";
import { INVESTIGATION_MAP_FIXTURES } from "../investigation/mapFixtures";
import { authRefreshWithConsumers } from "../whatIf/fixtures";
import { ReviewFocusScreen, type FocusInvestigateHost } from "./ReviewFocusScreen";
import type { LineComment } from "./FocusCode";
import { resetPartProof } from "../partProof";

vi.mock("../reviewApi", () => ({ fetchFile: vi.fn(), fetchFileContents: vi.fn(), fetchTargetHistory: vi.fn(), requestBrowserJson: vi.fn() }));
vi.mock("../checkRecipesApi", () => ({ fetchCheckRecipes: vi.fn(), postCheckRecipesRun: vi.fn() }));
vi.mock("@pierre/diffs", () => import("./pierreTestMock").then((m) => m.pierreCore));
vi.mock("@pierre/diffs/react", () => import("./pierreTestMock").then((m) => m.pierreReact));

const apiFile = vi.mocked(fetchFile);
const apiContents = vi.mocked(fetchFileContents);
const apiHistory = vi.mocked(fetchTargetHistory);
const apiChecks = vi.mocked(fetchCheckRecipes);
const apiRun = vi.mocked(postCheckRecipesRun);
const apiProof = vi.mocked(requestBrowserJson);
const proofRecord = (files: Record<string, { o: string; r?: string; t?: string[]; n?: number }>) => ({
  revision_id: "rev1", head_identity: "head", base_identity: "base", reach: "file", default: { o: "not_tested" }, files, recorded: true, complete: true, running: false,
  source: "auto", labels: {}, details: {}, reasons: { no_sandbox: "no sandbox is available here, and tests never run unsandboxed on their own" },
});
const MAPPED = { unit_key: "sym:can_view_order", path: "app/auth.py", symbol: "can_view_order", fingerprint: "sha256:code", entry_id: "checks.recipe:auth", recipe_id: "auth", recipe_digest: "recipe:sha256:test", title: "Permission test", mapping_kind: "explicit_trusted_base", coverage: "not_assessed" } as const;
const CHECKS = { revision_id: "rev1", part_checks: [], results: [] } as unknown as CheckRecipesProjection;
const PATCH = "@@ -1,3 +1,3 @@\n def can_view_order():\n-    return False\n+    return True\n";

function target(name: string, position: number, category: "decision" | "quick_check" = "decision"): ReviewTarget {
  return {
    target_id: name,
    unit_key: `sym:${name}`,
    kind: "symbol",
    path: name === "can_view_order" ? "app/auth.py" : "app/orders.py",
    label: name,
    symbol: name,
    start_line: 1,
    end_line: 3,
    hunk_ordinals: [0],
    spans: [],
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: position,
    attention_level: "high",
    reasons: [],
    additions: 1,
    deletions: 1,
    fingerprint_method: "ast",
    verification: { pass: 0, fail: 0, unknown: 1 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    answer: { category, rule: "", rule_label: "", reason: "" },
    priority: { version: 1, tier: 3, attention: "review_first", outstanding: true, position, reasons: [{ code: "removed_safeguard", detail: "" }] },
  };
}

const START = [target("can_view_order", 0), target("apply_discount", 1), target("order_total", 2, "quick_check")];
const OVERVIEW = { title: "Review the change", session: { id: "canonical-review-id", ref: "shop" }, revision: { id: "rev1", revision_number: 1, provenance_host: "claude_code" } } as ReviewOverview;
const PROJECTION = {
  decisions: [],
  questions: [{ id: "q1", origin: "lemoncrow", title: "Should guests see these orders?", target_ids: ["can_view_order"] }],
  context: { consumers: [] },
} as unknown as ReviewProjection;

function Location() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}</output>;
}

function Harness({
  initialTargets = START,
  projection = PROJECTION,
  aiAvailable = true,
  complete = true,
  readOnly = false,
  overview = OVERVIEW,
  annotations = [],
  proposals = [],
  onMark = vi.fn().mockResolvedValue(true),
  onNote = vi.fn().mockResolvedValue(true),
  investigate,
}: {
  investigate?: FocusInvestigateHost;
  initialTargets?: ReviewTarget[];
  projection?: ReviewProjection | null;
  aiAvailable?: boolean;
  complete?: boolean;
  readOnly?: boolean;
  overview?: ReviewOverview;
  annotations?: Annotation[];
  proposals?: ReviewChangeProposal[];
  onMark?: (target: ReviewTarget, state: MarkState) => Promise<boolean | "queued">;
  onNote?: (target: ReviewTarget, body: string, reconsideredAfterStructure: boolean, line?: LineComment) => Promise<boolean | "queued">;
}) {
  const location = useLocation();
  const [targets, setTargets] = useState(initialTargets);
  const part = location.pathname.startsWith("/r/shop/one/") ? decodeURIComponent(location.pathname.slice("/r/shop/one/".length)) : "";
  return <>
    <ReviewFocusScreen
      reviewRef="shop"
      apiReviewId="review-1"
      part={part}
      overview={overview}
      targets={targets}
      annotations={annotations}
      proposals={proposals}
      projection={projection}
      complete={complete}
      aiAvailable={aiAvailable}
      readOnly={readOnly}
      onMark={async (target, state) => {
        const ok = await onMark(target, state);
        if (ok === true) setTargets((old) => old.map((row) => row.target_id === target.target_id ? { ...row, state } : row));
        return ok;
      }}
      onNote={async (target, body, reconsideredAfterStructure, line) => {
        const ok = await (line ? onNote(target, body, reconsideredAfterStructure, line) : onNote(target, body, reconsideredAfterStructure));
        if (ok === true && !line) setTargets((old) => old.map((row) => row.target_id === target.target_id ? { ...row, state: "needs_changes" } : row));
        return ok;
      }}
      onAsk={aiAvailable ? vi.fn() : undefined}
      investigate={investigate}
    />
    <Location />
  </>;
}

function show(props: Parameters<typeof Harness>[0] = {}, initial = "/r/shop/one/can_view_order") {
  return render(<MemoryRouter initialEntries={[initial]}><Harness {...props} /></MemoryRouter>);
}

beforeEach(() => {
  window.localStorage.clear();
  resetPartProof();
  apiProof.mockReset().mockRejectedValue(new Error("offline"));
  apiHistory.mockReset().mockResolvedValue({ review_id: "review-1", reviewer_id: "reviewer", unit_key: "", events: [] });
  apiChecks.mockReset().mockResolvedValue(CHECKS);
  apiRun.mockReset();
  apiFile.mockReset().mockImplementation(async (_id, path) => ({ path, revision_id: "rev1", patch: PATCH, renderable: true, refusal: "", detail: "", degraded: [] }));
  apiContents.mockReset().mockImplementation(async (_id, path) => ({ path, revision_id: "rev1", old_path: path, status: "modified", old_content: "def can_view_order():\n    return False\n", new_content: "def can_view_order():\n    return True\n\n" }));
});
afterEach(cleanup);

describe("P11-1 the Focus card shows the part's proof", () => {
  it("the card says what the repository's tests say about the part in hand, from the one shared record", async () => {
    apiProof.mockResolvedValue(proofRecord({ "app/auth.py": { o: "proven", t: ["tests/test_auth.py"], n: 1 } }) as never);
    show();
    await waitFor(() => expect(screen.getByTestId("part-proof")).toHaveAttribute("data-outcome", "proven"));
    expect(screen.getByTestId("part-proof-note").textContent).toMatch(/Reached by tests\/test_auth\.py, by imports/);
    expect(apiProof.mock.calls.filter(([path]) => String(path).includes("/part-proof?"))).toHaveLength(1);
  });

  it("a record that could not be read is a neutral Couldn't load, never Not run yet", async () => {
    show();
    await waitFor(() => expect(screen.getByTestId("part-proof")).toHaveAttribute("data-outcome", "couldnt_run"));
    expect(screen.getByTestId("part-proof")).toHaveTextContent("Couldn't load");
    expect(screen.getByTestId("part-proof-note").textContent).not.toMatch(/Tests reach this file/);
  });
});

describe("Investigate in the Focus flow", () => {
  it("is not offered where the Reader has no Investigate host", async () => {
    show();
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByTestId("focus-open-investigate")).toBeNull();
  });

  it("opens Investigate for the part in hand, and Open source closes it on that part", async () => {
    show({ investigate: { projection: null, targets: START, canRun: false } });
    fireEvent.click(await screen.findByTestId("focus-open-investigate"));
    const panel = await screen.findByTestId("investigate", {}, { timeout: 10_000 });
    expect(panel).toHaveAttribute("data-analysed", "false");
    expect(screen.getByRole("dialog", { name: /^Investigate · .*can_view_order/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open source" }));
    await waitFor(() => expect(screen.queryByTestId("investigate")).toBeNull());
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/can_view_order");
  });
});

describe("Investigate leads to the X-Ray and the Brief", () => {
  const { graph } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
  const mapped = { ...graph, nodes: graph.nodes.map((node) => (node.id === "retry" ? { ...node, path: "app/auth.py" } : node)) };
  const investigate: FocusInvestigateHost = {
    projection: { ...authRefreshWithConsumers().projection, decisions: [fixtureDecision("guests", { target_ids: ["can_view_order"] })] },
    targets: START,
    canRun: false,
    loadMapGraph: async () => mapped,
  };

  it("Walk from here opens the X-Ray rooted on the part's file", async () => {
    show({ investigate });
    fireEvent.click(await screen.findByTestId("focus-open-investigate"));
    fireEvent.click(await screen.findByRole("button", { name: "Walk from here" }, { timeout: 10_000 }));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/xray"));
    expect(screen.queryByTestId("investigate")).toBeNull();
  });

  it("Decide in the Brief opens the Brief", async () => {
    show({ investigate });
    fireEvent.click(await screen.findByTestId("focus-open-investigate"));
    fireEvent.click(await screen.findByRole("button", { name: "Decide in the Brief" }, { timeout: 10_000 }));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/brief"));
  });
});

describe("mapped per-part tests", () => {
  it("does not offer a test without exact current-revision mapping", async () => {
    show();
    await waitFor(() => expect(apiChecks).toHaveBeenCalledWith("review-1", "rev1"));
    expect(screen.queryByRole("button", { name: "Run test" })).toBeNull();
    fireEvent.keyDown(window, { key: "r" });
    expect(apiRun).not.toHaveBeenCalled();
  });

  it("R runs the trusted mapped recipe, focuses the measured result, and never claims coverage", async () => {
    apiChecks.mockResolvedValue({ ...CHECKS, part_checks: [MAPPED] });
    let finish!: (result: CheckRunResponse) => void;
    apiRun.mockReturnValue(new Promise<CheckRunResponse>((resolve) => { finish = resolve; }));
    show();
    await waitFor(() => expect(screen.getByRole("button", { name: "Run test" })).toBeTruthy());
    fireEvent.keyDown(window, { key: "r" });
    expect(apiRun).toHaveBeenCalledWith("review-1", "rev1", "checks.recipe:auth", true, "", "sym:can_view_order", true);
    expect(screen.getByTestId("focus-test-result").textContent).toContain("Running");
    expect(screen.getByRole("button", { name: "Running…" }).hasAttribute("disabled")).toBe(true);
    finish({ revision_id: "rev1", entry_id: "checks.recipe:auth", verdicts: { "checks.recipe:auth": "PASS" }, executions: 1, reused: 0, notes: [], executor_class: "local", run_on: "local" });
    await waitFor(() => expect(screen.getByTestId("focus-test-result").textContent).toBe("Permission test · passed"));
    expect(document.activeElement).toBe(screen.getByTestId("focus-test-result"));
    expect(screen.getByText("Test passed")).toBeTruthy();
  });

  it("says a reused result was reused, never presenting it as this click's run", async () => {
    apiChecks.mockResolvedValue({ ...CHECKS, part_checks: [MAPPED] });
    apiRun.mockResolvedValue({ revision_id: "rev1", entry_id: "checks.recipe:auth", verdicts: { "checks.recipe:auth": "PASS" }, executions: 0, reused: 0, carried: 1, notes: [], executor_class: "local", run_on: "local" });
    show();
    await waitFor(() => expect(screen.getByRole("button", { name: "Run test" })).toBeTruthy());
    fireEvent.keyDown(window, { key: "r" });
    await waitFor(() => expect(screen.getByTestId("focus-test-result").textContent).toBe("Permission test · passed · reused from an earlier run, not run again for this click"));
    expect(screen.getByText("Test passed · reused")).toBeTruthy();
  });

  it("refuses a wrong part and a stale recipe projection without executing", async () => {
    apiChecks.mockResolvedValue({ ...CHECKS, part_checks: [{ ...MAPPED, path: "app/other.py" }] });
    show();
    await waitFor(() => expect(apiChecks).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Run test" })).toBeNull();
    expect(apiRun).not.toHaveBeenCalled();
  });
});

describe("Focus route and frozen evidence", () => {
  it("shows stored human notes after a reload, but not notes belonging to another part", async () => {
    const annotations = [
      { id: "note-1", review_id: "canonical-review-id", revision_id: "rev1", relation_to_revision: "originating", unit_key: "sym:can_view_order", path: "app/auth.py", parent_id: "", kind: "request_change", source: "human", created_by_actor: "reviewer", state: "open", body: "Validate ownership", anchored: true, anchor_exact: true, side: "additions", start_line: 2, end_line: 2 },
      { id: "note-2", review_id: "canonical-review-id", revision_id: "rev1", relation_to_revision: "originating", unit_key: "sym:apply_discount", path: "app/orders.py", parent_id: "", kind: "request_change", source: "human", created_by_actor: "reviewer", state: "open", body: "Check upper bound", anchored: true, anchor_exact: true, side: "additions", start_line: 3, end_line: 3 },
    ] as Annotation[];
    show({ annotations });
    expect(screen.getByTestId("focus-tray").textContent).toContain("2 notes for Claude Code");
    expect(screen.getByTestId("focus-part-notes").textContent).toContain("Validate ownership");
    expect(screen.getByTestId("focus-part-notes").textContent).not.toContain("Check upper bound");
    await waitFor(() => expect(screen.getByTestId("focus-code")).toBeTruthy());
    await waitFor(() => expect(screen.getByTestId("focus-inline-comment").textContent).toContain("Validate ownership"));
    fireEvent.click(screen.getByRole("button", { name: "Review notes" }));
    expect(screen.getByTestId("focus-notes-list").textContent).toContain("Check upper bound");
  });

  it("keeps the canonical /one URL while showing the first open part on a cold load", async () => {
    show({}, "/r/shop/one");
    expect(screen.getByRole("heading", { name: "can_view_order" })).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("focus-code")).toBeTruthy());
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/one");
  });

  it("says where a carried symbol came from on the part's own header (P11-3)", async () => {
    const moved = { ...START[0], state: "reviewed" as const, moved_from: { path: "app/old_auth.py" } };
    show({ initialTargets: [moved, START[1], START[2]] }, "/r/shop/one/can_view_order");
    expect((await screen.findByTestId("target-moved-from")).textContent).toBe("Moved from app/old_auth.py");
    cleanup();
    show({ initialTargets: [START[0], START[1], START[2]] }, "/r/shop/one/can_view_order");
    await screen.findByRole("heading", { name: "can_view_order" });
    expect(screen.queryByTestId("target-moved-from")).toBeNull();
  });

  it("never draws a rename as a move (P11-3)", async () => {
    const renamed = { ...START[0], state: "changed_since_review" as const, renamed_from: { path: "app/auth.py", symbol: "can_view" } };
    show({ initialTargets: [renamed, START[1], START[2]] }, "/r/shop/one/can_view_order");
    await screen.findByRole("heading", { name: "can_view_order" });
    expect(screen.queryByTestId("target-moved-from")).toBeNull();
  });

  it("starts Quick check on /one when all decision parts were judged, and then goes to Wrap", async () => {
    const already = START.map((row) => row.answer?.category === "decision" ? { ...row, state: "reviewed" as MarkState } : row);
    const onMark = vi.fn().mockResolvedValue(true);
    show({ initialTargets: already, onMark }, "/r/shop/one");
    expect(screen.getByRole("heading", { name: "order_total" })).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Looks good" })[0]);
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/wrap"));
    expect(onMark).toHaveBeenCalledWith(expect.objectContaining({ target_id: "order_total" }), "reviewed");
  });

  it("uses full pinned content for unchanged lines when it agrees with the patch", async () => {
    show();
    await waitFor(() => expect(screen.getByText("Whole function from the frozen revision")).toBeTruthy());
    expect(apiContents).toHaveBeenCalledWith("review-1", "app/auth.py", "rev1");
    expect(screen.getByTestId("focus-code").textContent).toContain("return True");
  });

  it("falls back to the patch when full source comes from a different revision", async () => {
    apiContents.mockImplementation(async (_id, path) => ({ path, revision_id: "old-revision", old_path: path, status: "modified", old_content: "", new_content: "Wrong code" }));
    show();
    await waitFor(() => expect(screen.getByTestId("focus-code")).toBeTruthy());
    expect(screen.getByText("Frozen patch · unchanged source may be omitted")).toBeTruthy();
    expect(screen.getByTestId("focus-code").textContent).not.toContain("Wrong code");
  });

  it("shows the part's recorded question, checked facts and its frozen patch", async () => {
    show();
    expect(screen.getByRole("heading", { name: "can_view_order" })).toBeTruthy();
    expect(screen.getByText("Should guests see these orders?")).toBeTruthy();
    expect(screen.getByText("Safeguard removed")).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("focus-code").textContent).toContain("return True"));
    expect(apiFile).toHaveBeenCalledWith("review-1", "app/auth.py", "rev1");
    fireEvent.click(screen.getByRole("button", { name: "Show whole change" }));
    expect(screen.getByText("Whole change · 2 files")).toBeTruthy();
    expect(screen.queryByText(/AI hinted at lines/)).toBeNull();
  });

  it("draws no Your call row when no question was recorded for the part", async () => {
    show({ projection: null });
    expect(screen.getByRole("heading", { name: "can_view_order" })).toBeTruthy();
    expect(screen.queryByTestId("focus-your-call")).toBeNull();
    expect(screen.queryByText(/No question was recorded/)).toBeNull();
    expect(screen.getByTestId("code-view-controls")).toBeTruthy();
  });

  it("refuses a patch that belongs to another revision", async () => {
    apiFile.mockResolvedValue({ path: "app/auth.py", revision_id: "rev-old", patch: PATCH, renderable: true, refusal: "", detail: "", degraded: [] });
    show();
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Code changed while loading"));
    expect(screen.queryByTestId("focus-code")).toBeNull();
  });

  it("shows only precisely anchored AI finding ranges and keeps the suggested note opt-in", async () => {
    const annotations = [{ id: "ai1", review_id: "canonical-review-id", revision_id: "rev1", unit_key: "sym:can_view_order", path: "app/auth.py", parent_id: "", kind: "suggestion", source: "ai_review", state: "open", finding_disposition: "open", body: "Review the changed permission result", anchored: true, anchor_exact: true, side: "additions", start_line: 2, end_line: 2 }] as Annotation[];
    show({ annotations });
    expect(screen.getByTestId("focus-ai-hint").textContent).toContain("AI hinted at line 2");
    await waitFor(() => expect(screen.getByTestId("focus-inline-comment").textContent).toContain("Review the changed permission result"));
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    const field = screen.getByRole("textbox", { name: "Say what should change" }) as HTMLTextAreaElement;
    expect(field.value).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "Use it" }));
    expect(field.value).toBe("Review the changed permission result");
  });

  it("saves the AI-linked suggested fix only after explicit opt-in", async () => {
    const annotations = [{ id: "ai-note", review_id: "canonical-review-id", revision_id: "rev1", unit_key: "sym:can_view_order", path: "app/auth.py", parent_id: "", kind: "suggestion", source: "ai_review", state: "open", body: "Check the permission result", anchored: true, anchor_exact: true, side: "additions", start_line: 2, end_line: 2 }] as Annotation[];
    const proposal = { id: "p1", base_revision_id: "rev1", path: "app/auth.py", target_unit_key: "sym:can_view_order", annotation_id: "ai-note", state: "proposed", patch: "-return True\n+return require_permission()" } as ReviewChangeProposal;
    const onNote = vi.fn().mockResolvedValue(true);
    show({ annotations, proposals: [proposal], onNote });
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    const input = screen.getByRole("textbox", { name: "Say what should change" });
    fireEvent.change(input, { target: { value: "Guard this path" } });
    const suggested = screen.getByTestId("focus-suggested-fix");
    expect(suggested.textContent).toContain("Suggested fix (AI)");
    fireEvent.click(screen.getByLabelText("Include a suggested fix"));
    fireEvent.click(screen.getByRole("button", { name: "Save note & next" }));
    await waitFor(() => expect(onNote).toHaveBeenCalledOnce());
    expect(onNote).toHaveBeenCalledWith(expect.anything(), "Guard this path\n\nAI suggested fix (unverified; explicitly included by reviewer):\n-return True\n+return require_permission()", false);
  });

  it("never offers an unrelated, stale, or unlinked patch as an AI suggestion", async () => {
    const annotations = [{ id: "ai-note", review_id: "canonical-review-id", revision_id: "rev1", unit_key: "sym:can_view_order", path: "app/auth.py", parent_id: "", kind: "suggestion", source: "ai_review", state: "open", body: "Check the permission result", anchored: true, anchor_exact: true, side: "additions", start_line: 2, end_line: 2 }] as Annotation[];
    const bad = { id: "p1", base_revision_id: "rev0", path: "app/auth.py", target_unit_key: "sym:can_view_order", annotation_id: "ai-note", state: "proposed", patch: "-old\n+new" } as ReviewChangeProposal;
    show({ annotations, proposals: [bad] });
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    expect(screen.queryByTestId("focus-suggested-fix")).toBeNull();
    expect(screen.getByRole("button", { name: "Use it" })).toBeTruthy();
  });

  it("suppresses AI findings and drafts when AI is off, retaining code evidence", async () => {
    const annotations = [{ id: "ai1", review_id: "canonical-review-id", revision_id: "rev1", unit_key: "sym:can_view_order", path: "app/auth.py", parent_id: "", kind: "suggestion", source: "ai_review", state: "open", body: "Consider a fix", anchored: true, anchor_exact: true, side: "additions", start_line: 2, end_line: 2 }] as Annotation[];
    show({ annotations, aiAvailable: false });
    expect(screen.queryByTestId("focus-ai-hint")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("focus-code")).toBeTruthy());
    expect(screen.queryByTestId("focus-inline-comment")).toBeNull();
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    expect(screen.queryByRole("button", { name: "Use it" })).toBeNull();
  });

  it("comments on a code line from the gutter + with the inline composer, anchored to that line", async () => {
    const onNote = vi.fn().mockResolvedValue(true);
    show({ onNote });
    await screen.findAllByRole("button", { name: "Comment on line 2" });
    fireEvent.click(document.querySelector<HTMLElement>('[data-line="2"][data-line-type="change-addition"] button')!);
    const field = await screen.findByRole("textbox", { name: "Comment text" });
    fireEvent.change(field, { target: { value: "Guests must not see this" } });
    fireEvent.click(screen.getByRole("button", { name: "Add comment" }));
    await waitFor(() => expect(onNote).toHaveBeenCalledWith(expect.objectContaining({ target_id: "can_view_order" }), "Guests must not see this", false, expect.objectContaining({ start: 2, end: 2, side: "additions" })));
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Comment text" })).toBeNull());
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/can_view_order");
  });

  it("hides AI-only Ask when AI help is off but retains checked facts", async () => {
    show({ aiAvailable: false });
    expect(screen.getByText("Safeguard removed")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Ask about this part" })).toBeNull();
    await waitFor(() => expect(screen.getByTestId("focus-code")).toBeTruthy());
  });
});

describe("Focus keyboard and part actions", () => {
  it("A marks once, then A on an already-good part moves forward without another write", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    show({ onMark });
    fireEvent.keyDown(window, { key: "a" });
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/apply_discount"));
    expect(onMark).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Previous part" }));
    fireEvent.keyDown(window, { key: "a" });
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/apply_discount"));
    expect(onMark).toHaveBeenCalledTimes(1);
  });

  it("names the agent that wrote the change, not always Claude Code", async () => {
    const codex = { ...OVERVIEW, revision: { ...OVERVIEW.revision, provenance_host: "codex" } } as ReviewOverview;
    show({ overview: codex, onNote: vi.fn().mockResolvedValue(true) });
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    expect(screen.getByRole("dialog", { name: "Note for Codex" })).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "Say what should change" }), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: /^Save note/ }));
    await waitFor(() => expect(screen.getByTestId("focus-tray").textContent).toContain("1 note for Codex"));
    expect(document.body.textContent).not.toContain("Claude Code");
  });

  it("names the commit in the Structure title only when the revision has one", async () => {
    show();
    fireEvent.keyDown(window, { key: "s" });
    expect(screen.getByRole("dialog", { name: "Structure · can_view_order" })).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    cleanup();
    show({ overview: { ...OVERVIEW, revision: { ...OVERVIEW.revision, head_sha: "4e1b9c2d77aa" } } });
    fireEvent.keyDown(window, { key: "s" });
    expect(screen.getByRole("dialog", { name: "Structure · can_view_order · checked in the code at 4e1b9c2" })).toBeTruthy();
  });

  it("S opens Structure, E toggles whole change, J/K navigate and C opens a note", async () => {
    show();
    fireEvent.keyDown(window, { key: "s" });
    expect(screen.getByRole("dialog", { name: /Structure/ })).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /Structure/ })).toBeNull());
    fireEvent.keyDown(window, { key: "e" });
    expect(screen.getByText("Whole change · 2 files")).toBeTruthy();
    fireEvent.keyDown(window, { key: "j" });
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/apply_discount");
    fireEvent.keyDown(window, { key: "k" });
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/can_view_order");
    fireEvent.keyDown(window, { key: "c" });
    expect(screen.getByRole("dialog", { name: "Note for Claude Code" })).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "Say what should change" }), { target: { value: "Can safely type a, j, k, s, e in the draft" } });
    for (const key of ["a", "j", "k", "s", "e"]) fireEvent.keyDown(screen.getByRole("textbox", { name: "Say what should change" }), { key });
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/can_view_order");
  });

  it("Ctrl+Enter saves a non-empty note through the same guarded write path", async () => {
    const onNote = vi.fn().mockResolvedValue(true);
    show({ onNote });
    fireEvent.keyDown(window, { key: "c" });
    const field = screen.getByRole("textbox", { name: "Say what should change" });
    fireEvent.change(field, { target: { value: "Check the permission" } });
    fireEvent.keyDown(field, { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(onNote).toHaveBeenCalledTimes(1));
    expect(onNote).toHaveBeenCalledWith(expect.objectContaining({ target_id: "can_view_order" }), "Check the permission", false);
  });

  it("offers the part's copy link as its own button, not a one-item menu", () => {
    show();
    expect(screen.getByRole("button", { name: "Copy link to this part" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Part actions" })).toBeNull();
  });
});

describe("ST-1: with AI help off the review still works", () => {
  it("A writes Looks good and moves on", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    show({ onMark, aiAvailable: false });
    fireEvent.keyDown(window, { key: "a" });
    await waitFor(() => expect(onMark).toHaveBeenCalledWith(expect.objectContaining({ target_id: "can_view_order" }), "reviewed"));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/apply_discount"));
  });

  it("C opens the note and Ctrl+Enter saves a human note", async () => {
    const onNote = vi.fn().mockResolvedValue(true);
    show({ onNote, aiAvailable: false });
    fireEvent.keyDown(window, { key: "c" });
    const field = screen.getByRole("textbox", { name: "Say what should change" });
    fireEvent.change(field, { target: { value: "Check the permission" } });
    fireEvent.keyDown(field, { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(onNote).toHaveBeenCalledWith(expect.objectContaining({ target_id: "can_view_order" }), "Check the permission", false));
  });

  it("N writes Decide later through the key", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    show({ onMark, aiAvailable: false });
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Decide later" })[0].hasAttribute("disabled")).toBe(false));
    fireEvent.keyDown(window, { key: "n" });
    await waitFor(() => expect(onMark).toHaveBeenCalledWith(expect.objectContaining({ target_id: "can_view_order" }), "later"));
  });

  it("a server that does not record Decide later keeps the button off with its reason, and N writes nothing", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    show({ onMark, aiAvailable: false, overview: { ...OVERVIEW, later_marks: false } });
    const button = screen.getAllByRole("button", { name: "Decide later" })[0];
    await waitFor(() => expect(screen.getByText("Decide later is not available on this server yet")).toBeTruthy());
    expect(button.getAttribute("aria-disabled")).toBe("true");
    expect(button.getAttribute("title")).toBe("Decide later is not available on this server yet");
    fireEvent.click(button);
    fireEvent.keyDown(window, { key: "n" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(onMark).not.toHaveBeenCalled();
  });

  it("Decide later writes later and moves the part to the end", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    show({ onMark, aiAvailable: false });
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Decide later" })[0].hasAttribute("disabled")).toBe(false));
    fireEvent.click(screen.getAllByRole("button", { name: "Decide later" })[0]);
    await waitFor(() => expect(onMark).toHaveBeenCalledWith(expect.objectContaining({ target_id: "can_view_order" }), "later"));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/apply_discount"));
  });

  it("Quick check is answered and goes to Wrap", async () => {
    const already = START.map((row) => row.answer?.category === "decision" ? { ...row, state: "reviewed" as MarkState } : row);
    const onMark = vi.fn().mockResolvedValue(true);
    show({ initialTargets: already, onMark, aiAvailable: false }, "/r/shop/one");
    expect(screen.getByRole("heading", { name: "order_total" })).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Looks good" })[0]);
    await waitFor(() => expect(onMark).toHaveBeenCalledWith(expect.objectContaining({ target_id: "order_total" }), "reviewed"));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/wrap"));
  });
});

describe("answering", () => {
  it("saves Looks good before advancing, and Back to this part returns", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    show({ onMark });
    fireEvent.click(screen.getAllByRole("button", { name: "Looks good" })[0]);
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/apply_discount"));
    expect(onMark).toHaveBeenCalledWith(expect.objectContaining({ target_id: "can_view_order" }), "reviewed");
    expect(screen.getByTestId("focus-confirmation").textContent).toContain("Looks good · next: apply_discount");
    fireEvent.click(screen.getByRole("button", { name: "Back to this part" }));
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/can_view_order");
  });

  it("opens an empty note composer, keeps a canceled draft and saves a human-written note", async () => {
    const onNote = vi.fn().mockResolvedValue(true);
    show({ onNote });
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    const textbox = screen.getByRole("textbox", { name: "Say what should change" }) as HTMLTextAreaElement;
    expect(textbox.value).toBe("");
    fireEvent.change(textbox, { target: { value: "Please restore the permission check." } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByText("Draft kept")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    expect((screen.getByRole("textbox", { name: "Say what should change" }) as HTMLTextAreaElement).value).toContain("restore");
    fireEvent.click(screen.getByRole("button", { name: "Save note & next" }));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/apply_discount"));
    expect(onNote).toHaveBeenCalledWith(expect.objectContaining({ target_id: "can_view_order" }), "Please restore the permission check.", false);
  });

  it("keeps drafts tied to their own part, never reusing one on another part", async () => {
    show();
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    fireEvent.change(screen.getByRole("textbox", { name: "Say what should change" }), { target: { value: "Only for can_view_order" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.keyDown(window, { key: "j" });
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    expect((screen.getByRole("textbox", { name: "Say what should change" }) as HTMLTextAreaElement).value).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.keyDown(window, { key: "k" });
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    expect((screen.getByRole("textbox", { name: "Say what should change" }) as HTMLTextAreaElement).value).toBe("Only for can_view_order");
  });

  it("restores a canceled draft for exactly its part after the screen remounts", async () => {
    const first = show();
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    fireEvent.change(screen.getByRole("textbox", { name: "Say what should change" }), { target: { value: "Kept across visits" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    first.unmount();
    show();
    expect(screen.getByText("Draft kept")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    expect((screen.getByRole("textbox", { name: "Say what should change" }) as HTMLTextAreaElement).value).toBe("Kept across visits");
  });

  it("after the last decision focuses Quick check and opens it as the next step", async () => {
    const already = START.map((row) => row.target_id === "can_view_order" ? { ...row, state: "reviewed" as MarkState } : row);
    show({ initialTargets: already }, "/r/shop/one/apply_discount");
    fireEvent.click(screen.getAllByRole("button", { name: "Looks good" })[0]);
    await waitFor(() => expect(screen.getByTestId("focus-interstitial")).toBeTruthy());
    const action = screen.getByRole("button", { name: /Quick check · 1 part/ });
    // SPEC 5.3: an interstitial moves focus to its heading, not to its action.
    expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: /risky parts judged/ }));
    expect(action).toBeTruthy();
    expect(screen.getByTestId("focus-interstitial").textContent).toContain("can_view_order");
    expect(screen.getByTestId("focus-interstitial").textContent).toContain("apply_discount");
    fireEvent.click(action);
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/order_total");
  });

  it("moves Decide later once and blocks another move on the same revision", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    show({ onMark });
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Decide later" })[0].hasAttribute("disabled")).toBe(false));
    fireEvent.click(screen.getAllByRole("button", { name: "Decide later" })[0]);
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/apply_discount"));
    fireEvent.keyDown(window, { key: "j" });
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Decide later" })[0].getAttribute("aria-disabled")).toBe("true"));
    expect(screen.getByText("You already moved this one to the end once")).toBeTruthy();
    expect(screen.queryByTestId("focus-interstitial")).toBeNull();
    expect(onMark).toHaveBeenCalledTimes(1);
  });

  it("does not advance on a failed mark", async () => {
    const onMark = vi.fn().mockResolvedValue(false);
    show({ onMark });
    fireEvent.click(screen.getAllByRole("button", { name: "Looks good" })[0]);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("not saved"));
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/can_view_order");
  });

  it("does not advance a queued hosted judgment before the server has applied it", async () => {
    const onMark = vi.fn().mockResolvedValue("queued" as const);
    show({ onMark });
    fireEvent.click(screen.getAllByRole("button", { name: "Looks good" })[0]);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("queued for hosted"));
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/can_view_order");
    expect(screen.queryByTestId("focus-confirmation")).toBeNull();
  });

  it("removes a queued note draft so retry cannot duplicate the hosted submission", async () => {
    const onNote = vi.fn().mockResolvedValue("queued" as const);
    show({ onNote });
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    fireEvent.change(screen.getByRole("textbox", { name: "Say what should change" }), { target: { value: "Queued note" } });
    fireEvent.click(screen.getByRole("button", { name: "Save note & next" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("queued for hosted"));
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/can_view_order");
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    expect((screen.getByRole("textbox", { name: "Say what should change" }) as HTMLTextAreaElement).value).toBe("");
    expect(onNote).toHaveBeenCalledTimes(1);
  });

  it("blocks writes from a partial list even when the part appears in the slice", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    show({ onMark, complete: false });
    expect(screen.getAllByRole("button", { name: "Looks good" })[0].hasAttribute("disabled")).toBe(true);
    expect(onMark).not.toHaveBeenCalled();
  });

  it("pauses for a checked signature change and allows clearing the answer", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    const projection = { ...PROJECTION, context: { consumers: [{ key: "caller", path: "app/auth.py", target_ids: ["can_view_order"], join: "exact", sites: [{ impact_kind: "signature_change", exactness: "snapshot", path: "app/views.py", snippet: "can_view_order(request)", in_patch: false, uncertainty: "" }] }] } } as unknown as ReviewProjection;
    show({ projection, onMark });
    fireEvent.click(screen.getAllByRole("button", { name: "Looks good" })[0]);
    await waitFor(() => expect(screen.getByTestId("focus-pause")).toBeTruthy());
    expect(onMark).toHaveBeenCalledWith(expect.objectContaining({ target_id: "can_view_order" }), "reviewed");
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/can_view_order");
    fireEvent.click(screen.getByRole("button", { name: "Change it" }));
    await waitFor(() => expect(onMark).toHaveBeenLastCalledWith(expect.objectContaining({ target_id: "can_view_order" }), "unreviewed"));
    expect(screen.queryByTestId("focus-pause")).toBeNull();
  });

  it("records a pausable Looks good before Keep my answer advances, as the spec requires", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    const projection = { ...PROJECTION, context: { consumers: [{ key: "caller", path: "app/auth.py", target_ids: ["can_view_order"], join: "exact", sites: [{ impact_kind: "signature_change", exactness: "snapshot", path: "app/views.py", snippet: "can_view_order(request)", in_patch: false, uncertainty: "" }] }] } } as unknown as ReviewProjection;
    show({ projection, onMark });
    fireEvent.click(screen.getAllByRole("button", { name: "Looks good" })[0]);
    await waitFor(() => expect(screen.getByTestId("focus-pause")).toBeTruthy());
    expect(onMark).toHaveBeenCalledWith(expect.objectContaining({ target_id: "can_view_order" }), "reviewed");
    fireEvent.click(screen.getByRole("button", { name: "Keep my answer" }));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/apply_discount"));
    expect(onMark).toHaveBeenCalledTimes(1);
  });

  it("records a human note after reconsidering checked Structure evidence", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    const onNote = vi.fn().mockResolvedValue(true);
    const projection = { ...PROJECTION, context: { consumers: [{ key: "caller", path: "app/auth.py", target_ids: ["can_view_order"], join: "exact", sites: [{ impact_kind: "signature_change", exactness: "snapshot", path: "app/views.py", snippet: "can_view_order(request)", in_patch: false, uncertainty: "" }] }] } } as unknown as ReviewProjection;
    show({ projection, onMark, onNote });
    fireEvent.click(screen.getAllByRole("button", { name: "Looks good" })[0]);
    await waitFor(() => expect(screen.getByTestId("focus-pause")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Change it" }));
    await waitFor(() => expect(onMark).toHaveBeenLastCalledWith(expect.objectContaining({ target_id: "can_view_order" }), "unreviewed"));
    fireEvent.click(screen.getAllByRole("button", { name: "Needs a change" })[0]);
    expect(screen.getByText("· changed after seeing structure")).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "Say what should change" }), { target: { value: "Update the caller too" } });
    fireEvent.click(screen.getByRole("button", { name: "Save note & next" }));
    await waitFor(() => expect(onNote).toHaveBeenCalledOnce());
    expect(onNote).toHaveBeenCalledWith(expect.objectContaining({ target_id: "can_view_order" }), "Update the caller too", true);
  });

  it("does not show the pause if the initial positive mark failed", async () => {
    const onMark = vi.fn().mockResolvedValue(false);
    const projection = { ...PROJECTION, context: { consumers: [{ key: "caller", path: "app/auth.py", target_ids: ["can_view_order"], join: "exact", sites: [{ impact_kind: "signature_change", exactness: "snapshot", path: "app/views.py", snippet: "can_view_order(request)", in_patch: false, uncertainty: "" }] }] } } as unknown as ReviewProjection;
    show({ projection, onMark });
    fireEvent.click(screen.getAllByRole("button", { name: "Looks good" })[0]);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("not saved"));
    expect(screen.queryByTestId("focus-pause")).toBeNull();
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/can_view_order");
  });
});
