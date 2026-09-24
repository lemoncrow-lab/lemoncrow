import { useEffect, useRef, type ReactNode } from "react";
import { AlertTriangle, CheckCircle2, CircleDot, X } from "lucide-react";

import { trapModalTab } from "./focusTrap";
import ReviewEvidenceFacts, { type ReviewEvidenceFact, type ReviewEvidenceFactState } from "./ReviewEvidenceFacts";
import type { EvidenceRow, ReviewOutcome, ReviewProgress } from "./types";

export type ReviewReadinessTone = "neutral" | "amber" | "rose" | "sky";

export interface ReviewReadinessBlocker {
  id: string;
  label: string;
  detail: string;
  tone: ReviewReadinessTone;
}

export interface ReviewReadinessModel {
  ready: boolean;
  label: string;
  detail: string;
  blockers: ReviewReadinessBlocker[];
  evidenceFacts: ReviewEvidenceFact[];
  targetPending: number;
  openComments: number;
  failedChecks: number;
  unresolvedChecks: number;
  outcome: ReviewOutcome | null;
}

export function deriveReviewReadiness({
  progress,
  openComments,
  commentsState = "ready",
  verification,
  evidence = [],
  staleArtifacts = 0,
  additionalBlockers = [],
  outcome,
}: {
  progress: ReviewProgress;
  openComments: number;
  commentsState?: "loading" | "ready" | "failed";
  verification: { pass: number; fail: number; not_run: number; unknown: number };
  evidence?: readonly EvidenceRow[];
  staleArtifacts?: number;
  additionalBlockers?: readonly ReviewReadinessBlocker[];
  outcome: ReviewOutcome | null;
}): ReviewReadinessModel {
  const blockers: ReviewReadinessModel["blockers"] = [];
  const evidenceFacts: ReviewEvidenceFact[] = [];
  const factState = (status: EvidenceRow["status"]): ReviewEvidenceFactState => {
    if (status === "PASS") return "verified";
    if (status === "FAIL") return "failed";
    if (status === "NOT_RUN") return "not_run";
    return "unknown";
  };
  for (const item of evidence) {
    evidenceFacts.push({
      id: `evidence:${item.name}`,
      label: item.name,
      detail: item.detail || (item.status === "PASS"
        ? "Current verification evidence reports PASS."
        : item.status === "FAIL"
          ? "Current verification evidence reports FAIL."
          : item.status === "NOT_RUN"
            ? "This verification was recorded as NOT_RUN."
            : "This verification remains UNKNOWN."),
      source: item.source || "verification",
      state: factState(item.status),
    });
  }
  if (evidenceFacts.length === 0) {
    if (verification.pass > 0) {
      evidenceFacts.push({
        id: "evidence:pass-summary",
        label: `${verification.pass} check${verification.pass === 1 ? "" : "s"} passed`,
        detail: "Current aggregate verification reports PASS.",
        source: "verification",
        state: "verified",
      });
    }
    if (verification.fail > 0) {
      evidenceFacts.push({
        id: "evidence:fail-summary",
        label: `${verification.fail} check${verification.fail === 1 ? "" : "s"} failed`,
        detail: "Current aggregate verification reports FAIL.",
        source: "verification",
        state: "failed",
      });
    }
    if (verification.not_run > 0) {
      evidenceFacts.push({
        id: "evidence:not-run-summary",
        label: `${verification.not_run} check${verification.not_run === 1 ? "" : "s"} not run`,
        detail: "Verification explicitly reports NOT_RUN.",
        source: "verification",
        state: "not_run",
      });
    }
    if (verification.unknown > 0) {
      evidenceFacts.push({
        id: "evidence:unknown-summary",
        label: `${verification.unknown} check${verification.unknown === 1 ? "" : "s"} unknown`,
        detail: "Verification remains UNKNOWN.",
        source: "verification",
        state: "unknown",
      });
    }
  }
  if (staleArtifacts > 0) {
    evidenceFacts.push({
      id: "evidence:stale-artifacts",
      label: `${staleArtifacts} stale artifact${staleArtifacts === 1 ? "" : "s"}`,
      detail: "These artifacts belong to older source and are not current verification.",
      source: "artifact",
      state: "stale",
    });
  }
  const unjudged = progress.unreviewed + progress.unknown;
  if (unjudged > 0) {
    blockers.push({
      id: "unjudged",
      label: `${unjudged} target${unjudged === 1 ? "" : "s"} need judgment`,
      detail: "Current targets with no durable human judgment yet.",
      tone: "amber",
    });
  }
  if (progress.changed_since_review > 0) {
    blockers.push({
      id: "changed",
      label: `${progress.changed_since_review} target${progress.changed_since_review === 1 ? "" : "s"} changed since review`,
      detail: "Previous judgments no longer match the current content fingerprint.",
      tone: "sky",
    });
  }
  if (progress.needs_changes > 0) {
    blockers.push({
      id: "needs-changes",
      label: `${progress.needs_changes} target${progress.needs_changes === 1 ? "" : "s"} need changes`,
      detail: "The reviewer explicitly asked for changes on these targets.",
      tone: "rose",
    });
  }
  if (commentsState !== "ready") {
    blockers.push({
      id: "comments-unavailable",
      label: commentsState === "loading" ? "Checking review discussions" : "Review discussions unavailable",
      detail: commentsState === "loading"
        ? "Comment state is still loading; readiness cannot assume there are no open threads."
        : "Comment state could not be loaded; readiness cannot treat that as zero open threads.",
      tone: "neutral",
    });
  } else if (openComments > 0) {
    blockers.push({
      id: "comments",
      label: `${openComments} open discussion${openComments === 1 ? "" : "s"}`,
      detail: "Human review threads remain unresolved.",
      tone: "amber",
    });
  }
  if (verification.fail > 0) {
    blockers.push({
      id: "failed-checks",
      label: `${verification.fail} failed check${verification.fail === 1 ? "" : "s"}`,
      detail: "Current verification evidence explicitly reports failure.",
      tone: "rose",
    });
  }
  const unresolvedChecks = verification.not_run + verification.unknown;
  if (unresolvedChecks > 0) {
    blockers.push({
      id: "unresolved-checks",
      label: `${unresolvedChecks} unresolved check${unresolvedChecks === 1 ? "" : "s"}`,
      detail: "Required review evidence is not run or remains unknown.",
      tone: "amber",
    });
  }
  blockers.push(...additionalBlockers);

  const currentOutcome = outcome && !outcome.stale ? outcome : null;
  if (currentOutcome?.outcome === "changes_requested") {
    blockers.push({
      id: "outcome",
      label: "Current reviewer outcome requests changes",
      detail: currentOutcome.summary || "The reviewer has not withdrawn the changes-requested verdict.",
      tone: "rose",
    });
  }

  const ready = blockers.length === 0;
  let label = ready ? "Ready for verdict" : `Not ready · ${blockers.length}`;
  let detail = ready
    ? "Review coverage, feedback, and verification have no blocking items."
    : blockers[0]?.label ?? "Review is blocked.";

  if (currentOutcome?.outcome === "lgtm") {
    label = ready ? "LGTM · ready" : `LGTM · ${blockers.length} blocker${blockers.length === 1 ? "" : "s"}`;
    detail = currentOutcome.summary || "Current revision has an LGTM verdict.";
  } else if (currentOutcome?.outcome === "comment" && ready) {
    label = "Reviewed · ready";
    detail = currentOutcome.summary || "Review completed without an overall approval verdict.";
  } else if (currentOutcome?.outcome === "changes_requested") {
    label = "Changes requested";
    detail = currentOutcome.summary || "The current reviewer verdict requests changes.";
  } else if (outcome?.stale && ready) {
    label = "Ready for re-verdict";
    detail = `Previous ${outcome.outcome === "lgtm" ? "LGTM" : outcome.outcome.replaceAll("_", " ")} applies to an older revision.`;
  }

  return {
    ready,
    label,
    detail,
    blockers,
    evidenceFacts,
    targetPending: unjudged + progress.changed_since_review + progress.needs_changes,
    openComments,
    failedChecks: verification.fail,
    unresolvedChecks,
    outcome,
  };
}

