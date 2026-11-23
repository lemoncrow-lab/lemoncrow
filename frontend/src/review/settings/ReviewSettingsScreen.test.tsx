import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const request = vi.fn();
vi.mock("../reviewApi", () => ({
  requestBrowserJson: (...args: unknown[]) => request(...args),
  fetchReviewDirectory: () => Promise.resolve({ reviews: [] }),
  fetchOverview: () => Promise.reject(new Error("no review")),
}));

import { DEFAULT_SETTINGS } from "./settingsModel";
import { ReviewSettingsScreen } from "./ReviewSettingsScreen";
import { resetAiBudgetForTests } from "../aiBudget";
import { resetUserSettingsForTest } from "./userSettings";

/** A tiny stand-in for the server: it keeps what was patched and refuses a taken key as the real one does. */
function server(aiInit: Record<string, unknown> = {}) {
  let stored: Record<string, unknown> = {};
  let ai: Record<string, unknown> = { "intelligence.monthly_budget_usd": null, "intelligence.run_on": "local", "intelligence.driver": "ollama", ...aiInit };
  const budgetBlock = () => {
    const limit = ai["intelligence.monthly_budget_usd"] as number | null;
    return { monthly_budget_usd: limit, used_usd: 1.2, unreported_calls: 2, complete: false, calls: 5, period_start: "2026-11-01", resets_on: "2026-12-01", may_run: limit !== 0, reason_code: limit === 0 ? "budget_zero" : "", reason: limit === 0 ? "AI budget is $0" : "" };
  };
  request.mockImplementation(async (path: string, init?: RequestInit) => {
    if (path.startsWith("/api/review-intelligence")) {
      if (init?.method === "PUT") ai = { ...ai, ...(JSON.parse(String(init.body)) as { settings: Record<string, unknown> }).settings };
      return { preferences: ai, ai_budget: budgetBlock() };
    }
    if (init?.method === "PATCH") {
      const patch = JSON.parse(String(init.body)) as Record<string, unknown>;
      const next = { ...stored };
      for (const [key, value] of Object.entries(patch)) {
        if (value === null) delete next[key];
        else next[key] = value;
      }
      const taken = ["key_looks_good", "key_needs_change", "key_decide_later"].filter((key) => key in patch && Object.entries({ ...DEFAULT_SETTINGS, ...next }).some(([other, v]) => other !== key && other.startsWith("key_") && v === patch[key]));
      if (taken.length > 0) throw Object.assign(new Error("taken"), { status: 422 });
      stored = next;
    }
    return { settings: { ...DEFAULT_SETTINGS, ...stored }, set: Object.keys(stored) };
  });
}

function show(hash = "") {
  return render(<MemoryRouter initialEntries={[`/settings/review${hash}`]}><ReviewSettingsScreen /></MemoryRouter>);
}

beforeEach(() => {
  window.localStorage.clear();
  resetUserSettingsForTest();
  resetAiBudgetForTests();
  request.mockReset();
  server();
});
afterEach(cleanup);

describe("Settings address with a section", () => {
  it("focuses the named section's heading", async () => {
    show("#look");
    await screen.findByRole("heading", { name: "You" });
    await waitFor(() => expect(screen.getByRole("heading", { level: 2, name: "Look" })).toHaveFocus());
  });
  it("focuses the page heading when the hash names no section (SPEC 3.6)", async () => {
    show("#nothing-here");
    await waitFor(() => expect(screen.getByRole("heading", { level: 1, name: "You" })).toHaveFocus());
  });
});

