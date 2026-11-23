import { describe, expect, it } from "vitest";
import {
  LEGAL_SENTENCE, NOT_SIGNED_YET, latestHumanRecord, noRecordText, recordLineText, recordLink, verifyCommand, verifyFooter, verifyTone,
} from "./recordModel";
import type { ReviewRecordOutcomeName, ReviewRecordSummary, ReviewRecordVerifyResult } from "../types";

const record = (over: Partial<ReviewRecordSummary> = {}): ReviewRecordSummary => ({
  record_id: "rec_a", review_id: "r1", revision_id: "rev1", payload_digest: "sha256:1", signing_state: "signed", signing_mode: "user_key",
  key_id: "user-1", issued_at: "2026-11-07T10:00:00+00:00", decision_kind: "human_outcome", label: "Signed with personal key user-1", ...over,
});

describe("the collapsed record line", () => {
  it("reads SPEC 4.18's line", () => {
    expect(recordLineText(record({ parts_reviewed_one_by_one: 3 }), 1)).toBe("Signed record · Revision 1 · 3 decisions answered · Details");
    expect(recordLineText(record({ parts_reviewed_one_by_one: 1 }), 2)).toBe("Signed record · Revision 2 · 1 decision answered · Details");
  });
  it("counts zero honestly and leaves the count out for a record that bound no parts", () => {
    expect(recordLineText(record({ parts_reviewed_one_by_one: 0 }), 1)).toBe("Signed record · Revision 1 · 0 decisions answered · Details");
    expect(recordLineText(record({ parts_reviewed_one_by_one: null }), 1)).toBe("Signed record · Revision 1 · Details");
    expect(recordLineText(record(), 1)).toBe("Signed record · Revision 1 · Details");
  });
  it("never calls an unsigned record signed", () => {
    expect(recordLineText(record({ signing_state: "unsigned", parts_reviewed_one_by_one: 2 }), 1)).toBe("UNSIGNED record (no signature) · Revision 1 · 2 decisions answered · Details");
  });
  it("says why there is no record on Done, and what Wrap up shows before the send", () => {
    expect(noRecordText(false)).toBe("Not signed · no signing key is set up on this machine");
    expect(noRecordText(true)).toBe("Not signed · the record could not be signed");
    expect(noRecordText(null)).toBe("Checking for the signed record…");
    // While the server is still signing, the record is on its way: never a failure yet.
    expect(noRecordText(true, true)).toBe("Signing the record…");
    expect(NOT_SIGNED_YET).toBe("Not signed yet · set when you send");
  });
});

describe("which record the line shows", () => {
  it("is the newest human decision on this revision, not a landing record or another revision's", () => {
    const records = [
      record({ record_id: "old", issued_at: "2026-11-07T09:00:00+00:00" }),
      record({ record_id: "new", issued_at: "2026-11-07T11:00:00+00:00" }),
      record({ record_id: "landing", decision_kind: "landing", issued_at: "2026-11-07T12:00:00+00:00" }),
      record({ record_id: "other", revision_id: "rev2", issued_at: "2026-11-07T13:00:00+00:00" }),
    ];
    expect(latestHumanRecord(records, "rev1")?.record_id).toBe("new");
    expect(latestHumanRecord(records, "rev9")).toBeNull();
    expect(latestHumanRecord([], "rev1")).toBeNull();
  });
});

describe("Verify outcomes", () => {
  const tones: Record<ReviewRecordOutcomeName, string> = {
    valid: "success", code_changed: "destructive", not_valid: "destructive", key_not_allowed: "warning", unsigned: "warning", retracted: "neutral",
  };
  it.each(Object.entries(tones))("%s has its own colour", (name, tone) => {
    expect(verifyTone(name as ReviewRecordOutcomeName)).toBe(tone);
  });
  it("copies the command with the record's own address", () => {
    const link = recordLink("http://127.0.0.1:48080/", "r 1", "rec_a");
    expect(link).toBe("http://127.0.0.1:48080/api/reviews/r%201/records/rec_a");
    expect(verifyCommand(link)).toBe("lc review verify http://127.0.0.1:48080/api/reviews/r%201/records/rec_a");
  });
  it("names the revision, repository and commit the record is about, and what it was checked against", () => {
    const result = { record: record({ snapshot_number: 0, head_sha: "4e1b9c2deadbeef" }), repository: "shop" } as ReviewRecordVerifyResult;
    expect(verifyFooter(result)).toBe("Checked just now against the latest captured revision, not against this page. Revision 1 · shop @ 4e1b9c2");
    expect(verifyFooter({ record: record({ snapshot_number: 2 }), repository: "shop" } as ReviewRecordVerifyResult)).toBe(
      "Checked just now against the latest captured revision, not against this page. Revision 3 · shop");
    expect(verifyFooter({ record: record(), repository: "" } as ReviewRecordVerifyResult)).toBe("Checked just now against the latest captured revision, not against this page.");
  });
  it("keeps SPEC's legal sentence", () => {
    expect(LEGAL_SENTENCE).toBe("This is a technical signature that proves who reviewed what. It is not a legal e-signature.");
  });
});

// P6-2 Retract: the pure rules.
import {
  RETRACTED_LINE, canRetract, latestRetractedRecord, retractButtonText, retractRefusalText, retractUntilText, retractedNotice, utcClock,
} from "./recordModel";

