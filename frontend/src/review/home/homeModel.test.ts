import { describe, expect, it } from "vitest";

import type { ReviewDirectoryRow } from "../types";

import { cameBackRow, queueRow, waitingRow } from "./homeFixtures";
import {
  ageText, budgetLine, budgetOf, buildSections, canSnooze, chipCounts, countsLine, filtersActive, isDense, isFirstRun,
  isAuthorTurn, isQueueClear, isSnoozed, matchesFilters, NO_FILTERS, placeOf, resumeLine, resumePath, rowResume, sectionOf, snoozeUntil, sortRows,
  stateText, untilText,
} from "./homeModel";

const NOW = new Date(2026, 10, 7, 10, 0, 0); // Sat 7 Nov 2026, 10:00 local
const at = (hours: number) => new Date(NOW.getTime() + hours * 3_600_000).toISOString();

describe("which section a review is in", () => {
  it("puts a review waiting on the reviewer in Your turn, and one with returned comments in Came back", () => {
    expect(sectionOf(queueRow({ id: "a" }), NOW.getTime())).toBe("yourTurn");
    expect(sectionOf(cameBackRow("b"), NOW.getTime())).toBe("cameBack");
  });

  it("puts a review whose notes are with a coding agent in Waiting on others", () => {
    expect(sectionOf(waitingRow("w"), NOW.getTime())).toBe("waiting");
  });

  it("gives a review with nobody's turn no section instead of a wrong one", () => {
    const idle = queueRow({ id: "r", next_action: { owner_ids: [], for_me: false, my_reasons: [], reasons: [] }, views: ["ready"] });
    expect(sectionOf(idle, NOW.getTime())).toBeNull();
  });

  it("puts finished reviews in Finished and drops archived ones", () => {
    expect(sectionOf(queueRow({ id: "f", status: "finished" }), NOW.getTime())).toBe("finished");
    expect(sectionOf(queueRow({ id: "x", status: "archived" }), NOW.getTime())).toBeNull();
  });

  it("shows the team section only where the server has teams", () => {
    const team = waitingRow("t", { views: ["assigned_to_my_teams"], next_action: { owner_ids: [], for_me: false, my_reasons: [], reasons: [] } });
    expect(sectionOf(team, NOW.getTime())).toBeNull();
    expect(sectionOf(team, NOW.getTime(), { teamsSupported: true })).toBe("team");
  });

  it("moves a review to Snoozed only while the snooze holds, and back when it is due", () => {
    const row = queueRow({ id: "s", queue: { snoozed_until: at(2), resume: null } });
    expect(isSnoozed(row, NOW.getTime())).toBe(true);
    expect(sectionOf(row, NOW.getTime())).toBe("snoozed");
    expect(sectionOf(row, NOW.getTime() + 2 * 3_600_000)).toBe("yourTurn");
    expect(sectionOf(row, NOW.getTime() + 3 * 3_600_000)).toBe("yourTurn");
  });

  it("never snoozes a review that is waiting on someone else, or one that is already finished", () => {
    expect(canSnooze(waitingRow("w"), NOW.getTime())).toBe(false);
    expect(canSnooze(queueRow({ id: "f", status: "finished" }), NOW.getTime())).toBe(false);
    expect(canSnooze(queueRow({ id: "a" }), NOW.getTime())).toBe(true);
    expect(canSnooze(cameBackRow("b"), NOW.getTime())).toBe(true);
  });

  it("moves a review you approved out of Your turn and Came back into Done, out of the counts (review-1.4)", () => {
    const axes = { your_review: { state: "complete", approved: true }, team_review: { state: "not_applicable" }, scm: { state: "not_connected", connected: false, provider: "", failed_checks: 0, pending_checks: 0 } } as ReviewDirectoryRow["axes"];
    const approved = cameBackRow("ap", { axes });
    const landing = queueRow({ id: "land", axes, next_action: { owner_ids: ["local"], for_me: true, my_reasons: [{ principal_id: "local", kind: "landing_attention", fact: "", since: "" }], reasons: [] } });
    expect(sectionOf(approved, NOW.getTime())).toBe("done");
    expect(sectionOf(landing, NOW.getTime())).toBe("done");
    expect(stateText(approved, "done", NOW)).toBe("Revision 2 approved");
    const sections = buildSections([queueRow({ id: "a" }), approved, queueRow({ id: "f", status: "finished" })], NOW.getTime());
    expect(sections.map((section) => [section.id, section.rows.map((row) => row.id)])).toEqual([["yourTurn", ["a"]], ["done", ["ap"]], ["finished", ["f"]]]);
    expect(isQueueClear(buildSections([approved], NOW.getTime()))).toBe(true);
    expect(chipCounts([approved, cameBackRow("b")]).cameBack).toBe(1);
    expect(matchesFilters(approved, { ...NO_FILTERS, cameBack: true }, "")).toBe(false);
    expect(isDense([...Array.from({ length: 5 }, (_, i) => queueRow({ id: `q${i}` })), approved], NOW.getTime())).toBe(false);
  });

  it("draws only sections that have rows, in SPEC order, and keeps the order rows were given in", () => {
    const rows = [waitingRow("w"), queueRow({ id: "a" }), queueRow({ id: "c" }), cameBackRow("b"), queueRow({ id: "s", queue: { snoozed_until: at(1), resume: null } })];
    const sections = buildSections(rows, NOW.getTime());
    expect(sections.map((section) => section.id)).toEqual(["yourTurn", "cameBack", "waiting", "snoozed"]);
    expect(sections[0].rows.map((row) => row.id)).toEqual(["a", "c"]);
    expect(buildSections([], NOW.getTime())).toEqual([]);
  });
});

