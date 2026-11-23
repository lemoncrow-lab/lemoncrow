import { describe, expect, it } from "vitest";

import type { ReviewLandingReadiness, ReviewProjection } from "../types";
import { findingCounts, mergeReadinessLine, requiredCheckRows } from "./blockersModel";

const row = (id: string, target_ids: string[] = []) => ({ id, reason: "", title: id, detail: "", item_ids: [], target_ids, actions: [], tier: 0 });
const readiness = (ready: boolean, blocker_count: number, states: string[] = []) => ({
  ready, blocker_count, conditions: states.map((state, index) => ({ id: `c${index}`, label: "", state, category: "verification" })),
}) as unknown as ReviewLandingReadiness;

describe("blockersModel", () => {
  it("takes the plan's review-level rows only: no decision, no behavior, no part", () => {
    const actions = [row("check:tests"), row("decision:a"), row("behavior:b"), row("part", ["t1"])];
    expect(requiredCheckRows({ actions } as unknown as ReviewProjection).map((item) => item.id)).toEqual(["check:tests"]);
    expect(requiredCheckRows(null)).toEqual([]);
    expect(requiredCheckRows({} as ReviewProjection)).toEqual([]);
  });

  it("counts open findings by severity and drops the zeros", () => {
    expect(findingCounts({ severity_summary: { p0: 2, p1: 0, p2: 1 } } as never).map((item) => item.text)).toEqual(["2 P0", "1 P2"]);
    expect(findingCounts({ severity_summary: { p0: 0, p1: 0, p2: 0 } } as never)).toEqual([]);
    expect(findingCounts({} as never)).toEqual([]);
    expect(findingCounts(null)).toEqual([]);
  });

  it("says Ready, or which checks and how many merge blockers stand in the way", () => {
    expect(mergeReadinessLine(readiness(true, 0))).toEqual({ text: "Ready", tone: "ok" });
    expect(mergeReadinessLine(readiness(false, 2, ["fail", "pending"]))).toEqual({ text: "Checks failed · 2 merge blockers", tone: "p0" });
    expect(mergeReadinessLine(readiness(false, 1, ["pending"]))).toEqual({ text: "Checks not run · 1 merge blocker", tone: "p1" });
    expect(mergeReadinessLine(readiness(false, 1, ["pass"]))).toEqual({ text: "1 merge blocker", tone: "p1" });
    expect(mergeReadinessLine(readiness(false, 0))).toEqual({ text: "Not ready", tone: "p1" });
    expect(mergeReadinessLine(null)).toBeNull();
  });
});
