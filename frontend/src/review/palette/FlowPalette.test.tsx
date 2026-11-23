import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FlowPaletteProvider, useFlowPaletteScreen } from "./FlowPalette";
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

  it("arrow keys move the highlight", async () => {
    const user = userEvent.setup();
    mount();
    await user.keyboard("{Control>}k{/Control}");
    await screen.findByRole("combobox");
    await user.keyboard("{ArrowDown}");
    expect(screen.getAllByRole("option")[1]).toHaveAttribute("aria-selected", "true");
  });
});
