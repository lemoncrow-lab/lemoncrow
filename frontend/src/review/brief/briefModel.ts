/**
 * The Brief's evidence and change lines, pure: from the plan, the checks and the triage the Reader already holds.
 * A line with no server data behind it is not built (SPEC 1); an AI review is never read as a test result.
 */
import { findingCounts, type FindingCount } from "../overview/blockersModel";
import { plural } from "../overview/overviewModel";
import { revisionName } from "../revisionLabel";
import type {
  EvidenceRow,
  ReviewAiReviewStatus,
  ReviewArchitectureDelta,
  ReviewEvaluationTimeline,
  ReviewProjection,
  ReviewProjectionClaim,
  ReviewTriage,
} from "../types";

type Statement = NonNullable<NonNullable<ReviewProjection["summary"]>["statements"]>[number];

// ---------------------------------------------------------------- statements

const ORIGIN_LABEL: Readonly<Record<string, string>> = {
  inferred: "AI summary · intent inferred",
  author: "From author context",
  mixed: "Author context and AI explanations",
};

export interface ChangeStatements {
  /** Where the statements come from (brief_summary origin); "" when none is labelled. */
  label: string;
  texts: string[];
}

/**
 * The change statements the Brief does not already show. Engine facts (`observed`) are drawn as fact lines, and a
 * summary with no explanation repeats its facts as statements, so neither is listed again; nor is the title.
 */
export function changeStatements(statements: readonly Statement[] | undefined, shown: ReadonlySet<string>): ChangeStatements | null {
  const seen = new Set<string>();
  const rows = (statements ?? []).filter((row) => {
    const text = row.text.trim();
    if (!text || row.origin === "observed" || shown.has(text) || seen.has(text)) return false;
    seen.add(text);
    return true;
  });
  if (rows.length === 0) return null;
  const origins = new Set(rows.map((row) => row.origin));
  const origin = origins.size === 1 ? rows[0].origin : origins.has("inferred") || origins.has("mixed") ? "mixed" : "author";
  return { label: ORIGIN_LABEL[origin] ?? "", texts: rows.map((row) => row.text.trim()) };
}

// ------------------------------------------------------------------ priority

const PRIORITY_LABEL: Readonly<Record<string, string>> = { urgent: "Urgent", high: "High", normal: "Normal", low: "Low" };
const CRITICALITY_LABEL: Readonly<Record<string, string>> = { critical: "Critical", high: "High criticality", low: "Low criticality" };
const SEVERITY_LABEL: Readonly<Record<string, string>> = { p0: "P0", p1: "P1", p2: "P2", stale: "Stale findings" };

export interface PriorityLine {
  priority: string;
  tone: "p0" | "p1" | "plain";
  /** "" at normal criticality, which says nothing. */
  criticality: string;
  findings: FindingCount[];
  /** Each field's reason, as review_triage explained it. */
  reasons: { key: string; label: string; text: string }[];
}

/** "Priority High · Critical · 2 P0 · 1 P1" and why; null for the placeholder triage of a revision with no plan. */
export function priorityLine(triage: ReviewTriage | null | undefined): PriorityLine | null {
  if (!triage || triage.computed === false || !PRIORITY_LABEL[triage.priority]) return null;
  const reasons = (triage.explanation ?? []).flatMap((entry, index) => {
    const label = entry.field === "priority" ? "Priority" : entry.field === "criticality" ? "Criticality" : entry.field === "severity" ? SEVERITY_LABEL[entry.value] ?? "" : "";
    const text = entry.reason.trim();
    return label && text ? [{ key: `${entry.field}:${entry.value}:${index}`, label, text }] : [];
  });
  return {
    priority: PRIORITY_LABEL[triage.priority],
    tone: triage.priority === "urgent" ? "p0" : triage.priority === "high" ? "p1" : "plain",
    criticality: CRITICALITY_LABEL[triage.criticality] ?? "",
    findings: findingCounts(triage),
    reasons,
  };
}

// ------------------------------------------------------------------ evidence

const CHECK_ORDER: Readonly<Record<string, number>> = { FAIL: 0, PASS: 1, UNKNOWN: 2, NOT_RUN: 3 };

/** Failures first, then passes, then what has no result: a check that did not run is never shown as a pass. */
export function orderedChecks(checks: readonly EvidenceRow[] | null | undefined): EvidenceRow[] {
  return [...(checks ?? [])].sort((left, right) => (CHECK_ORDER[left.status] ?? 2) - (CHECK_ORDER[right.status] ?? 2));
}

/** Who recorded a check, in words; "" for the server's placeholders. */
export function checkSource(source: string): string {
  if (source.startsWith("session:")) return "the agent session";
  const known: Readonly<Record<string, string>> = { none: "", evidence: "", agent: "the agent", human: "a person", external: "an external check", run_ledger: "the run ledger" };
  return known[source] ?? source;
}

const CLAIM_ORDER: Readonly<Record<ReviewProjectionClaim["summary"], number>> = {
  contradictory: 0, inferred_failing: 1, none: 2, partial: 3, inferred: 4, author_cited: 5, observed: 6,
};

/** Contradicted claims first, then the ones nothing shows, then the shown ones. */
export function orderedClaims(claims: readonly ReviewProjectionClaim[] | null | undefined): ReviewProjectionClaim[] {
  return [...(claims ?? [])].filter((claim) => (claim.statement || claim.id).trim())
    .sort((left, right) => (CLAIM_ORDER[left.summary] ?? 2) - (CLAIM_ORDER[right.summary] ?? 2));
}

/** "Regression · login · PASS → FAIL · Revision 3": comparable results that went from passing to failing, newest revision first. */
export function regressionLines(timeline: ReviewEvaluationTimeline | null | undefined): { key: string; text: string }[] {
  return (timeline?.snapshots ?? []).flatMap((snapshot) => (snapshot.comparable_results ?? [])
    .filter((result) => result.change === "regression")
    .map((result) => ({
      key: `${snapshot.revision_id}:${result.scenario}`,
      text: `Regression · ${result.scenario} · ${result.from} → ${result.to} · ${revisionName(snapshot.snapshot_number)}`,
    })));
}

/** Where AI review stands, in one line with no control; "" when the server sent no status. */
export function aiReviewLine(review: ReviewAiReviewStatus | null | undefined): string {
  if (!review) return "";
  if (review.available === false) return `AI review · ${review.unavailable_reason || "not available here"}`;
  const max = Number(review.budget?.max_calls ?? 0);
  const limited = (max > 0 && Number(review.budget?.calls_used ?? 0) >= max) || Number(review.skipped_by_budget ?? 0) > 0;
  const status = {
    ready: `complete · ${plural(Number(review.findings ?? 0), "finding")} · not a test result`,
    pending: "running",
    failed: "failed",
    cancelled: "cancelled",
    skipped_unnecessary: "not needed for this change",
    none: limited ? "" : "not run",
  }[review.status] ?? "";
  return `AI review · ${[status, limited ? "limit reached" : ""].filter(Boolean).join(" · ")}`;
}

// -------------------------------------------------------------- architecture

/** The top concerns of a material architecture move (architecture_delta.py); null when nothing moved. */
export function architectureMoved(delta: ReviewArchitectureDelta | null | undefined): string[] | null {
  const summary = delta?.summary;
  if (!summary?.material) return null;
  const concerns = (summary.important_concerns ?? []).map((text) => text.trim()).filter(Boolean).slice(0, 3);
  if (concerns.length > 0) return concerns;
  return summary.changed_components > 0 ? [`${plural(summary.changed_components, "component")} changed`] : [];
}
