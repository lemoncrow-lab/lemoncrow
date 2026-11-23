import { describe, expect, it } from "vitest";

import type { TeamState } from "../types";
import { agoText, awayLine, firstName, initials, needsPresenceDialog, noticeText, presenceBody, teamLine, untilText } from "./teamCopy";

const NOW = Date.parse("2026-11-09T10:00:00Z"); // a Monday
const MAYA = { id: "usr_maya", name: "Maya Chen" };
const SAM = { id: "usr_sam", name: "Sam Rivera" };
const LEO = { id: "usr_leo", name: "Leo Park" };
const team = (over: Partial<TeamState> = {}): TeamState => ({ reviewing: [], claim: null, handoff: null, ...over });
const inIt = { ...MAYA, started_at: "2026-11-09T09:50:00Z" };

describe("how long ago (SPEC 4.16 'started 10 min ago')", () => {
  it.each([
    ["2026-11-09T09:59:40Z", "just now"],
    ["2026-11-09T09:50:00Z", "10 min ago"],
    ["2026-11-09T09:01:00Z", "59 min ago"],
    ["2026-11-09T09:00:00Z", "1 h ago"],
    ["2026-11-08T08:00:00Z", "1 d ago"],
    ["not a time", "a while ago"],
  ])("%s reads %s", (at, text) => expect(agoText(at, NOW)).toBe(text));

  it("a start in the future reads just now, never negative", () => expect(agoText("2026-11-09T10:05:00Z", NOW)).toBe("just now"));
});

describe("the away line (SPEC 4.16)", () => {
  it("names the day inside the week and the date after it", () => {
    expect(untilText("2026-11-13T12:00:00Z", NOW)).toBe("Fri");
    expect(untilText("2026-11-30T12:00:00Z", NOW)).toBe("Nov 30");
    expect(untilText("", NOW)).toBe("");
  });
  it("reads 'Leo is away until Fri · reassigned to Maya'", () => {
    expect(awayLine("Leo Park", "2026-11-13T12:00:00Z", "Maya Chen", NOW)).toBe("Leo is away until Fri · reassigned to Maya");
  });
  it("says the stand-in is not on the review when there is none to name", () => {
    expect(awayLine("Leo Park", "2026-11-13T12:00:00Z", null, NOW)).toBe("Leo is away until Fri \u00b7 their stand-in is not on this review");
  });
  it("says nothing without a readable end", () => expect(awayLine("Leo Park", "soon", "Maya Chen", NOW)).toBe(""));
});

describe("names", () => {
  it("first names and initials", () => {
    expect(firstName("Maya Chen")).toBe("Maya");
    expect(initials("Maya Chen")).toBe("MC");
    expect(initials("Cher")).toBe("CH");
    expect(initials("")).toBe("");
  });
});

