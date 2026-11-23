import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./reviewApi", () => ({ requestBrowserJson: vi.fn().mockRejectedValue(new Error("no server")) }));
const hosted = vi.hoisted(() => ({ value: false }));
vi.mock("./hostedOnly", () => ({ isHostedWorkspace: () => Promise.resolve(hosted.value) }));

import { AiBudgetBanner } from "./AiBudgetBanner";
import {
  aiBlockedReason,
  budgetSavedToast,
  budgetStatusLine,
  budgetUseLine,
  parseAiBudget,
  parseBudgetInput,
  resetAiBudgetForTests,
  resetLabel,
  toastAiReason,
  usedUpBannerText,
  useAiGate,
  type AiBudget,
} from "./aiBudget";
import { currentFlowToast, dismissFlowToast, FlowToastHost } from "./flowToast";
import { flowAction } from "./flowKeymap";
import { FlowTopBar } from "./FlowTopBar";

const BASE: AiBudget = { monthly_budget_usd: 5, used_usd: 1.2, unreported_calls: 0, complete: true, resets_on: "2026-12-01", may_run: true, reason_code: "", reason: "", organization: "" };
const ZERO: AiBudget = { ...BASE, monthly_budget_usd: 0, used_usd: 0, may_run: false, reason_code: "budget_zero", reason: "AI budget is $0" };
const USED_UP: AiBudget = { ...BASE, used_usd: 5.1, may_run: false, reason_code: "budget_used_up", reason: "AI budget used up until Dec 1" };

const ORG_USED_UP: AiBudget = { ...BASE, monthly_budget_usd: 500, used_usd: 500, may_run: false, reason_code: "org_cap_used_up", reason: "AI hints paused for Acme until Dec 1 · reviews still work", organization: "Acme" };
const ORG_ZERO: AiBudget = { ...ORG_USED_UP, monthly_budget_usd: 0, used_usd: 0, reason_code: "org_cap_zero", reason: "AI hints paused for Acme · reviews still work" };

beforeEach(() => { hosted.value = false; resetAiBudgetForTests(); });
afterEach(() => { cleanup(); dismissFlowToast(); resetAiBudgetForTests(); });

describe("a hosted organization's cost cap builds on the same budget (P9-5)", () => {
  it("names the organization and the date, or says 'your organization' when there is no name", () => {
    expect(aiBlockedReason(ORG_USED_UP)).toBe("AI hints paused for Acme until Dec 1");
    expect(aiBlockedReason(ORG_ZERO)).toBe("AI hints paused for Acme");
    expect(aiBlockedReason({ ...ORG_USED_UP, organization: "" })).toBe("AI hints paused for your organization until Dec 1");
  });

  it("the used-up banner is SPEC's line and links to the organization page; $0 shows no banner", () => {
    resetAiBudgetForTests(ORG_USED_UP);
    const first = render(<MemoryRouter><AiBudgetBanner /></MemoryRouter>);
    expect(screen.getByTestId("ai-budget-banner").textContent).toContain("AI hints paused for Acme until Dec 1 · reviews still work");
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings/org");
    first.unmount();
    resetAiBudgetForTests(ORG_ZERO);
    render(<MemoryRouter><AiBudgetBanner /></MemoryRouter>);
    expect(screen.queryByTestId("ai-budget-banner")).toBeNull();
  });

  it("a hosted workspace asks for the organization's cap, never the personal budget", async () => {
    hosted.value = true;
    const api = await import("./reviewApi");
    const request = vi.mocked(api.requestBrowserJson);
    request.mockReset().mockResolvedValue({ ai_budget: ORG_USED_UP });
    render(<MemoryRouter><AiBudgetBanner /></MemoryRouter>);
    expect((await screen.findByTestId("ai-budget-banner")).textContent).toContain("AI hints paused for Acme until Dec 1");
    expect(request).toHaveBeenCalledWith("/v1/organization/ai-cap");
    expect(request).not.toHaveBeenCalledWith("/api/review-intelligence");
  });
});

