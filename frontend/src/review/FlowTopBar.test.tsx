import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FlowReviewToolsProvider, type FlowReviewTools } from "./flowReviewTools";
import { FlowShortcutsDialog, FlowTopBar, repoNameOf, type FlowTopBarProps } from "./FlowTopBar";
import type { Counter } from "./focus/focusModel";
import { note, target } from "./notes/notesFixtures";
import { FlowPaletteProvider, useFlowPaletteScreen } from "./palette/FlowPalette";

vi.mock("./home/homeApi", () => {
  const source = { load: vi.fn(async () => ({ rows: [], teamsSupported: false, truncated: false, queueSupported: false })), terminal: true };
  return { useHomeSource: () => source };
});

const COUNTER: Counter = {
  judged: 1, total: 3, later: 1, text: "1 of 3 decisions", label: "1 of 3 decisions judged · 1 left for later",
  quick: { judged: 2, total: 40, text: "quick checks 2/40" },
  pips: [
    { id: "a", state: "good", quick: false, current: false },
    { id: "b", state: "open", quick: false, current: true },
    { id: "c", state: "open", quick: true, current: false },
  ],
};

function Where() {
  return <output data-testid="where">{useLocation().pathname}</output>;
}

function bar(extra: Partial<FlowTopBarProps> = {}) {
  render(
    <MemoryRouter>
      <FlowTopBar repo="billing-api" documentTitle="Overview" title="Review the change" revisionNumber={0} back={{ text: "Overview", onClick: vi.fn() }} onShortcuts={vi.fn()} {...extra} />
      <Where />
    </MemoryRouter>,
  );
}

afterEach(cleanup);

