/**
 * What blocks this change, from data the Reader already holds (no request from here): the plan's review-level rows
 * (a required check failing), the triage's finding counts, and the server's merge readiness. Absent data builds
 * nothing, never a zero or a guess.
 */
import type { ReviewLandingReadiness, ReviewProjection, ReviewProjectionQuestion, ReviewTriage } from "../types";
import { plural } from "./overviewModel";

/** The Brief's Evidence card, where "Inspect evidence" lands (`/r/<review>/brief#evidence`). */
export const BRIEF_EVIDENCE_HASH = "#evidence";

/**
 * The plan's review-level rows (review_plan.py `review_level`: "Required check failing"). They name no part
 * (`target_ids` is empty), so a list keyed on parts never shows them; decisions and behavior rows share `actions`
 * and have their own sections.
 */
export function requiredCheckRows(projection: Pick<ReviewProjection, "actions"> | null | undefined): ReviewProjectionQuestion[] {
  return (projection?.actions ?? []).filter(
    (row) => !row.id.startsWith("decision:") && !row.id.startsWith("behavior:") && (row.target_ids ?? []).length === 0,
  );
}

export interface FindingCount {
  severity: "p0" | "p1" | "p2";
  count: number;
  text: string;
}

/** "2 P0", "1 P1": open findings with current evidence (review_triage severity_summary), only the counts above zero. */
export function findingCounts(triage: Pick<ReviewTriage, "severity_summary"> | null | undefined): FindingCount[] {
  const summary = triage?.severity_summary;
  if (!summary) return [];
  return (["p0", "p1", "p2"] as const)
    .map((severity) => ({ severity, count: Number(summary[severity] ?? 0), text: `${Number(summary[severity] ?? 0)} ${severity.toUpperCase()}` }))
    .filter((row) => row.count > 0);
}

export interface ReadinessLine {
  text: string;
  tone: "ok" | "p0" | "p1";
}

/**
 * The server's merge readiness in one line: "Ready", or what blocks it ("Checks failed · 3 merge blockers").
 * Checks are the `verification` conditions (readiness.py): any failure reads failed, any still pending reads not run.
 */
export function mergeReadinessLine(readiness: ReviewLandingReadiness | null | undefined): ReadinessLine | null {
  if (!readiness) return null;
  if (readiness.ready) return { text: "Ready", tone: "ok" };
  const checks = (readiness.conditions ?? []).filter((condition) => condition.category === "verification");
  const failed = checks.some((condition) => condition.state === "fail");
  const notRun = !failed && checks.some((condition) => condition.state === "pending" || condition.state === "unknown");
  const blockers = Number(readiness.blocker_count ?? 0);
  const parts = [
    failed ? "Checks failed" : notRun ? "Checks not run" : "",
    blockers > 0 ? plural(blockers, "merge blocker") : "",
  ].filter(Boolean);
  return { text: parts.length > 0 ? parts.join(" · ") : "Not ready", tone: failed ? "p0" : "p1" };
}
