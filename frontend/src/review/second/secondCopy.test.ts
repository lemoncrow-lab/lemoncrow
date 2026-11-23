import { describe, expect, it } from "vitest";

import type { SecondReviewerStatus } from "./secondApi";
import { doneFor, firstName, initials, stripFor, utcClock, verifyTail } from "./secondCopy";

const MAYA = { id: "usr_maya", name: "Maya Chen", handle: "mchen" };
const SAM = { id: "usr_sam", name: "Sam Rivera", handle: "srivera" };

function status(over: Partial<SecondReviewerStatus> = {}): SecondReviewerStatus {
  return {
    state: "will_be_asked", role: "first", required: true, unavailable: "", area: "app/auth", decision_count: 1,
    revision_id: "rev1", reviewer: MAYA, first_reviewer: null, first: null, second: null,
    signatures: { have: 0, needed: 2 }, verdict: "waiting", ...over,
  };
}
const signedFirst = { outcome_id: "o1", outcome: "lgtm", signed: true, signed_at: "2026-11-04T14:02:11+00:00", record_id: "rec1" } as const;
const signedSecond = { outcome_id: "o2", outcome: "lgtm", signed: true, signed_at: "2026-11-04T14:20:00+00:00", record_id: "rec2" } as const;

describe("the strip (SPEC 4.16)", () => {
  it("TEAM-5: before approving, Maya is named as will be asked; Request changes asks nobody", () => {
    expect(stripFor(status(), null)).toEqual({
      line: "Second reviewer · Maya Chen (CODEOWNER, app/auth) · asked when you approve",
      chip: { text: "Will be asked", tone: "neutral" },
    });
    expect(stripFor(status(), "lgtm")?.chip?.text).toBe("Will be asked");
    expect(stripFor(status(), "changes_requested")).toEqual({ line: "Not needed yet · asked when you approve", chip: null });
    expect(stripFor(status(), "comment")?.line).toBe("Not needed yet · asked when you approve");
  });
  it("waiting, agrees and disagrees read as SPEC says", () => {
    expect(stripFor(status({ state: "waiting" }), null)).toEqual({ line: "Waiting for Maya", chip: { text: "Waiting", tone: "warning" } });
    expect(stripFor(status({ state: "agrees", second: signedSecond }), null)).toEqual({ line: "Maya signed · agrees", chip: { text: "Agrees", tone: "success" } });
    const disagrees = status({ state: "disagrees", second: { ...signedSecond, outcome: "changes_requested", needs_change: ["can_view_order"] } });
    expect(stripFor(disagrees, null)).toEqual({ line: "Maya disagrees: can_view_order needs a change", chip: { text: "Disagrees", tone: "danger" } });
  });
  it("never says someone signed or disagrees unless that signature exists", () => {
    const no = { ...signedSecond, signed: false, outcome: "changes_requested" } as const;
    expect(stripFor(status({ state: "agrees", second: no }), null)?.line).toBe("Waiting for Maya");
    expect(stripFor(status({ state: "disagrees", second: no }), null)?.line).toBe("Waiting for Maya");
    expect(stripFor(status({ state: "disagrees", second: null }), null)?.chip?.text).toBe("Waiting");
  });
  it("draws nothing when no second signature is involved, and never on the second reviewer's own side", () => {
    expect(stripFor(status({ state: "not_required", reviewer: null }), null)).toBeNull();
    expect(stripFor(status({ state: "unavailable", reviewer: null }), null)).toBeNull();
    expect(stripFor(status({ role: "second" }), null)).toBeNull();
  });
});