describe("the Home row line", () => {
  it("shows who started and when, for the team (TEAM-6)", () => {
    expect(teamLine(team({ reviewing: [inIt] }), NOW)).toEqual({ tone: "in", text: "Maya started 10 min ago · from your team", who: "Maya Chen" });
  });
  it("shows nothing when nobody is in it", () => expect(teamLine(team(), NOW)).toBeNull());
  it("my claim reads 'Taken by you' and says someone was told only once their own screen got it", () => {
    const base = { by: SAM, by_me: true, at: "2026-11-09T09:59:00Z" };
    expect(teamLine(team({ reviewing: [inIt], claim: { ...base, told: [MAYA], waiting: [] } }), NOW)?.text).toBe("Taken by you \u00b7 Maya was told");
    expect(teamLine(team({ reviewing: [inIt], claim: { ...base, told: [], waiting: [MAYA] } }), NOW)?.text).toBe("Taken by you \u00b7 Maya will see it");
    expect(teamLine(team({ claim: { ...base, told: [], waiting: [] } }), NOW)?.text).toBe("Taken by you");
  });
  it("two told read 'Maya and Leo were told', three read 'Maya, Leo and Sam', and each group has its own verb", () => {
    const base = { by: SAM, by_me: true, at: "x" };
    expect(teamLine(team({ claim: { ...base, told: [MAYA, LEO], waiting: [] } }), NOW)?.text).toBe("Taken by you \u00b7 Maya and Leo were told");
    expect(teamLine(team({ claim: { ...base, told: [MAYA, LEO, SAM], waiting: [] } }), NOW)?.text).toBe("Taken by you \u00b7 Maya, Leo and Sam were told");
    expect(teamLine(team({ claim: { ...base, told: [MAYA], waiting: [LEO] } }), NOW)?.text).toBe("Taken by you \u00b7 Maya was told \u00b7 Leo will see it");
  });
  it("someone else's claim reads 'Taken by Sam'", () => {
    expect(teamLine(team({ claim: { by: SAM, by_me: false, at: "x", told: [], waiting: [] } }), NOW)?.text).toBe("Taken by Sam");
  });
  it("a handoff I made reads 'Handed off to Maya Chen · Leo is away until Fri · reassigned to Maya' and keeps the note", () => {
    const handoff = {
      to: MAYA, by: SAM, to_me: false, by_me: true, note: "Start at the verifier.", at: "x",
      away: { ...LEO, until: "2026-11-13T12:00:00Z" },
    };
    expect(teamLine(team({ handoff }), NOW)?.text).toBe(
      "Handed off to Maya Chen · Leo is away until Fri · reassigned to Maya · Start at the verifier.",
    );
  });
  it("a handoff to me names who gave it", () => {
    const handoff = { to: MAYA, by: SAM, to_me: true, by_me: false, note: "", at: "x", away: null };
    expect(teamLine(team({ handoff }), NOW)?.text).toBe("Handed to you by Sam Rivera");
  });
  it("a handoff whose giver's words are held back reads without the giver or the note", () => {
    const handoff = { to: MAYA, by: { id: "", name: "" }, to_me: true, by_me: false, note: "", at: "x", away: null };
    expect(teamLine(team({ handoff }), NOW)?.text).toBe("Handed to you");
    expect(noticeText({ kind: "handed_off", by: { id: "", name: "" }, at: "x", note: "" })).toBe("A teammate handed this review to you");
  });
  it("a claim outranks presence", () => {
    const claim = { by: SAM, by_me: false, at: "x", told: [], waiting: [] };
    expect(teamLine(team({ reviewing: [inIt], claim }), NOW)?.tone).toBe("other");
  });
});

describe("when opening a review asks first", () => {
  it("only when someone is in it and nobody took it or was given it", () => {
    expect(needsPresenceDialog(team({ reviewing: [inIt] }))).toBe(true);
    expect(needsPresenceDialog(team())).toBe(false);
    expect(needsPresenceDialog(undefined)).toBe(false);
    expect(needsPresenceDialog(team({ reviewing: [inIt], claim: { by: SAM, by_me: true, at: "x", told: [], waiting: [] } }))).toBe(false);
    expect(needsPresenceDialog(team({ reviewing: [inIt], handoff: { to: MAYA, by: SAM, to_me: false, by_me: true, note: "", at: "x", away: null } }))).toBe(false);
  });
});

describe("the dialog body (SPEC 4.16)", () => {
  it("reads as SPEC when nothing has been sent", () => {
    expect(presenceBody({ ...inIt, sent_notes: 0 }, NOW)).toBe(
      "Maya Chen started 10 min ago. Maya has not sent anything yet. Your notes stay private until you send them.",
    );
  });
  it("counts the notes sent, in singular and plural", () => {
    expect(presenceBody({ ...inIt, sent_notes: 1 }, NOW)).toContain("Maya has sent 1 note.");
    expect(presenceBody({ ...inIt, sent_notes: 2 }, NOW)).toContain("Maya has sent 2 notes.");
  });
  it("claims nothing about notes the server withheld", () => {
    const text = presenceBody({ ...inIt, sent_notes: null }, NOW);
    expect(text).toBe("Maya Chen started 10 min ago. Your notes stay private until you send them.");
    expect(text).not.toContain("not sent anything");
  });
});

describe("notices", () => {
  it("someone took it; someone handed it to me with a note", () => {
    expect(noticeText({ kind: "taken", by: SAM, at: "x" })).toBe("Sam took this review");
    expect(noticeText({ kind: "handed_off", by: SAM, at: "x", note: "Look at auth first." })).toBe("Sam Rivera handed this review to you: Look at auth first.");
    expect(noticeText({ kind: "handed_off", by: SAM, at: "x", note: "" })).toBe("Sam Rivera handed this review to you");
  });
});