describe("the AI budget model (OWNER-3 copy)", () => {
  it("reads the server block and nothing else", () => {
    expect(parseAiBudget({ ai_budget: { ...BASE } })).toEqual(BASE);
    expect(parseAiBudget({ ai_budget: { ...BASE, monthly_budget_usd: null } })?.monthly_budget_usd).toBeNull();
    for (const raw of [null, "x", {}, { ai_budget: null }, { ai_budget: { may_run: true } }, { ai_budget: { ...BASE, may_run: "yes" } }]) expect(parseAiBudget(raw)).toBeNull();
  });

  it("names the reason a new AI run cannot start, and says nothing while the answer is unknown", () => {
    expect(aiBlockedReason(null)).toBe("");
    expect(aiBlockedReason(BASE)).toBe("");
    expect(aiBlockedReason(ZERO)).toBe("AI budget is $0");
    expect(aiBlockedReason(USED_UP)).toBe("AI budget used up until Dec 1");
    expect(aiBlockedReason({ ...ZERO, reason_code: "budget_invalid", reason: "intelligence.monthly_budget_usd must be a number" })).toContain("must be a number");
  });

  it("status, use, toast and banner lines are SPEC's", () => {
    expect(budgetStatusLine(5)).toBe("AI stops at $5/month. Reviews still work.");
    expect(budgetStatusLine(12.5)).toBe("AI stops at $12.50/month. Reviews still work.");
    expect(budgetStatusLine(0)).toBe("$0 · no new AI runs. AI results you already have stay.");
    expect(budgetStatusLine(null)).toBe("No limit.");
    expect(budgetUseLine({ used_usd: 1.2, unreported_calls: 0 })).toBe("$1.20 used this month");
    expect(budgetUseLine({ used_usd: 1.2, unreported_calls: 1 })).toBe("$1.20 used this month · 1 call with cost not reported");
    expect(budgetUseLine({ used_usd: 0, unreported_calls: 3 })).toBe("$0.00 used this month · 3 calls with cost not reported");
    expect(budgetUseLine({ used_usd: 0.004, unreported_calls: 0 })).toBe("<$0.01 used this month");
    expect(budgetSavedToast(5)).toBe("AI budget saved · $5/month");
    expect(budgetSavedToast(null)).toBe("AI budget removed · no limit");
    expect(usedUpBannerText("2026-12-01")).toBe("AI budget used up until Dec 1 · reviews and AI results you already have still work");
    expect(resetLabel("2027-01-01")).toBe("Jan 1");
  });

  it("the field: empty is no limit, 0 and decimals are amounts, anything else is invalid (min 0)", () => {
    expect(parseBudgetInput("")).toBeNull();
    expect(parseBudgetInput("  ")).toBeNull();
    expect(parseBudgetInput("0")).toBe(0);
    expect(parseBudgetInput("7.5")).toBe(7.5);
    for (const bad of ["-1", "-0", "abc", "1e3", "1,5", "Infinity", "NaN", "1e999", "2000000000"]) expect(parseBudgetInput(bad)).toBeUndefined();
  });
});

describe("reading the answer", () => {
  async function world() {
    vi.useFakeTimers();
    const api = await import("./reviewApi");
    return vi.mocked(api.requestBrowserJson);
  }
  const twoScreens = () => render(<MemoryRouter><AiBudgetBanner /><AiBudgetBanner /></MemoryRouter>);

  afterEach(() => vi.useRealTimers());

  it("asks again when the first read finds no answer, and stops once it has one", async () => {
    const read = (await world()).mockReset().mockRejectedValueOnce(new Error("not paired yet")).mockResolvedValue({ ai_budget: USED_UP });
    render(<MemoryRouter><AiBudgetBanner /></MemoryRouter>);
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.queryByTestId("ai-budget-banner")).toBeNull();
    await vi.advanceTimersByTimeAsync(1_100);
    expect(screen.getByTestId("ai-budget-banner")).toBeTruthy();
    const asked = read.mock.calls.length;
    await vi.advanceTimersByTimeAsync(40_000);
    expect(read.mock.calls.length).toBe(asked);
  });

  it("a server that answers with no budget block (older, hosted) is asked once, however many screens mount", async () => {
    const read = (await world()).mockReset().mockResolvedValue({ preferences: {} });
    twoScreens();
    await vi.advanceTimersByTimeAsync(3_000);
    expect(read).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("ai-budget-banner")).toBeNull(); // no budget is shown
    cleanup();
    twoScreens(); // a remount within the freshness window does not ask again either
    await vi.advanceTimersByTimeAsync(1_000);
    expect(read).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(60_000); // and nothing is retried: it did answer
    expect(read).toHaveBeenCalledTimes(1);
  });

  it("a server that never answers is asked on one shared schedule, not once per screen", async () => {
    const read = (await world()).mockReset().mockRejectedValue(Object.assign(new Error("down"), { status: 503 }));
    twoScreens();
    await vi.advanceTimersByTimeAsync(120_000);
    expect(read).toHaveBeenCalledTimes(1 + 5); // the first read and the five waits, for both screens together
    cleanup();
    twoScreens(); // a remount joins the schedule that already ended
    await vi.advanceTimersByTimeAsync(120_000);
    expect(read.mock.calls.length).toBeLessThanOrEqual(1 + 5 + 1);
  });

  it("a 4xx other than 401 stops at once; a 401 (not paired yet) is asked again", async () => {
    const read = (await world()).mockReset().mockRejectedValue(Object.assign(new Error("gone"), { status: 404 }));
    twoScreens();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read).toHaveBeenCalledTimes(1);
    cleanup();
    resetAiBudgetForTests();
    read.mockReset().mockRejectedValueOnce(Object.assign(new Error("unauthorized"), { status: 401 })).mockResolvedValue({ ai_budget: USED_UP });
    twoScreens();
    await vi.advanceTimersByTimeAsync(1_100);
    expect(screen.getAllByTestId("ai-budget-banner").length).toBe(2);
  });

  it("leaving the screens ends the schedule", async () => {
    const read = (await world()).mockReset().mockRejectedValue(new Error("down"));
    const view = twoScreens();
    await vi.advanceTimersByTimeAsync(1_100);
    view.unmount();
    const asked = read.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read.mock.calls.length).toBe(asked);
  });
});

