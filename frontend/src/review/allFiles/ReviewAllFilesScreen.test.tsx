import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ReviewOverview, ReviewTarget } from "../types";
import { requestBrowserJson } from "../reviewApi";
import { resetPartProof } from "../partProof";
import { ReviewAllFilesScreen } from "./ReviewAllFilesScreen";

vi.mock("../checkRecipesApi", () => ({ fetchCheckRecipes: vi.fn().mockResolvedValue({ revision_id: "rev1", part_checks: [], results: [] }), postCheckRecipesRun: vi.fn() }));
vi.mock("../reviewApi", async (importOriginal) => ({ ...(await importOriginal<typeof import("../reviewApi")>()), requestBrowserJson: vi.fn() }));
vi.mock("../usePreviewCount", () => ({ usePreviewCount: () => 0 }));
vi.mock("../focus/FocusCode", () => ({ FocusCode: ({ target }: { target: ReviewTarget }) => <pre data-testid="code-stub">{target.symbol}</pre> }));
vi.mock("../focus/FocusStructurePanel", () => ({ FocusStructurePanel: () => null }));

const apiProof = vi.mocked(requestBrowserJson);
const proofRecord = (files: Record<string, { o: string; r?: string; t?: string[]; n?: number }>) => ({
  revision_id: "rev1", head_identity: "head", base_identity: "base", reach: "file", default: { o: "not_tested" }, files, recorded: true, complete: true, running: false,
  source: "auto", labels: {}, details: {}, reasons: { no_sandbox: "no sandbox is available here, and tests never run unsandboxed on their own" },
});

function part(index: number, category: "decision" | "quick_check"): ReviewTarget {
  const name = `part_${String(index).padStart(5, "0")}`;
  return {
    target_id: name, unit_key: `sym:${name}`, kind: "symbol", path: `src/m${index % 400}.py`, label: name, symbol: name,
    start_line: 1, end_line: 3, hunk_ordinals: [0], spans: [], state: "unreviewed", changed_since_mark: false, reviewed_revision_id: "",
    attention_rank: index, attention_level: "normal", reasons: [], additions: 1, deletions: 1, fingerprint_method: "ast",
    verification: { pass: 0, fail: 0, unknown: 1 }, annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    answer: { category, rule: "", rule_label: "", reason: "" },
    priority: { version: 1, tier: 3, attention: "routine", outstanding: true, position: index, reasons: [] },
  } as ReviewTarget;
}

const TARGETS = Array.from({ length: 10_000 }, (_, index) => part(index, index < 5_000 ? "decision" : "quick_check"));
const OVERVIEW = { title: "Large change", session: { id: "canonical-review-id", ref: "shop" }, revision: { id: "rev1", revision_number: 1 } } as ReviewOverview;

function show(hash = "", options: { targets?: readonly ReviewTarget[]; overview?: ReviewOverview; onMark?: ReturnType<typeof vi.fn> } = {}) {
  window.history.replaceState(null, "", `/r/shop/all${hash}`);
  return render(
    <MemoryRouter initialEntries={[`/r/shop/all${hash}`]}>
      <ReviewAllFilesScreen
        reviewRef="shop" apiReviewId="review-1" overview={options.overview ?? OVERVIEW} targets={options.targets ?? TARGETS} annotations={[]} projection={null}
        complete readOnly={false} aiAvailable={false}
        onMark={options.onMark ?? vi.fn().mockResolvedValue(true)} onNote={vi.fn().mockResolvedValue(true)}
      />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  resetPartProof();
  apiProof.mockReset().mockRejectedValue(new Error("offline"));
  Element.prototype.scrollIntoView = vi.fn();
  window.requestAnimationFrame = ((callback: FrameRequestCallback) => window.setTimeout(() => callback(performance.now()), 0)) as typeof window.requestAnimationFrame;
});
afterEach(cleanup);