describe("the shared top bar", () => {
  it("offers the reading-view switch with neither side pressed on a screen that is neither view (review-0.4)", () => {
    const onChange = vi.fn();
    bar({ view: { current: null, onChange } });
    const group = screen.getByRole("group", { name: "Reading view" });
    for (const name of ["One at a time", "All files"]) expect(within(group).getByRole("button", { name }).getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(within(group).getByRole("button", { name: "All files" }));
    expect(onChange).toHaveBeenCalledWith("all");
  });
  it("shows the repo, title, revision, count, Shortcuts and the settings gear", () => {
    bar({ counter: COUNTER });
    expect(screen.getByTestId("flow-top-bar-repo").textContent).toBe("billing-api");
    expect(screen.getByTestId("flow-top-bar-title").getAttribute("title")).toBe("Review the change");
    expect(screen.getByText("Revision 1")).toBeTruthy();
    expect(screen.getByTestId("flow-top-bar-counter").textContent).toBe("1 of 3 decisions");
    expect(screen.getByTestId("flow-top-bar-quick").textContent).toBe("quick checks 2/40");
    expect(screen.getByRole("img", { name: "1 of 3 decisions judged · 1 left for later" })).toBeTruthy();
    expect(within(screen.getByTestId("flow-top-bar-pips")).queryAllByRole("button")).toHaveLength(0);
    expect(screen.getByRole("button", { name: /^Shortcuts/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Review settings" }));
    expect(screen.getByTestId("where").textContent).toBe("/settings/review");
  });

  it("above twelve parts draws the segmented bar: focusable, exact counts as its name, no zero segment", () => {
    const label = "2 decisions · 0 quick checks · 5 accepted by rule";
    bar({ counter: { judged: 0, total: 2, later: 0, pips: null, text: "0 of 2 decisions", label: "0 of 2 decisions judged", bar: { label, segments: [
      { kind: "decision", count: 2, text: "2 decisions" }, { kind: "quick_check", count: 0, text: "0 quick checks" }, { kind: "accepted_by_rule", count: 5, text: "5 accepted by rule" },
    ] } } });
    const el = screen.getByRole("img", { name: label });
    expect(el.getAttribute("tabindex")).toBe("0");
    expect(el.querySelectorAll("[data-segment]")).toHaveLength(2);
    cleanup();
    const full = "5 decisions · 8 quick checks · 3 accepted by rule";
    bar({ counter: { judged: 0, total: 13, later: 0, pips: null, text: "0 of 13 decisions", label: "0 of 13 decisions judged", bar: { label: full, segments: [
      { kind: "decision", count: 5, text: "5 decisions" }, { kind: "quick_check", count: 8, text: "8 quick checks" }, { kind: "accepted_by_rule", count: 3, text: "3 accepted by rule" },
    ] } } });
    expect([...screen.getByRole("img", { name: full }).querySelectorAll("[data-segment]")].map((s) => s.getAttribute("data-segment"))).toEqual(["decision", "quick_check", "accepted_by_rule"]);
    expect(screen.queryByTestId("flow-top-bar-pips")).toBeNull();
    // Unanswered is not an alarm: no segment is red before anything is answered.
    for (const segment of screen.getByRole("img", { name: full }).querySelectorAll("[data-segment]")) expect(segment.className).not.toMatch(/p0/);
  });

  it("the Shortcuts dialog lists this screen's keys and every binding from the key map", () => {
    render(<FlowShortcutsDialog open onOpenChange={vi.fn()} legend="A good · C change · ? all shortcuts" />);
    const here = screen.getByTestId("shortcuts-here");
    expect(here).toHaveTextContent("A good");
    expect(here).not.toHaveTextContent("? all shortcuts");
    const groups = screen.getAllByTestId("shortcuts-group");
    expect(groups.length).toBe(5);
    expect(groups.map((group) => group.textContent).join(" ")).toMatch(/search and commands/);
  });

  it("starts the back control's name with its visible text", () => {
    const onClick = vi.fn();
    bar({ back: { text: "Wrap up", onClick } });
    const back = screen.getByRole("button", { name: "Wrap up, go back" });
    fireEvent.click(back);
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("draws no pips and no count while the parts list is incomplete", () => {
    bar({ counter: null, status: { text: "Loading all parts…" } });
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByText("Loading all parts…")).toBeTruthy();
  });

  it("names Ask 'Ask about the change' and shows it only when given", () => {
    bar({ onAsk: vi.fn() });
    expect(screen.getByRole("button", { name: "Ask about the change" })).toBeTruthy();
    cleanup();
    bar();
    expect(screen.queryByRole("button", { name: /Ask/ })).toBeNull();
  });

  it("offers the reading-view switch only when asked, and switches to the other view", () => {
    const onChange = vi.fn();
    bar({ view: { current: "one", onChange } });
    const group = screen.getByRole("group", { name: "Reading view" });
    expect(within(group).getByRole("button", { name: "One at a time" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(within(group).getByRole("button", { name: "All files" }));
    expect(onChange).toHaveBeenCalledWith("all");
    cleanup();
    bar();
    expect(screen.queryByRole("group", { name: "Reading view" })).toBeNull();
  });

  it("names the repository by its checkout folder, never by the opaque repo id", () => {
    expect(repoNameOf({ workspace_root: "/Users/me/code/billing-api/" } as never)).toBe("billing-api");
    expect(repoNameOf({ workspace_root: "" } as never)).toBe("");
    expect(repoNameOf({} as never)).toBe("");
  });

  it("hides the repo and its dot when there is no name", () => {
    bar({ repo: "" });
    expect(screen.queryByTestId("flow-top-bar-repo")).toBeNull();
  });

  it("colours the revision chip cyan from Revision 2 and neutral for Revision 1", () => {
    bar({ revisionNumber: 1 });
    expect(screen.getByText("Revision 2").className).toContain("text-p2");
    cleanup();
    bar({ revisionNumber: 0 });
    expect(screen.getByText("Revision 1").className).toContain("text-muted-foreground");
  });

  it("goes back where the reader came from when the address names it, and says Code for Code", () => {
    const onClick = vi.fn();
    render(
      <MemoryRouter initialEntries={[`/r/shop-7?from=${encodeURIComponent("/code?repo=shop&file=a.py")}`]}>
        <FlowTopBar repo="shop" documentTitle="Overview" title="T" revisionNumber={0} back={{ text: "Reviews", onClick }} onShortcuts={vi.fn()} />
        <Where />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Code, go back" }));
    expect(onClick).not.toHaveBeenCalled();
    expect(screen.getByTestId("where").textContent).toBe("/code");
    cleanup();
    render(
      <MemoryRouter initialEntries={[`/r/shop-7?from=${encodeURIComponent("/reviews")}`]}>
        <FlowTopBar repo="shop" documentTitle="Overview" title="T" revisionNumber={0} back={{ text: "Reviews", onClick }} onShortcuts={vi.fn()} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("button", { name: "Back, go back" })).toBeInTheDocument();
    cleanup();
    // An address outside this app is never a way back.
    render(
      <MemoryRouter initialEntries={[`/r/shop-7?from=${encodeURIComponent("//evil.example")}`]}>
        <FlowTopBar repo="shop" documentTitle="Overview" title="T" revisionNumber={0} back={{ text: "Reviews", onClick }} onShortcuts={vi.fn()} />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Reviews, go back" }));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("announces the Q key on Ask only where Q is bound", () => {
    bar({ onAsk: vi.fn(), askKey: true });
    expect(screen.getByRole("button", { name: "Ask about the change" }).getAttribute("aria-keyshortcuts")).toBe("Q");
    cleanup();
    bar({ onAsk: vi.fn() });
    expect(screen.getByRole("button", { name: "Ask about the change" }).hasAttribute("aria-keyshortcuts")).toBe(false);
  });
});

describe("the top bar inside the Reader", () => {
  function tools(extra: Partial<FlowReviewTools> = {}): FlowReviewTools {
    return {
      reviewId: "api-1", reviewRef: "shop-7", revisionId: "s1", revisionNumber: 1, historical: false, readOnly: false, busy: false,
      annotations: [note("c1"), note("c2"), note("c3", { state: "resolved" })], targets: [target("a", { unit_key: "u-a", path: "src/a.py" })],
      revisionHistory: null, setState: vi.fn(), setDisposition: vi.fn(), createNote: vi.fn(), hosted: null,
      ...extra,
    };
  }
  function Registered() {
    useFlowPaletteScreen({ context: { screen: "overview", actions: {} }, reviewRef: "shop-7" });
    return null;
  }
  function readerBar(value: FlowReviewTools) {
    render(
      <MemoryRouter initialEntries={["/r/shop-7"]}>
        <FlowPaletteProvider>
          <FlowReviewToolsProvider value={value}>
            <Registered />
            <FlowTopBar repo="shop" documentTitle="Overview" title="T" revisionNumber={1} back={{ text: "Reviews", onClick: vi.fn() }} onShortcuts={vi.fn()} />
          </FlowReviewToolsProvider>
        </FlowPaletteProvider>
      </MemoryRouter>,
    );
  }

  it("makes the revision chip a menu and shows Notes with its open count, opening the list", async () => {
    const user = userEvent.setup();
    readerBar(tools());
    expect(screen.getByRole("button", { name: "Revision 2, revision history" })).toHaveAttribute("aria-haspopup", "dialog");
    expect(screen.getByText("Revision 2").className).toContain("text-p2");
    await user.click(screen.getByRole("button", { name: "Notes · 2 open" }));
    expect(await screen.findByTestId("notes-sheet")).toBeInTheDocument();
    expect(screen.getAllByTestId("notes-row")).toHaveLength(2);
  });

  it("offers Notes in ⌘K under In this review", async () => {
    const user = userEvent.setup();
    readerBar(tools());
    await user.keyboard("{Control>}k{/Control}");
    const option = await screen.findByRole("option", { name: /Notes/ });
    expect(option).toHaveTextContent("2 open");
    await user.click(option);
    expect(await screen.findByTestId("notes-sheet")).toBeInTheDocument();
  });

  it("shows the hosted queue chip only on a hosted review", () => {
    readerBar(tools());
    expect(screen.queryByTestId("hosted-queue-chip")).toBeNull();
    cleanup();
    readerBar(tools({ hosted: {
      info: { reference: "acme/shop#12", origin: "", url: "", local_review_id: "local-1" },
      status: { attached: true, review_id: "api-1", counts: { pending: 1, conflict: 2, applied: 0, discarded: 0 } },
      error: "", onStatus: vi.fn(), onDetached: vi.fn(),
    } }));
    expect(screen.getByRole("button", { name: "Hosted acme/shop#12 · 1 queued · 2 conflicts" })).toBeInTheDocument();
  });

  it("draws the plain chip and no Notes outside the Reader", () => {
    bar();
    expect(screen.queryByRole("button", { name: /revision history/ })).toBeNull();
    expect(screen.queryByTestId("flow-top-bar-notes")).toBeNull();
  });
});