describe("snooze options", () => {
  it("1 hour ends an hour from now", () => {
    expect(snoozeUntil(1, NOW).getTime() - NOW.getTime()).toBe(3_600_000);
  });

  it("This afternoon is 14:00 before 13:00 and four hours on after it", () => {
    expect(snoozeUntil(2, NOW).getHours()).toBe(14);
    const late = new Date(2026, 10, 7, 15, 30);
    expect(snoozeUntil(2, late).getTime() - late.getTime()).toBe(4 * 3_600_000);
  });

  it("Tomorrow 9:00 is the next day at 9", () => {
    const until = snoozeUntil(3, NOW);
    expect([until.getDate(), until.getHours(), until.getMinutes()]).toEqual([8, 9, 0]);
  });

  it("Next week is the coming Monday at 9, and a Monday goes to the next Monday", () => {
    const saturday = snoozeUntil(4, NOW);
    expect([saturday.getDay(), saturday.getDate(), saturday.getHours()]).toEqual([1, 9, 9]);
    const monday = new Date(2026, 10, 9, 8, 0);
    expect(snoozeUntil(4, monday).getDate()).toBe(16);
  });

  it("words the end with the shared date format", () => {
    expect(untilText(new Date(2026, 10, 7, 14, 0), NOW)).toBe("today 14:00");
    expect(untilText(new Date(2026, 10, 7, 14, 30), NOW)).toBe("today 14:30");
    expect(untilText(new Date(2026, 10, 8, 9, 0), NOW)).toBe("tomorrow 09:00");
    expect(untilText(new Date(2026, 10, 9, 9, 0), NOW)).toBe("Mon 09:00");
  });
});

