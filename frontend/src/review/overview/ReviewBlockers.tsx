/** The blockers lines the Overview, the Brief and Wrap up share (blockersModel); each draws nothing without its data. */
import { Fragment } from "react";

import { Button } from "../../components/ui/button";
import type { ReviewProjectionQuestion } from "../types";
import type { FindingCount, ReadinessLine } from "./blockersModel";
import { Label } from "./OverviewParts";

const SEVERITY_TONE: Record<FindingCount["severity"], string> = { p0: "text-p0", p1: "text-p1", p2: "text-p2" };
const READINESS_TONE: Record<ReadinessLine["tone"], string> = { ok: "text-ok", p0: "text-p0", p1: "text-p1" };

/** One "Required check failing" row: the plan's own title and detail, and the one way to its evidence. */
export function RequiredCheckRow({ row, onInspectEvidence, testId }: { row: ReviewProjectionQuestion; onInspectEvidence?: () => void; testId: string }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1" data-testid={testId}>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-label font-medium text-p0">{row.title}</span>
        {row.detail ? <span className="break-words text-meta text-muted-foreground">{row.detail}</span> : null}
      </span>
      {onInspectEvidence ? (
        <Button variant="outline" size="compact" className="shrink-0" onClick={onInspectEvidence}>Inspect evidence</Button>
      ) : null}
    </div>
  );
}

/** "Findings 2 P0 · 1 P1"; nothing when no finding is open. */
export function FindingCountLine({ counts, testId }: { counts: readonly FindingCount[]; testId: string }) {
  if (counts.length === 0) return null;
  return (
    <p className="flex flex-wrap items-baseline gap-x-2 text-label" data-testid={testId}>
      <Label>Findings</Label>
      <span className="font-mono tabular-nums">
        {counts.map((row, index) => (
          <Fragment key={row.severity}>
            {index > 0 ? <span className="text-muted-foreground"> · </span> : null}
            <span className={SEVERITY_TONE[row.severity]}>{row.text}</span>
          </Fragment>
        ))}
      </span>
    </p>
  );
}

/** "Merge readiness Ready" or what blocks it; nothing until the server's readiness has loaded. */
export function MergeReadinessLine({ line, testId }: { line: ReadinessLine | null; testId: string }) {
  if (!line) return null;
  return (
    <p className="flex flex-wrap items-baseline gap-x-2 text-label" data-testid={testId}>
      <Label>Merge readiness</Label>
      <span className={`font-medium ${READINESS_TONE[line.tone]}`}>{line.text}</span>
    </p>
  );
}
