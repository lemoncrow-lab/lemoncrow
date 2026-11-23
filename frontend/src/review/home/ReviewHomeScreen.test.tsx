import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchReviewDirectory, fetchReviewJournal, putReviewQueueState } from "../reviewApi";
import type { ReviewDirectoryPage, ReviewJournal } from "../types";
import { cameBackRow, queueRow, waitingRow } from "./homeFixtures";
import { HOME_PREFS_KEY } from "./homePrefs";
import ReviewHomeScreen from "./ReviewHomeScreen";

vi.mock("../reviewApi", () => ({
  fetchReviewDirectory: vi.fn(),
  fetchReviewJournal: vi.fn(),
  putReviewQueueState: vi.fn(),
  reviewHref: (ref: string) => `/r/${ref.replace(/^r\//, "")}`,
}));
vi.mock("../reviewEnvironment", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../reviewEnvironment")>()),
  fetchReviewEnvironment: () => Promise.resolve({ mode: "local", discovered: true, review: {}, unsupported: [] }),
}));
const directory = vi.mocked(fetchReviewDirectory);
const journal = vi.mocked(fetchReviewJournal);
const put = vi.mocked(putReviewQueueState);

function page(reviews: ReturnType<typeof queueRow>[]): ReviewDirectoryPage {
  return { reviews, status: "all", query: "", next_cursor: "", teams_supported: false } as unknown as ReviewDirectoryPage;
}
function Location() { return <output data-testid="location">{useLocation().pathname}</output>; }
function show() {
  return render(<MemoryRouter initialEntries={["/reviews"]}><ReviewHomeScreen /><Location /></MemoryRouter>);
}
async function ready() {
  await screen.findByTestId("review-home");
  await waitFor(() => expect(screen.queryByText("Loading reviews…")).toBeNull());
}
const TASK: ReviewJournal = {
  available: true, reason: "", review_id: "a", revision_id: "rev", revision_number: 0, session_id: "s", host: "claude",
  entries: [{ id: "1", kind: "intent", summary: "Let guests view a guest checkout order", path: "", status: "", detail: "", at: "", source: "run_ledger" }],
} as unknown as ReviewJournal;

beforeEach(() => {
  window.localStorage.clear();
  directory.mockReset().mockResolvedValue(page([queueRow({ id: "a", title: "Change can_view_order" }), queueRow({ id: "b", title: "Retry webhook delivery" })]));
  journal.mockReset().mockResolvedValue(TASK);
  put.mockReset().mockResolvedValue({ snoozed_until: "", resume: null });
});
afterEach(cleanup);

