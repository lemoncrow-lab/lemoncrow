import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";

import "./reviewUi.css";
import { trapModalTab } from "./focusTrap";
import ReviewEvidenceFacts, { type ReviewEvidenceFact } from "./ReviewEvidenceFacts";
import type { DegradedNote, ReviewClosure, ReviewOutcome, ReviewOutcomeKind } from "./types";

interface FinishSheetProps {
  summary: ReviewClosure;
  degraded?: DegradedNote[];
  evidenceFacts?: readonly ReviewEvidenceFact[];
  busy: boolean;
  currentOutcome?: ReviewOutcome | null;
  onClose(): void;
  onFinish(outcome: ReviewOutcomeKind, summary: string): void;
}

function CountRow({ label, count, tone = "text-neutral-300", detail }: { label: string; count: number; tone?: string; detail: string }) {
  return (
    <div className="flex gap-3 border-t border-neutral-900 py-2 first:border-t-0">
      <div className={`w-5 shrink-0 text-right font-mono text-[10px] ${tone}`}>{count}</div>
      <div className="min-w-0">
        <div className={`text-[10px] font-medium ${tone}`}>{label}</div>
        <div className="mt-0.5 text-[10px] leading-4 text-neutral-600">{detail}</div>
      </div>
    </div>
  );
}

const OUTCOMES: { id: ReviewOutcomeKind; label: string; hint: string }[] = [
  { id: "comment", label: "Comment", hint: "Finish without an overall approval verdict." },
  { id: "lgtm", label: "LGTM", hint: "This revision looks good to you." },
  { id: "changes_requested", label: "Request changes", hint: "Author action is still required." },
];

