import { AlertTriangle, CheckCircle2, CircleDot } from "lucide-react";

export type ReviewEvidenceFactState = "verified" | "failed" | "not_run" | "unknown" | "stale";

export interface ReviewEvidenceFact {
  id: string;
  label: string;
  detail: string;
  source: string;
  state: ReviewEvidenceFactState;
}

function stateLabel(state: ReviewEvidenceFactState): string {
  if (state === "verified") return "Passed";
  if (state === "failed") return "Failed";
  if (state === "not_run") return "Did not run";
  if (state === "stale") return "Older evidence";
  return "Couldn’t determine";
}

function stateClass(state: ReviewEvidenceFactState): string {
  if (state === "verified") return "text-emerald-300";
  if (state === "failed") return "text-rose-300";
  if (state === "not_run" || state === "stale") return "text-amber-300";
  return "text-neutral-400";
}

export default function ReviewEvidenceFacts({
  facts,
  emptyText = "No named verification evidence is attached to this Snapshot.",
}: {
  facts: readonly ReviewEvidenceFact[];
  emptyText?: string;
}) {
  if (facts.length === 0) {
    return (
      <div className="rounded-md border border-border/70 bg-muted/20 px-3 py-2.5 text-caption text-muted-foreground">
        {emptyText}
      </div>
    );
  }

  return (
    <div className="divide-y divide-neutral-900 border-y border-neutral-900">
      {facts.map((item) => (
        <div key={item.id} className="flex gap-2.5 py-2.5">
          {item.state === "verified"
            ? <CheckCircle2 size={12} className="mt-0.5 shrink-0 text-emerald-400" />
            : item.state === "failed"
              ? <AlertTriangle size={12} className="mt-0.5 shrink-0 text-rose-400" />
              : <CircleDot size={11} className={`mt-0.5 shrink-0 ${stateClass(item.state)}`} />}
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-caption font-medium text-neutral-300">{item.label}</span>
              <span className={`shrink-0 text-micro ${stateClass(item.state)}`}>{stateLabel(item.state)}</span>
            </div>
            <details className="mt-1 text-caption text-muted-foreground">
              <summary className="cursor-pointer select-none text-caption text-muted-foreground">Evidence details</summary>
              <div className="mt-1 leading-4">{item.detail}</div>
              <div className="mt-0.5 font-mono text-micro">Source · {item.source}</div>
            </details>
          </div>
        </div>
      ))}
    </div>
  );
}
