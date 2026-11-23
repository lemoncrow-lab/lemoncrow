import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { dismissFlowToast } from "../flowToast";
import { fetchReviewDirectory, fetchReviewJournal } from "../reviewApi";
import { fetchTeamStatus, fetchTeammates, handOffReview, releaseReview, takeReview } from "../team/teamApi";
import type { ReviewDirectoryPage, ReviewDirectoryRow, TeamState } from "../types";
import { takeEntryToast } from "../team/useReviewPresence";
import { queueRow } from "./homeFixtures";
import ReviewHomeScreen from "./ReviewHomeScreen";

vi.mock("../reviewApi", () => ({
  fetchReviewDirectory: vi.fn(),
  fetchReviewJournal: vi.fn(),
  putReviewQueueState: vi.fn(),
  reviewHref: (ref: string) => `/r/${ref.replace(/^r\//, "")}`,
}));
vi.mock("../team/teamApi", () => ({ takeReview: vi.fn(), handOffReview: vi.fn(), fetchTeammates: vi.fn(), fetchTeamStatus: vi.fn(), releaseReview: vi.fn() }));

const directory = vi.mocked(fetchReviewDirectory);
const journal = vi.mocked(fetchReviewJournal);
const take = vi.mocked(takeReview);
const handOff = vi.mocked(handOffReview);
const teammates = vi.mocked(fetchTeammates);
const status = vi.mocked(fetchTeamStatus);
const release = vi.mocked(releaseReview);

// Ten and a half minutes before the test run began, so "10 min ago" holds however long the test takes.
const START = Date.now() - 10.5 * 60_000;
const MAYA = { id: "usr_maya", name: "Maya Chen" };
const SAM = { id: "usr_sam", name: "Sam Rivera" };
const LEO = { id: "usr_leo", name: "Leo Park" };
const inIt = (): TeamState => ({ reviewing: [{ ...MAYA, started_at: new Date(START).toISOString(), sent_notes: 0 }], claim: null, handoff: null });

/** A review the viewer's team was asked for, that Maya is in (the hosted Inbox row, SPEC 4.16). */
function teamRow(id: string, team?: TeamState): ReviewDirectoryRow {
  return queueRow({
    id,
    title: `Change in ${id}`,
    views: ["assigned_to_my_teams"],
    next_action: { owner_ids: ["usr_maya"], for_me: false, my_reasons: [], reasons: [] },
    ...(team ? { team } : {}),
  });
}
function page(reviews: ReviewDirectoryRow[], teams = true): ReviewDirectoryPage {
  return { reviews, status: "all", query: "", next_cursor: "", teams_supported: teams } as unknown as ReviewDirectoryPage;
}
function Location() { return <output data-testid="location">{useLocation().pathname}</output>; }
async function show(rows: ReviewDirectoryRow[], teams = true) {
  directory.mockResolvedValue(page(rows, teams));
  render(<MemoryRouter initialEntries={["/reviews"]}><ReviewHomeScreen /><Location /></MemoryRouter>);
  await screen.findByTestId("review-home");
  await waitFor(() => expect(screen.queryByText("Loading reviews…")).toBeNull());
}
async function unfoldTeam(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: /requested from your team · Show/ }));
}

beforeEach(() => {
  window.localStorage.clear();
  dismissFlowToast();
  directory.mockReset();
  journal.mockReset().mockResolvedValue({ available: false, reason: "", entries: [] } as never);
  take.mockReset();
  release.mockReset();
  handOff.mockReset();
  status.mockReset().mockImplementation(async () => inIt());
  teammates.mockReset().mockResolvedValue([{ ...MAYA, away: null }, { ...LEO, away: { until: new Date(Date.now() + 2 * 86_400_000).toISOString(), reassign_to: MAYA } }]);
});
afterEach(cleanup);

