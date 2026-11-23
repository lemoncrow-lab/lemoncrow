import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FlowShortcutsDialog, FlowTopBar, repoNameOf, type FlowTopBarProps } from "./FlowTopBar";
import type { Counter } from "./focus/focusModel";

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
    expect(groups.length).toBe(3);
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

  it("announces the Q key on Ask only where Q is bound", () => {
    bar({ onAsk: vi.fn(), askKey: true });
    expect(screen.getByRole("button", { name: "Ask about the change" }).getAttribute("aria-keyshortcuts")).toBe("Q");
    cleanup();
    bar({ onAsk: vi.fn() });
    expect(screen.getByRole("button", { name: "Ask about the change" }).hasAttribute("aria-keyshortcuts")).toBe(false);
  });
});
