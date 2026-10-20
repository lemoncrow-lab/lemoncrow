import { Fragment, type ReactNode } from "react";

import { Badge } from "../components/ui/badge";
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
  // Severity counts are badges (P0 red, P1 amber, P2 cyan); the surrounding text and separators are unchanged.
  const severity: ReactNode[] = [
    summary.p0 ? <Badge key="p0" tone="p0">{summary.p0} P0</Badge> : null,
    summary.p1 ? <Badge key="p1" tone="p1">{summary.p1} P1</Badge> : null,
    summary.p2 ? <Badge key="p2" tone="p2">{summary.p2} P2</Badge> : null,
  ].filter(Boolean);
  const parts: ReactNode[] = [
    `Priority ${LEVEL[triage.priority] ?? triage.priority}`,
    triage.criticality !== "normal" ? `${LEVEL[triage.criticality] ?? triage.criticality} criticality` : "",
    ...severity,
    triage.evidence_state === "current" ? "" : triage.evidence_label,
  ].filter(Boolean);
  return (
    <details data-testid="brief-review-priority">
      <summary className="cursor-pointer">
        {parts.map((part, index) => (
          <Fragment key={index}>
            {index > 0 ? " · " : null}
            {part}
          </Fragment>
        ))}
        {triage.override ? " · overridden" : ""}
      </summary>
      <div className="mt-1 space-y-1 pl-2">
        {triage.explanation.map((entry, index) => (
          <div key={`${entry.field}:${index}`} data-testid="triage-explanation">
            <span className="text-subtle">{FIELD[entry.field] ?? entry.field}</span>
            {entry.value ? ` · ${LEVEL[entry.value] ?? entry.value}` : ""}
            {" — "}
            {entry.reason}
            <span className="text-muted-foreground"> ({triageOriginLabel(entry.origin)})</span>
          </div>
        ))}
        {summary.below_threshold > 0 && (
          <div>
            {summary.below_threshold} finding{summary.below_threshold === 1 ? "" : "s"} below the {triage.finding_threshold.label} threshold
            {summary.below_threshold === 1 ? " is" : " are"} not surfaced; still counted for priority.
          </div>
        )}
        {triage.policy_errors.map((error) => <div key={error} className="text-p1">Priority policy degraded: {error}</div>)}
      </div>
    </details>
  );
}