function toneClass(tone: ReviewReadinessModel["blockers"][number]["tone"]): string {
  if (tone === "rose") return "text-rose-300";
  if (tone === "sky") return "text-sky-300";
  if (tone === "amber") return "text-amber-300";
  return "text-neutral-300";
}

export default function ReviewReadinessSheet({
  model,
  extraSections,
  onClose,
}: {
  model: ReviewReadinessModel;
  extraSections?: ReactNode;
  onClose(): void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => closeRef.current?.focus(), []);
  const verificationBlockers = new Set(["failed-checks", "unresolved-checks"]);
  const humanBlockers = model.blockers.filter((item) => !verificationBlockers.has(item.id));

  return (
    <div
      className="review-sheet-backdrop absolute z-[82] justify-end"
      role="dialog"
      aria-modal="true"
      aria-labelledby="review-readiness-title"
      onKeyDown={(event) => {
        if (trapModalTab(event)) return;
        if (event.key === "Escape") {
          event.stopPropagation();
          onClose();
        }
      }}
    >
      <section className="review-sheet flex h-full w-[min(440px,100%)] flex-col border-y-0 border-r-0">
        <div className="flex items-center gap-3 border-b border-neutral-800/80 px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 id="review-readiness-title" className="text-[12px] font-semibold text-neutral-100">Review readiness</h2>
            <div className={`mt-0.5 text-[10px] ${model.ready ? "text-emerald-300" : "text-neutral-500"}`} title={model.detail}>{model.label}</div>
          </div>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close review readiness" className="review-icon-button">
            <X size={14} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-4">
          <section>
            <div className="review-kicker mb-1">Human review</div>
            {humanBlockers.length === 0 ? (
              <div className="flex items-center gap-2 rounded-md border border-emerald-900/35 bg-emerald-950/10 px-3 py-2.5 text-[10px] text-emerald-300">
                <CheckCircle2 size={12} />
                No unresolved human-review blockers on the current revision.
              </div>
            ) : (
              <>
                <div className="mb-2 text-[10px] leading-4 text-neutral-600">
                  These require human judgment or an explicit workflow transition.
                </div>
                <div className="divide-y divide-neutral-900 border-y border-neutral-900">
                  {humanBlockers.map((item) => (
                    <div key={item.id} className="flex gap-2.5 py-2.5">
                      {item.tone === "rose" ? <AlertTriangle size={12} className="mt-0.5 shrink-0 text-rose-400" /> : <CircleDot size={11} className={`mt-0.5 shrink-0 ${toneClass(item.tone)}`} />}
                      <div className="min-w-0">
                        <div className={`text-[10px] font-medium ${toneClass(item.tone)}`}>{item.label}</div>
                        <div className="mt-0.5 text-[10px] leading-4 text-neutral-600">{item.detail}</div>
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </section>

          <section className="mt-5">
            <div className="review-kicker mb-1">Evidence</div>
            <div className="mb-2 text-[10px] leading-4 text-neutral-600">
              Observed or recorded verification only. Author and agent claims remain separate review context.
            </div>
<ReviewEvidenceFacts facts={model.evidenceFacts} />
          </section>

          {extraSections}

          {model.outcome && (
            <section className="mt-5">
              <div className="review-kicker mb-2">Your latest outcome</div>
              <div className="rounded-md border border-neutral-800/70 bg-neutral-900/25 px-3 py-2.5">
                <div className="flex items-center gap-2 text-[10px]">
                  <span className="font-medium capitalize text-neutral-200">{model.outcome.outcome.replaceAll("_", " ")}</span>
                  {model.outcome.stale && <span className="review-pill text-amber-300">older revision</span>}
                </div>
                {model.outcome.summary && <div className="mt-1 text-[10px] leading-4 text-neutral-500">{model.outcome.summary}</div>}
              </div>
            </section>
          )}
        </div>
      </section>
    </div>
  );
}