const SIGNED = "2026-11-04T14:02:11+00:00";
const UNTIL = "2026-11-05T14:02:11+00:00";
const at = (iso: string) => Date.parse(iso);
const open = (over: Partial<ReviewRecordSummary> = {}) => record({ issued_at: SIGNED, retract_until: UNTIL, retractable: true, retracted: null, ...over });

describe("the Retract wording (SPEC 4.18)", () => {
  it("names the end of the window relative to today in UTC, the clock Signed at uses", () => {
    expect(retractUntilText(UNTIL, at("2026-11-04T14:05:00+00:00"))).toBe("until tomorrow 14:02 UTC");
    expect(retractUntilText(UNTIL, at("2026-11-05T09:00:00+00:00"))).toBe("until today 14:02 UTC");
    expect(retractUntilText(UNTIL, at("2026-11-02T09:00:00+00:00"))).toBe("until 2026-11-05 14:02 UTC");
  });
  it("cuts to the minute, so the shown end is never later than the real one", () => {
    expect(utcClock("2026-11-05T14:02:59+00:00")).toBe("14:02 UTC");
    expect(retractUntilText("2026-11-05T14:02:59.999+00:00", at(SIGNED))).toBe("until tomorrow 14:02 UTC");
  });
  it("is SPEC's button text", () => {
    expect(retractButtonText(open(), at("2026-11-04T14:05:00+00:00"))).toBe("Retract this verdict · until tomorrow 14:02 UTC");
  });
  it("reads an unreadable end as no end, never as a wrong one", () => {
    expect(retractUntilText("", 0)).toBe("");
    expect(retractButtonText(open({ retract_until: "soon" }), 0)).toBe("Retract this verdict");
  });
  it("uses a UTC day, not the browser's", () => {
    // 23:59 UTC on the 4th and 00:01 UTC on the 5th are different days wherever the browser is.
    expect(retractUntilText("2026-11-05T14:02:00+00:00", at("2026-11-04T23:59:00+00:00"))).toBe("until tomorrow 14:02 UTC");
    expect(retractUntilText("2026-11-05T14:02:00+00:00", at("2026-11-05T00:01:00+00:00"))).toBe("until today 14:02 UTC");
  });
});

describe("when Retract is offered", () => {
  it("only while the server says retractable and the window has not ended on this clock", () => {
    expect(canRetract(open(), at(SIGNED))).toBe(true);
    expect(canRetract(open(), at("2026-11-05T14:02:10.999+00:00"))).toBe(true);
    expect(canRetract(open(), at(UNTIL))).toBe(false); // the end itself is closed, as on the server
    expect(canRetract(open(), at("2026-11-05T14:02:12+00:00"))).toBe(false);
  });
  it("never when the server did not say so, or it is already retracted, or there is no end", () => {
    expect(canRetract(open({ retractable: false }), at(SIGNED))).toBe(false);
    expect(canRetract(open({ retractable: undefined }), at(SIGNED))).toBe(false);
    expect(canRetract(open({ retracted: { by: "srivera", at: "2026-11-04T15:10:00+00:00" } }), at(SIGNED))).toBe(false);
    expect(canRetract(open({ retract_until: "" }), at(SIGNED))).toBe(false);
    expect(canRetract(null, at(SIGNED))).toBe(false);
  });
});

describe("what a retracted record says", () => {
  const retracted = open({ retracted: { by: "srivera", at: "2026-11-04T15:10:33+00:00" }, head_sha: "4e1b9c2deadbeef" });
  it("heads its line Retracted record and keeps the rest of the line", () => {
    expect(RETRACTED_LINE).toBe("Retracted record");
    expect(recordLineText({ ...retracted, parts_reviewed_one_by_one: 3 }, 1)).toBe("Retracted record · Revision 1 · 3 decisions answered · Details");
  });
  it("tells Wrap up in SPEC's sentence", () => {
    expect(retractedNotice(retracted, 1, "shop")).toBe("You retracted the earlier record · Revision 1 · shop @ 4e1b9c2 · 15:10 UTC. Sending again signs a new one.");
  });
  it("leaves out the repository or the commit it does not have", () => {
    expect(retractedNotice(retracted, 2, "")).toBe("You retracted the earlier record · Revision 2 · 4e1b9c2 · 15:10 UTC. Sending again signs a new one.");
    expect(retractedNotice({ ...retracted, head_sha: "" }, 1, "shop")).toBe("You retracted the earlier record · Revision 1 · shop · 15:10 UTC. Sending again signs a new one.");
  });
  it("shows the retracted record only while it is the newest send on this revision", () => {
    const old = retracted;
    const newer = open({ record_id: "rec_b", issued_at: "2026-11-04T16:00:00+00:00" });
    expect(latestRetractedRecord([old], "rev1")?.record_id).toBe("rec_a");
    expect(latestRetractedRecord([old, newer], "rev1")).toBeNull();
    expect(latestRetractedRecord([old], "rev2")).toBeNull();
    expect(latestRetractedRecord([newer], "rev1")).toBeNull();
  });
  it("names why the server refused, and that nothing changed", () => {
    expect(retractRefusalText(Object.assign(new Error("x"), { detail: { code: "window_closed" } }))).toBe("Can't retract · the 24 hours from signing have passed. Nothing was changed.");
    expect(retractRefusalText(Object.assign(new Error("x"), { detail: { code: "different_key" } }))).toContain("not the key set up now");
    expect(retractRefusalText(new Error("network"))).toBe("Couldn't retract. Nothing was changed.");
  });
});