describe("RR-CLAIM: a Home that cannot load says so", () => {
  it("leaves loading on a final refusal and shows the reconnect message with a retry", async () => {
    directory.mockRejectedValue(Object.assign(new Error("This browser is not connected to LemonCrow Review. Run `lc review --open` in your terminal to reconnect."), { status: 401 }));
    show();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("lc review --open");
    expect(screen.getByTestId("review-home")).toHaveAttribute("data-home", "error");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("Home error: plain words, the code on its own line, no key legend", () => {
  it("a bare status code becomes a sentence; the legend is hidden while nothing is listed", async () => {
    directory.mockRejectedValue(Object.assign(new Error("500"), { status: 500 }));
    show();
    const alert = await screen.findByTestId("home-load-failed");
    expect(alert).toHaveTextContent("Couldn't load your reviews. The local server returned an error.");
    expect(alert).toHaveTextContent("Server answered 500.");
    expect(screen.queryByTestId("home-legend")).toBeNull();
  });

  it("a command in the message is drawn as code, not as backticks", async () => {
    directory.mockRejectedValue(new Error("Run `lc review --open` in your terminal to reconnect."));
    show();
    const alert = await screen.findByTestId("home-load-failed");
    expect(alert.textContent).not.toContain("`");
    expect(within(alert).getByText("lc review --open").tagName).toBe("CODE");
  });
});

describe("P7-3: the policy warning and the hosted-only bell", () => {
  it("a change that edits the review policy says so, in red, on its row and on the hero (POL-1)", async () => {
    directory.mockResolvedValue(page([queueRow({ id: "a", title: "Loosen review policy for payments", edits_policy: true }), queueRow({ id: "b", title: "Retry webhook delivery" })]));
    show();
    await ready();
    const warnings = await screen.findAllByTestId("home-edits-policy");
    expect(warnings.map((node) => node.textContent)).toEqual(["This change edits the review policy"]);
    expect(warnings[0].closest("[data-row]")?.getAttribute("data-row")).toBe("a");
  });

  it("a local install draws no bell and asks for no notifications (SPEC 7.1 P0-2)", async () => {
    show();
    await ready();
    expect(screen.queryByTestId("notification-bell")).toBeNull();
    expect(screen.queryByRole("button", { name: /^Notifications/ })).toBeNull();
  });
});

describe("Home sections and hero", () => {
  it("shows the hero with a time range and the untrusted-task line, and the budget as a range (HOME-1)", async () => {
    show();
    await ready();
    expect(await screen.findByTestId("home-untrusted")).toHaveTextContent("Task text from the agent session · treated as untrusted");
    expect(screen.getByTestId("home-hero-pips")).toHaveTextContent("about 8–14 min");
    expect(screen.getByTestId("home-hero-pips")).not.toHaveTextContent("quick check");
    expect(screen.getByTestId("home-budget")).toHaveTextContent("2 reviews · 16–28 min · 4 decisions");
    expect(screen.getAllByRole("button", { name: /^Start review/ })).toHaveLength(1);
  });

  it("says only the decisions still open need you on a started review", async () => {
    directory.mockResolvedValue(page([queueRow({ id: "a", progress: { ...queueRow({ id: "a" }).progress!, decision: 2, decision_open: 1, quick_check_open: 0 } })]));
    show();
    await ready();
    expect(screen.getByText("1 decision needs you.")).toBeInTheDocument();
  });

  it("draws only the sections that have rows (HOME-2)", async () => {
    directory.mockResolvedValue(page([queueRow({ id: "a" }), waitingRow("w", { title: "Billing fix" })]));
    show();
    await ready();
    expect(screen.getByTestId("home-section-yourTurn")).toBeInTheDocument();
    expect(screen.getByTestId("home-section-waiting")).toBeInTheDocument();
    for (const id of ["cameBack", "snoozed", "team", "finished"]) expect(screen.queryByTestId(`home-section-${id}`)).toBeNull();
  });

  it("reads Continue review and the saved place for a review that was started (HOME-4)", async () => {
    directory.mockResolvedValue(page([queueRow({ id: "a", unsent_count: 1, queue: { snoozed_until: "", resume: { screen: "one", part: "apply_discount", position: 2, total: 2 } } })]));
    show();
    await ready();
    expect(screen.getByTestId("home-resume")).toHaveTextContent("Paused at decision 2 of 2 · 1 note kept");
    await userEvent.click(screen.getByRole("button", { name: /^Continue review/ }));
    expect(screen.getByTestId("location")).toHaveTextContent("/r/a/one/apply_discount");
  });

  it("shows Open in terminal with the exact command and copies it (HOME-7)", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    show();
    await ready();
    await userEvent.click(screen.getByRole("button", { name: "Open in terminal" }));
    expect(screen.getByTestId("home-terminal-command")).toHaveTextContent("lc review open /r/a");
    await userEvent.click(screen.getByRole("button", { name: /^Copy/ }));
    expect(writeText).toHaveBeenCalledWith("lc review open /r/a");
    expect(await screen.findByText("Command copied")).toBeInTheDocument();
  });

  it("says Queue clear when only others wait", async () => {
    directory.mockResolvedValue(page([waitingRow("w")]));
    show();
    await ready();
    expect(screen.getByTestId("home-queue-clear")).toHaveTextContent("Nothing waits on you.");
    expect(screen.getByTestId("home-queue-clear")).toHaveTextContent("Start a review from the terminal: lc review");
  });

  it("shows a Came back row with its revision", async () => {
    directory.mockResolvedValue(page([queueRow({ id: "a" }), cameBackRow("b", { title: "Billing fix" })]));
    show();
    await ready();
    expect(within(screen.getByTestId("home-section-cameBack")).getByText("Revision 2 is back")).toBeInTheDocument();
  });
});

describe("keys (HOME-3, HOME-8)", () => {
  it("J and K move the roving selection and Enter opens it; the live region is empty on arrival", async () => {
    show();
    await ready();
    expect(screen.getByTestId("home-live")).toHaveTextContent("");
    expect(document.querySelector('[aria-current="true"]')).toHaveAttribute("data-row", "a");
    await userEvent.keyboard("j");
    expect(document.querySelector('[aria-current="true"]')).toHaveAttribute("data-row", "b");
    expect(screen.getByTestId("home-live")).toHaveTextContent("Retry webhook delivery, 2 of 2");
    await userEvent.keyboard("k");
    expect(document.querySelector('[aria-current="true"]')).toHaveAttribute("data-row", "a");
    await userEvent.keyboard("j{Enter}");
    expect(screen.getByTestId("location")).toHaveTextContent("/r/b");
  });

  it("S opens the picker, a key from 1 to 4 snoozes with an Undo toast, and Undo puts the review back (HOME-3)", async () => {
    show();
    await ready();
    await userEvent.keyboard("s");
    expect(await screen.findByRole("heading", { name: "Snooze this review" })).toBeInTheDocument();
    expect(screen.getByText("Press 1 to 4, or Esc to cancel.")).toBeInTheDocument();
    await userEvent.keyboard("1");
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put.mock.calls[0][0]).toBe("a");
    expect(Date.parse(put.mock.calls[0][1].snoozed_until as string) - Date.now()).toBeGreaterThan(3_500_000);
    expect(await screen.findByTestId("home-section-snoozed")).toBeInTheDocument();
    expect(screen.getByTestId("flow-toast")).toHaveTextContent(/^Snoozed until .+Undo/);
    await userEvent.click(within(screen.getByTestId("flow-toast")).getByRole("button", { name: "Undo" }));
    expect(put).toHaveBeenLastCalledWith("a", { snoozed_until: null });
    await waitFor(() => expect(screen.queryByTestId("home-section-snoozed")).toBeNull());
    await waitFor(() => expect(document.activeElement).toHaveAttribute("data-row", "a"));
  });

  it("S on a review that is waiting on someone else says it cannot be snoozed", async () => {
    directory.mockResolvedValue(page([waitingRow("w", { title: "Billing fix" })]));
    show();
    await ready();
    await userEvent.keyboard("s");
    expect(await screen.findByText("Only reviews waiting on you can be snoozed.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Snooze this review" })).toBeNull();
  });

  it("puts the review back and says so when the snooze cannot be saved", async () => {
    put.mockRejectedValueOnce(new Error("boom"));
    show();
    await ready();
    await userEvent.keyboard("s1");
    expect(await screen.findByText(/Couldn't snooze 1 review/)).toBeInTheDocument();
    expect(screen.queryByTestId("home-section-snoozed")).toBeNull();
  });

  it("the footer legend is D22 and `/` and X work", async () => {
    show();
    await ready();
    expect(screen.getByTestId("home-legend")).toHaveTextContent("J/K move · Enter open · X select · S snooze · ⌘K search · ? all shortcuts");
    await userEvent.keyboard("x");
    expect(screen.getByTestId("home-bulk")).toHaveTextContent("1 selected");
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByTestId("home-bulk")).toBeNull();
    await userEvent.keyboard("/");
    expect(screen.getByRole("searchbox", { name: "Search reviews" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
  });

  it("Esc in the search field clears the query", async () => {
    show();
    await ready();
    await userEvent.keyboard("/webhook");
    expect(screen.getByRole("searchbox")).toHaveValue("webhook");
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("searchbox")).toHaveValue("");
  });
});

describe("dense list (HOME-5)", () => {
  const many = () => Array.from({ length: 7 }, (_, index) => queueRow({ id: `r${index}`, title: `Review ${index}`, project: index % 2 ? "payments" : "shop" }));

  it("shows the toolbar, remembers the sort across a reload, and Shift+J extends the selection", async () => {
    directory.mockResolvedValue(page(many()));
    const first = show();
    await ready();
    expect(screen.getByTestId("home-toolbar")).toBeInTheDocument();
    expect(screen.getByTestId("home-budget")).toHaveTextContent("7 reviews waiting · about");
    await userEvent.keyboard("{Shift>}J{/Shift}");
    expect(screen.getByTestId("home-bulk")).toHaveTextContent("2 selected");
    await userEvent.click(screen.getByRole("button", { name: "payments · 3" }));
    expect(JSON.parse(window.localStorage.getItem(HOME_PREFS_KEY) as string).filters.repos).toEqual(["payments"]);
    expect(screen.getByTestId("home-budget")).toHaveTextContent("showing 3 of 7");
    first.unmount();
    show();
    await ready();
    expect(screen.getByRole("button", { name: "payments · 3" })).toHaveAttribute("aria-pressed", "true");
  });

  it("the bulk picker snoozes every selected review", async () => {
    directory.mockResolvedValue(page(many()));
    show();
    await ready();
    await userEvent.keyboard("x");
    await userEvent.keyboard("j");
    await userEvent.keyboard("x");
    await userEvent.keyboard("s");
    expect(await screen.findByRole("heading", { name: "Snooze 2 reviews" })).toBeInTheDocument();
    await userEvent.keyboard("3");
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2));
    expect(put.mock.calls.map((call) => call[0]).sort()).toEqual(["r0", "r1"]);
  });

  it("says no reviews match and clears the filters", async () => {
    directory.mockResolvedValue(page(many()));
    show();
    await ready();
    await userEvent.type(screen.getByRole("searchbox"), "zzz");
    expect(screen.getByTestId("home-no-match")).toHaveTextContent("No reviews match.");
    await userEvent.click(within(screen.getByTestId("home-no-match")).getByRole("button", { name: "Clear filters" }));
    expect(screen.getByTestId("home-section-yourTurn")).toBeInTheDocument();
  });
});

describe("rows past the read limit", () => {
  it("says so instead of hiding them", async () => {
    directory.mockResolvedValue({ ...page([queueRow({ id: "a" })]), next_cursor: "more" });
    show();
    await ready();
    expect(screen.getByTestId("home-truncated")).toHaveTextContent("Showing the 500 most recently updated reviews. Older ones are left out.");
    // One read of up to 500 rows, not five pages that each make the server score every review again.
    expect(directory).toHaveBeenCalledTimes(1);
    expect(directory).toHaveBeenCalledWith(expect.objectContaining({ view: "all", status: "all", limit: 500, read: "home", cursor: "" }));
  });
});

describe("first run (HOME-6)", () => {
  it("shows one sentence and one lemon Start review, no search and no sections; Enter shows the command", async () => {
    directory.mockResolvedValue(page([]));
    show();
    await ready();
    expect(screen.getByText("LemonCrow helps you review what your coding agent changed. You decide; AI only points.")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Start review/ })).toHaveLength(1);
    expect(screen.queryByRole("searchbox")).toBeNull();
    expect(document.querySelectorAll('[data-testid^="home-section-"]')).toHaveLength(0);
    expect(screen.getByTestId("home-legend")).toHaveTextContent("Enter start · ? all shortcuts");
    await userEvent.keyboard("{Enter}");
    expect(screen.getByTestId("home-terminal-command")).toHaveTextContent("lc review");
  });
});