describe("Done (TEAM-1, TEAM-2, TEAM-3)", () => {
  const waiting = status({ state: "waiting", first_reviewer: SAM, first: signedFirst, signatures: { have: 1, needed: 2 } });
  it("TEAM-1: waiting says 1 of 2 signatures and that nothing counts as approved yet", () => {
    const copy = doneFor(waiting, "Revision 1");
    expect(copy?.title).toBe("Approval waiting for a second signature.");
    expect(copy?.bar).toBe("Waiting for 2nd signature");
    expect(copy?.card).toBe("Approval waiting for 1 of 2 signatures · Maya Chen");
    expect(copy?.body).toBe("Sam Rivera signed at 14:02 UTC. Maya Chen (CODEOWNER, app/auth) signs next. Nothing counts as approved until both have signed. Nothing was merged.");
  });
  it("TEAM-8: while waiting, Done says who is actually told: the first reviewer, by notification, never an agent", () => {
    const copy = doneFor(waiting, "Revision 1");
    expect(copy?.told).toBe("You'll be told when Maya signs.");
    expect(copy?.told).not.toMatch(/Claude Code/);
    // once she has signed there is nothing left to wait for
    expect(doneFor(status({ state: "agrees", first_reviewer: SAM, first: signedFirst, second: signedSecond, signatures: { have: 2, needed: 2 } }), "Revision 1")?.told).toBe("");
  });
  it("never prints a count the server did not send (no first signature: no claim that Sam signed)", () => {
    const copy = doneFor(status({ state: "waiting", first_reviewer: SAM, first: { ...signedFirst, signed: false, signed_at: "" }, signatures: { have: 0, needed: 2 } }), "Revision 1");
    expect(copy?.card).toBe("Approval waiting for 0 of 2 signatures · Maya Chen");
    expect(copy?.body).not.toContain("signed at");
  });
  it("TEAM-2: agrees is Approved with 2 of 2 only when the second signature exists", () => {
    const agrees = status({ state: "agrees", first_reviewer: SAM, first: signedFirst, second: signedSecond, signatures: { have: 2, needed: 2 } });
    const copy = doneFor(agrees, "Revision 1");
    expect(copy?.title).toBe("Approved · 2 of 2 signatures · Sam Rivera and Maya Chen");
    expect(copy?.body).toBe("Maya Chen signed at 14:20 UTC and agrees. Both signatures are in the record. Nothing was merged.");
    // a signature that is not there is not said to be: it is still waited for
    const unsigned = doneFor({ ...agrees, second: { ...signedSecond, signed: false } }, "Revision 1");
    expect(unsigned?.title).toBe("Approval waiting for a second signature.");
    expect(unsigned?.tone).toBe("warning");
  });
  it("TEAM-3: a disagreement turns the verdict into Request changes and says whose note is where", () => {
    const copy = doneFor(status({ state: "disagrees", first_reviewer: SAM, first: signedFirst, second: { ...signedSecond, outcome: "changes_requested" }, signatures: { have: 2, needed: 2 } }), "Revision 1");
    expect(copy?.title).toBe("Maya disagrees · the verdict is now Request changes");
    expect(copy?.tone).toBe("danger");
    expect(copy?.body).toBe("Maya's note is saved on this review, with yours. Nothing was merged.");
  });
  it("TEAM-3: once her note is in the author's note set, Done says it was sent, with the count and where", () => {
    const sent = { ...signedSecond, outcome: "changes_requested", note: "Keep the guard.", note_sent: { count: 1, to: "the author" } } as const;
    const copy = doneFor(status({ state: "disagrees", first_reviewer: SAM, first: signedFirst, second: sent, signatures: { have: 2, needed: 2 } }), "Revision 1");
    expect(copy?.body).toBe("Maya's 1 note sent to the author, with your notes. Nothing was merged.");
  });
  it("says nothing for the second reviewer or when nobody was asked", () => {
    expect(doneFor({ ...waiting, role: "second" }, "Revision 1")).toBeNull();
    expect(doneFor(status({ state: "not_required", reviewer: null }), "Revision 1")).toBeNull();
  });
});

describe("Verify tail", () => {
  const agrees = { state: "agrees", reviewer: MAYA, signed_at: "2026-11-04T14:20:00+00:00" };
  it("names the second signature only on a Valid result and only when it exists", () => {
    expect(verifyTail("valid", agrees)).toBe(" · second signature @mchen · agrees · 14:20 UTC");
    expect(verifyTail("valid", { ...agrees, state: "disagrees" })).toBe(" · second signature @mchen · disagrees · verdict changed to Request changes");
    expect(verifyTail("valid", { ...agrees, state: "waiting" })).toBe("");
    expect(verifyTail("not_valid", agrees)).toBe("");
    expect(verifyTail("valid", undefined)).toBe("");
  });
});

describe("names", () => {
  it("initials, first names and the clock", () => {
    expect(initials("Maya Chen")).toBe("MC");
    expect(firstName("Maya Chen")).toBe("Maya");
    expect(utcClock("2026-11-04T14:02:11+00:00")).toBe("14:02 UTC");
    expect(utcClock("nope")).toBe("");
  });
});
