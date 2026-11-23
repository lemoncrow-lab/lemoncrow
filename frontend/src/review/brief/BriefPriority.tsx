/** "Priority High · Critical · 2 P0 · 1 P1" (the Review's triage), with each field's reason one click away. */
import { Fragment } from "react";

import type { PriorityLine } from "./briefModel";

const TONE: Record<PriorityLine["tone"], string> = { p0: "text-p0", p1: "text-p1", plain: "text-foreground" };
const SEVERITY_TONE = { p0: "text-p0", p1: "text-p1", p2: "text-p2" } as const;

export default function BriefPriority({ line }: { line: PriorityLine }) {
  const rest = [
    ...(line.criticality ? [<span key="criticality" className="text-foreground">{line.criticality}</span>] : []),
    ...line.findings.map((row) => <span key={row.severity} className={`font-mono tabular-nums ${SEVERITY_TONE[row.severity]}`}>{row.text}</span>),
  ];
  const body = (
    <>
      <span className="text-muted-foreground">Priority </span>
      <span className={`font-semibold ${TONE[line.tone]}`}>{line.priority}</span>
      {rest.map((part) => <Fragment key={part.key}><span className="text-muted-foreground"> · </span>{part}</Fragment>)}
    </>
  );
  if (line.reasons.length === 0) return <p className="text-caption" data-testid="brief-priority">{body}</p>;
  return (
    <details className="text-caption" data-testid="brief-priority">
      <summary className="cursor-pointer">{body}</summary>
      <ul className="mt-1 flex flex-col gap-0.5 pl-2 text-muted-foreground" data-testid="brief-priority-reasons">
        {line.reasons.map((reason) => (
          <li key={reason.key}><span className="text-foreground">{reason.label}</span>{` · ${reason.text}`}</li>
        ))}
      </ul>
    </details>
  );
}