export default function FinishSheet({
  summary,
  degraded = [],
  evidenceFacts = [],
  busy,
  currentOutcome = null,
  onClose,
  onFinish,
}: FinishSheetProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const current = currentOutcome && !currentOutcome.stale ? currentOutcome : null;
  const [outcome, setOutcome] = useState<ReviewOutcomeKind>(current?.outcome ?? "comment");
  const [outcomeSummary, setOutcomeSummary] = useState(current?.summary ?? "");

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
      <section className="review-sheet flex h-full w-[min(500px,100%)] flex-col border-y-0 border-r-0">
        <div className="flex items-start gap-3 border-b border-neutral-800/80 px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 id="finish-sheet-title" className="text-[13px] font-semibold text-neutral-100">Finish review</h2>
            <p id="finish-sheet-description" className="mt-0.5 text-[10px] leading-4 text-neutral-500">
              Record your LemonCrow verdict for this revision. Publishing an approval to GitHub is separate.
            </p>
          </div>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close finish review" className="review-icon-button">
            <X size={14} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto px-4 py-4">
          <section>
            <div className="review-kicker mb-2">Your verdict</div>
            {currentOutcome?.stale && (
              <div className="mb-2 text-[10px] leading-4 text-amber-400/80">
                Your previous {currentOutcome.outcome === "lgtm" ? "LGTM" : currentOutcome.outcome.replaceAll("_", " ")} applies to an older revision. Choose a verdict for this revision.
              </div>
            )}
            <div className="review-segment grid grid-cols-3" role="radiogroup" aria-label="Overall review outcome">
              {OUTCOMES.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  role="radio"
                  aria-checked={outcome === item.id}
                  title={item.hint}
                  onClick={() => setOutcome(item.id)}
                  className={`review-segment-button justify-center text-[10px] ${outcome === item.id ? "review-segment-button-active" : ""}`}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <input
              type="text"
              value={outcomeSummary}
              onChange={(event) => setOutcomeSummary(event.target.value)}
              aria-label="Review outcome summary"
              placeholder={outcome === "changes_requested" ? "What still needs to change? (optional)" : "Review summary (optional)"}
              className="review-field mt-2 h-8 w-full"
              maxLength={16_384}
            />
            {outcome === "lgtm" && !complete && (
              <div className="mt-2 text-[10px] leading-4 text-amber-400/80">
                LGTM can be recorded independently, but {outstandingTargets + outstandingSignals} current review blocker{outstandingTargets + outstandingSignals === 1 ? "" : "s"} remain.
              </div>
            )}
          </section>

          <div className="mt-4 flex items-center gap-2 border-y border-neutral-900 py-2 text-[10px]">
            <span className="text-neutral-500">Review coverage</span>
            <span className="font-mono text-neutral-200">{summary.reviewed_targets}/{summary.target_count}</span>
            <span className="text-neutral-600">· {percent}%</span>
            {complete && <span className="ml-auto text-emerald-300">complete</span>}
          </div>

          {!complete && (
            <div className="mt-5">
              <div className="review-kicker mb-1">Before you finish</div>
              {summary.unreviewed_targets > 0 && <CountRow label="Unreviewed" count={summary.unreviewed_targets} detail="Current targets with no human judgment yet." />}
              {summary.changed_since_review > 0 && <CountRow label="Changed since review" count={summary.changed_since_review} tone="text-sky-300" detail="You reviewed an earlier version; these targets need another look." />}
              {summary.needs_changes > 0 && <CountRow label="Needs changes" count={summary.needs_changes} tone="text-rose-300" detail="Targets on which you explicitly requested changes." />}
              {summary.unknown_targets > 0 && <CountRow label="Unknown identity" count={summary.unknown_targets} tone="text-amber-300" detail="LemonCrow cannot prove these targets stayed the same, so they cannot count as reviewed." />}
              {summary.open_comments > 0 && <CountRow label="Open comments" count={summary.open_comments} detail="Human comment threads still open on this review." />}
              {summary.orphaned_comments > 0 && <CountRow label="Orphaned comments" count={summary.orphaned_comments} tone="text-amber-300" detail="Comments whose code anchor could not be relocated safely." />}
              {summary.failed_verification > 0 && <CountRow label="Failed verification" count={summary.failed_verification} tone="text-rose-300" detail="Current verification evidence explicitly reports failure." />}
              {summary.unresolved_verification > 0 && <CountRow label="Unresolved verification" count={summary.unresolved_verification} tone="text-amber-300" detail="Checks that are not run or whose result is unknown." />}
            </div>
          )}

          <section className="mt-5">
            <div className="review-kicker mb-1">Evidence at finish</div>
            <div className="mb-2 text-[10px] leading-4 text-neutral-600">
              Current observed verification only. Stale evidence stays visible but does not become current proof.
            </div>
            <ReviewEvidenceFacts facts={evidenceFacts} />
          </section>

          {degraded.length > 0 && (
            <details className="mt-5 border-t border-neutral-900 pt-3">
              <summary className="cursor-pointer text-[10px] text-amber-300/80">Analysis limitations · {degraded.length}</summary>
              <div className="mt-2 space-y-2 border-l border-amber-900/40 pl-2">
                {degraded.map((item) => (
                  <div key={item.name} className="text-[10px] leading-4 text-neutral-500">
                    <span className="font-mono text-amber-200/80">{item.name}</span> · {item.note}
                  </div>
                ))}
              </div>
            </details>
          )}

          {(summary.previous_revision_evidence > 0 || summary.discarded_verdicts > 0) && (
            <details className="mt-5 border-t border-neutral-900 pt-3">
              <summary className="cursor-pointer text-[10px] text-neutral-500 hover:text-neutral-50">Previous review history</summary>
              <div className="mt-2">
                {summary.previous_revision_evidence > 0 && <CountRow label="Previous-revision evidence" count={summary.previous_revision_evidence} detail="Superseded proof kept for history; it is not current proof." />}
                {summary.discarded_verdicts > 0 && <CountRow label="Past judgments no longer apply" count={summary.discarded_verdicts} detail="Past human judgments whose reviewed targets left the current revision." />}
              </div>
            </details>
          )}
        </div>

        <div className="border-t border-neutral-800/80 bg-neutral-950/95 px-4 py-3">
          <div className={`mb-3 text-[10px] ${complete ? "text-emerald-300" : "text-amber-300"}`}>
            {complete
              ? "No current blockers."
              : `${outstandingTargets} target${outstandingTargets === 1 ? "" : "s"} and ${outstandingSignals} risk signal${outstandingSignals === 1 ? "" : "s"} still need attention.`}
          </div>
          <div className="flex items-center justify-end gap-2">
            <button type="button" disabled={busy} onClick={onClose} className="review-toolbar-button">
              Continue reviewing
            </button>
            <button type="button" disabled={busy} onClick={() => onFinish(outcome, outcomeSummary.trim())} className="review-toolbar-button-primary">
              {complete ? "Finish review" : "Finish anyway"}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