describe("row text", () => {
  it("counts decisions and quick checks from the server's split, and says 'no decisions' for none", () => {
    expect(countsLine(queueRow({ id: "a" }))).toBe("2 decisions · 1 quick check");
    expect(countsLine(queueRow({ id: "b", progress: { ...queueRow({ id: "b" }).progress!, decision_open: 0, quick_check_open: 4 } }))).toBe("no decisions · 4 quick checks");
    expect(countsLine(queueRow({ id: "c", progress: { ...queueRow({ id: "c" }).progress!, decision_open: 1, quick_check_open: 0 } }))).toBe("1 decision");
  });

  it("counts only what is still to answer, so a started review does not say two decisions need you", () => {
    const started = queueRow({ id: "s", progress: { ...queueRow({ id: "s" }).progress!, decision: 2, decision_open: 1, quick_check_open: 0 } });
    expect(countsLine(started)).toBe("1 decision");
    expect(budgetLine(budgetOf([started]), false)).toBe("1 review · 4–7 min · 1 decision");
  });

  it("falls back to the part count when the server sent no split", () => {
    const { decision_open: _d, quick_check_open: _q, ...older } = queueRow({ id: "o" }).progress!;
    expect(countsLine(queueRow({ id: "o", progress: older }))).toBe("3 parts");
  });

  it("names where a review lives", () => {
    expect(placeOf(queueRow({ id: "a" }))).toBe("shop · main");
    expect(placeOf(queueRow({ id: "g", source_ref: "github:acme/payments#412" }))).toBe("payments · #412");
  });

  it("writes how long a review has waited", () => {
    expect(ageText(30_000)).toBe("under 1 min");
    expect(ageText(6 * 60_000)).toBe("6 min");
    expect(ageText(3 * 3_600_000)).toBe("3 h");
    expect(ageText((24 + 4) * 3_600_000)).toBe("1 d 4 h");
    expect(ageText(48 * 3_600_000)).toBe("2 d");
  });

  it("writes the resume line with the kept notes", () => {
    expect(resumeLine({ screen: "one", part: "p", position: 2, total: 2 }, 0)).toBe("Paused at decision 2 of 2");
    expect(resumeLine({ screen: "quick_check", part: "q", position: 1, total: 1 }, 0)).toBe("Paused at the quick check");
    expect(resumeLine({ screen: "wrap", part: "", position: 0, total: 0 }, 1)).toBe("Ready to wrap up · 1 note kept");
    expect(resumeLine(null, 3)).toBe("");
  });

  it("offers Ready to wrap up only when the server counts nothing left to answer", () => {
    const wrap = { screen: "wrap" as const, part: "", position: 0, total: 0 };
    const open = queueRow({ id: "w", queue: { snoozed_until: "", resume: wrap } });
    expect(rowResume(open)).toBeNull();
    expect(stateText(open, "yourTurn", NOW)).toBe("Your turn");
    const done = queueRow({ id: "d", queue: { snoozed_until: "", resume: wrap }, progress: { ...open.progress!, decision_open: 0, quick_check_open: 0 } });
    expect(rowResume(done)).toEqual(wrap);
    expect(stateText(done, "yourTurn", NOW)).toBe("Ready to wrap up");
  });

  it("sends Continue review to the saved part", () => {
    expect(resumePath("/r/abc", { screen: "one", part: "can view/order", position: 1, total: 2 })).toBe("/r/abc/one/can%20view%2Forder");
    expect(resumePath("/r/abc", { screen: "wrap", part: "", position: 0, total: 0 })).toBe("/r/abc/wrap");
    expect(resumePath("/r/abc", null)).toBe("/r/abc");
  });

  it("ends each row with its state", () => {
    expect(stateText(queueRow({ id: "a" }), "yourTurn", NOW)).toBe("Your turn");
    expect(stateText(queueRow({ id: "a", unsent_count: 1, queue: { snoozed_until: "", resume: { screen: "one", part: "p", position: 2, total: 2 } } }), "yourTurn", NOW)).toBe("Paused at decision 2 of 2 · 1 note kept");
    expect(stateText(cameBackRow("b"), "cameBack", NOW)).toBe("Revision 2 is back");
    expect(stateText(queueRow({ id: "s", queue: { snoozed_until: new Date(2026, 10, 7, 14).toISOString(), resume: null } }), "snoozed", NOW)).toBe("Snoozed until today 14:00");
    expect(stateText(waitingRow("w"), "waiting", NOW)).toBe("Waiting on Claude Code");
  });
});

