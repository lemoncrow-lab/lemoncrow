import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, vi } from "vitest";

import { budgetFixture, routeFixture } from "../test/intelligenceFixtures";
import * as reviewApi from "../review/reviewApi";
import Settings, { LOCAL_INTELLIGENCE_PATH } from "./Settings";

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
  it("badges Watchdogs as coming soon without labelling the rest", () => {
    renderAdvanced();

    const watchdogCard = screen.getByRole("link", { name: /Watchdogs/ });
    expect(within(watchdogCard).getByText("Coming soon")).toBeInTheDocument();

    for (const label of ["Hosts", "Agents", "Skills", "MCP", "Projection"]) {
      const card = screen.getByRole("link", { name: new RegExp(`^${label}`) });
      expect(within(card).queryByText("Coming soon")).not.toBeInTheDocument();
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
    expect(screen.getByRole("form", { name: "Select Review repository" })).toBeInTheDocument();
    expect(screen.getByText(/independently of LemonCrow Runtime and MCP/)).toBeInTheDocument();
  });
});

describe("Settings AI & background", () => {
  afterEach(() => {
    vi.restoreAllMocks();
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
      "intelligence.enabled": true,
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
      .mockResolvedValueOnce(original)
      .mockResolvedValueOnce(saved);
    renderAi();

    expect(await screen.findByRole("form", { name: "Configure local AI" })).toBeInTheDocument();
    const provider = screen.getByRole("combobox", { name: "Provider" });
    expect(provider).toHaveAttribute("data-value", "auto");
    fireEvent.click(provider);
    fireEvent.click(screen.getByRole("option", { name: "Claude Code" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: /Monthly model-call limit/ }), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "Save AI settings" }));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
    expect(request).toHaveBeenNthCalledWith(2, LOCAL_INTELLIGENCE_PATH + "/settings", expect.objectContaining({
      method: "PUT",
      body: JSON.stringify({ settings: saved.preferences }),
    }));
    expect(await screen.findByText(/Saved to this machine/)).toBeInTheDocument();
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
