import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Annotation, MarkState, ReviewOverview, ReviewTarget } from "../types";
import { fetchFile, fetchFileContents, requestBrowserJson } from "../reviewApi";
import { fetchCheckRecipes, postCheckRecipesRun, type CheckRecipesProjection, type CheckRunResponse } from "../checkRecipesApi";
import { FlowPaletteProvider } from "../palette/FlowPalette";
import { ReviewQuickCheckScreen } from "./ReviewQuickCheckScreen";
import { reviewQueue } from "../queue";
import { guardContext, resetGuardLatch } from "./quickCheckModel";
import { resetPatchEvidence } from "./patchEvidence";
import { resetPartTestRuns } from "../partTestRuns";
import { resetPartProof } from "../partProof";

vi.mock("../reviewApi", () => ({ fetchFile: vi.fn(), fetchFileContents: vi.fn(), requestBrowserJson: vi.fn() }));
vi.mock("../home/homeApi", () => ({ useHomeSource: () => ({ load: vi.fn(async () => ({ rows: [], teamsSupported: false, truncated: false, queueSupported: false })), snooze: vi.fn(async () => ({})), terminal: true }) }));
vi.mock("../checkRecipesApi", () => ({ fetchCheckRecipes: vi.fn(), postCheckRecipesRun: vi.fn() }));
vi.mock("@pierre/diffs", () => import("../focus/pierreTestMock").then((m) => m.pierreCore));
vi.mock("@pierre/diffs/react", () => import("../focus/pierreTestMock").then((m) => m.pierreReact));

const apiFile = vi.mocked(fetchFile);
const apiContents = vi.mocked(fetchFileContents);
const apiChecks = vi.mocked(fetchCheckRecipes);
const apiRun = vi.mocked(postCheckRecipesRun);
const apiProof = vi.mocked(requestBrowserJson);
const proofRecord = (files: Record<string, { o: string; r?: string; t?: string[]; n?: number }>) => ({
  revision_id: "rev1", head_identity: "head", base_identity: "base", reach: "file", default: { o: "not_tested" }, files, recorded: true, complete: true, running: false,
  source: "auto", labels: {}, details: {}, reasons: { no_sandbox: "no sandbox is available here, and tests never run unsandboxed on their own" },
});
const CHECKS = { revision_id: "rev1", results: [], part_checks: [] } as unknown as CheckRecipesProjection;
const MAPPED = { unit_key: "sym:order_total", path: "app/orders.py", symbol: "order_total", fingerprint: "code:sha", entry_id: "checks.recipe:orders", recipe_id: "orders", recipe_digest: "recipe:sha", title: "Orders regression", mapping_kind: "explicit_trusted_base", coverage: "not_assessed" } as const;

