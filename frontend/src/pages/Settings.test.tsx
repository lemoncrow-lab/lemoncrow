import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, vi } from "vitest";

import { budgetFixture, routeFixture } from "../test/intelligenceFixtures";
import { currentAiBudget, resetAiBudgetForTests, type AiBudget } from "../review/aiBudget";
import * as reviewApi from "../review/reviewApi";
import Settings, { LOCAL_INTELLIGENCE_PATH } from "./Settings";

const KNOWN_BUDGET: AiBudget = { monthly_budget_usd: 5, used_usd: 5.2, unreported_calls: 0, complete: true, resets_on: "2026-12-01", may_run: false, reason_code: "budget_used_up", reason: "AI budget used up until Dec 1" };

function renderAdvanced() {
  return render(
    <MemoryRouter initialEntries={["/settings/advanced"]}>
      <Routes>
        <Route path="/settings/:section" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

describe("Settings advanced cards", () => {
  it("lists only the inspectors that work, without Watchdogs or Projection", () => {
    renderAdvanced();

    for (const label of ["Hosts", "Agents", "Skills", "MCP"]) {
      const card = screen.getByRole("link", { name: new RegExp(`^${label}`) });
      expect(within(card).queryByText("Coming soon")).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("link", { name: /Watchdogs/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Projection/ })).not.toBeInTheDocument();
  });
});

describe("Settings section addresses", () => {
  it("/settings/<a Review section> opens the Review settings at that section, never the Advanced page", () => {
    function Where() { const location = useLocation(); return <output data-testid="where">{location.pathname + location.hash}</output>; }
    for (const section of ["reading", "notifications", "keyboard", "look", "more", "policy"]) {
      const view = render(
        <MemoryRouter initialEntries={[`/settings/${section}`]}>
          <Routes><Route path="/settings/:section" element={<><Settings /><Where /></>} /></Routes>
        </MemoryRouter>
      );
      expect(screen.getByTestId("where")).toHaveTextContent(`/settings/review#${section}`);
      view.unmount();
    }
  });
});

describe("Settings Review connections", () => {
  it("remains accessible as a standalone Review settings section without Runtime", () => {
    render(
      <MemoryRouter initialEntries={["/settings/devices"]}>
        <Routes><Route path="/settings/:section" element={<Settings />} /></Routes>
      </MemoryRouter>,
    );
    expect(screen.getByRole("heading", { name: "Review connections" })).toBeInTheDocument();
    // A local LemonCrow has no Hosted Review account: one line on how to connect, not the pairing form.
    expect(screen.getByTestId("review-connections-no-account")).toHaveTextContent(/lcr connect/);
    expect(screen.queryByRole("form", { name: "Select Review repository" })).not.toBeInTheDocument();
    expect(screen.getByText(/independently of Workspaces and MCP/)).toBeInTheDocument();
  });
});

describe("Settings AI & background", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    resetAiBudgetForTests();
  });

  function renderAi() {
    return render(
      <MemoryRouter initialEntries={["/settings/ai"]}>
        <Routes>
          <Route path="/settings/:section" element={<Settings />} />
        </Routes>
      </MemoryRouter>
    );
  }

  it("reads this machine\'s route and budget from the local Review service", async () => {
    const request = vi
      .spyOn(reviewApi, "requestBrowserJson")
      .mockResolvedValue({
        scope: "local",
        intelligence: routeFixture(),
        background: budgetFixture({
          managed_spend: {
            applies: false,
            enforced: false,
            note: "no LemonCrow-managed execution here",
          },
        }),
        lifetime:
          "Background work runs only while LemonCrow is running on this machine; nothing runs while it is stopped.",
      });
    renderAi();

    expect(
      await screen.findByTestId("intelligence-overview")
    ).toBeInTheDocument();
    expect(request).toHaveBeenCalledWith(LOCAL_INTELLIGENCE_PATH);
    expect(
      screen.getByText(/only while LemonCrow is running on this machine/)
    ).toBeInTheDocument();
    expect(screen.getByText("This machine")).toBeInTheDocument();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
  it("edits local AI preferences and refreshes the effective route without exposing secrets", async () => {
    const preferences = {
      "intelligence.monthly_budget_usd": null,
      "intelligence.driver": "auto",
      "intelligence.model": "auto",
      "intelligence.run_on": "auto",
      "intelligence.background.enabled": true,
      "intelligence.background.monthly_model_calls": 200,
      "intelligence.background.daily_model_calls": -1,
    };
    const original = {
      scope: "local" as const,
      intelligence: routeFixture({ editable: true }),
      background: budgetFixture(),
      preferences,
      lifetime: "Runs while this machine is on.",
    };
    const saved = {
      ...original,
      preferences: { ...preferences, "intelligence.driver": "claude", "intelligence.background.monthly_model_calls": 100 },
    };
    const request = vi.spyOn(reviewApi, "requestBrowserJson")
      .mockImplementation(async (_path: string, init?: RequestInit) => (init?.method === "PUT" ? saved : original) as never);
    resetAiBudgetForTests(KNOWN_BUDGET);
    renderAi();

    expect(await screen.findByRole("form", { name: "Configure local AI" })).toBeInTheDocument();
    // The retired switch is gone from the form (OWNER-1). The dollar budget has one control, in Review flow › AI help;
    // this page links there instead of repeating it.
    expect(screen.queryByRole("checkbox", { name: /Enable AI intelligence/ })).toBeNull();
    expect(screen.queryByRole("form", { name: "AI budget" })).toBeNull();
    expect(screen.getByRole("link", { name: "Review flow › AI help" })).toHaveAttribute("href", "/settings/review#ai-help");
    // Two limits, each named for what it counts (review-23.5).
    expect(screen.getByText(/The limits below count model calls, and only for automatic background work\./)).toBeInTheDocument();
    // One control for background work; where it runs is Review flow's "Where AI jobs run".
    expect(screen.queryByRole("combobox", { name: "Run background work" })).toBeNull();
    expect(screen.getByRole("checkbox", { name: "Run background work on this machine" })).toBeChecked();
    // -1 is "no limit", shown as a checkbox rather than a magic number.
    expect(screen.getByRole("checkbox", { name: "No daily limit" })).toBeChecked();
    expect(screen.getByRole("spinbutton", { name: /Daily model-call limit/ })).toBeDisabled();
    expect(screen.queryByText(/means unlimited/)).toBeNull();
    const provider = screen.getByRole("combobox", { name: "Provider" });
    expect(provider).toHaveAttribute("data-value", "auto");
    fireEvent.click(provider);
    fireEvent.click(screen.getByRole("option", { name: "Claude Code" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: /Monthly model-call limit/ }), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "Save AI settings" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith(LOCAL_INTELLIGENCE_PATH + "/settings", expect.objectContaining({ method: "PUT" })));
    expect(request).toHaveBeenCalledWith(LOCAL_INTELLIGENCE_PATH + "/settings", expect.objectContaining({
      method: "PUT",
      // The form writes only its own fields: never the budget (a stale copy must not overwrite what the budget
      // control saved) and never where AI runs (Review flow owns it).
      body: JSON.stringify({ settings: {
        "intelligence.driver": "claude",
        "intelligence.model": "auto",
        "intelligence.background.enabled": true,
        "intelligence.background.monthly_model_calls": 100,
        "intelligence.background.daily_model_calls": -1,
      } }),
    }));
    // A save whose answer carries no budget block must not wipe the shared budget the banner and Ask read.
    expect(currentAiBudget()).toEqual(KNOWN_BUDGET);
    expect(await screen.findByText(/AI settings saved/)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Provider" })).toHaveAttribute("data-value", "claude");
    expect(screen.getByTestId("intelligence-edit-source")).not.toHaveTextContent("Read-only here");
  });

  it("offers a retry when the local service cannot answer", async () => {
    vi.spyOn(reviewApi, "requestBrowserJson").mockRejectedValue(
      new Error("503")
    );
    renderAi();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not read AI settings: 503"
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