describe("P11-1 All files shows each part's proof from one request", () => {
  it("10,000 parts cost one proof request, every drawn card reads its outcome, and a Couldn't run card says why", async () => {
    apiProof.mockResolvedValue(proofRecord({ "src/m0.py": { o: "couldnt_run", r: "no_sandbox" }, "src/m1.py": { o: "fails", t: ["tests/test_m1.py"], n: 1 } }) as never);
    show();
    await screen.findByTestId("all-files-view");
    await waitFor(() => expect(screen.getAllByTestId("part-proof").some((node) => node.getAttribute("data-outcome") === "fails")).toBe(true));
    const outcomes = new Set(screen.getAllByTestId("part-proof").map((node) => node.getAttribute("data-outcome")));
    expect(outcomes).toEqual(new Set(["couldnt_run", "fails", "not_tested"]));
    expect(screen.getAllByTestId("part-proof-reason")[0].textContent).toMatch(/^Couldn't run: no sandbox is available here/);
    expect(apiProof.mock.calls.filter(([path]) => String(path).includes("/part-proof"))).toHaveLength(1);
  });
});

describe("All files at 10,000 parts", () => {
  it("draws a window of cards and rail rows, not one per part", async () => {
    const view = show();
    await screen.findByTestId("all-files-view");
    const cards = screen.getAllByTestId("all-files-part");
    expect(cards.length).toBe(30);
    expect(view.container.querySelectorAll("*").length, "elements for a 10,000-part change").toBeLessThan(6_000);
    // The header still counts every part, and the rail is windowed too.
    expect(screen.getByText(/10000 parts/)).toBeInTheDocument();
    expect(screen.getByTestId("all-files-list").querySelectorAll("button[aria-current], button.mb-1").length).toBeLessThanOrEqual(121);
    expect(screen.getByTestId("all-files-later")).toHaveTextContent("9970 after these 30");
    expect(screen.queryByTestId("all-files-earlier")).toBeNull();
  });

  it("says where a carried symbol came from on its card, and nowhere else (P11-3)", async () => {
    const targets = TARGETS.map((target, index) => index === 2 ? { ...target, state: "reviewed" as const, moved_from: { path: "src/old/home.py" } } : target);
    show("", { targets });
    await screen.findByTestId("all-files-view");
    const labels = screen.getAllByTestId("target-moved-from");
    expect(labels.map((label) => label.textContent)).toEqual(["Moved from src/old/home.py"]);
    expect(labels[0].closest('[data-testid="all-files-part"]')?.getAttribute("data-part-id")).toBe("part_00002");
  });

  it("opens around the part the link names, in order, with the rest one click away", async () => {
    show("#part_07000");
    await screen.findByTestId("all-files-view");
    const ids = screen.getAllByTestId("all-files-part").map((card) => card.getAttribute("data-part-id"));
    expect(ids.length).toBe(30);
    expect(ids).toContain("part_07000");
    expect(ids).toEqual([...ids].sort());
    expect(screen.getByTestId("all-files-earlier")).toBeInTheDocument();
  });

  it("J past the end of the window draws the next part and puts focus on it", async () => {
    show("#part_00000");
    await screen.findByTestId("all-files-view");
    // 29 presses walk to the last card of the window; each lands inside the window and moves nothing under the reader.
    for (let press = 0; press < 29; press += 1) {
      await act(async () => { fireEvent.keyDown(window, { key: "j" }); });
    }
    expect(document.querySelector('[aria-current="location"]')?.textContent).toContain("part_00029");
    expect(document.querySelector('[data-part-id="part_00030"]')).toBeNull();
    expect(screen.getAllByTestId("all-files-part")[0].getAttribute("data-part-id")).toBe("part_00000");
    await act(async () => { fireEvent.keyDown(window, { key: "j" }); });
    await waitFor(() => expect(document.querySelector('[data-part-id="part_00030"]')).not.toBeNull());
    await waitFor(() => expect(document.activeElement?.closest("[data-part-id]")?.getAttribute("data-part-id")).toBe("part_00030"));
    const ids = screen.getAllByTestId("all-files-part").map((card) => card.getAttribute("data-part-id"));
    expect(ids.length).toBe(30);
    expect(ids).toEqual([...ids].sort());
  }, 30_000);

  it("Show later parts moves the window on and keeps the order", async () => {
    show();
    await screen.findByTestId("all-files-view");
    fireEvent.click(screen.getByTestId("all-files-later"));
    await waitFor(() => expect(document.querySelector('[data-part-id="part_00030"]')).not.toBeNull());
    const ids = screen.getAllByTestId("all-files-part").map((card) => card.getAttribute("data-part-id"));
    expect(ids).toEqual([...ids].sort());
    expect(screen.getByTestId("all-files-earlier")).toBeInTheDocument();
  });
});

describe("parts accepted by rule and quick checks", () => {
  const rule = (index: number, path: string): ReviewTarget => ({ ...part(index, "quick_check"), path, answer: { category: "accepted_by_rule", rule: "generated_files", rule_label: "generated files", reason: "" } });
  const targets = [
    part(0, "decision"),
    { ...part(1, "quick_check"), batch_guard: { guarded: false, code: "", reason: "" } },
    rule(2, "assets/Inter.woff2"), rule(3, "uv.lock"), rule(4, "uv.lock"),
  ] as ReviewTarget[];

  it("folds every accepted-by-rule part behind one collapsed group counted in files, out of the counter", async () => {
    show("", { targets });
    await screen.findByTestId("all-files-view");
    const list = screen.getByTestId("all-files-list");
    expect([...list.querySelectorAll("section > p")].map((node) => node.textContent)).toEqual(["Decisions · 1", "Quick check · 1", "Accepted by rule · 2 files"]);
    // Folded: no rail row and no card for the accepted parts.
    expect(within(list).getAllByTestId("all-files-rail-row").map((row) => row.getAttribute("data-rail-id"))).toEqual(["part_00000", "part_00001"]);
    expect(screen.getAllByTestId("all-files-part").map((card) => card.getAttribute("data-part-id"))).toEqual(["part_00000", "part_00001"]);
    expect(screen.getByTestId("flow-top-bar-counter").textContent).toBe("0 of 1 decision");
    expect(screen.getByTestId("flow-top-bar-quick").textContent).toBe("quick checks 0/1");
    fireEvent.click(within(list).getByTestId("all-files-folded-toggle"));
    await waitFor(() => expect(screen.getAllByTestId("all-files-part")).toHaveLength(5));
  });

  it("offers Confirm all on the quick-check group only where answers can be recorded", async () => {
    const confirm = vi.fn().mockResolvedValue({ marked: [], single: [], skipped: [] });
    render(
      <MemoryRouter initialEntries={["/r/shop/all"]}>
        <ReviewAllFilesScreen reviewRef="shop" apiReviewId="review-1" overview={OVERVIEW} targets={targets} annotations={[]} projection={null}
          complete readOnly={false} aiAvailable={false} onMark={vi.fn()} onNote={vi.fn()} quickConfirm={{ confirm, undo: vi.fn() }} />
      </MemoryRouter>,
    );
    const section = await screen.findByRole("region", { name: "Quick check parts" });
    expect(within(section).getByRole("button", { name: "Confirm 1 quick check" })).toBeTruthy();
    cleanup();
    show("", { targets });
    await screen.findByTestId("all-files-view");
    expect(screen.queryByTestId("confirm-all-quick")).toBeNull();
  });
});

describe("Decide later while the server's gate is shut", () => {
  const few = TARGETS.slice(0, 3);
  const shut = { ...OVERVIEW, later_marks: false } as ReviewOverview;

  it("is aria-disabled with its visible reason, and neither a click nor N sends anything", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    show("#part_00000", { targets: few, overview: shut, onMark });
    await screen.findByTestId("all-files-view");
    const later = screen.getAllByRole("button", { name: /Decide later/ })[0];
    expect(later).toHaveAttribute("aria-disabled", "true");
    expect(later).not.toBeDisabled();
    const reason = document.getElementById(later.getAttribute("aria-describedby") ?? "none");
    expect(reason).not.toBeNull();
    expect(reason).toHaveTextContent("Decide later is not available on this server yet");
    fireEvent.click(later);
    await act(async () => { fireEvent.keyDown(window, { key: "n" }); });
    expect(onMark).not.toHaveBeenCalled();
  });

  it("is a normal button when the gate is open", async () => {
    const onMark = vi.fn().mockResolvedValue(true);
    show("#part_00000", { targets: few, onMark });
    await screen.findByTestId("all-files-view");
    const later = screen.getAllByRole("button", { name: /Decide later/ })[0];
    expect(later).not.toHaveAttribute("aria-disabled");
    fireEvent.click(later);
    await waitFor(() => expect(onMark).toHaveBeenCalledWith(expect.objectContaining({ target_id: "part_00000" }), "later"));
  });
});