describe("the budget line", () => {
  it("adds the ranges and decisions of what waits on the reviewer", () => {
    const budget = budgetOf([queueRow({ id: "a" }), queueRow({ id: "b" })]);
    expect(budget.decisions).toBe(4);
    expect(budgetLine(budget, false)).toBe("2 reviews · 16–28 min · 4 decisions");
  });

  it("says 'about' and 'waiting' at volume", () => {
    const rows = Array.from({ length: 12 }, (_, index) => queueRow({ id: `r${index}` }));
    expect(budgetLine(budgetOf(rows), true)).toBe("12 reviews waiting · about 1 h 36 min–2 h 48 min · 24 decisions");
  });

  it("leaves the time and the decisions out when any review carries no split, rather than understate them", () => {
    const { decision_open: _d, quick_check_open: _q, ...older } = queueRow({ id: "o" }).progress!;
    const mixed = budgetOf([queueRow({ id: "a" }), queueRow({ id: "o", progress: older })]);
    expect(mixed).toMatchObject({ decisions: null, range: null });
    expect(budgetLine(mixed, false)).toBe("2 reviews");
  });

  it("says no decisions and no range for three reviews when only one has been derived (a hosted page)", () => {
    const { progress: _p, ...bare } = queueRow({ id: "x" });
    const rows = [queueRow({ id: "a" }), { ...bare, id: "b" }, { ...bare, id: "c" }] as ReviewDirectoryRow[];
    expect(budgetLine(budgetOf(rows), false)).toBe("3 reviews");
    expect(budgetLine(budgetOf(rows), true)).toBe("3 reviews waiting");
  });
});

describe("the dense list", () => {
  const rows = [
    queueRow({ id: "late", created_at: "2026-11-05T08:00:00Z", attention: { score: 1, waiting_since: "2026-11-07T09:00:00Z", factors: [] } }),
    queueRow({ id: "long", created_at: "2026-11-06T08:00:00Z", attention: { score: 1, waiting_since: "2026-11-05T09:00:00Z", factors: [] }, progress: { ...queueRow({ id: "x" }).progress!, decision_open: 5 } }),
    queueRow({ id: "none", created_at: "2026-11-07T08:00:00Z", attention: undefined }),
  ];

  it("sorts by waiting longest, fewest decisions or newest, and a row with no value goes last", () => {
    expect(sortRows(rows, "waiting").map((row) => row.id)).toEqual(["long", "late", "none"]);
    expect(sortRows(rows, "fewest").map((row) => row.id)).toEqual(["late", "none", "long"]);
    expect(sortRows(rows, "newest").map((row) => row.id)).toEqual(["none", "long", "late"]);
  });

  it("is dense above five open reviews, not at five", () => {
    const open = (count: number) => Array.from({ length: count }, (_, index) => queueRow({ id: `o${index}` }));
    expect(isDense(open(5), NOW.getTime())).toBe(false);
    expect(isDense(open(6), NOW.getTime())).toBe(true);
    expect(isDense([...open(5), queueRow({ id: "f", status: "finished" })], NOW.getTime())).toBe(false);
  });

  it("filters by repo, source, came back and search text", () => {
    const github = queueRow({ id: "g", source_ref: "github:acme/payments#412", title: "Retry webhook delivery" });
    const local = queueRow({ id: "l" });
    const back = cameBackRow("b");
    expect(matchesFilters(github, { ...NO_FILTERS, repos: ["payments"] }, "")).toBe(true);
    expect(matchesFilters(local, { ...NO_FILTERS, repos: ["payments"] }, "")).toBe(false);
    expect(matchesFilters(github, { ...NO_FILTERS, sources: ["Local"] }, "")).toBe(false);
    expect(matchesFilters(back, { ...NO_FILTERS, cameBack: true }, "")).toBe(true);
    expect(matchesFilters(local, { ...NO_FILTERS, cameBack: true }, "")).toBe(false);
    expect(matchesFilters(github, NO_FILTERS, "webhook")).toBe(true);
    expect(matchesFilters(github, NO_FILTERS, "billing")).toBe(false);
    expect(filtersActive(NO_FILTERS, "  ")).toBe(false);
    expect(filtersActive(NO_FILTERS, "x")).toBe(true);
  });

  it("counts chips over the rows given", () => {
    const counts = chipCounts([queueRow({ id: "a" }), queueRow({ id: "b" }), queueRow({ id: "g", source_ref: "github:acme/payments#1" }), cameBackRow("c")]);
    expect(counts.repos).toEqual([{ name: "shop", count: 3 }, { name: "payments", count: 1 }]);
    expect(counts.sources).toEqual([{ name: "Local", count: 3 }, { name: "GitHub", count: 1 }]);
    expect(counts.cameBack).toBe(1);
  });
});

