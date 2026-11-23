import { describe, expect, it } from "vitest";
import type { ReviewConsumerGroup, ReviewConsumerSite, ReviewProjection, ReviewTarget } from "../types";
import { callerCoverage, siteProvenance, structureGroups, structureLimitations } from "./focusStructureModel";

const target = { target_id: "t", unit_key: "u", path: "src/auth.py", symbol: "can_view_order" } as ReviewTarget;
const group = (extra: Partial<ReviewConsumerGroup> = {}): ReviewConsumerGroup => ({
  key: "key", path: "src/auth.py", symbol: "can_view_order", target_ids: ["t"], join: "exact", known_callers: null,
  sites: [], omitted: 0, unlisted_callers: 0, ...extra,
} as ReviewConsumerGroup);
const site = (extra: Partial<ReviewConsumerSite> = {}): ReviewConsumerSite => ({
  path: "src/views.py", symbol: "show", line: 17, snippet: "return can_view_order()", kind: "caller", impact_kind: "signature_change",
  in_patch: false, exactness: "snapshot", uncertainty: "", ...extra,
} as ReviewConsumerSite);

describe("Focus Structure provenance and coverage", () => {
  it("lists only exact target or same-file groups and never unrelated callers", () => {
    const groups = [group(), group({ key: "file", target_ids: [], join: "file" }), group({ key: "unrelated", path: "src/elsewhere.py", target_ids: [], join: "file" }), group({ key: "review", path: "", target_ids: [], join: "review" })];
    const projection = { context: { consumers: groups } } as unknown as ReviewProjection;
    expect(structureGroups(target, projection).map((row) => row.key)).toEqual(["key", "file"]);
    expect(structureGroups(target, null)).toEqual([]);
  });

  it("lists one call site once, keeping its checked-in-code reading", () => {
    const caller = site({ path: "src/jobs/sweep.py", line: 5 });
    const groups = [
      group({ key: "key", sites: [caller] }),
      group({ key: "file", target_ids: [], join: "file", sites: [{ ...caller, exactness: "current_index", impact_kind: "untouched_caller" }] }),
    ];
    const projection = { context: { consumers: groups } } as unknown as ReviewProjection;
    const shown = structureGroups(target, projection);
    expect(shown.map((row) => row.key)).toEqual(["key"]);
    expect(shown.flatMap((row) => row.sites).map((row) => siteProvenance(row).label)).toEqual(["Checked in code"]);
  });

  it("marks only exact snapshot facts as checked, never an index fall-back or name-match candidate", () => {
    expect(siteProvenance(site())).toEqual({ label: "Checked in code", checked: true });
    expect(siteProvenance(site({ exactness: "current_index" }))).toEqual({ label: "Not verified · read from the current index, not this revision", checked: false });
    expect(siteProvenance(site({ exactness: "snapshot", uncertainty: "ambiguous namesake" }))).toEqual({ label: "Not verified · ambiguous namesake", checked: false });
    expect(siteProvenance(site({ exactness: "unknown" }))).toEqual({ label: "Not verified · evidence origin unknown", checked: false });
  });

  it("does not turn unknown or omitted caller coverage into zero", () => {
    expect(callerCoverage(group())).toBe("Not verified · caller count unavailable");
    expect(callerCoverage(group({ known_callers: 0 }))).toBe("No caller found in this repository. Callers outside this repository are not checked.");
    expect(callerCoverage(group({ known_callers: 2 }))).toBe("2 callers · checked in code");
    expect(structureLimitations(group({ omitted: 12, unlisted_callers: 5 }))).toEqual([
      "12 more sites not shown", "5 callers counted but not listed",
    ]);
  });
});
