import type { FinishRecordFacts } from "./FinishSheet";
import { reviewQueue, targetAttention, targetDisplayLabel } from "./readerModel";
import type { ReviewReadinessModel } from "./ReviewReadinessSheet";
import type { ReviewTarget, RevisionInfo } from "./types";

/**
 * Readiness conditions the Finish record already counts in their own units
 * (target marks, comments) or that the verdict being recorded resolves.
 */
const COUNTED_ELSEWHERE = new Set([
  // Local readiness.
  "unjudged",
  "changed",
  "needs-changes",
  "comments",
  "outcome",
  // Server landing conditions.
  "latest-snapshot-reviewed",
  "human-change-requests",
  "human-comments",
  "human-approval-state",
]);

const TESTS_NOT_RUN = "Tests or checks have not run";

/** Unmet conditions not counted elsewhere. Checks that never ran are said once, in plain words, however the readiness lists them. */
function openConditions(
  readiness: Pick<ReviewReadinessModel, "blockers"> & Partial<Pick<ReviewReadinessModel, "unresolvedChecks" | "failedChecks">>,
): { id: string; label: string }[] {
  const conditions = readiness.blockers
    .filter((blocker) => !COUNTED_ELSEWHERE.has(blocker.id) && blocker.id !== "unresolved-checks")
    .map((blocker) => ({ id: blocker.id, label: blocker.label }));
  const notRun = readiness.blockers.some((blocker) => blocker.id === "unresolved-checks")
    || ((readiness.unresolvedChecks ?? 0) > 0 && (readiness.failedChecks ?? 0) === 0);
  return notRun ? [{ id: "unresolved-checks", label: TESTS_NOT_RUN }, ...conditions] : conditions;
}

/** What Finish shows the reviewer they are signing, from state the Reader already holds. */
export function finishRecordFacts({
  revision,
  targets,
  readiness,
  reviewer,
  notMeasured = 0,
}: {
  revision: Pick<RevisionInfo, "revision_number" | "head_sha" | "commit_sha" | "dirty">;
  targets: readonly ReviewTarget[];
  readiness: Pick<ReviewReadinessModel, "blockers" | "evidenceFacts"> & Partial<Pick<ReviewReadinessModel, "unresolvedChecks" | "failedChecks">>;
  reviewer: string;
  /** Analyses that could not run or measure on this revision (`overview.degraded`). */
  notMeasured?: number;
}): FinishRecordFacts {
  return {
    revisionNumber: revision.revision_number,
    headSha: revision.commit_sha || revision.head_sha || "",
    dirty: revision.dirty,
    targets: targets.map((target) => ({
      id: target.target_id,
      label: targetDisplayLabel(target),
      path: target.path,
      reviewed: target.state === "reviewed" && !target.changed_since_mark,
      needsChanges: target.state === "needs_changes" && !target.changed_since_mark,
    })),
    passedChecks: readiness.evidenceFacts
      .filter((fact) => fact.state === "verified")
      .map((fact) => ({ id: fact.id, label: fact.label, source: fact.source })),
    // The engine's own queue: risky parts first, in the order the reader walks them.
    openParts: reviewQueue(targets).map((target) => {
      const attention = targetAttention(target);
      return {
        id: target.target_id,
        label: targetDisplayLabel(target),
        path: target.path,
        reason: attention.reason_label,
        risky: attention.attention === "review_first",
      };
    }),
    openConditions: openConditions(readiness),
    notMeasured,
    reviewer,
  };
}