describe("the rail in Folder tree order", () => {
  const sortedByPath = (list: readonly ReviewTarget[]) => [...list].sort((a, b) => a.path.localeCompare(b.path));

  it.each([230, 10_000])("at %i parts draws a contiguous run of the path-sorted list, with earlier and later rows", async (count) => {
    const targets = TARGETS.slice(0, count).map((item, index) => ({ ...item, answer: { ...item.answer!, category: "decision" as const }, path: `src/d${String(index % 97).padStart(2, "0")}/m${index % 13}.py` }));
    show("", { targets });
    await screen.findByTestId("all-files-view");
    fireEvent.click(screen.getByRole("button", { name: "Folder tree" }));
    const rows = screen.getAllByTestId("all-files-rail-row").map((row) => row.getAttribute("data-rail-id"));
    const expected = sortedByPath(targets).map((item) => item.target_id);
    const first = expected.indexOf(rows[0]!);
    expect(first).toBeGreaterThanOrEqual(0);
    expect(rows, "rows are one contiguous slice of the path-sorted list").toEqual(expected.slice(first, first + rows.length));
    if (count > 120) {
      expect(rows.length).toBe(120);
      expect(screen.getByTestId("all-files-rail-later")).toHaveTextContent(`${count - 120} later`);
      fireEvent.click(screen.getByTestId("all-files-rail-later"));
      const moved = screen.getAllByTestId("all-files-rail-row").map((row) => row.getAttribute("data-rail-id"));
      const at = expected.indexOf(moved[0]!);
      expect(at).toBeGreaterThan(first);
      expect(moved).toEqual(expected.slice(at, at + moved.length));
      expect(screen.getByTestId("all-files-rail-earlier")).toBeInTheDocument();
    } else {
      expect(rows.length).toBe(count);
      expect(screen.queryByTestId("all-files-rail-later")).toBeNull();
    }
  }, 60_000);
});

