import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TeamScreen } from "./TeamScreen";
import { fetchTeamHealth, repoRows, signaturesText, waitText, type TeamHealth } from "./teamModel";

vi.mock("./teamModel", async (importOriginal) => ({ ...(await importOriginal<typeof import("./teamModel")>()), fetchTeamHealth: vi.fn() }));
const read = vi.mocked(fetchTeamHealth);

const HEALTH: TeamHealth = {
  scope: "repository",
  window: "now",
  repositories: [
    { repo_id: "r1", repo: "shop", open_reviews: 4, waiting_reviews: 3, median_wait_seconds: 12_000, decisions_asked: 5, decisions_signed: 3, older_than_1_day: 1 },
    { repo_id: "r2", repo: "billing", open_reviews: 1, waiting_reviews: 0, median_wait_seconds: null, decisions_asked: 0, decisions_signed: 0, older_than_1_day: 0 },
  ],
};
function show(onAbsent = vi.fn()) {
  render(<MemoryRouter><TeamScreen onAbsent={onAbsent} backTo="/reviews" /></MemoryRouter>);
  return onAbsent;
}
beforeEach(() => { read.mockReset().mockResolvedValue(HEALTH); });
afterEach(cleanup);

describe("Team health (SPEC 4.19, POL-3)", () => {
  it("shows one card per repo with the metrics, and SPEC's footer", async () => {
    show();
    expect(await screen.findByRole("heading", { level: 1, name: "Team health" })).toBeTruthy();
    expect(screen.getByText("How review is going in each repo right now. Per repo, never per person.")).toBeTruthy();
    const cards = screen.getAllByTestId("team-repo");
    expect(cards).toHaveLength(2);
    expect(within(cards[0]).getByRole("heading", { name: "shop" })).toBeTruthy();
    expect(within(cards[0]).getByText("Median time waiting").nextSibling?.textContent).toBe("3 h 20 min");
    expect(within(cards[0]).getByText("Changes that always need a decision, signed by two people").nextSibling?.textContent).toBe("3 of 5");
    expect(within(cards[0]).getByText("Reviews older than 1 day").nextSibling?.textContent).toBe("1");
    expect(within(cards[1]).getByText("Median time waiting").nextSibling?.textContent).toBe("—");
    expect(screen.getByTestId("team-footer").textContent).toBe("Numbers here are per repository, never per person");
  });

  it("draws no person: no name, no handle, no per-person table or column", async () => {
    show();
    await screen.findAllByTestId("team-repo");
    const page = screen.getByTestId("team-health");
    expect(page.querySelector("table")).toBeNull();
    expect(page.querySelector("th, [role=columnheader], [role=row]")).toBeNull();
    expect(page.textContent ?? "").not.toMatch(/\b(usr_|@\w+|reviewer:|author:)/);
    // The rows a card can show are fixed by the model; none of them is keyed by a person.
    expect(repoRows(HEALTH.repositories[0]).map((row) => row.label)).toEqual([
      "Median time waiting", "Changes that always need a decision, signed by two people", "Reviews older than 1 day",
    ]);
  });

  it("tells a non-lead so, and shows no metric", async () => {
    read.mockRejectedValue(Object.assign(new Error("principal is not authorized to administer the index"), { status: 403 }));
    show();
    expect((await screen.findByTestId("team-not-lead")).textContent).toBe("Only team leads can see this page.");
    expect(screen.queryByTestId("team-repo")).toBeNull();
    expect(screen.queryByTestId("team-footer")).toBeNull();
  });

  it("says when a read failed, not that nothing is waiting", async () => {
    read.mockRejectedValue(new Error("down"));
    show();
    expect((await screen.findByRole("alert")).textContent).toBe("Couldn't read team health.");
  });

  it("a local install has no such page (SPEC 7.1 P0-2)", async () => {
    read.mockResolvedValue(null);
    const onAbsent = show();
    await waitFor(() => expect(onAbsent).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("team-repo")).toBeNull();
  });
});

describe("the formats", () => {
  it("reads a wait in the largest two units", () => {
    expect([null, 5, 59, 60, 3_599, 3_600, 3_660, 86_400, 90_000].map(waitText)).toEqual(["—", "under a minute", "under a minute", "1 min", "59 min", "1 h", "1 h 1 min", "1 d", "1 d 1 h"]);
    expect(signaturesText({ decisions_asked: 0, decisions_signed: 0 })).toBe("—");
    expect(signaturesText({ decisions_asked: 2, decisions_signed: 2 })).toBe("2 of 2");
  });
});