describe("first run and queue clear", () => {
  it("is first run only when no review exists in any state", () => {
    expect(isFirstRun([])).toBe(true);
    expect(isFirstRun([queueRow({ id: "f", status: "finished" })])).toBe(false);
  });

  it("is queue clear when nothing waits on the reviewer, even if other sections have rows", () => {
    expect(isQueueClear(buildSections([waitingRow("w"), queueRow({ id: "f", status: "finished" })], NOW.getTime()))).toBe(true);
    expect(isQueueClear(buildSections([queueRow({ id: "a" })], NOW.getTime()))).toBe(false);
    expect(isQueueClear(buildSections([cameBackRow("b")], NOW.getTime()))).toBe(false);
  });
});

describe("Needs your second signature (SPEC 4.16, TEAM-4)", () => {
  const asked = (id: string) =>
    queueRow({ id, second_signature: { for_me: true, path: "/r/second/second", first_reviewer_id: "usr_sam", first_reviewer_name: "Sam Rivera", area: "app/auth", decision_count: 1 } });

  it("is its own section, first, and the queue is not clear while one waits", () => {
    const sections = buildSections([queueRow({ id: "turn" }), asked("second")], NOW.getTime());
    expect(sections.map((section) => section.id)).toEqual(["secondSignature", "yourTurn"]);
    expect(sections[0]?.title).toBe("Needs your second signature");
    expect(isQueueClear(buildSections([asked("second")], NOW.getTime()))).toBe(false);
  });
  it("reads 'Sam reviewed · 1 decision in app/auth'", () => {
    expect(stateText(asked("second"), "secondSignature", NOW)).toBe("Sam reviewed · 1 decision in app/auth");
  });
  it("a local row has no such field, so it never lands there", () => {
    expect(sectionOf(queueRow({ id: "local" }), NOW.getTime())).toBe("yourTurn");
    expect(sectionOf(queueRow({ id: "x", second_signature: { for_me: false, path: "", first_reviewer_id: "", first_reviewer_name: "", area: "", decision_count: 0 } }), NOW.getTime())).toBe("yourTurn");
  });
  it("only an open review asks", () => {
    expect(sectionOf({ ...asked("done"), status: "finished" }, NOW.getTime())).toBe("finished");
  });
});

