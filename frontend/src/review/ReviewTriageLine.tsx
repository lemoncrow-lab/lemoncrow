import type { ReviewTriage, ReviewTriageOrigin } from "./types";

const LEVEL: Record<string, string> = { urgent: "Urgent", high: "High", normal: "Normal", low: "Low", critical: "Critical" };
const FIELD: Record<string, string> = {
  priority: "Priority",
  override: "Override",
  criticality: "Criticality",
  severity: "Severity",
  required_verification: "Required checks",
  dependency: "Dependencies",
  required_focus: "Required review",
  release_urgency: "Release urgency",
  evidence: "Evidence",
  finding_threshold: "Finding threshold",
};
const LAYER: Record<string, string> = {
  built_in_default: "built-in default",
  organization: "organization policy",
  team: "team policy",
  repository: "repository policy",
  trusted_base_repository: "trusted base",
  directory: "directory policy",
  evidence: "Snapshot evidence",
  override: "override",
};

export function triageOriginLabel(origin: ReviewTriageOrigin): string {
  const layer = LAYER[origin.layer] ?? origin.layer.replaceAll("_", " ");
  if (origin.layer === "built_in_default" || origin.layer === "evidence") return layer;
  const where = origin.commit ? `${origin.source}@${origin.commit.slice(0, 12)}` : origin.source;
  return where ? `${layer} · ${where}` : layer;
}

/**
 * Review priority with its inspectable explanation, collapsed in the Brief footer like
 * the other policy/coverage lines. Priority, criticality and severity stay separate.
 */
export default function ReviewTriageLine({ triage }: { triage?: ReviewTriage | null }) {
  if (!triage) return null;
  const summary = triage.severity_summary;
  const severity = [
    summary.p0 ? `${summary.p0} P0` : "",
    summary.p1 ? `${summary.p1} P1` : "",
    summary.p2 ? `${summary.p2} P2` : "",
  ].filter(Boolean).join(" · ");
  const parts = [
    `Priority ${LEVEL[triage.priority] ?? triage.priority}`,
    triage.criticality !== "normal" ? `${LEVEL[triage.criticality] ?? triage.criticality} criticality` : "",
    severity,
    triage.evidence_state === "current" ? "" : triage.evidence_label,
  ].filter(Boolean);
  return (
    <details data-testid="brief-review-priority">
      <summary className="cursor-pointer">
        {parts.join(" · ")}
        {triage.override ? " · overridden" : ""}
      </summary>
      <div className="mt-1 space-y-1 pl-2">
        {triage.explanation.map((entry, index) => (
          <div key={`${entry.field}:${index}`} data-testid="triage-explanation">
            <span className="text-neutral-300">{FIELD[entry.field] ?? entry.field}</span>
            {entry.value ? ` · ${LEVEL[entry.value] ?? entry.value}` : ""}
            {" — "}
            {entry.reason}
            <span className="text-neutral-500"> ({triageOriginLabel(entry.origin)})</span>
          </div>
        ))}
        {summary.below_threshold > 0 && (
          <div>
            {summary.below_threshold} finding{summary.below_threshold === 1 ? "" : "s"} below the {triage.finding_threshold.label} threshold
            {summary.below_threshold === 1 ? " is" : " are"} not surfaced; still counted for priority.
          </div>
        )}
        {triage.policy_errors.map((error) => <div key={error} className="text-amber-300/90">Priority policy degraded: {error}</div>)}
      </div>
    </details>
  );
}
