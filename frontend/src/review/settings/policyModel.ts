import type { ReviewDirectoryRow, ReviewPolicyProjection } from "../types";

/** The repository Settings › Review policy reads: the review changed most recently (the server has no "opened" time). */
export function latestReview(rows: readonly ReviewDirectoryRow[]): ReviewDirectoryRow | null {
  let best: ReviewDirectoryRow | null = null;
  for (const row of rows) if (best === null || row.updated_at > best.updated_at) best = row;
  return best;
}

export interface PolicyArea {
  paths: string;
  /** Why it needs a person, only as far as the rules file says so. */
  why: string;
}

/** "Areas that always need a person": the critical `review.focus` rules (SPEC 4.11). Nothing is invented about why. */
export function alwaysADecision(policy: ReviewPolicyProjection | null): PolicyArea[] {
  return (policy?.authority.focus ?? [])
    .filter((rule) => rule.criticality === "critical")
    .map((rule) => ({
      paths: rule.paths.join(", "),
      why: rule.require && rule.require.length > 0 ? `Critical · needs a ${rule.require.join(", ")} review` : "Critical",
    }));
}

/** The folder name of a review's repository, for "Rules for <name>". */
export function repoLabel(row: Pick<ReviewDirectoryRow, "project" | "directory" | "project_root">): string {
  const raw = row.project || row.directory || row.project_root || "";
  return raw.split("/").filter(Boolean).pop() ?? "";
}

/** One commit that changed the rules file on the base branch (`GET /reviews/<id>/policy-history`, local only). */
export interface PolicyCommit {
  commit: string;
  date: string;
  author: string;
  subject: string;
}

export interface PolicyHistory {
  path: string;
  available: boolean;
  entries: PolicyCommit[];
}

/** "7c1d2e0 · 2026-10-28 · Maya Chen · "Add public API routes to always a decision"" (SPEC 4.19). */
export function commitLine(entry: PolicyCommit): string {
  return `${entry.commit} \u00b7 ${entry.date} \u00b7 ${entry.author} \u00b7 \u201c${entry.subject}\u201d`;
}

export interface PendingPolicyChange {
  id: string;
  title: string;
  href: string;
}

/** The open reviews whose change edits the rules file, CODEOWNERS or the allowed signers: waiting for a CODEOWNER (SPEC 4.19). */
export function pendingPolicyChanges(rows: readonly ReviewDirectoryRow[]): PendingPolicyChange[] {
  return rows
    .filter((row) => row.edits_policy === true && row.status === "open")
    .map((row) => ({ id: row.id, title: row.title, href: row.review_path || `/r/${row.id}` }));
}