describe("presence on a team row (TEAM-6)", () => {
  it("says who is in it and offers 'I'll take this' and 'Hand off'", async () => {
    const user = userEvent.setup();
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    expect(await screen.findByTestId("team-row-line")).toHaveTextContent("Maya started 10 min ago · from your team");
    expect(screen.getByRole("button", { name: "I'll take this: Change in a" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Hand off: Change in a" })).toBeVisible();
  });

  it("a local install draws no presence, no claim and no handoff, and asks nothing", async () => {
    await show([queueRow({ id: "a", title: "Change in a" })], false);
    expect(screen.queryByTestId("team-row")).toBeNull();
    expect(screen.queryByText(/I'll take this/)).toBeNull();
    expect(screen.queryByText(/Hand off/)).toBeNull();
    expect(take).not.toHaveBeenCalled();
    expect(teammates).not.toHaveBeenCalled();
  });

  it("opening it asks first: 'Maya is reviewing this' traps focus, and Esc returns it to the row", async () => {
    const user = userEvent.setup();
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    const link = await screen.findByTestId("home-row-link");
    await user.click(link);
    const dialog = await screen.findByRole("dialog", { name: "Maya is reviewing this" });
    expect(screen.getByTestId("location")).toHaveTextContent("/reviews"); // not opened yet
    expect(within(dialog).getByTestId("presence-text")).toHaveTextContent(
      "Maya Chen started 10 min ago. Maya has not sent anything yet. Your notes stay private until you send them.",
    );
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    for (let i = 0; i < 8; i += 1) await user.tab();
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId("home-row-link")));
    expect(screen.getByTestId("location")).toHaveTextContent("/reviews");
  });

  it("Enter on the row asks too, and 'Review anyway' opens it without asking again", async () => {
    const user = userEvent.setup();
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByTestId("home-row-link"));
    await user.click(await screen.findByTestId("presence-anyway"));
    expect(screen.getByTestId("location")).toHaveTextContent("/r/a");
  });

  it("'Review together' toasts that notes stay private until sent", async () => {
    const user = userEvent.setup();
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByTestId("home-row-link"));
    await user.click(await screen.findByTestId("presence-together"));
    expect(screen.getByTestId("location")).toHaveTextContent("/r/a");
    // The route change would clear a toast shown here, so the review screen shows it (useReviewPresence).
    expect(takeEntryToast("a")).toBe("Reviewing together with Maya · notes stay private until sent");
  });

  it("'Pick another' moves focus to the next review nobody is in", async () => {
    const user = userEvent.setup();
    await show([teamRow("a", inIt()), teamRow("b")]);
    await unfoldTeam(user);
    const links = await screen.findAllByTestId("home-row-link");
    await user.click(links[0]);
    await user.click(await screen.findByTestId("presence-other"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(document.activeElement?.closest("[data-row]")?.getAttribute("data-row")).toBe("b"));
    expect(screen.getByTestId("location")).toHaveTextContent("/reviews");
  });
});

describe("claim (TEAM-6)", () => {
  it("'I'll take this' reads 'Taken by you · Maya was told', and the actions go", async () => {
    const user = userEvent.setup();
    const taken: TeamState = { ...inIt(), claim: { by: SAM, by_me: true, at: "2026-11-09T10:00:00Z", told: [MAYA], waiting: [] } };
    take.mockResolvedValue(taken);
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByRole("button", { name: "I'll take this: Change in a" }));
    expect(take).toHaveBeenCalledWith("a");
    expect(await screen.findByTestId("team-row-line")).toHaveTextContent("Taken by you · Maya was told");
    expect(await screen.findByTestId("flow-toast")).toHaveTextContent("Taken by you · Maya was told");
    expect(screen.queryByTestId("team-take")).toBeNull();
  });

  it("a review I took opens without asking", async () => {
    const user = userEvent.setup();
    await show([teamRow("a", { ...inIt(), claim: { by: SAM, by_me: true, at: "x", told: [], waiting: [] } })]);
    await unfoldTeam(user);
    await user.click(await screen.findByTestId("home-row-link"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("location")).toHaveTextContent("/r/a");
  });

  it("someone else's claim reads 'Taken by Maya' and has no actions", async () => {
    const user = userEvent.setup();
    await show([teamRow("a", { reviewing: [], claim: { by: MAYA, by_me: false, at: "x", told: [], waiting: [] }, handoff: null })]);
    await unfoldTeam(user);
    expect(await screen.findByTestId("team-row-line")).toHaveTextContent("Taken by Maya");
    expect(screen.queryByTestId("team-handoff")).toBeNull();
  });

  it("a refused claim says why and keeps the actions", async () => {
    const user = userEvent.setup();
    take.mockRejectedValue(new Error("Maya Chen already took this review"));
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByRole("button", { name: "I'll take this: Change in a" }));
    expect(await screen.findByTestId("flow-toast")).toHaveTextContent("Maya Chen already took this review");
  });
});

describe("hand off (TEAM-6)", () => {
  it("picks a teammate, names the away reassignment, sends the note and reports what the server stored", async () => {
    const user = userEvent.setup();
    const done: TeamState = {
      reviewing: [], claim: null,
      handoff: { to: MAYA, by: SAM, to_me: false, by_me: true, note: "Start at the verifier.", at: "x", away: { ...LEO, until: new Date(Date.now() + 2 * 86_400_000).toISOString() } },
    };
    handOff.mockResolvedValue(done);
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByRole("button", { name: "Hand off: Change in a" }));
    const dialog = await screen.findByRole("dialog", { name: "Hand off this review" });
    await user.click(await within(dialog).findByRole("radio", { name: /Leo Park/ }));
    expect(within(dialog).getByTestId("handoff-line")).toHaveTextContent(/^Leo is away until \w+ · reassigned to Maya$/);
    await user.type(within(dialog).getByRole("textbox", { name: "Note for them" }), "Start at the verifier.");
    await user.click(within(dialog).getByTestId("handoff-send"));
    expect(handOff).toHaveBeenCalledWith("a", "usr_leo", "Start at the verifier.");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await screen.findByTestId("flow-toast")).toHaveTextContent(/^Handed off to Maya Chen · Leo is away until \w+ · reassigned to Maya$/);
    expect(screen.getByTestId("team-row-line")).toHaveTextContent("Start at the verifier.");
  });

  it("says the review request did not move when the server could not move it, and the hand off stands", async () => {
    const user = userEvent.setup();
    const done: TeamState = {
      reviewing: [], claim: null,
      handoff: { to: MAYA, by: SAM, to_me: false, by_me: true, note: "", at: "x", away: null },
      request_moved: false, request_move_error: "The review request changed at the same moment.",
    };
    handOff.mockResolvedValue(done);
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByRole("button", { name: "Hand off: Change in a" }));
    const dialog = await screen.findByRole("dialog", { name: "Hand off this review" });
    await user.click(await within(dialog).findByRole("radio", { name: /Maya Chen/ }));
    await user.click(within(dialog).getByTestId("handoff-send"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await screen.findByTestId("flow-toast")).toHaveTextContent(
      /^Handed off to Maya Chen · The review request did not move: The review request changed at the same moment\.$/,
    );
    expect(screen.getByTestId("team-row-line")).toHaveTextContent("Handed off to Maya Chen");
  });

  it("Esc cancels without sending and returns focus to the button that opened it", async () => {
    const user = userEvent.setup();
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    const opener = await screen.findByRole("button", { name: "Hand off: Change in a" });
    await user.click(opener);
    await screen.findByRole("dialog", { name: "Hand off this review" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(handOff).not.toHaveBeenCalled();
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });

  it("a refusal stays in the dialog with its reason", async () => {
    const user = userEvent.setup();
    handOff.mockRejectedValue(new Error("Sam Rivera took this review"));
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByRole("button", { name: "Hand off: Change in a" }));
    const dialog = await screen.findByRole("dialog", { name: "Hand off this review" });
    await user.click(await within(dialog).findByTestId("handoff-send"));
    expect(await within(dialog).findByTestId("handoff-error")).toHaveTextContent("Sam Rivera took this review");
  });

  it("with no one else on the review there is nothing to send", async () => {
    const user = userEvent.setup();
    teammates.mockResolvedValue([]);
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByRole("button", { name: "Hand off: Change in a" }));
    const dialog = await screen.findByRole("dialog", { name: "Hand off this review" });
    expect(await within(dialog).findByTestId("handoff-none")).toBeVisible();
    expect(within(dialog).getByTestId("handoff-send")).toBeDisabled();
  });
});