describe("hosted rows (P9-2): only reviews the viewer has a part in, in the right place", () => {
  const hosted = { teamsSupported: true } as const;
  const noOne = { owner_ids: ["rev_x"], for_me: false, my_reasons: [], reasons: [] };
  const stranger = (id: string, overrides = {}) => queueRow({ id, views: [], next_action: noOne, ...overrides });

  it("leaves a review the viewer has no part in out of Waiting on others and Finished", () => {
    expect(sectionOf(stranger("s"), NOW.getTime(), hosted)).toBeNull();
    expect(sectionOf(stranger("f", { status: "finished" }), NOW.getTime(), hosted)).toBeNull();
    expect(sectionOf(stranger("l"), NOW.getTime())).toBe("waiting"); // the local list has no such rows and keeps its rule
  });

  it.each(["authored_by_me", "participated", "to_review", "waiting_on_me", "assigned_to_my_teams"])("keeps one in %s", (view) => {
    expect(sectionOf(stranger("w", { views: [view] }), NOW.getTime(), hosted)).toBe(view === "assigned_to_my_teams" ? "team" : "waiting");
    expect(sectionOf(stranger("f", { views: [view], status: "finished" }), NOW.getTime(), hosted)).toBe("finished");
  });

  it("counts a review toward dense only when it lands in a section", () => {
    const strangers = Array.from({ length: 8 }, (_, index) => stranger(`s${index}`));
    expect(isDense(strangers, NOW.getTime(), hosted)).toBe(false);
    const mine = Array.from({ length: 6 }, (_, index) => stranger(`m${index}`, { views: ["participated"] }));
    expect(isDense([...strangers, ...mine], NOW.getTime(), hosted)).toBe(true);
    expect(isDense(mine.slice(0, 5), NOW.getTime(), hosted)).toBe(false);
  });

  it("puts a returned revision (work_returned) in Came back, as the bell says Revision N is back", () => {
    const back = queueRow({
      id: "b",
      revision_number: 2,
      next_action: { owner_ids: ["me"], for_me: true, my_reasons: [{ principal_id: "me", kind: "work_returned", fact: "", since: "" }], reasons: [] },
    });
    expect(sectionOf(back, NOW.getTime(), hosted)).toBe("cameBack");
    expect(stateText(back, "cameBack", NOW)).toBe("Revision 3 is back");
    expect(chipCounts([back]).cameBack).toBe(1);
  });

  it.each([
    ["feedback_received", "Feedback arrived"],
    ["changes_requested", "Changes were requested"],
    ["check_failed", "A check failed"],
    ["landing_attention", "Needs your decision to land"],
  ])("tells the author what %s means and never offers it as a review to start or snooze", (kind, words) => {
    const row = queueRow({ id: "a", next_action: { owner_ids: ["me"], for_me: true, my_reasons: [{ principal_id: "me", kind, fact: "", since: "" }], reasons: [] } });
    expect(isAuthorTurn(row)).toBe(true);
    expect(sectionOf(row, NOW.getTime(), hosted)).toBe("yourTurn");
    expect(stateText(row, "yourTurn", NOW)).toBe(words);
    expect(canSnooze(row, NOW.getTime(), hosted)).toBe(false);
    // A reviewer's turn alongside an author's is still a review to read.
    const both = queueRow({ id: "b", next_action: { owner_ids: ["me"], for_me: true, my_reasons: [{ principal_id: "me", kind, fact: "", since: "" }, { principal_id: "me", kind: "review_requested", fact: "", since: "" }], reasons: [] } });
    expect(isAuthorTurn(both)).toBe(false);
  });
});

describe("policy changes only a CODEOWNER can approve (SPEC 4.19, P9-5)", () => {
  const policy = queueRow({ id: "p", edits_policy: true });
  it("a review that edits the policy and that the viewer may approve is in its own section, ahead of Your turn", () => {
    const options = { policyApprovable: new Set(["p"]) };
    expect(sectionOf(policy, NOW.getTime(), options)).toBe("policyApproval");
    expect(buildSections([queueRow({ id: "a" }), policy], NOW.getTime(), options).map((s) => [s.id, s.title])).toEqual([
      ["policyApproval", "Policy changes only a CODEOWNER can approve"],
      ["yourTurn", "Your turn"],
    ]);
  });
  it("is Your turn for someone who may not approve it, for a review that edits no policy, and where nothing was asked", () => {
    expect(sectionOf(policy, NOW.getTime(), { policyApprovable: new Set() })).toBe("yourTurn");
    expect(sectionOf(policy, NOW.getTime())).toBe("yourTurn");
    expect(sectionOf(queueRow({ id: "q" }), NOW.getTime(), { policyApprovable: new Set(["q"]) })).toBe("yourTurn");
  });
});