describe("Settings › You (SET-1, SET-3, SET-6)", () => {
  it("lists You's sections and Review policy, and none of the prototype's tools", async () => {
    show();
    await screen.findByRole("heading", { name: "You" });
    for (const name of ["Reading", "AI help", "Notifications", "Keyboard", "Look", "More settings", "Review policy"]) {
      expect(screen.getAllByRole("heading", { name }).length).toBeGreaterThan(0);
    }
    expect(screen.queryByText(/Prototype tools|Preview state|Simulate/)).toBeNull();
  });

  it("saves the default view to the server", async () => {
    show();
    const group = await screen.findByRole("radiogroup", { name: "Default view" });
    fireEvent.click(within(group).getByRole("radio", { name: "All files" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/api/review-settings", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ default_view: "all" }) })));
  });

  it("rebinds Looks good to Y and every hint on the page follows", async () => {
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Change the key for Looks good" }));
    expect(screen.getByTestId("settings-capture").textContent).toBe("Press the new key for Looks good. Esc cancels.");
    fireEvent.keyDown(window, { key: "y" });
    await waitFor(() => expect(screen.getByTestId("settings-message").textContent).toBe("Looks good is now Y"));
    expect(screen.getByTestId("settings-key-line").textContent).toBe("Y good · C change · N decide later · J / K move · ? all shortcuts");
  });

  it("names the conflict and keeps the old key", async () => {
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Change the key for Looks good" }));
    fireEvent.keyDown(window, { key: "c" });
    expect(screen.getByTestId("settings-capture-error").textContent).toBe("C is already used for Needs a change. Pick another key.");
    expect(screen.getByTestId("settings-key-line").textContent).toContain("A good");
  });

  it("resets the three keys to A, C and N", async () => {
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Change the key for Looks good" }));
    fireEvent.keyDown(window, { key: "y" });
    await waitFor(() => expect(screen.getByTestId("settings-key-line").textContent).toContain("Y good"));
    fireEvent.click(screen.getByRole("button", { name: "Reset to defaults" }));
    await waitFor(() => expect(screen.getByTestId("settings-message").textContent).toBe("Keys reset to A, C and N"));
    expect(screen.getByTestId("settings-key-line").textContent).toContain("A good");
  });

  it("says so, with what the browser remembers, when the server cannot be reached", async () => {
    request.mockReset();
    request.mockRejectedValue(new Error("Failed to fetch"));
    show();
    expect((await screen.findByTestId("settings-offline")).textContent).toContain("Couldn't reach this machine's server");
  });
});

describe("AI help (the machine's real AI settings)", () => {
  it("has no AI switch; it shows the AI budget field with the status and use lines (OWNER-1, OWNER-3)", async () => {
    show();
    const field = await screen.findByLabelText("AI budget ($ per month)");
    expect(screen.queryByRole("switch", { name: "AI help" })).toBeNull();
    expect(screen.getByTestId("settings-ai-budget-status").textContent).toBe("No limit.");
    expect(screen.getByTestId("settings-ai-budget-use").textContent).toBe("$1.20 used this month · 2 calls with cost not reported");
    expect(screen.getByTestId("settings-ai-budget-scope").textContent).toMatch(/^This covers all AI help, asked for or automatic\. .*model-call limits in AI & background\.$/);
    expect((field as HTMLInputElement).value).toBe("");
    expect(field).toHaveAttribute("min", "0");
  });

  it("saves a budget, toasts it, and shows the new status line", async () => {
    show();
    const field = await screen.findByLabelText("AI budget ($ per month)");
    fireEvent.change(field, { target: { value: "12.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/api/review-intelligence/settings", expect.objectContaining({ method: "PUT", body: JSON.stringify({ settings: { "intelligence.monthly_budget_usd": 12.5 } }) })));
    expect((await screen.findByTestId("flow-toast")).textContent).toContain("AI budget saved · $12.50/month");
    await waitFor(() => expect(screen.getByTestId("settings-ai-budget-status").textContent).toBe("AI stops at $12.50/month. Reviews still work."));
  });

  it("saves $0 as a number (not as no limit) and says nothing already made is lost", async () => {
    show();
    fireEvent.change(await screen.findByLabelText("AI budget ($ per month)"), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/api/review-intelligence/settings", expect.objectContaining({ body: JSON.stringify({ settings: { "intelligence.monthly_budget_usd": 0 } }) })));
    expect((await screen.findByTestId("flow-toast")).textContent).toContain("AI budget saved · $0/month");
    await waitFor(() => expect(screen.getByTestId("settings-ai-budget-status").textContent).toBe("$0 · no new AI runs. AI results you already have stay."));
  });

  it("an empty field removes the limit (null) and a negative or non-number is refused without a request", async () => {
    server({ "intelligence.monthly_budget_usd": 5 });
    show();
    const field = await screen.findByLabelText("AI budget ($ per month)");
    await waitFor(() => expect((field as HTMLInputElement).value).toBe("5"));
    for (const bad of ["-1", "abc", "1e3"]) {
      // A real number field answers "" for typed junk and says so in validity.badInput; that is not "no limit".
      Object.defineProperty(field, "validity", { configurable: true, value: { badInput: bad === "abc" } });
      fireEvent.change(field, { target: { value: bad === "abc" ? "" : bad } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      expect(screen.getByTestId("settings-ai-budget-invalid").textContent).toBe("Enter 0 or more, or leave it empty for no limit.");
    }
    expect(request.mock.calls.some(([path, init]) => String(path).endsWith("/settings") && (init as RequestInit | undefined)?.method === "PUT")).toBe(false);
    // The error is announced once, by the shared toast host (an alert), and the field only points at its visible text.
    expect(screen.getByTestId("flow-toast")).toHaveAttribute("role", "alert");
    expect(screen.getByTestId("flow-toast").textContent).toContain("Enter 0 or more");
    expect(document.querySelector("[data-testid='ai-budget'] [role='alert'], [data-testid='ai-budget'] [role='status']")).toBeNull();
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field.getAttribute("aria-describedby")).toContain(screen.getByTestId("settings-ai-budget-invalid").id);
    Object.defineProperty(field, "validity", { configurable: true, value: { badInput: false } });
    fireEvent.change(field, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/api/review-intelligence/settings", expect.objectContaining({ body: JSON.stringify({ settings: { "intelligence.monthly_budget_usd": null } }) })));
    expect((await screen.findByTestId("flow-toast")).textContent).toContain("AI budget removed · no limit");
  });

  it("writes where AI jobs run, and shows worker and managed as they are, unchangeable here", async () => {
    show();
    fireEvent.click(await screen.findByRole("radio", { name: "Automatic" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/api/review-intelligence/settings", expect.objectContaining({ body: JSON.stringify({ settings: { "intelligence.run_on": "auto" } }) })));
    expect((screen.getByRole("radio", { name: "A worker" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("radio", { name: "Managed by LemonCrow" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("never says the model is on this machine when a provider is configured, even with run_on local", async () => {
    server({ "intelligence.driver": "claude", "intelligence.run_on": "local" });
    show();
    const line = await screen.findByTestId("settings-ai-model");
    expect(line.textContent).toBe("Model: claude, a provider. Code in AI checks can be sent to it.");
    expect(screen.getByTestId("review-settings").textContent).not.toMatch(/Local model|on this machine/i);
  });
});

describe("Review policy", () => {
  it("says honestly that there is no review to read rules from", async () => {
    show();
    await waitFor(() => expect(screen.getByTestId("settings-policy-note").textContent).toBe("No review has been opened yet, so there are no rules to show."));
  });
});

describe("a server that keeps no personal settings", () => {
  it("says so instead of blaming the connection", async () => {
    request.mockReset();
    request.mockRejectedValue(Object.assign(new Error("404"), { status: 404 }));
    show();
    expect((await screen.findByTestId("settings-unavailable")).textContent).toContain("doesn't keep personal settings");
  });
});
