import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FlowPaletteProvider, useFlowPaletteExtras, useFlowPaletteReviewActions, useFlowPaletteScreen } from "./FlowPalette";
import { queueRow } from "../home/homeFixtures";
import type { PaletteContext } from "./paletteModel";

const mocked = vi.hoisted(() => ({ queueSupported: true as boolean, rows: [] as unknown[], loads: 0 }));
vi.mock("../home/homeApi", () => {
  const source = { load: vi.fn(async () => { mocked.loads += 1; return { rows: mocked.rows, teamsSupported: false, truncated: false, queueSupported: mocked.queueSupported }; }), snooze: vi.fn(async () => ({})), terminal: true };
  return { useHomeSource: () => source };
});

function Screen({ context, onShortcuts }: { context: PaletteContext; onShortcuts?: () => void }) {
  useFlowPaletteScreen({ context, place: { label: "shop · Fix", sub: "One at a time" }, openShortcuts: onShortcuts });
  return <button type="button">opener</button>;
}

const lookGood = vi.fn();
const part: PaletteContext = { screen: "one", part: "apply_discount", canAnswer: true, actions: { looksGood: lookGood, wrapUp: vi.fn() } };

function mount(children: React.ReactNode = <Screen context={part} />) {
  return render(<MemoryRouter><FlowPaletteProvider>{children}</FlowPaletteProvider></MemoryRouter>);
}

afterEach(() => { cleanup(); lookGood.mockReset(); mocked.queueSupported = true; mocked.rows = []; mocked.loads = 0; });

describe("the palette's Snooze this review", () => {
  function Placeholder() {
    useFlowPaletteScreen({ context: { screen: "one", placeholder: true, actions: {} } as PaletteContext, reviewRef: "r/a" });
    return <button type="button">opener</button>;
  }

  it("is offered when the source can snooze and says so, and not when the server keeps no queue state", async () => {
    mocked.rows = [queueRow({ id: "a" })];
    const user = userEvent.setup();
    mount(<Placeholder />);
    await user.keyboard("{Control>}k{/Control}");
    expect(await screen.findByText("Snooze this review")).toBeInTheDocument();
    cleanup();
    mocked.queueSupported = false;
    mount(<Placeholder />);
    await user.keyboard("{Control>}k{/Control}");
    await screen.findByRole("combobox");
    // The palette has asked the source and the answer has landed (not just "not loaded yet") before Snooze is checked.
    await waitFor(() => expect(mocked.loads).toBeGreaterThanOrEqual(2));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(screen.queryByText("Snooze this review")).toBeNull();
  });
});

describe("entries a mounted control adds", () => {
  function Extra({ run }: { run: () => void }) {
    useFlowPaletteExtras([{ id: "notes", label: "Notes", sub: "2 open", run }]);
    return null;
  }

  it("list under In this review while the control is mounted, and go with it", async () => {
    const run = vi.fn();
    const user = userEvent.setup();
    const view = render(<MemoryRouter><FlowPaletteProvider><Screen context={part} /><Extra run={run} /></FlowPaletteProvider></MemoryRouter>);
    await user.keyboard("{Control>}k{/Control}");
    const group = await screen.findByRole("group", { name: "In this review" });
    expect(group).toHaveTextContent("Notes");
    await user.click(screen.getByRole("option", { name: /Notes/ }));
    await waitFor(() => expect(run).toHaveBeenCalledOnce());
    view.rerender(<MemoryRouter><FlowPaletteProvider><Screen context={part} /></FlowPaletteProvider></MemoryRouter>);
    await user.keyboard("{Control>}k{/Control}");
    await screen.findByRole("combobox");
    expect(screen.queryByRole("option", { name: /Notes/ })).toBeNull();
  });
});