describe("a hosted source (P9-2)", () => {
  const nobody = { owner_ids: ["rev_x"], for_me: false, my_reasons: [], reasons: [] };
  const mine = (id: string, extra = {}) => queueRow({ id, views: ["participated"], ...extra });
  function source(rows: ReturnType<typeof queueRow>[], more: Partial<import("./homeApi").HomeSource> = {}, result: Partial<import("./homeApi").HomeRows> = {}): import("./homeApi").HomeSource {
    return { load: vi.fn(async () => ({ rows, teamsSupported: true, truncated: false, ...result })), terminal: false, ...more };
  }
  const showWith = (home: import("./homeApi").HomeSource) =>
    render(<MemoryRouter initialEntries={["/reviews"]}><ReviewHomeScreen source={home} /><Location /></MemoryRouter>);

  it("lists a review you have no part in nowhere, and does not count it toward the dense list", async () => {
    const strangers = Array.from({ length: 8 }, (_, index) => queueRow({ id: `s${index}`, title: `Stranger ${index}`, views: [], next_action: nobody }));
    showWith(source([queueRow({ id: "a", title: "Mine to read" }), ...strangers]));
    await ready();
    expect(screen.getByText("Mine to read")).toBeInTheDocument();
    expect(screen.queryByText(/Stranger/)).toBeNull();
    expect(screen.queryByTestId("home-section-waiting")).toBeNull();
    expect(screen.getByTestId("review-home")).toHaveAttribute("data-dense", "false");
  });

  it("says how many of how many it shows when the read was cut off, and a search asks the source", async () => {
    const found = mine("far", { title: "Old billing fix", next_action: nobody });
    const home = source([queueRow({ id: "a", title: "Mine to read" })], { search: vi.fn(async () => [found]) }, { truncated: true, total: 340 });
    showWith(home);
    await ready();
    expect(screen.getByTestId("home-truncated")).toHaveTextContent("Showing 1 of 340 reviews. Everything that waits on you is here; search looks through all of them.");
    await userEvent.type(screen.getByRole("searchbox", { name: "Search reviews" }), "billing");
    await waitFor(() => expect(home.search).toHaveBeenCalledWith("billing"));
    expect(await screen.findByText("Old billing fix")).toBeInTheDocument();
  });

  it("a Home that is showing an error does not say it is loading", async () => {
    showWith({ load: vi.fn().mockRejectedValue(new Error("Nope.")), terminal: false });
    expect(await screen.findByText("Nope.")).toBeInTheDocument();
    expect(screen.getByTestId("review-home")).toHaveAttribute("data-home", "error");
  });

  it("keeps the rows and says the load failed when a refresh fails after a good first load", async () => {
    const load = vi.fn()
      .mockResolvedValueOnce({ rows: [queueRow({ id: "a", title: "Mine to read" })], teamsSupported: true, truncated: false })
      .mockRejectedValue(new Error("The server is not reachable."));
    showWith({ load, terminal: false });
    await ready();
    document.dispatchEvent(new Event("visibilitychange"));
    expect(await screen.findByTestId("home-refresh-failed")).toHaveTextContent("These reviews may be out of date. The server is not reachable.");
    expect(screen.getByText("Mine to read")).toBeInTheDocument();
  });

  it("says nothing waits on you in the source's own words, with no terminal or Claude Code", async () => {
    showWith(source([waitingRow("w", { views: ["participated"] })], { queueClear: { sparse: "A review comes back here when a teammate asks for yours.", dense: "x" } }));
    await ready();
    const clear = await screen.findByTestId("home-queue-clear");
    expect(clear).toHaveTextContent("Nothing waits on you.");
    expect(clear).toHaveTextContent("A review comes back here when a teammate asks for yours.");
    expect(clear).not.toHaveTextContent(/lc review|Claude Code/);
  });

  it("puts a returned revision under Came back to you", async () => {
    const back = mine("b", { title: "Needs another look", revision_number: 1, next_action: { owner_ids: ["me"], for_me: true, my_reasons: [{ principal_id: "me", kind: "work_returned", fact: "", since: "" }], reasons: [] } });
    showWith(source([back]));
    await ready();
    expect(within(await screen.findByTestId("home-section-cameBack")).getByText("Needs another look")).toBeInTheDocument();
    expect(screen.queryByTestId("home-section-yourTurn")).toBeNull();
  });

  it("shows a turn on your own change in Your turn with what happened, and no Start review", async () => {
    const author = mine("m", { title: "My change", next_action: { owner_ids: ["me"], for_me: true, my_reasons: [{ principal_id: "me", kind: "check_failed", fact: "", since: "" }], reasons: [] } });
    showWith(source([author]));
    await ready();
    const turn = await screen.findByTestId("home-section-yourTurn");
    expect(within(turn).getByText("My change")).toBeInTheDocument();
    expect(within(turn).getByText("A check failed")).toBeInTheDocument();
    expect(screen.queryByTestId("home-start")).toBeNull();
    expect(within(turn).queryByRole("checkbox")).toBeNull();
    expect(screen.queryByTestId("home-queue-clear")).toBeNull();
  });

  it("snoozes through the source, and offers no snooze or S key when the source has none", async () => {
    const snooze = vi.fn(async () => ({}));
    showWith(source([queueRow({ id: "a", title: "Mine to read" })], { snooze }));
    await ready();
    expect(screen.getByTestId("home-legend")).toHaveTextContent("S snooze");
    await userEvent.keyboard("s");
    await userEvent.keyboard("1");
    await waitFor(() => expect(snooze).toHaveBeenCalledWith("a", expect.stringMatching(/^\d{4}-/)));
    cleanup();
    showWith(source([queueRow({ id: "a", title: "Mine to read" })]));
    await ready();
    expect(screen.getByTestId("home-legend")).not.toHaveTextContent("S snooze");
    await userEvent.keyboard("s");
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