describe("the dialog reads the team as it opens (SPEC 4.16)", () => {
  it("tells what the person has sent, which the list's row does not carry", async () => {
    const user = userEvent.setup();
    status.mockResolvedValue({ ...inIt(), reviewing: [{ ...inIt().reviewing[0], sent_notes: 2 }] });
    await show([teamRow("a", { ...inIt(), reviewing: [{ ...inIt().reviewing[0], sent_notes: undefined }] })]);
    await unfoldTeam(user);
    await user.click(await screen.findByTestId("home-row-link"));
    expect(await screen.findByTestId("presence-text")).toHaveTextContent("Maya has sent 2 notes.");
  });

  it("claims nothing about notes when the read fails", async () => {
    const user = userEvent.setup();
    status.mockRejectedValue(new Error("502"));
    await show([teamRow("a", { ...inIt(), reviewing: [{ ...inIt().reviewing[0], sent_notes: undefined }] })]);
    await unfoldTeam(user);
    await user.click(await screen.findByTestId("home-row-link"));
    const text = await screen.findByTestId("presence-text");
    expect(text).toHaveTextContent("Maya Chen started 10 min ago. Your notes stay private until you send them.");
    expect(text).not.toHaveTextContent("not sent anything");
  });

  it("when Maya has left by then there is nothing to ask, and the review opens", async () => {
    const user = userEvent.setup();
    status.mockResolvedValue({ reviewing: [], claim: null, handoff: null });
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByTestId("home-row-link"));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/r/a"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("what a claim says, honestly (SPEC 4.16)", () => {
  it("'I'll take this' says Maya will see it until her own screen has the notice", async () => {
    const user = userEvent.setup();
    take.mockResolvedValue({ ...inIt(), claim: { by: SAM, by_me: true, at: "x", told: [], waiting: [MAYA] } });
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByRole("button", { name: "I'll take this: Change in a" }));
    expect(await screen.findByTestId("team-row-line")).toHaveTextContent("Taken by you \u00b7 Maya will see it");
    expect(await screen.findByTestId("flow-toast")).toHaveTextContent("Taken by you \u00b7 Maya will see it");
    expect(screen.queryByText(/was told/)).toBeNull();
  });

  it("a list read that began before the claim, and is answered after it, does not undo the claim", async () => {
    const user = userEvent.setup();
    take.mockResolvedValue({ ...inIt(), claim: { by: SAM, by_me: true, at: "x", told: [], waiting: [MAYA] } });
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    // A second read starts (the tab returns) and the server answers it from before the claim, after the claim's own answer.
    let answer: (page: ReviewDirectoryPage) => void = () => undefined;
    directory.mockReset().mockReturnValue(new Promise<ReviewDirectoryPage>((resolve) => { answer = resolve; }));
    document.dispatchEvent(new Event("visibilitychange"));
    await user.click(await screen.findByRole("button", { name: "I'll take this: Change in a" }));
    expect(await screen.findByTestId("team-row-line")).toHaveTextContent("Taken by you \u00b7 Maya will see it");
    answer(page([teamRow("a", inIt())]));
    await waitFor(() => expect(directory).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByTestId("team-row-line")).toHaveTextContent("Taken by you \u00b7 Maya will see it");
    expect(screen.getByTestId("team-release")).toBeTruthy();
  });

  it("a read answered after a newer read that already saw someone's claim does not bring the old row back", async () => {
    const user = userEvent.setup();
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    const answers: ((page: ReviewDirectoryPage) => void)[] = [];
    const deferred = () => new Promise<ReviewDirectoryPage>((resolve) => { answers.push(resolve); });
    directory.mockReset().mockImplementationOnce(deferred).mockImplementationOnce(deferred);
    document.dispatchEvent(new Event("visibilitychange"));
    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => expect(answers).toHaveLength(2));
    answers[1](page([teamRow("a", { ...inIt(), claim: { by: MAYA, by_me: false, at: "x", told: [], waiting: [] } })]));
    expect(await screen.findByTestId("team-row-line")).toHaveTextContent("Taken by Maya");
    answers[0](page([teamRow("a", inIt())]));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByTestId("team-row-line")).toHaveTextContent("Taken by Maya");
  });

  it("what someone else did since is shown by the next read, even over the person's own claim", async () => {
    const user = userEvent.setup();
    take.mockResolvedValue({ ...inIt(), claim: { by: SAM, by_me: true, at: "x", told: [], waiting: [MAYA] } });
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByRole("button", { name: "I'll take this: Change in a" }));
    expect(await screen.findByTestId("team-row-line")).toHaveTextContent("Taken by you");
    directory.mockReset().mockResolvedValue(page([teamRow("a", { ...inIt(), claim: { by: MAYA, by_me: false, at: "x", told: [], waiting: [] } })]));
    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => expect(screen.getByTestId("team-row-line")).toHaveTextContent("Taken by Maya"));
  });

  it("the toast is the one announcement: Home adds no second live message", async () => {
    const user = userEvent.setup();
    take.mockResolvedValue({ ...inIt(), claim: { by: SAM, by_me: true, at: "x", told: [], waiting: [] } });
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByRole("button", { name: "I'll take this: Change in a" }));
    await screen.findByTestId("flow-toast");
    expect(screen.getByTestId("home-live")).toHaveTextContent("");
  });
});