describe("FlowPaletteProvider", () => {
  it("opens on Ctrl+K from the keymap with the input focused, and closes on the second press", async () => {
    const user = userEvent.setup();
    mount();
    await user.keyboard("{Control>}k{/Control}");
    const box = await screen.findByRole("combobox", { name: "Search actions, parts, reviews and settings" });
    expect(box).toHaveFocus();
    expect(box).toHaveAttribute("placeholder", "Type a function name, a repo, or an action");
    await user.keyboard("{Control>}k{/Control}");
    await waitFor(() => expect(screen.queryByRole("combobox")).toBeNull());
  });

  it("opens on / on a review screen, without typing the slash into the search", async () => {
    const user = userEvent.setup();
    mount();
    await user.keyboard("/");
    const box = await screen.findByRole("combobox", { name: "Search actions, parts, reviews and settings" });
    expect(box).toHaveFocus();
    expect(box).toHaveValue("");
    // A second / is typed into the open search; it does not close the palette.
    await user.keyboard("/");
    expect(box).toHaveValue("/");
  });

  it("Send feedback opens the feedback dialog", async () => {
    const user = userEvent.setup();
    mount();
    await user.keyboard("{Control>}k{/Control}");
    await user.type(await screen.findByRole("combobox"), "Send feedback");
    await user.keyboard("{Enter}");
    expect(await screen.findByTestId("send-feedback-dialog")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Feedback" })).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("does nothing on a page no flow screen registered (Classic)", async () => {
    const user = userEvent.setup();
    mount(<button type="button">classic</button>);
    await user.keyboard("{Control>}k{/Control}");
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("does not open over another dialog", async () => {
    const user = userEvent.setup();
    mount(<><Screen context={part} /><div role="dialog" aria-modal="true" aria-label="Other">x</div></>);
    await user.keyboard("{Control>}k{/Control}");
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("shows the context group first and says 'No match' for a query nothing matches", async () => {
    const user = userEvent.setup();
    mount();
    await user.keyboard("{Control>}k{/Control}");
    const options = await screen.findAllByRole("option");
    expect(options[0]).toHaveTextContent("Looks good");
    expect(screen.getByText("This part · apply_discount")).toBeInTheDocument();
    await user.keyboard("xyzq");
    expect(screen.getByTestId("flow-palette-empty")).toHaveTextContent("No match for 'xyzq'. Try a function name, a repo, or an action.");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("Esc closes the palette and focus goes back to the control that was focused", async () => {
    const user = userEvent.setup();
    mount();
    screen.getByRole("button", { name: "opener" }).focus();
    await user.keyboard("{Control>}k{/Control}");
    await screen.findByRole("combobox");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("combobox")).toBeNull());
    expect(screen.getByRole("button", { name: "opener" })).toHaveFocus();
  });

  it("Enter runs the highlighted row's own function once and closes", async () => {
    const user = userEvent.setup();
    mount();
    await user.keyboard("{Control>}k{/Control}");
    await screen.findByRole("combobox");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(lookGood).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("shows the person's rebound keys on the rows", async () => {
    const user = userEvent.setup();
    const settings = await import("../settings/userSettings");
    vi.spyOn(settings, "getFlowBindings").mockReturnValue({ looksGood: "y", needsChange: "c", decideLater: "n" });
    mount();
    await user.keyboard("{Control>}k{/Control}");
    expect((await screen.findAllByRole("option"))[0]).toHaveTextContent(/Looks good\s*Y/);
    vi.restoreAllMocks();
  });

  it("lists the Reader's own Review actions under In this review on a part's screen too; a disabled one says why and does not run", async () => {
    const fixAll = vi.fn();
    const fixCi = vi.fn();
    function Reader() {
      useFlowPaletteReviewActions([
        { id: "correction-fix_all", label: "Fix all open findings (2)…", run: fixAll },
        { id: "correction-fix_ci_failure", label: "Fix CI failure…", detail: "no failing check on this revision", disabled: true, run: fixCi },
      ]);
      return null;
    }
    const user = userEvent.setup();
    mount(<><Reader /><Screen context={part} /></>);
    await user.keyboard("{Control>}k{/Control}");
    const group = await screen.findByRole("group", { name: "In this review" });
    const ci = screen.getAllByRole("option").find((option) => option.getAttribute("data-pal-item") === "Fix CI failure…")!;
    expect(group).toContainElement(ci);
    expect(ci).toHaveAttribute("aria-disabled", "true");
    expect(ci).toHaveTextContent("Fix CI failure… · no failing check on this revision");
    await user.click(ci);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fixCi).not.toHaveBeenCalled();
    expect(screen.getByRole("combobox")).toBeInTheDocument();
    await user.click(screen.getAllByRole("option").find((option) => option.getAttribute("data-pal-item") === "Fix all open findings (2)…")!);
    await waitFor(() => expect(fixAll).toHaveBeenCalledTimes(1));
  });

  it("arrow keys move the highlight", async () => {
    const user = userEvent.setup();
    mount();
    await user.keyboard("{Control>}k{/Control}");
    await screen.findByRole("combobox");
    await user.keyboard("{ArrowDown}");
    expect(screen.getAllByRole("option")[1]).toHaveAttribute("aria-selected", "true");
  });
});
