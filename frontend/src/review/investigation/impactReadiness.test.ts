import { describe, expect, it } from "vitest";

import type { ReviewImpactReadiness } from "../types";
import { consumersComplete, degradedSentence, readinessGaps } from "./impactReadiness";

const base: ReviewImpactReadiness = {
  source: "checkout_search",
  impact_pass: "ran",
  index_status: "current",
  degraded: [],
  caller_expansion: { max_symbols: 40, callers_per_symbol: 25 },
  sites_recorded: 0,
};

describe("readinessGaps", () => {
  it("says callers came from a search of the code, never from the current index", () => {
    const gaps = readinessGaps(base);
    expect(gaps.map((gap) => gap.title)).toEqual(["Callers found by searching the code"]);
    expect(gaps[0].detail).not.toMatch(/current code index/);
    expect(consumersComplete(base)).toBe(true);
  });

  it("names the symbols the search could not finish and counts them as hiding callers", () => {
    const unmeasured = ["a", "b", "c", "d"].map((symbol) => ({ path: "app/x.py", symbol, reason: "past the 400-file bound" }));
    const gaps = readinessGaps({ ...base, unmeasured });
    expect(gaps[1]).toMatchObject({
      key: "callers:unmeasured",
      detail: "The search could not finish for a (past the 400-file bound); b (past the 400-file bound); c (past the 400-file bound) and 1 more.",
      hidesConsumers: true,
    });
    expect(consumersComplete({ ...base, unmeasured })).toBe(false);
  });

  it("keeps the current-index wording when the current index answered", () => {
    const gaps = readinessGaps({ ...base, source: "current_index" });
    expect(gaps[0].key).toBe("index:current");
  });
});

describe("Structure drawer gaps in plain words", () => {
  it("maps every recorded code a reviewer sees to a sentence, and an unknown one to none", () => {
    for (const code of ["agent_reads_unrecorded", "centrality", "non_definition_symbols", "test_evidence_unavailable"]) {
      const text = degradedSentence(code);
      expect(text, code).toBeTruthy();
      expect(text).not.toMatch(/_/);
    }
    expect(degradedSentence("brand_new_code")).toBeNull();
  });
});
