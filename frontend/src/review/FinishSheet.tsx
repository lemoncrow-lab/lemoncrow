import { useEffect, useRef } from "react";

import { trapModalTab } from "./focusTrap";
import type { DegradedNote, ReviewClosure } from "./types";
interface FinishSheetProps {
  summary: ReviewClosure;
  degraded?: DegradedNote[];
  busy: boolean;
  onClose(): void;
  onFinish(): void;
}

function CountRow({ label, count, tone = "text-neutral-300", detail }: { label: string; count: number; tone?: string; detail: string }) {
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-4 border-t border-neutral-900 py-2 first:border-t-0">
      <div className="min-w-0">
        <div className={`text-[11px] ${tone}`}>{label}</div>
        <div className="mt-0.5 text-[10px] leading-4 text-neutral-600">{detail}</div>
      </div>
      <div className={`font-mono text-[12px] ${tone}`}>{count}</div>
    </div>
  );
}

export default function FinishSheet({ summary, degraded = [], busy, onClose, onFinish }: FinishSheetProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  const outstandingTargets =
    summary.unreviewed_targets
    + summary.changed_since_review
    + summary.needs_changes
    + summary.unknown_targets;
  const outstandingSignals =
    summary.open_comments
    + summary.orphaned_comments
    + summary.failed_verification
    + summary.unresolved_verification;
  const complete = outstandingTargets === 0 && outstandingSignals === 0;
  // Completion floors rather than rounds: rounding paints a full bar at 99.5%, which reads
  // as a finished review while targets still need judgment. 100% must mean 100%.
  const percent = summary.target_count > 0
    ? Math.floor((summary.reviewed_targets / summary.target_count) * 100)
    : 100;

  return (
    <div
      className="absolute inset-0 z-[70] flex justify-end bg-black/45"
      role="dialog"
      aria-modal="true"
      aria-labelledby="finish-sheet-title"
      aria-describedby="finish-sheet-description"
      onKeyDown={(event) => {
        if (trapModalTab(event)) return;
        if (event.key === "Escape") {
          event.stopPropagation();
          onClose();
        }
      }}
    >
      <section className="flex h-full w-[min(520px,100%)] flex-col border-l border-neutral-700 bg-neutral-950 shadow-2xl">
        <div className="flex items-start gap-3 border-b border-neutral-800 px-5 py-4">
          <div className="min-w-0 flex-1">
            <h2 id="finish-sheet-title" className="text-[14px] font-semibold text-neutral-100">Finish review</h2>
            <p id="finish-sheet-description" className="mt-1 text-[11px] leading-4 text-neutral-500">
              This records your decision to stop reviewing this revision. It does not approve the code or resolve outstanding feedback.
            </p>
          </div>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close finish review" className="text-[12px] text-neutral-500 hover:text-neutral-200">Esc</button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
          <div className="border border-neutral-800 bg-neutral-900/30 p-3">
            <div className="flex items-end gap-3">
              <div className="text-2xl font-semibold text-neutral-100">{summary.reviewed_targets}/{summary.target_count}</div>
              <div className="pb-0.5 text-[11px] text-neutral-500">targets reviewed · {percent}%</div>
            </div>
            <div className="mt-2 h-1 overflow-hidden bg-neutral-800">
              <div className="h-full bg-emerald-600" style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} />
            </div>
            <div className="mt-2 text-[10px] text-neutral-600">
              Target total is the non-overlapping symbol/hunk/file fallback set shown in the reader; raw semantic units are not counted here.
            </div>
          </div>

          <div className="mt-5">
            <div className="mb-1 text-[10px] font-medium uppercase tracking-widest text-neutral-500">Target judgment</div>
            <CountRow label="Reviewed" count={summary.reviewed_targets} tone="text-emerald-300" detail="Human judgment still matches the current target content." />
            <CountRow label="Unreviewed" count={summary.unreviewed_targets} detail="Current targets with no human judgment yet." />
            <CountRow label="Changed since review" count={summary.changed_since_review} tone="text-sky-300" detail="You reviewed an earlier fingerprint; the current target needs another look." />
            <CountRow label="Needs changes" count={summary.needs_changes} tone="text-rose-300" detail="Targets on which you explicitly requested changes." />
            <CountRow label="Unknown identity" count={summary.unknown_targets} tone="text-amber-300" detail="LemonCrow cannot prove stable content identity, so these cannot count as reviewed." />
          </div>

          <div className="mt-5">
            <div className="mb-1 text-[10px] font-medium uppercase tracking-widest text-neutral-500">Outstanding risk</div>
            <CountRow label="Open human comments" count={summary.open_comments} detail="Human comment threads still open on this review." />
            <CountRow label="Orphaned human comments" count={summary.orphaned_comments} tone="text-amber-300" detail="Comments whose code anchor could not be relocated safely." />
            <CountRow label="Failed verification" count={summary.failed_verification} tone="text-rose-300" detail="Current verification evidence explicitly reports failure." />
            <CountRow label="Unresolved verification" count={summary.unresolved_verification} tone="text-amber-300" detail="Checks that are not run or whose result is unknown." />
          </div>

          {degraded.length > 0 && (
            <div className="mt-5 border border-amber-900/60 bg-amber-950/15 p-3">
              <div className="text-[10px] font-medium uppercase tracking-widest text-amber-300">Uncertain signals · {degraded.length}</div>
              <div className="mt-2 space-y-2">
                {degraded.map((item) => (
                  <div key={item.name} className="border-l border-amber-900/50 pl-2 text-[10px] leading-4 text-neutral-500">
                    <div className="font-mono text-amber-200/80">{item.name}</div>
                    <div>{item.note}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {(summary.previous_revision_evidence > 0 || summary.discarded_verdicts > 0) && (
            <div className="mt-5">
              <div className="mb-1 text-[10px] font-medium uppercase tracking-widest text-neutral-500">Review history</div>
              <CountRow label="Previous-revision evidence" count={summary.previous_revision_evidence} detail="Superseded evidence retained for audit history; it is not current proof." />
              <CountRow label="Discarded verdicts" count={summary.discarded_verdicts} detail="Past human judgments whose reviewed units left the current revision." />
            </div>
          )}
        </div>

        <div className="border-t border-neutral-800 px-5 py-4">
          <div className={`mb-3 text-[11px] ${complete ? "text-emerald-300" : "text-amber-300"}`}>
            {complete
              ? "Nothing outstanding in the current target and risk summary."
              : `${outstandingTargets} target${outstandingTargets === 1 ? "" : "s"} still need judgment · ${outstandingSignals} unresolved risk signal${outstandingSignals === 1 ? "" : "s"}.`}
          </div>
          <div className="flex items-center justify-end gap-2">
            <button type="button" disabled={busy} onClick={onClose} className="border border-neutral-800 px-3 py-1.5 text-[11px] text-neutral-400 hover:border-neutral-600 hover:text-neutral-200 disabled:opacity-40">
              Continue reviewing
            </button>
            <button type="button" disabled={busy} onClick={onFinish} className="border border-neutral-500 bg-neutral-100 px-3 py-1.5 text-[11px] font-medium text-neutral-950 hover:bg-white disabled:opacity-40">
              {complete ? "Finish review" : "Finish with outstanding work"}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