describe("the rail after the person moves it", () => {
  const railIds = () => screen.getAllByTestId("all-files-rail-row").map((row) => row.getAttribute("data-rail-id"));

  it("puts focus on the first row that was not shown before, for later and for earlier", async () => {
    show();
    await screen.findByTestId("all-files-view");
    expect(railIds()[0]).toBe("part_00000");
    fireEvent.click(screen.getByTestId("all-files-rail-later"));
    await waitFor(() => expect(document.activeElement?.getAttribute("data-rail-id")).toBe("part_00120"));
    expect(railIds()[0]).toBe("part_00060");
    fireEvent.click(screen.getByTestId("all-files-rail-earlier"));
    await waitFor(() => expect(document.activeElement?.getAttribute("data-rail-id")).toBe("part_00000"));
    expect(railIds()[0]).toBe("part_00000");
  });

  it("after a clamped later, earlier focuses the new first row", async () => {
    show("", { targets: TARGETS.slice(0, 200) });
    await screen.findByTestId("all-files-view");
    fireEvent.click(screen.getByTestId("all-files-rail-later"));
    await waitFor(() => expect(document.activeElement?.getAttribute("data-rail-id")).toBe("part_00120"));
    fireEvent.click(screen.getByTestId("all-files-rail-later"));
    await waitFor(() => expect(railIds()[0]).toBe("part_00080"));
    fireEvent.click(screen.getByTestId("all-files-rail-earlier"));
    // The window was clamped at 80, so "earlier" steps back half a window from there (20), not from the unclamped 120 (60).
    await waitFor(() => expect(railIds()[0]).toBe("part_00020"));
    await waitFor(() => expect(document.activeElement?.getAttribute("data-rail-id")).toBe("part_00020"));
  });

  it("in the phone Files sheet, focus lands in the sheet's list and not the aside's", async () => {
    show();
    await screen.findByTestId("all-files-view");
    fireEvent.click(screen.getByRole("button", { name: /^Files$/ }));
    const lists = await screen.findAllByTestId("all-files-list");
    expect(lists.length).toBe(2);
    const sheetList = lists[lists.length - 1]!;
    expect(sheetList.closest("aside")).toBeNull();
    fireEvent.click(within(sheetList).getByTestId("all-files-rail-later"));
    await waitFor(() => expect(document.activeElement?.getAttribute("data-rail-id")).toBe("part_00120"));
    expect(sheetList.contains(document.activeElement)).toBe(true);
  });

  it("keeps the window where the person put it (an absolute position) when scrolling brings a later part into view", async () => {
    show("#part_00100");
    await screen.findByTestId("all-files-view");
    fireEvent.click(screen.getByTestId("all-files-rail-later"));
    await waitFor(() => expect(railIds()[0]).toBe("part_00100"));
    const original = Element.prototype.getBoundingClientRect;
    Element.prototype.getBoundingClientRect = function (this: Element) {
      const top = this.getAttribute("data-part-id") === "part_00105" ? 160 : -5000;
      return { top, bottom: top + 300, left: 0, right: 100, width: 100, height: 300, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
    };
    try {
      await act(async () => { fireEvent.scroll(window); await new Promise((resolve) => setTimeout(resolve, 50)); });
      await waitFor(() => expect(document.querySelector('[data-testid="all-files-rail-row"][aria-current="location"]')?.getAttribute("data-rail-id")).toBe("part_00105"));
      expect(railIds()[0]).toBe("part_00100");
    } finally {
      Element.prototype.getBoundingClientRect = original;
    }
  }, 30_000);

  it("keeps the part a jump landed on in view when the jump's own scroll is read, however short that part is", async () => {
    show("#part_00100");
    await screen.findByTestId("all-files-view");
    const current = () => document.querySelector('[data-testid="all-files-rail-row"][aria-current="location"]')?.getAttribute("data-rail-id");
    await waitFor(() => expect(current()).toBe("part_00100"));
    // The jump put part_00100's top where every jump lands a part; its code has not loaded, so the next card starts close below.
    const tops: Record<string, number> = { part_00100: 76, part_00101: 188, part_00102: 300 };
    const original = Element.prototype.getBoundingClientRect;
    Element.prototype.getBoundingClientRect = function (this: Element) {
      const top = tops[this.getAttribute("data-part-id") ?? ""] ?? -5000;
      return { top, bottom: top + 100, left: 0, right: 100, width: 100, height: 100, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
    };
    try {
      await act(async () => { fireEvent.scroll(window); await new Promise((resolve) => setTimeout(resolve, 50)); });
      expect(current()).toBe("part_00100");
    } finally {
      Element.prototype.getBoundingClientRect = original;
    }
  }, 30_000);

  it("keeps a shifted rail when scrolling changes the part in view to a shown row, and brings it back on J", async () => {
    show("#part_00100");
    await screen.findByTestId("all-files-view");
    expect(railIds()[0]).toBe("part_00040");
    fireEvent.click(screen.getByTestId("all-files-rail-earlier"));
    await waitFor(() => expect(railIds()[0]).toBe("part_00000"));
    // Scrolling: part_00105 is now the one under the top of the page, and it is a row the shifted rail shows.
    const original = Element.prototype.getBoundingClientRect;
    Element.prototype.getBoundingClientRect = function (this: Element) {
      const top = this.getAttribute("data-part-id") === "part_00105" ? 160 : -5000;
      return { top, bottom: top + 300, left: 0, right: 100, width: 100, height: 300, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
    };
    try {
      await act(async () => { fireEvent.scroll(window); await new Promise((resolve) => setTimeout(resolve, 50)); });
      await waitFor(() => expect(document.querySelector('[data-testid="all-files-rail-row"][aria-current="location"]')?.getAttribute("data-rail-id")).toBe("part_00105"));
      // The part in view changed by scrolling; the rail stayed where the person put it.
      expect(railIds()[0]).toBe("part_00000");
    } finally {
      Element.prototype.getBoundingClientRect = original;
    }
    await act(async () => { fireEvent.keyDown(window, { key: "j" }); });
    await waitFor(() => expect(railIds()[0]).toBe("part_00046"));
  }, 30_000);
});