describe("the used-up banner (ST-2)", () => {
  const banner = () => render(<MemoryRouter><AiBudgetBanner /></MemoryRouter>);

  it("shows for a used-up budget, with the date and a Settings link, and says nothing is lost", () => {
    resetAiBudgetForTests(USED_UP);
    banner();
    const node = screen.getByTestId("ai-budget-banner");
    expect(node.textContent).toContain("AI budget used up until Dec 1 · reviews and AI results you already have still work");
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings/review#ai-help");
    expect(node).not.toHaveAttribute("role"); // the toast host is the only announcer
  });

  it.each([["no answer yet", null], ["room left", BASE], ["a $0 budget (a disabled Ask, no banner)", ZERO]])("is absent with %s", (_name, state) => {
    resetAiBudgetForTests(state);
    banner();
    expect(screen.queryByTestId("ai-budget-banner")).toBeNull();
  });

  it("appears when the shared answer changes, and goes when the budget is raised", () => {
    resetAiBudgetForTests(BASE);
    banner();
    expect(screen.queryByTestId("ai-budget-banner")).toBeNull();
    act(() => resetAiBudgetForTests(USED_UP));
    expect(screen.getByTestId("ai-budget-banner")).toBeTruthy();
    act(() => resetAiBudgetForTests(BASE));
    expect(screen.queryByTestId("ai-budget-banner")).toBeNull();
  });
});

describe("Ask stays visible and is disabled with the reason (ST-1, FOC-7)", () => {
  function bar(onAsk = vi.fn()) {
    render(
      <MemoryRouter>
        <FlowTopBar repo="shop" documentTitle="Overview" title="Review" revisionNumber={0} back={{ text: "Overview", onClick: vi.fn() }} onShortcuts={vi.fn()} onAsk={onAsk} askKey />
        <FlowToastHost />
      </MemoryRouter>,
    );
    return onAsk;
  }

  it("at $0 the top bar's Ask is there, reads as disabled, names why, and a press toasts the reason without asking", () => {
    resetAiBudgetForTests(ZERO);
    const onAsk = bar();
    const ask = screen.getByRole("button", { name: "Ask about the change" });
    expect(ask).toHaveAttribute("aria-disabled", "true");
    expect(ask).toHaveAttribute("title", "AI budget is $0");
    fireEvent.click(ask);
    expect(onAsk).not.toHaveBeenCalled();
    expect(screen.getByTestId("flow-toast").textContent).toContain("AI budget is $0");
  });

  it("used up names the date; with room it asks", () => {
    resetAiBudgetForTests(USED_UP);
    bar();
    expect(screen.getByRole("button", { name: "Ask about the change" })).toHaveAttribute("title", "AI budget used up until Dec 1");
    cleanup();
    resetAiBudgetForTests(BASE);
    const onAsk = bar();
    const ask = screen.getByRole("button", { name: "Ask about the change" });
    expect(ask).not.toHaveAttribute("aria-disabled");
    fireEvent.click(ask);
    expect(onAsk).toHaveBeenCalledTimes(1);
  });

  it("the gate wraps any AI action the same way", () => {
    resetAiBudgetForTests(ZERO);
    const seen: Array<ReturnType<typeof useAiGate>> = [];
    const action = vi.fn();
    function Probe() { const gate = useAiGate(); seen.push(gate); return <button type="button" {...gate.attrs} onClick={gate.run(action)}>go</button>; }
    render(<Probe />);
    fireEvent.click(screen.getByRole("button", { name: "go" }));
    expect(action).not.toHaveBeenCalled();
    expect(currentFlowToast()?.text).toBe("AI budget is $0");
    expect(seen.at(-1)?.run(undefined)).toBeUndefined();
  });

  it("Q keeps its place in the legend context and answers with the reason through flowAction", () => {
    const key = { key: "q" };
    for (const screenName of ["overview", "one", "quickCheck", "allFiles", "welcomeBack"] as const) {
      expect(flowAction(key, { screen: screenName, aiAsk: true, aiBlocked: "AI budget is $0" })).toEqual({ intent: "aiUnavailable", reason: "AI budget is $0" });
      expect(flowAction(key, { screen: screenName, aiAsk: true })).toEqual({ intent: "ask" });
    }
    // No Ask at all (review.ask absent): Q does nothing, blocked or not.
    expect(flowAction(key, { screen: "one", aiAsk: false, aiBlocked: "AI budget is $0" })).toBeNull();
    // Typing in a field is never a shortcut.
    expect(flowAction(key, { screen: "one", aiAsk: true, aiBlocked: "x", focus: "field" })).toBeNull();
    toastAiReason("AI budget is $0");
    expect(currentFlowToast()?.text).toBe("AI budget is $0");
  });
});