function target(id: string, path = "app/orders.py", rest: Partial<ReviewTarget> = {}): ReviewTarget {
  return {
    target_id: id, unit_key: `sym:${id}`, kind: "symbol", path, label: id, symbol: id,
    start_line: 1, end_line: 3, hunk_ordinals: [0], spans: [], state: "unreviewed",
    changed_since_mark: false, reviewed_revision_id: "", attention_rank: 1, attention_level: "normal",
    reasons: [], additions: 1, deletions: 1, fingerprint_method: "ast",
    verification: { pass: 0, fail: 0, unknown: 1 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    answer: { category: "quick_check", rule: "", rule_label: "", reason: "" },
    priority: { version: 1, tier: 3, attention: "routine", outstanding: true, position: 1, reasons: [] },
    // The server's classification (batch_guard.py): nothing in these fixtures touches arithmetic unless it says so.
    batch_guard: { guarded: false, code: "", reason: "" },
    ...rest,
  };
}

const ORDER = target("order_total", "app/orders.py", { batch_guard: { guarded: true, code: "loop_bound", reason: "Test didn't run and the loop bound changed." } });
const LABEL = target("label", "app/labels.py", { attention_rank: 2, priority: { ...ORDER.priority!, position: 2 } });
const DOC = target("guide", "docs/guide.md", { attention_rank: 3, priority: { ...ORDER.priority!, position: 3 } });
const OVERVIEW = { title: "Review the change", session: { id: "canonical-review-id", ref: "shop" }, revision: { id: "rev1", revision_number: 1 } } as ReviewOverview;
const PATCHES: Record<string, string> = {
  "app/orders.py": "@@ -1,3 +1,3 @@\n def order_total():\n-    for i in range(5):\n+    for i in range(4):\n     pass\n",
  "app/labels.py": "@@ -1,3 +1,3 @@\n def label():\n-    status = old\n+    status = new\n     pass\n",
  "docs/guide.md": "@@ -1,3 +1,3 @@\n # Guide\n-Status: old\n+Status: new\n Notes\n",
};

function Location() {
  const { pathname } = useLocation();
  return <output data-testid="location">{pathname}</output>;
}

function Harness({ initialTargets = [ORDER], annotations = [], complete = true, readOnly = false,
  onMark = vi.fn().mockResolvedValue(true), onNote = vi.fn().mockResolvedValue(true), aiAvailable = true,
}: {
  initialTargets?: ReviewTarget[]; annotations?: Annotation[]; complete?: boolean; readOnly?: boolean;
  onMark?: (target: ReviewTarget, state: MarkState) => Promise<boolean | "queued">;
  onNote?: (target: ReviewTarget, body: string, reconsideredAfterStructure: boolean) => Promise<boolean | "queued">;
  aiAvailable?: boolean;
}) {
  const [targets, setTargets] = useState(initialTargets);
  const { pathname } = useLocation();
  const part = pathname.startsWith("/r/shop/one/") ? decodeURIComponent(pathname.slice("/r/shop/one/".length)) : "";
  return <>
    <ReviewQuickCheckScreen
      reviewRef="shop" apiReviewId="review-1" part={part} overview={OVERVIEW}
      targets={targets} annotations={annotations} complete={complete} readOnly={readOnly}
      aiAvailable={aiAvailable}
      onMark={async (target, state) => {
        const result = await onMark(target, state);
        if (result === true) setTargets((before) => before.map((row) => row.target_id === target.target_id ? { ...row, state } : row));
        return result;
      }}
      onNote={async (target, body, reconsidered) => {
        const result = await onNote(target, body, reconsidered);
        if (result === true) setTargets((before) => before.map((row) => row.target_id === target.target_id ? { ...row, state: "needs_changes" } : row));
        return result;
      }}
    />
    <Location />
  </>;
}

function show(props: Parameters<typeof Harness>[0] = {}, route = "/r/shop/one/order_total") {
  return render(<MemoryRouter initialEntries={[route]}><Harness {...props} /></MemoryRouter>);
}

beforeEach(() => {
  window.localStorage.clear();
  resetGuardLatch();
  resetPatchEvidence();
  resetPartTestRuns();
  resetPartProof();
  apiProof.mockReset().mockRejectedValue(new Error("offline"));
  apiFile.mockReset().mockImplementation(async (_id, path) => ({
    path, revision_id: "rev1", patch: PATCHES[path] ?? "", renderable: true, refusal: "", detail: "", degraded: [],
  }));
  apiContents.mockReset().mockImplementation(async (_id, path) => ({
    path, revision_id: "rev1", old_path: path, status: "modified", old_content: "", new_content: null,
  }));
  apiChecks.mockReset().mockResolvedValue(CHECKS);
  apiRun.mockReset();
});
afterEach(cleanup);

describe("P11-1 Quick check rows show the part's proof", () => {
  it("every row reads its outcome from one record, and a Couldn't run row says why in visible text", async () => {
    apiProof.mockResolvedValue(proofRecord({ "app/orders.py": { o: "couldnt_run", r: "no_sandbox" }, "app/labels.py": { o: "exercised", t: ["tests/test_labels.py"], n: 1 } }) as never);
    show({ initialTargets: [ORDER, LABEL, DOC] });
    await waitFor(() => expect(screen.getAllByTestId("part-proof").length).toBeGreaterThanOrEqual(2));
    const outcomes = screen.getAllByTestId("part-proof").map((node) => node.getAttribute("data-outcome"));
    expect(outcomes).toContain("couldnt_run");
    expect(outcomes).toContain("exercised");
    expect(outcomes).toContain("not_tested");
    // The reason is text on the row, not only a hover title.
    expect(screen.getAllByTestId("part-proof-reason")[0].textContent).toBe("Couldn't run: no sandbox is available here, and tests never run unsandboxed on their own.");
    expect(apiProof.mock.calls.filter(([path]) => String(path).includes("/part-proof?"))).toHaveLength(1);
  });
});

describe("P2-3 Quick check screen", () => {
  it("P4-2 K: a part the server holds because a line moved into another block has its own card and the reason", async () => {
    const moved = target("moved", "app/moved.py", { batch_guard: { guarded: true, code: "block_moved", reason: "Test didn't run and a line moved into a different block." } });
    show({ initialTargets: [moved] }, "/r/shop/one/moved");
    await waitFor(() => expect(screen.getByTestId("quick-guard-reason").textContent).toBe("Test didn't run and a line moved into a different block."));
    expect(within(screen.getByTestId("quick-guarded-card")).getByRole("heading", { name: "moved" })).toBeTruthy();
    expect(screen.queryByTestId("quick-batch-group")).toBeNull();
  });

  it("counts only the guarded parts still waiting for an answer", async () => {
    const guard = { batch_guard: { guarded: true, code: "block_moved", reason: "A line moved." } as never };
    show({ initialTargets: [target("open_one", "app/a.py", guard), target("done_one", "app/b.py", { ...guard, state: "reviewed" })] });
    expect(await screen.findByRole("heading", { name: /Needs its own answer · 1/ })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "One part needs its own answer." })).toBeTruthy();
  });

  it("review-8.1: with other quick checks still open, the heading counts against the same total as the tray", async () => {
    const guard = { batch_guard: { guarded: true, code: "block_moved", reason: "A line moved." } as never };
    show({ initialTargets: [target("held", "app/a.py", guard), target("safe_one", "app/b.py"), target("safe_two", "app/c.py")] });
    expect(await screen.findByRole("heading", { name: "1 of 3 parts needs its own answer." })).toBeTruthy();
    expect(screen.getByTestId("quick-tray").textContent).toContain("3 parts left to confirm");
  });

  it("a change with a hundred parts that each need an answer draws a window of cards around the one in hand", async () => {
    const many = Array.from({ length: 100 }, (_, index) => target(`part_${index}`, `app/f${index}.py`, { batch_guard: { guarded: true, code: "block_moved", reason: "A line moved." } as never }));
    show({ initialTargets: many }, "/r/shop/one/part_60");
    await waitFor(() => expect(screen.getAllByTestId("quick-guarded-card").length).toBe(20));
    expect(screen.getByTestId("quick-window-note").textContent).toContain("20 of 100");
    // The window is around the part in hand, and the count in the heading is still the whole.
    expect(within(screen.getAllByTestId("quick-guarded-card")[10]).getByRole("heading", { name: "part_60" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: /Needs its own answer · 100/ })).toBeTruthy();
  });

  it("a change of 150 files reads every patch, as before", async () => {
    const small = Array.from({ length: 150 }, (_, index) => target(`unit_${index}`, `app/s${index}.py`, { batch_guard: { guarded: false, code: "", reason: "" } as never }));
    apiFile.mockImplementation(async (_id, path) => ({ path, revision_id: "rev1", patch: PATCHES["app/labels.py"], renderable: true, refusal: "", detail: "", degraded: [] }));
    show({ initialTargets: small, complete: true }, "/r/shop/one/unit_75");
    await waitFor(() => expect(new Set(apiFile.mock.calls.map(([, path]) => path)).size).toBe(150));
  });

  it("a change of a thousand files reads patches for a window and look-ahead around the part in hand, not one per fifty files", async () => {
    const big = Array.from({ length: 1000 }, (_, index) => target(`unit_${String(index).padStart(4, "0")}`, `app/f${String(index).padStart(4, "0")}.py`, { batch_guard: { guarded: false, code: "", reason: "" } as never }));
    apiFile.mockImplementation(async (_id, path) => ({ path, revision_id: "rev1", patch: PATCHES["app/labels.py"], renderable: true, refusal: "", detail: "", degraded: [] }));
    show({ initialTargets: big, complete: true }, "/r/shop/one/unit_0500");
    await waitFor(() => expect(apiFile.mock.calls.some(([, path]) => path === "app/f0500.py")).toBe(true));
    await waitFor(() => expect(screen.getAllByTestId("quick-batch-group").length).toBeGreaterThan(0));
    const paths = new Set(apiFile.mock.calls.map(([, path]) => path));
    // The code shown for the part in hand reads its own patch too, so the calls may exceed the files by that one.
    expect(apiFile.mock.calls.length).toBeLessThanOrEqual(paths.size + 1);
    expect(paths.size).toBeLessThanOrEqual(30);
    expect(paths.has("app/f0000.py")).toBe(false);
    expect(paths.has("app/f0999.py")).toBe(false);
  });

  it("asks for the part in hand's patch alone, and for its neighbours' only once that patch has landed", async () => {
    const big = Array.from({ length: 1000 }, (_, index) => target(`unit_${String(index).padStart(4, "0")}`, `app/f${String(index).padStart(4, "0")}.py`, { batch_guard: { guarded: false, code: "", reason: "" } as never }));
    let land: () => void = () => {};
    const landed = new Promise<void>((resolve) => { land = resolve; });
    apiFile.mockImplementation(async (_id, path) => {
      if (path === "app/f0500.py") await landed;
      return { path, revision_id: "rev1", patch: PATCHES["app/labels.py"], renderable: true, refusal: "", detail: "", degraded: [] };
    });
    show({ initialTargets: big, complete: true }, "/r/shop/one/unit_0500");
    await waitFor(() => expect(apiFile.mock.calls.some(([, path]) => path === "app/f0500.py")).toBe(true));
    await new Promise((resolve) => setTimeout(resolve, 100));
    // The part in hand is still loading: no neighbour has been asked for yet.
    expect(new Set(apiFile.mock.calls.map(([, path]) => path))).toEqual(new Set(["app/f0500.py"]));
    land();
    await waitFor(() => expect(new Set(apiFile.mock.calls.map(([, path]) => path)).size).toBeGreaterThan(10));
    expect(new Set(apiFile.mock.calls.map(([, path]) => path)).size).toBeLessThanOrEqual(30);
  });

  describe("a thousand files: the window follows the answers", () => {
    const SAFE = { guarded: false, code: "", reason: "" } as never;
    const name = (index: number) => String(index).padStart(4, "0");
    const thousand = () => Array.from({ length: 1000 }, (_, index) => target(`unit_${name(index)}`, `app/f${name(index)}.py`, { batch_guard: SAFE }));
    const fetchedPaths = () => apiFile.mock.calls.map(([, path]) => path);
    const countOf = (path: string) => fetchedPaths().filter((item) => item === path).length;
    beforeEach(() => {
      apiFile.mockImplementation(async (_id, path) => ({ path, revision_id: "rev1", patch: PATCHES["app/labels.py"], renderable: true, refusal: "", detail: "", degraded: [] }));
    });
    const confirmWholeGroup = async () => {
      const batch = await screen.findByTestId("quick-batch-group", {}, { timeout: 5000 });
      await userEvent.click(within(batch).getByRole("button", { name: /Show all/ }));
      await userEvent.click(within(batch).getByRole("button", { name: /Confirm \d+ together/ }));
    };

    // A change past QUICK_PATCH_ALL_FILES reads only the files around the part in hand. That decides what is drawn, never who is in the batch.
    const large = () => Array.from({ length: 260 }, (_, index) => target(`unit_${name(index)}`, `app/f${name(index)}.py`, { batch_guard: SAFE }));

    it("keeps every part the server proved safe in the batch, read or not, and reads the rest when the group is confirmed", async () => {
      const onMark = vi.fn().mockResolvedValue(true);
      show({ initialTargets: large(), complete: true, onMark }, "/r/shop/one/unit_0130");
      await waitFor(() => expect(countOf("app/f0149.py")).toBeGreaterThan(0));
      // Only the files around the part in hand were read, yet the whole change is one group of 260.
      expect(new Set(fetchedPaths()).size).toBeLessThanOrEqual(31);
      expect(countOf("app/f0000.py")).toBe(0);
      expect(await screen.findByRole("heading", { name: /^app · 260$/ })).toBeTruthy();
      expect(screen.queryAllByTestId("quick-guarded-card")).toHaveLength(0);
      await confirmWholeGroup();
      await waitFor(() => expect(onMark).toHaveBeenCalledTimes(260), { timeout: 20000 });
      // Every file was read before anything was marked, and none more than the code shown for the part in hand needs.
      expect(new Set(fetchedPaths()).size).toBe(260);
      for (const path of new Set(fetchedPaths())) expect(countOf(path)).toBeLessThanOrEqual(2);
    }, 30000);

    it.each([199, 200, 201])("the Overview's queue counts the Quick check group, with %i files and only a window read", async (files) => {
      const mixed = Array.from({ length: files }, (_, index) => target(`unit_${name(index)}`, `app/f${name(index)}.py`, { batch_guard: index % 6 === 5 ? { guarded: true, code: "loop_bound", reason: "The loop bound changed." } as never : SAFE }));
      const hint = { id: "h", review_id: OVERVIEW.session.id, revision_id: "rev1", relation_to_revision: "current", source: "ai_review", state: "open", finding_disposition: "open", unit_key: mixed[7].unit_key, path: mixed[7].path, anchored: true, anchor_exact: true, file_level: false, start_line: 1, end_line: 1, side: "additions", body: "This changes the result." } as unknown as Annotation;
      // What the Overview counts: the queue over the same inputs it is given (the session id, the notes, nothing read yet).
      const context = guardContext({ reviewId: OVERVIEW.session.id, annotationReviewId: OVERVIEW.session.id, revisionId: "rev1", parts: mixed, annotations: [hint], failedPaths: new Set() });
      const confirm = reviewQueue(mixed, "rev1", undefined, context).confirm.length;
      show({ initialTargets: mixed, annotations: [hint], complete: true }, "/r/shop/one/unit_0100");
      const group = await screen.findByRole("heading", { name: /^app · \d+$/ });
      expect(Number(/(\d+)$/.exec(group.textContent ?? "")?.[1])).toBe(confirm);
      expect(confirm).toBeGreaterThan(0);
    });

    it("takes a part whose code cannot be read out of the batch when it is confirmed, and marks nothing", async () => {
      apiFile.mockImplementation(async (_id, path) => {
        if (path === "app/f0001.py") throw new Error("gone");
        return { path, revision_id: "rev1", patch: PATCHES["app/labels.py"], renderable: true, refusal: "", detail: "", degraded: [] };
      });
      const onMark = vi.fn().mockResolvedValue(true);
      show({ initialTargets: large(), complete: true, onMark }, "/r/shop/one/unit_0130");
      await screen.findByRole("heading", { name: /^app · 260$/ });
      await confirmWholeGroup();
      await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Batch blocked: the code of 1 part could not be read here"));
      expect(onMark).not.toHaveBeenCalled();
      // The part is out of the group and has its own card with the reason.
      await screen.findByRole("heading", { name: /^app · 259$/ });
    });

    it("with no part in the route, A marks a part that is drawn, and the cards move on as the drawn ones are answered", async () => {
      // Parts 0..29 are safe (grouped); 30..99 each need their own answer, so they are the cards.
      const mixed = thousand().map((part, index) => (index >= 30 && index < 100 ? { ...part, batch_guard: { guarded: true, code: "block_moved", reason: "A line moved." } as never } : part));
      const onMark = vi.fn().mockResolvedValue(true);
      show({ initialTargets: mixed, complete: true, onMark }, "/r/shop/one/");
      const headings = () => screen.getAllByTestId("quick-guarded-card").map((card) => within(card).getByRole("heading").textContent);
      await waitFor(() => expect(headings()).toContain("unit_0030"));
      for (let answered = 1; answered <= 20; answered += 1) {
        fireEvent.keyDown(window, { key: "a" });
        await waitFor(() => expect(onMark).toHaveBeenCalledTimes(answered));
        expect(headings()).toContain(onMark.mock.calls[answered - 1][0].target_id);
      }
      // The twenty drawn cards are answered: the next part to answer is drawn, and so is the one A marks.
      await waitFor(() => expect(headings()).toContain("unit_0050"));
      const drawn = headings();
      fireEvent.keyDown(window, { key: "a" });
      await waitFor(() => expect(onMark).toHaveBeenCalledTimes(21));
      expect(drawn).toContain(onMark.mock.calls[20][0].target_id);
      expect(onMark.mock.calls[20][0].target_id).toBe("unit_0050");
    });

    it("with the route on an answered part, A marks a part that is drawn and leaves the routed part alone", async () => {
      const held = thousand().map((part) => ({ ...part, batch_guard: { guarded: true, code: "block_moved", reason: "A line moved." } as never }))
        .map((part) => (part.target_id === "unit_0500" ? { ...part, state: "needs_changes" as const } : part));
      const onMark = vi.fn().mockResolvedValue(true);
      show({ initialTargets: held, complete: true, onMark }, "/r/shop/one/unit_0500");
      const cards = () => screen.getAllByTestId("quick-guarded-card");
      const headings = () => cards().map((card) => within(card).getByRole("heading").textContent);
      await waitFor(() => expect(cards().length).toBe(20));
      for (let answered = 1; answered <= 30; answered += 1) {
        const open = cards().find((card) => within(card).queryByText("Not checked yet"));
        await userEvent.click(within(open!).getByRole("button", { name: "Looks good" }));
        await waitFor(() => expect(onMark).toHaveBeenCalledTimes(answered));
      }
      const drawn = headings();
      expect(drawn).not.toContain("unit_0500");
      fireEvent.keyDown(window, { key: "a" });
      await waitFor(() => expect(onMark).toHaveBeenCalledTimes(31));
      const marked = onMark.mock.calls[30][0].target_id;
      expect(drawn).toContain(marked);
      expect(onMark.mock.calls.some(([part]) => part.target_id === "unit_0500")).toBe(false);
    });

    it("when the parts after the part in hand are not held back, the cards wrap round to those held back before it", async () => {
      // 0..29 are held back and open, 30..994 are answered, 995..999 are open and safe once read; the route is 997.
      const mixed = thousand().map((part, index) => {
        if (index < 30) return { ...part, batch_guard: { guarded: true, code: "block_moved", reason: "A line moved." } as never };
        if (index < 995) return { ...part, state: "reviewed" as const };
        return part;
      });
      show({ initialTargets: mixed, complete: true }, "/r/shop/one/unit_0997");
      await waitFor(() => expect(countOf("app/f0997.py")).toBeGreaterThan(0));
      await waitFor(() => expect(screen.getAllByTestId("quick-batch-group").length).toBeGreaterThan(0));
      const drawn = screen.getAllByTestId("quick-guarded-card").map((card) => within(card).getByRole("heading").textContent);
      expect(drawn).toContain("unit_0000");
    });

    it("Show all counts the parts still to answer in the group, not every row", async () => {
      const group = Array.from({ length: 40 }, (_, index) => target(`unit_${String(index).padStart(2, "0")}`, "app/labels.py", index < 10 ? { state: "reviewed" as const } : {}));
      show({ initialTargets: group, complete: true }, "/r/shop/one/unit_10");
      const batch = await screen.findByTestId("quick-batch-group", {}, { timeout: 5000 });
      expect(within(batch).getByTestId("quick-group-more").textContent).toContain("10 more in this group are not listed");
      expect(within(batch).getByRole("button", { name: "Show all 30" })).toBeTruthy();
    });

    it("a part whose code could not be read leaves the group with the reason, here as in the Overview's count", async () => {
      apiFile.mockImplementation(async (_id, path) => { throw new Error(`no ${path}`); });
      show({ initialTargets: large(), complete: true }, "/r/shop/one/unit_0130");
      await waitFor(() => expect(screen.getAllByTestId("quick-guard-reason").map((item) => item.textContent)).toContain("The frozen code evidence is unavailable or belongs to another revision; batch confirmation is unavailable."));
      // The files read and unusable are out; the ones never read stay in until they are read.
      expect(screen.queryByRole("heading", { name: /^app · 260$/ })).toBeNull();
      expect(screen.getByRole("heading", { name: /^app · \d+$/ })).toBeTruthy();
    });

    it("a long confirmation keeps the rows drawn bounded", async () => {
      show({ initialTargets: large(), complete: true }, "/r/shop/one/unit_0130");
      const batch = await screen.findByTestId("quick-batch-group", {}, { timeout: 5000 });
      await userEvent.click(within(batch).getByRole("button", { name: /Confirm \d+ together/ }));
      await waitFor(() => expect(within(screen.getByTestId("quick-batch-group")).getAllByText("Confirmed").length).toBeGreaterThan(0), { timeout: 20000 });
      // Answered rows are capped at one window, however many have been confirmed.
      expect(within(screen.getByTestId("quick-batch-group")).queryAllByText("Confirmed").length).toBeLessThanOrEqual(20);
    }, 30000);

    it("keeps the code already read when the set of files changes, and reads only what is missing", async () => {
      const props = (targets: ReviewTarget[]) => ({
        reviewRef: "shop", apiReviewId: "review-1", part: "unit_0130", overview: OVERVIEW, targets, annotations: [], complete: true,
        aiAvailable: false, readOnly: false, onMark: async () => true as const, onNote: async () => true as const,
      });
      const before = large();
      const view = render(<MemoryRouter><ReviewQuickCheckScreen {...props(before)} /></MemoryRouter>);
      await waitFor(() => expect(countOf("app/f0130.py")).toBeGreaterThan(0));
      await waitFor(() => expect(countOf("app/f0149.py")).toBeGreaterThan(0));
      const first = fetchedPaths().length;
      view.rerender(<MemoryRouter><ReviewQuickCheckScreen {...props([...before, target("unit_0260", "app/f0260.py", { batch_guard: SAFE })])} /></MemoryRouter>);
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
      expect(fetchedPaths().length).toBeLessThanOrEqual(first + 1);
    });
  });

  it("a group of forty parts lists twenty, says Confirm together covers all forty, and Show all lists every one", async () => {
    const group = Array.from({ length: 40 }, (_, index) => target(`unit_${String(index).padStart(2, "0")}`, "app/labels.py"));
    show({ initialTargets: group, complete: true }, "/r/shop/one/unit_00");
    const batch = await screen.findByTestId("quick-batch-group", {}, { timeout: 5000 });
    expect(within(batch).getAllByRole("button", { name: /View code/ }).length).toBe(20);
    expect(within(batch).getByTestId("quick-group-more").textContent).toContain("covers only the 20 listed");
    // Until the rest are shown, Confirm together says 20 and confirms only the 20 drawn.
    expect(within(batch).getByRole("button", { name: /Confirm 20 together/ })).toBeTruthy();
    await userEvent.click(within(batch).getByRole("button", { name: "Show all 40" }));
    expect(within(batch).getAllByRole("button", { name: /View code/ }).length).toBe(40);
    expect(within(batch).queryByTestId("quick-group-more")).toBeNull();
    expect(within(batch).getByRole("button", { name: /Confirm 40 together/ })).toBeTruthy();
  });

  it("Confirm together on a group of forty confirms only the twenty listed", async () => {
    const group = Array.from({ length: 40 }, (_, index) => target(`unit_${String(index).padStart(2, "0")}`, "app/labels.py"));
    const onMark = vi.fn().mockResolvedValue(true);
    show({ initialTargets: group, complete: true, onMark }, "/r/shop/one/unit_00");
    const batch = await screen.findByTestId("quick-batch-group", {}, { timeout: 5000 });
    await userEvent.click(within(batch).getByRole("button", { name: /Confirm 20 together/ }));
    await waitFor(() => expect(onMark).toHaveBeenCalledTimes(20));
    const marked = new Set(onMark.mock.calls.map(([part]) => (part as ReviewTarget).target_id));
    expect(marked.has("unit_19")).toBe(true);
    expect(marked.has("unit_20")).toBe(false);
  });

  it("QC-1: an untested loop-bound part has an individual card, a reason and NO batch action", async () => {
    show();
    await waitFor(() => expect(screen.getByTestId("quick-guard-reason").textContent).toContain("loop bound"));
    const card = screen.getByTestId("quick-guarded-card");
    expect(within(card).getByRole("heading", { name: "order_total" })).toBeTruthy();
    expect(within(card).getByText("No AI check")).toBeTruthy();
    expect(within(card).getByText("Test did not run here")).toBeTruthy();
    expect(screen.queryByTestId("quick-batch-group")).toBeNull();
    expect(within(card).getByRole("button", { name: "Looks good" })).toBeTruthy();
    expect(within(card).getByRole("button", { name: "Needs a change" })).toBeTruthy();
    expect(within(card).queryByRole("button", { name: /Decide later/ })).toBeNull();
  });

  it("QC-2/QC-3: exact AI hint links to its code line; saving a typed note makes a human answer", async () => {
    const note = { id: "a1", review_id: "canonical-review-id", revision_id: "rev1", parent_id: "", source: "ai_review",
      relation_to_revision: "originating", state: "open", kind: "request_change", body: "Counts every item but skips the last item",
      unit_key: "sym:order_total", path: "app/orders.py", start_line: 2, end_line: 2, side: "additions", anchor_exact: true, anchored: true, file_level: false,
    } as Annotation;
    const onNote = vi.fn().mockResolvedValue(true);
    show({ annotations: [note], onNote });
    await waitFor(() => expect(screen.getByText(/Counts every item but skips the last item/)).toBeTruthy());
    expect(screen.getByRole("button", { name: "based on app/orders.py 2 · open line" })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Needs a change" }));
    const area = screen.getByRole("textbox", { name: "Say what should change" });
    expect(area).toHaveValue("");
    await userEvent.type(area, "Please cover the final item");
    await userEvent.click(screen.getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(onNote).toHaveBeenCalledWith(expect.objectContaining({ target_id: "order_total" }), "Please cover the final item", false));
    await waitFor(() => expect(screen.getByText("All quick checks confirmed")).toBeTruthy());
    expect(screen.queryByText("AI verified")).toBeNull();
  });

  it("QC-3: Wrap up works while unconfirmed; the screen never records it as reviewed", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    show({ onMark });
    expect(screen.getByTestId("quick-tray").textContent).toContain("1 part left to confirm");
    await userEvent.click(within(screen.getByTestId("quick-tray")).getByRole("button", { name: /^Wrap up/ }));
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/wrap");
    expect(onMark).not.toHaveBeenCalled();
  });

  it("QC-4: Enter after an individual answer navigates to Wrap up", async () => {
    show();
    await userEvent.click(screen.getByRole("button", { name: "Looks good" }));
    await waitFor(() => expect(screen.getByText("All quick checks confirmed")).toBeTruthy());
    fireEvent.keyDown(window, { key: "Enter" });
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/wrap"));
  });

  it("QC-5: a real mapped test passes with neutral coverage wording, never unlocks untested arithmetic", async () => {
    apiChecks.mockResolvedValue({ ...CHECKS, part_checks: [MAPPED] });
    const onMark = vi.fn().mockResolvedValue(true);
    apiRun.mockResolvedValue({
      revision_id: "rev1", entry_id: MAPPED.entry_id, verdicts: { [MAPPED.entry_id]: "PASS" },
      executions: 1, reused: 0, notes: [], executor_class: "local", run_on: "local",
    } as CheckRunResponse);
    show({ onMark });
    await waitFor(() => expect(screen.getByRole("button", { name: "Run test" })).toBeTruthy());
    await userEvent.click(screen.getByRole("button", { name: "Run test" }));
    await waitFor(() => expect(screen.getByTestId("quick-test-result").textContent).toContain("Orders regression · passed · A pass does not show the changed lines are checked."));
    expect(screen.getByText("Test passed · coverage not assumed")).toBeTruthy();
    expect(screen.getByTestId("quick-guard-reason").textContent).toBe("Test passed, but nothing shows it checks the changed lines. Answer for this part.");
    expect(screen.getByTestId("quick-guarded-card")).toBeTruthy();
    expect(screen.queryByTestId("quick-batch-group")).toBeNull();
    expect(onMark).not.toHaveBeenCalled();
    expect(apiRun).toHaveBeenCalledWith("review-1", "rev1", MAPPED.entry_id, true, "", "sym:order_total", true);
  });

  it("QC-6: only verified non-guarded parts receive batch confirmation and Undo", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    show({ initialTargets: [LABEL, DOC, ORDER], onMark }, "/r/shop/one/label");
    await waitFor(() => expect(screen.getAllByTestId("quick-batch-group")).toHaveLength(2));
    expect(screen.getByTestId("quick-guarded-card")).toBeTruthy();
    const batches = screen.getAllByTestId("quick-batch-group");
    await userEvent.click(within(batches[0]).getByRole("button", { name: "Looks good · 1" }));
    await waitFor(() => expect(screen.getAllByText("1 part saved as reviewed").length).toBeGreaterThan(0));
    expect(onMark).toHaveBeenCalledWith(expect.objectContaining({ target_id: "label" }), "reviewed");
    expect(onMark).not.toHaveBeenCalledWith(expect.objectContaining({ target_id: "order_total" }), "reviewed");
    await userEvent.click(within(screen.getAllByTestId("quick-batch-group")[0]).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(onMark).toHaveBeenCalledWith(expect.objectContaining({ target_id: "label" }), "unreviewed"));
  });

  it("never claims a whole batch succeeded after a partial write failure", async () => {
    const onMark = vi.fn().mockResolvedValueOnce(true).mockResolvedValueOnce(false);
    const other = target("message", "app/messages.py", { attention_rank: 3, priority: { ...ORDER.priority!, position: 3 } });
    const label = LABEL;
    apiFile.mockImplementation(async (_id, path) => ({ path, revision_id: "rev1", patch: PATCHES[path] ?? PATCHES["app/labels.py"], renderable: true, refusal: "", detail: "", degraded: [] }));
    show({ initialTargets: [label, other], onMark }, "/r/shop/one/label");
    await waitFor(() => expect(screen.getByTestId("quick-batch-group")).toBeTruthy());
    await userEvent.click(screen.getByRole("button", { name: "Looks good · 2" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Only 1 of 2"));
    expect(screen.queryByText("2 parts saved as reviewed")).toBeNull();
    expect(screen.getByText("1 part saved as reviewed")).toBeTruthy();
  });

  it("a stale patch or incomplete target pagination keeps all batch controls unavailable", async () => {
    apiFile.mockImplementation(async (_id, path) => ({ path, revision_id: "old", patch: PATCHES[path], renderable: true, refusal: "", detail: "", degraded: [] }));
    show({ initialTargets: [LABEL] }, "/r/shop/one/label");
    await waitFor(() => expect(screen.getByTestId("quick-guard-reason").textContent).toContain("unavailable"));
    expect(screen.queryByTestId("quick-batch-group")).toBeNull();
  });

  it("read-only Quick check cannot write and the AI-off path does not hallucinate a hint", async () => {
    const onMark = vi.fn();
    show({ readOnly: true, aiAvailable: false, onMark });
    await waitFor(() => expect(screen.getByTestId("quick-guarded-card")).toBeTruthy());
    expect(screen.getByRole("button", { name: "Looks good" })).toBeDisabled();
    expect(screen.queryByText(/AI hint ·/)).toBeNull();
    expect(onMark).not.toHaveBeenCalled();
  });

  it("QC-6: accepted by rule is not counted as a human review, and a new package stays a decision", async () => {
    const generated = target("generated", "generated/types.ts", { answer: { category: "accepted_by_rule", rule: "generated_files", rule_label: "generated files", reason: "rule:generated_files" } });
    const dependency = target("upgrade", "poetry.lock", { kind: "dependency", reasons: ["direct dependency added"], answer: { category: "decision", rule: "", rule_label: "", reason: "new_dependency" } });
    show({ initialTargets: [LABEL, generated, dependency] }, "/r/shop/one/label");
    await waitFor(() => expect(screen.getByTestId("quick-batch-group")).toBeTruthy());
    const byRule = screen.getByTestId("quick-rules");
    expect(byRule.textContent).toContain("accepted by rule “generated files”");
    expect(byRule.textContent).toContain("not by a person");
    expect(screen.getByTestId("quick-dependency-decisions").textContent).toContain("New dependency");
    expect(screen.getByTestId("quick-dependency-fact").textContent).toBe("Checked in code · direct dependency added");
    expect(screen.getByTestId("quick-dependency-decisions").textContent).toContain("Not verified · no package registry lookup");
    expect(screen.getByTestId("quick-tray").textContent).toContain("1 part left to confirm");
    await userEvent.click(screen.getByRole("button", { name: "Review decision" }));
    expect(screen.getByTestId("location").textContent).toContain("/r/shop/one/upgrade");
  });

  it("typing a note cannot trigger review keyboard shortcuts", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    show({ onMark });
    await userEvent.click(screen.getByRole("button", { name: "Needs a change" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Say what should change" }), "a good change");
    expect(onMark).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: "Say what should change" })).toHaveValue("a good change");
  });

  it("a batch that is only queued is tracked but never announced as saved", async () => {
    const onMark = vi.fn().mockResolvedValue("queued");
    show({ initialTargets: [LABEL], onMark }, "/r/shop/one/label");
    await waitFor(() => expect(screen.getByTestId("quick-batch-group")).toBeTruthy());
    await userEvent.click(screen.getByRole("button", { name: "Looks good · 1" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("queued"));
    expect(screen.queryByText("1 part saved as reviewed")).toBeNull();
  });

  it("copy says exactly what a batch saves and never claims an Overview skip", async () => {
    show({ initialTargets: [LABEL] }, "/r/shop/one/label");
    await waitFor(() => expect(screen.getByTestId("quick-batch-group")).toBeTruthy());
    expect(screen.getByText(/saves each of its parts as reviewed/)).toBeTruthy();
    expect(screen.queryByText(/Overview is skipped/)).toBeNull();
  });

  it("Ctrl+Enter in the note composer saves the note", async () => {
    const onNote = vi.fn().mockResolvedValue(true);
    show({ onNote });
    const card = await screen.findByTestId("quick-guarded-card");
    await userEvent.click(within(card).getByRole("button", { name: "Needs a change" }));
    const box = await screen.findByRole("textbox", { name: "Say what should change" });
    await userEvent.type(box, "Stop one early");
    fireEvent.keyDown(box, { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(onNote).toHaveBeenCalledWith(expect.objectContaining({ target_id: "order_total" }), "Stop one early", false));
  });

  it("Undo skips a part the reviewer changed to Needs a change by hand after the batch", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    const other = target("message", "app/messages.py", { attention_rank: 3, priority: { ...ORDER.priority!, position: 3 } });
    apiFile.mockImplementation(async (_id, path) => ({ path, revision_id: "rev1", patch: PATCHES[path] ?? PATCHES["app/labels.py"], renderable: true, refusal: "", detail: "", degraded: [] }));
    show({ initialTargets: [LABEL, other], onMark }, "/r/shop/one/label");
    await waitFor(() => expect(screen.getByTestId("quick-batch-group")).toBeTruthy());
    await userEvent.click(screen.getByRole("button", { name: "Looks good · 2" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Undo" })).toBeTruthy());
    const group = screen.getByTestId("quick-batch-group");
    await userEvent.click(within(group).getAllByRole("button", { name: "View code" })[0]);
    await userEvent.click(await within(group).findByRole("button", { name: "Needs a change" }));
    await userEvent.type(await screen.findByRole("textbox", { name: "Say what should change" }), "Wrong");
    await userEvent.click(screen.getByRole("button", { name: /Save note/ }));
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Say what should change" })).toBeNull());
    onMark.mockClear();
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(onMark).toHaveBeenCalledTimes(1));
    expect(onMark).toHaveBeenCalledWith(expect.objectContaining({ target_id: "message" }), "unreviewed");
    expect(onMark).not.toHaveBeenCalledWith(expect.objectContaining({ target_id: "label" }), "unreviewed");
  });
});

describe("P5-3 Quick check groups by directory in a large change (SCALE-2)", () => {
  // 30 parts: 14 in app/a and 12 in app/b that the guard clears, plus 4 loop-bound parts in app/b that it does not.
  const clear = (dir: string, count: number) => Array.from({ length: count }, (_, i) => target(`${dir}${i}`, `app/${dir}/f${i}.py`));
  const guardedParts = Array.from({ length: 4 }, (_, i) => target(`loop${i}`, `app/b/loop${i}.py`, { batch_guard: ORDER.batch_guard }));
  const parts = [...clear("a", 14), ...clear("b", 12), ...guardedParts];

  beforeEach(() => {
    apiFile.mockImplementation(async (_id, path) => ({
      path, revision_id: "rev1", patch: path.includes("loop") ? PATCHES["app/orders.py"] : PATCHES["app/labels.py"], renderable: true, refusal: "", detail: "", degraded: [],
    }));
  });

  it("offers one group per directory, confirms exactly that group's unanswered parts, and never a guarded one", async () => {
    expect(parts).toHaveLength(30);
    const onMark = vi.fn().mockResolvedValue(true);
    // b0 is already answered, so the group of 12 has 11 left to confirm.
    show({ initialTargets: parts.map((p) => p.target_id === "b0" ? { ...p, state: "reviewed" as const } : p), onMark }, "/r/shop/one/a0");
    await waitFor(() => expect(screen.getByTestId("quick-confirm-together").textContent).toBe("Confirm together · 2 groups"));
    const groups = screen.getAllByTestId("quick-batch-group");
    expect(groups.map((g) => within(g).getByRole("heading").textContent)).toEqual(["app/a · 14", "app/b · 12"]);
    expect(screen.getAllByTestId("quick-guarded-card")).toHaveLength(4);
    expect(screen.getAllByTestId("quick-pulled-out").map((n) => n.textContent)).toEqual(Array(4).fill(" · pulled out of the app/b group"));
    expect(within(groups[0]).getByRole("button", { name: "Confirm 14 together" })).toBeTruthy();
    await userEvent.click(within(groups[1]).getByRole("button", { name: "Confirm 11 together" }));
    await waitFor(() => expect(onMark).toHaveBeenCalledTimes(11));
    const marked = onMark.mock.calls.map(([part, state]) => [part.target_id, state]).sort();
    expect(marked).toEqual(clear("b", 12).slice(1).map((p) => [p.target_id, "reviewed"]).sort());
  });
});

describe("what a key or the palette answers is always drawn", () => {
  const unit = (index: number, rest: Partial<ReviewTarget> = {}) => target(`unit_${String(index).padStart(2, "0")}`, "app/labels.py", rest);
  const group = (count: number, state: (index: number) => ReviewTarget["state"]) => Array.from({ length: count }, (_, index) => unit(index, { state: state(index) }));
  const marksAfterA = async (targets: ReviewTarget[], route: string, showAll = false) => {
    const onMark = vi.fn().mockResolvedValue(true);
    const view = show({ initialTargets: targets, complete: true, onMark }, route);
    const batch = await screen.findByTestId("quick-batch-group", {}, { timeout: 5000 });
    if (showAll) await userEvent.click(within(batch).getByRole("button", { name: /Show all/ }));
    fireEvent.keyDown(window, { key: "a" });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    const marked = onMark.mock.calls.map(([part]) => (part as ReviewTarget).target_id);
    view.unmount();
    return marked;
  };

  it("S3: once every part is answered, A marks nothing that is not drawn", async () => {
    const all = group(60, (index) => (index === 3 ? "needs_changes" : "reviewed"));
    expect(await marksAfterA(all, "/r/shop/one/unit_03")).toEqual([]);
    expect(await marksAfterA(all, "/r/shop/one/")).toEqual([]);
  });

  it("S2b: the route's part is answered by A only while it is drawn in its group, and Show all draws the rest", async () => {
    const open = group(60, () => "unreviewed");
    // The group draws its first 20 unanswered rows: unit_19 is the 20th (drawn), unit_20 the 21st (not drawn).
    expect(await marksAfterA(open, "/r/shop/one/unit_19")).toEqual(["unit_19"]);
    expect(await marksAfterA(open, "/r/shop/one/unit_20")).toEqual(["unit_00"]);
    expect(await marksAfterA(open, "/r/shop/one/unit_35")).toEqual(["unit_00"]);
    expect(await marksAfterA(open, "/r/shop/one/unit_35", true)).toEqual(["unit_35"]);
  });

  it("Enter goes to Wrap up once every part is answered, with no route, on a small change", async () => {
    show({ initialTargets: group(3, () => "reviewed"), complete: true }, "/r/shop/one/");
    await screen.findByText("All quick checks confirmed");
    fireEvent.keyDown(window, { key: "Enter" });
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/wrap"));
  });

  it("E4: Enter goes to Wrap up when the route is on an answered part that is not drawn", async () => {
    const answered = Array.from({ length: 1000 }, (_, index) => target(`unit_${String(index).padStart(4, "0")}`, `app/f${String(index).padStart(4, "0")}.py`, { state: "reviewed", batch_guard: { guarded: false, code: "", reason: "" } as never }));
    apiFile.mockImplementation(async (_id, path) => ({ path, revision_id: "rev1", patch: PATCHES["app/labels.py"], renderable: true, refusal: "", detail: "", degraded: [] }));
    show({ initialTargets: answered, complete: true }, "/r/shop/one/unit_0000");
    // Once the patches are read the answered files are a group that draws its last twenty rows, so unit_0000 is not drawn.
    const batch = await screen.findByTestId("quick-batch-group", {}, { timeout: 5000 });
    expect(within(batch).queryByText("unit_0000")).toBeNull();
    fireEvent.keyDown(window, { key: "Enter" });
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/wrap"));
  });

  it("Enter stays put while the part in hand is open: no route, every part open", async () => {
    show({ initialTargets: group(3, () => "unreviewed"), complete: true }, "/r/shop/quick");
    await screen.findByTestId("quick-batch-group", {}, { timeout: 5000 });
    fireEvent.keyDown(window, { key: "Enter" });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/quick");
  });

  it("Enter stays put when the route is on an open part and the others are answered", async () => {
    show({ initialTargets: group(3, (index) => (index === 1 ? "unreviewed" : "reviewed")), complete: true }, "/r/shop/one/unit_01");
    await screen.findByText("1 part left to confirm");
    fireEvent.keyDown(window, { key: "Enter" });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(screen.getByTestId("location").textContent).toBe("/r/shop/one/unit_01");
  });

  it("Enter goes to Wrap up when the route is on an answered part, even with others still open", async () => {
    show({ initialTargets: group(3, (index) => (index === 0 ? "reviewed" : "unreviewed")), complete: true }, "/r/shop/one/unit_00");
    await screen.findByText("2 parts left to confirm");
    fireEvent.keyDown(window, { key: "Enter" });
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/r/shop/wrap"));
  });

  it("the All files control opens at the route's part even when it is not drawn", async () => {
    function Hash() { return <output data-testid="hash">{useLocation().hash}</output>; }
    render(<MemoryRouter initialEntries={["/r/shop/one/unit_35"]}><ReviewQuickCheckScreen reviewRef="shop" apiReviewId="review-1" part="unit_35" overview={OVERVIEW}
      targets={group(60, () => "unreviewed")} annotations={[]} complete readOnly={false} aiAvailable={false} onMark={async () => true} onNote={async () => true} /><Hash /></MemoryRouter>);
    const batch = await screen.findByTestId("quick-batch-group", {}, { timeout: 5000 });
    expect(within(batch).queryByText("unit_35")).toBeNull();
    const control = screen.getAllByRole("button", { name: /All files/ })[0];
    await userEvent.click(control);
    await waitFor(() => expect(screen.getByTestId("hash").textContent).toContain("unit_35"));
  });

  it("Copy link from the palette names the route's part even when it is not drawn", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(<MemoryRouter initialEntries={["/r/shop/one/unit_35"]}><FlowPaletteProvider><ReviewQuickCheckScreen reviewRef="shop" apiReviewId="review-1" part="unit_35" overview={OVERVIEW}
      targets={group(60, () => "unreviewed")} annotations={[]} complete readOnly={false} aiAvailable={false} onMark={async () => true} onNote={async () => true} /></FlowPaletteProvider></MemoryRouter>);
    const batch = await screen.findByTestId("quick-batch-group", {}, { timeout: 5000 });
    expect(within(batch).queryByText("unit_35")).toBeNull();
    await userEvent.keyboard("{Control>}{Shift>}c{/Shift}{/Control}");
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(writeText.mock.calls[0][0]).toContain("unit_35");
  });

  it("where the reviewer is (the All files target, the counter) follows the route's part even when it is not drawn", async () => {
    // The route is on unit_35 of a group that draws only its first 20 rows; g opens All files at that part.
    function Hash() { return <output data-testid="hash">{useLocation().hash}</output>; }
    render(<MemoryRouter initialEntries={["/r/shop/one/unit_35"]}><ReviewQuickCheckScreen reviewRef="shop" apiReviewId="review-1" part="unit_35" overview={OVERVIEW}
      targets={group(60, () => "unreviewed")} annotations={[]} complete readOnly={false} aiAvailable={false} onMark={async () => true} onNote={async () => true} /><Hash /></MemoryRouter>);
    await screen.findByTestId("quick-batch-group", {}, { timeout: 5000 });
    fireEvent.keyDown(window, { key: "g" });
    await waitFor(() => expect(screen.getByTestId("hash").textContent).toContain("unit_35"));
  });

  it("the counter counts decisions only; quick checks show their own small progress", async () => {
    // unit_00 and unit_04 are answered quick checks; there are no decisions, so no pips and no decision count moves.
    show({ initialTargets: group(6, (index) => (index === 0 || index === 4 ? "reviewed" : "unreviewed")), complete: true }, "/r/shop/one/unit_00");
    expect((await screen.findByTestId("flow-top-bar-quick")).textContent).toBe("quick checks 2/6");
    expect(screen.getByTestId("flow-top-bar-counter").textContent).toBe("No decisions");
    expect(screen.queryByTestId("flow-top-bar-pips")).toBeNull();
  });

  it("a group keeps the last twenty answered rows, not the first twenty", async () => {
    show({ initialTargets: group(60, (index) => (index < 40 ? "reviewed" : "unreviewed")), complete: true }, "/r/shop/one/unit_40");
    const batch = await screen.findByTestId("quick-batch-group", {}, { timeout: 5000 });
    const names = Array.from(batch.querySelectorAll(".min-w-0 > p.font-mono")).map((node) => node.textContent);
    expect(names).toContain("unit_39");
    expect(names).toContain("unit_20");
    expect(names).not.toContain("unit_19");
    expect(names).not.toContain("unit_00");
    expect(names.length).toBe(40);
  });

  it("a group of forty with fifteen to answer lists them all, offers no Show all, and keeps twenty answered rows", async () => {
    show({ initialTargets: group(40, (index) => (index < 25 ? "reviewed" : "unreviewed")), complete: true }, "/r/shop/one/unit_30");
    const batch = await screen.findByTestId("quick-batch-group", {}, { timeout: 5000 });
    expect(within(batch).queryByTestId("quick-group-more")).toBeNull();
    expect(within(batch).getByRole("button", { name: "Confirm 15 together" })).toBeTruthy();
    expect(within(batch).getAllByRole("button", { name: /View code/ }).length).toBe(35);
  });

  describe("property: after any action, every part whose answer changed was drawn just before it", () => {
    const rng = (seed: number) => () => {
      seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    const HELD = { guarded: true, code: "block_moved", reason: "A line moved." } as never;
    type Controls = { targets: ReviewTarget[]; setPart: (part: string) => void; path: string };
    function Where({ controls }: { controls: { current: Controls } }) { controls.current.path = useLocation().pathname; return null; }
    function Walk({ initial, controls }: { initial: ReviewTarget[]; controls: { current: Controls } }) {
      const [targets, setTargets] = useState(initial);
      const [part, setPart] = useState("");
      controls.current = { targets, setPart, path: controls.current.path };
      const set = (target: ReviewTarget, state: MarkState) => setTargets((before) => before.map((row) => row.target_id === target.target_id ? { ...row, state } : row));
      return <MemoryRouter><ReviewQuickCheckScreen reviewRef="shop" apiReviewId="review-1" part={part} overview={OVERVIEW} targets={targets} annotations={[]} complete readOnly={false} aiAvailable={false}
        onMark={async (target, state) => { set(target, state); return true; }} onNote={async (target) => { set(target, "needs_changes"); return true; }} /><Where controls={controls} /></MemoryRouter>;
    }
    const drawnNames = () => new Set([
      ...Array.from(document.querySelectorAll('[data-testid="quick-guarded-card"] h3')).map((node) => node.textContent ?? ""),
      ...Array.from(document.querySelectorAll('[data-testid="quick-batch-group"] .min-w-0 > p.font-mono')).map((node) => node.textContent ?? ""),
    ]);
    const settle = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 10)); });

    const walk = async (seed: number, big: boolean, steps: number) => {
      const next = rng(seed);
      const pick = <T,>(list: readonly T[]): T => list[Math.floor(next() * list.length)];
      const allAnswered = !big && seed % 3 === 0;
      const base = big
        ? Array.from({ length: 1000 }, (_, index) => target(`unit_${String(index).padStart(4, "0")}`, `app/f${String(index).padStart(4, "0")}.py`, { batch_guard: SAFE_FIXTURE }))
        : group(60, () => "unreviewed");
      const initial = base.map((part, index) => {
        const roll = next();
        const state: ReviewTarget["state"] = allAnswered ? (index % 2 === 1 ? "needs_changes" : "reviewed") : roll < 0.15 ? "needs_changes" : roll < 0.3 ? "reviewed" : "unreviewed";
        return { ...part, state, batch_guard: !big && next() < 0.15 ? HELD : big && next() < 0.04 ? HELD : part.batch_guard };
      });
      const controls = { current: { targets: initial, setPart: () => {}, path: "" } as Controls };
      const view = render(<Walk initial={initial} controls={controls} />);
      await settle();
      const ids = initial.map((part) => part.target_id);
      for (let step = 0; step < steps; step += 1) {
        const before = new Map(controls.current.targets.map((part) => [part.target_id, part.state]));
        const drawn = drawnNames();
        const action = pick(allAnswered ? ["A", "route", "show", "enter"] : ["A", "A", "route", "route", "show", "confirm", "mouse"]);
        if (action === "enter") {
          controls.current.path = "";
          fireEvent.keyDown(window, { key: "Enter" });
          await settle();
          expect(controls.current.path, `seed ${seed} step ${step}: Enter with every part answered goes to Wrap up`).toBe("/r/shop/wrap");
        } else if (action === "A") fireEvent.keyDown(window, { key: "a" });
        else if (action === "route") await act(async () => { controls.current.setPart(next() < 0.15 ? "" : pick(ids)); });
        else if (action === "show") { const more = screen.queryAllByRole("button", { name: /Show all/ }); if (more.length) await userEvent.click(pick(more)); }
        else if (action === "confirm") { const button = screen.queryAllByRole("button", { name: /(Confirm \d+ together|Looks good · \d+)/ }).find((item) => !(item as HTMLButtonElement).disabled); if (button) await userEvent.click(button); }
        else { const buttons = screen.queryAllByRole("button", { name: "Looks good" }).filter((item) => !(item as HTMLButtonElement).disabled); if (buttons.length) await userEvent.click(pick(buttons)); }
        await settle();
        for (const part of controls.current.targets) {
          if (before.get(part.target_id) !== part.state) expect(drawn.has(part.label), `seed ${seed} step ${step} action ${action}: ${part.target_id} changed but was not drawn`).toBe(true);
        }
      }
      view.unmount();
    };

    it("one group of sixty parts", async () => {
      for (let seed = 1; seed <= 60; seed += 1) await walk(seed, false, 8);
    }, 120_000);

    it("a thousand files", async () => {
      apiFile.mockImplementation(async (_id, path) => ({ path, revision_id: "rev1", patch: PATCHES["app/labels.py"], renderable: true, refusal: "", detail: "", degraded: [] }));
      for (let seed = 1; seed <= 25; seed += 1) await walk(seed, true, 5);
    }, 300_000);
  });
});
const SAFE_FIXTURE = { guarded: false, code: "", reason: "" } as never;
