import { describe, expect, it } from "vitest";

import type { EvidenceRow, ReviewAiReviewStatus, ReviewArchitectureDelta, ReviewEvaluationTimeline, ReviewProjectionClaim, ReviewTriage } from "../types";
import { aiReviewLine, architectureMoved, changeStatements, checkSource, orderedChecks, orderedClaims, priorityLine, regressionLines } from "./briefModel";

const statement = (text: string, origin: string) => ({ text, origin, target_ids: [], claim_ids: [] }) as never;
const ai = (fields: Partial<ReviewAiReviewStatus>) => ({ status: "none", findings: 0, budget: { calls_used: 0, max_calls: 4 }, ...fields }) as ReviewAiReviewStatus;

describe("briefModel", () => {
  it("lists the statements the Brief does not already show, labelled by where they come from", () => {
    const shown = new Set(["Fix sign-in", "Tests were not run"]);
    expect(changeStatements([statement("Rejects expired tokens", "inferred"), statement("Tests were not run", "observed"), statement("Fix sign-in", "author")], shown))
      .toEqual({ label: "AI summary · intent inferred", texts: ["Rejects expired tokens"] });
    expect(changeStatements([statement("Guests can sign in", "author")], shown)).toEqual({ label: "From author context", texts: ["Guests can sign in"] });
    expect(changeStatements([statement("A", "author"), statement("B", "inferred")], shown)?.label).toBe("Author context and AI explanations");
    expect(changeStatements([statement("Tests were not run", "observed")], shown)).toBeNull();
    expect(changeStatements(undefined, shown)).toBeNull();
  });

  it("reads the triage as one priority line with each field's reason, and never the placeholder", () => {
    const triage = {
      priority: "high", criticality: "critical", severity_summary: { p0: 2, p1: 1, p2: 0 }, computed: true,
      explanation: [{ field: "priority", value: "high", reason: "Touches auth", origin: "engine" }, { field: "severity", value: "p0", reason: "2 blocking findings", origin: "engine" }, { field: "other", value: "", reason: "skip", origin: "" }],
    } as unknown as ReviewTriage;
    const line = priorityLine(triage);
    expect(line).toMatchObject({ priority: "High", tone: "p1", criticality: "Critical" });
    expect(line?.findings.map((row) => row.text)).toEqual(["2 P0", "1 P1"]);
    expect(line?.reasons.map((row) => `${row.label}: ${row.text}`)).toEqual(["Priority: Touches auth", "P0: 2 blocking findings"]);
    expect(priorityLine({ ...triage, criticality: "normal" } as ReviewTriage)?.criticality).toBe("");
    expect(priorityLine({ ...triage, computed: false } as ReviewTriage)).toBeNull();
    expect(priorityLine(null)).toBeNull();
  });

  it("orders checks failures first and never as passes when they did not run", () => {
    const rows = ["NOT_RUN", "PASS", "UNKNOWN", "FAIL"].map((status) => ({ name: status, status, detail: "", source: "none" }) as EvidenceRow);
    expect(orderedChecks(rows).map((row) => row.status)).toEqual(["FAIL", "PASS", "UNKNOWN", "NOT_RUN"]);
    expect(orderedChecks(null)).toEqual([]);
    expect(["session:abc", "none", "agent", "human", "external", "run_ledger", "ci"].map(checkSource))
      .toEqual(["the agent session", "", "the agent", "a person", "an external check", "the run ledger", "ci"]);
  });

  it("puts contradicted claims first", () => {
    const claim = (id: string, summary: string) => ({ id, statement: id, summary }) as unknown as ReviewProjectionClaim;
    expect(orderedClaims([claim("a", "observed"), claim("b", "contradictory"), claim("c", "none")]).map((row) => row.id)).toEqual(["b", "c", "a"]);
    expect(orderedClaims(undefined)).toEqual([]);
  });

  it("names each regression with its revision, and skips recoveries", () => {
    const timeline = { snapshots: [{ revision_id: "r2", snapshot_number: 2, comparable_results: [
      { scenario: "login", from: "PASS", to: "FAIL", change: "regression" },
      { scenario: "cart", from: "FAIL", to: "PASS", change: "recovered" },
    ] }, { revision_id: "r1", snapshot_number: 1 }] } as unknown as ReviewEvaluationTimeline;
    expect(regressionLines(timeline).map((row) => row.text)).toEqual(["Regression · login · PASS → FAIL · Revision 3"]);
    expect(regressionLines(null)).toEqual([]);
  });

  it("says where AI review stands in one line, never as a test result of its own", () => {
    expect(aiReviewLine(ai({ status: "ready", findings: 3 }))).toBe("AI review · complete · 3 findings · not a test result");
    expect(aiReviewLine(ai({ status: "none", budget: { calls_used: 4, max_calls: 4 } } as never))).toBe("AI review · limit reached");
    expect(aiReviewLine(ai({ status: "ready", findings: 1, skipped_by_budget: 2 }))).toBe("AI review · complete · 1 finding · not a test result · limit reached");
    expect(aiReviewLine(ai({ status: "pending" }))).toBe("AI review · running");
    expect(aiReviewLine(ai({ status: "none" }))).toBe("AI review · not run");
    expect(aiReviewLine(ai({ available: false, unavailable_reason: "No AI provider is set up" }))).toBe("AI review · No AI provider is set up");
    expect(aiReviewLine(null)).toBe("");
  });

  it("draws the architecture line only for a material move", () => {
    const delta = (summary: Partial<ReviewArchitectureDelta["summary"]>) => ({ summary: { material: true, changed_components: 0, important_concerns: [], ...summary } }) as unknown as ReviewArchitectureDelta;
    expect(architectureMoved(delta({ important_concerns: ["a", " ", "b", "c", "d"] }))).toEqual(["a", "b", "c"]);
    expect(architectureMoved(delta({ changed_components: 2 }))).toEqual(["2 components changed"]);
    expect(architectureMoved(delta({ material: false, important_concerns: ["a"] }))).toBeNull();
    expect(architectureMoved(null)).toBeNull();
  });
});