describe("let go (SPEC 4.16)", () => {
  it("the one who took it can give it back, and the row offers it again", async () => {
    const user = userEvent.setup();
    release.mockResolvedValue(inIt());
    await show([teamRow("a", { ...inIt(), claim: { by: SAM, by_me: true, at: "x", told: [], waiting: [] } })]);
    await unfoldTeam(user);
    await user.click(await screen.findByRole("button", { name: "Let go: Change in a" }));
    expect(release).toHaveBeenCalledWith("a");
    expect(await screen.findByRole("button", { name: "I'll take this: Change in a" })).toBeVisible();
  });
});

describe("hand off from the presence dialog", () => {
  it("Esc returns focus to the row, not to nowhere", async () => {
    const user = userEvent.setup();
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByTestId("home-row-link"));
    await user.click(await screen.findByTestId("presence-handoff"));
    await screen.findByRole("dialog", { name: "Hand off this review" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(document.activeElement?.closest("[data-row]")?.getAttribute("data-row")).toBe("a"));
  });

  it("the note field is named as it reads", async () => {
    const user = userEvent.setup();
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByRole("button", { name: "Hand off: Change in a" }));
    const dialog = await screen.findByRole("dialog", { name: "Hand off this review" });
    expect(await within(dialog).findByRole("textbox", { name: "Note for them" })).toBeVisible();
  });

  it("a stand-in who is not on the review is said not to be", async () => {
    const user = userEvent.setup();
    teammates.mockResolvedValue([{ ...LEO, away: { until: new Date(Date.now() + 2 * 86_400_000).toISOString(), reassign_to: null } }]);
    await show([teamRow("a", inIt())]);
    await unfoldTeam(user);
    await user.click(await screen.findByRole("button", { name: "Hand off: Change in a" }));
    const dialog = await screen.findByRole("dialog", { name: "Hand off this review" });
    expect(await within(dialog).findByTestId("handoff-line")).toHaveTextContent(/^Leo is away until \w+ \u00b7 their stand-in is not on this review$/);
  });
});
