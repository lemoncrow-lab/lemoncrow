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
  if (state === "verified") return "PASS";
  if (state === "failed") return "FAIL";
  if (state === "not_run") return "NOT RUN";
  if (state === "stale") return "STALE";
  return "UNKNOWN";
}

function stateClass(state: ReviewEvidenceFactState): string {
  if (state === "verified") return "text-emerald-300";
  if (state === "failed") return "text-rose-300";
  if (state === "not_run" || state === "stale") return "text-amber-300";
  return "text-neutral-400";
}

export default function ReviewEvidenceFacts({
  facts,
  emptyText = "No named verification evidence is attached to this revision.",
}: {
  facts: readonly ReviewEvidenceFact[];
  emptyText?: string;
}) {
  if (facts.length === 0) {
    return (
      <div className="rounded-md border border-neutral-800/70 bg-neutral-900/20 px-3 py-2.5 text-[10px] text-neutral-500">
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
              <span className="min-w-0 flex-1 truncate text-[10px] font-medium text-neutral-300">{item.label}</span>
              <span className={`shrink-0 font-mono text-[9px] ${stateClass(item.state)}`}>{stateLabel(item.state)}</span>
            </div>
            <div className="mt-0.5 text-[10px] leading-4 text-neutral-600">{item.detail}</div>
            <div className="mt-0.5 font-mono text-[9px] text-neutral-700">{item.source}</div>
          </div>
        </div>
      ))}
    </div>
  );
}
