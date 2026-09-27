import { useEffect, useRef, type ReactNode } from "react";
import { AlertTriangle, CheckCircle2, CircleDot, ExternalLink, X } from "lucide-react";

import { trapModalTab } from "./focusTrap";
import { webHref } from "./safeLinks";
import ReviewEvidenceFacts, { type ReviewEvidenceFact, type ReviewEvidenceFactState } from "./ReviewEvidenceFacts";
import type { EvidenceRow, ReviewLandingCondition, ReviewLandingReadiness, ReviewOutcome, ReviewProgress, ReviewRelationshipInfo } from "./types";

export type ReviewReadinessTone = "neutral" | "amber" | "rose" | "sky";

export interface ReviewReadinessBlocker {
  id: string;
  label: string;
  detail: string;
  tone: ReviewReadinessTone;
}

export function scmRequirementBlockers(
  requirements: readonly ReviewRelationshipInfo[],
): ReviewReadinessBlocker[] {
  const satisfied = new Set(["pass", "passed", "success", "satisfied", "ready", "complete", "completed"]);
  const failed = new Set(["fail", "failed", "blocked", "rejected"]);
  return requirements
    .filter((item) => !satisfied.has(item.status.toLowerCase()))
    .map((item) => {
      const identity = item.title || item.external_id || item.relation || "SCM requirement";
      return {
        id: "scm-requirement:" + item.id,
        label: item.status ? identity + " · " + item.status : identity + " · status unknown",
        detail: (item.provider || "Connected SCM") + " remains authoritative for this landing requirement.",
        tone: failed.has(item.status.toLowerCase()) ? "rose" : "amber",
      };
    });
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
      label: `${unjudged} review area${unjudged === 1 ? "" : "s"} need judgment`,
      detail: "Current review areas with no durable human judgment yet.",
      tone: "amber",
    });
  }
  if (progress.changed_since_review > 0) {
    blockers.push({
      id: "changed",
      label: `${progress.changed_since_review} review area${progress.changed_since_review === 1 ? "" : "s"} changed since review`,
      detail: "Previous judgments no longer match the current content fingerprint.",
      tone: "sky",
    });
  }
  if (progress.needs_changes > 0) {
    blockers.push({
      id: "needs-changes",
      label: `${progress.needs_changes} review area${progress.needs_changes === 1 ? "" : "s"} need changes`,
      detail: "The reviewer explicitly asked for changes on these review areas.",
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
    detail = currentOutcome.summary || "Current Snapshot has an LGTM verdict.";
  } else if (currentOutcome?.outcome === "comment" && ready) {
    label = "Reviewed · ready";
    detail = currentOutcome.summary || "Review completed without an overall approval verdict.";
  } else if (currentOutcome?.outcome === "changes_requested") {
    label = "Changes requested";
    detail = currentOutcome.summary || "The current reviewer verdict requests changes.";
  } else if (outcome?.stale && ready) {
    label = "Ready for re-verdict";
    detail = `Previous ${outcome.outcome === "lgtm" ? "LGTM" : outcome.outcome.replaceAll("_", " ")} applies to an older Snapshot.`;
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

function landingConditionClass(condition: ReviewLandingCondition): string {
  if (condition.state === "pass") return "text-emerald-300";
  if (condition.state === "fail") return "text-rose-300";
  if (condition.state === "not_applicable") return "text-neutral-500";
  if (condition.state === "waived") return "text-neutral-300";
  return "text-amber-300";
}

function LandingConditionIcon({ condition }: { condition: ReviewLandingCondition }) {
  if (condition.state === "pass") return <CheckCircle2 size={12} className="mt-0.5 shrink-0 text-emerald-400" />;
  if (condition.state === "fail") return <AlertTriangle size={12} className="mt-0.5 shrink-0 text-rose-400" />;
  if (condition.state === "not_applicable") return <CircleDot size={11} className="mt-0.5 shrink-0 text-neutral-700" />;
  if (condition.state === "waived") return <CircleDot size={11} className="mt-0.5 shrink-0 text-neutral-400" />;
  return <CircleDot size={11} className="mt-0.5 shrink-0 text-amber-300" />;
}

export default function ReviewReadinessSheet({
  model,
  landing,
  landingBusy = false,
  landingError = "",
  readOnly = false,
  extraSections,
  onArmLanding,
  onCancelLanding,
  onClose,
}: {
  model: ReviewReadinessModel;
  landing?: ReviewLandingReadiness | null;
  landingBusy?: boolean;
  landingError?: string;
  readOnly?: boolean;
  extraSections?: ReactNode;
  onArmLanding?(): void;
  onCancelLanding?(): void;
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
            {landing ? (
              <div
                className={`mt-0.5 text-[10px] ${
                  landing.landing.state === "merged" || (landing.ready && landing.landing.supported)
                    ? "text-emerald-300"
                    : landing.ready
                      ? "text-neutral-400"
                      : "text-amber-300"
                }`}
              >
                {landing.landing.state === "merged"
                  ? "Merged"
                  : landing.ready
                  ? landing.landing.supported
                    ? "Ready to land"
                    : "Review ready · SCM landing unavailable"
                  : `Not ready · ${landing.blocker_count} condition${landing.blocker_count === 1 ? "" : "s"}`}
                <span className="ml-2 font-mono text-neutral-600">Snapshot {landing.snapshot_number}</span>
              </div>
            ) : (
              <div className={`mt-0.5 text-[10px] ${model.ready ? "text-emerald-300" : "text-neutral-500"}`} title={model.detail}>{model.label}</div>
            )}
          </div>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close review readiness" className="review-icon-button">
            <X size={14} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto p-4">
          {!landing && landingError && (
            <div className="mb-4 rounded-md border border-amber-900/50 bg-amber-950/10 px-3 py-2.5 text-[10px] leading-4 text-amber-300" role="alert">
              {landingError}
              <div className="mt-1 text-neutral-600">Readiness is not assumed from partial local state. Close and reopen this panel to retry.</div>
            </div>
          )}
          {landing && (
            <>
              <section>
                <div className="review-kicker mb-2">Landing conditions</div>
                <div className="divide-y divide-neutral-900 border-y border-neutral-900">
                  {landing.conditions.map((condition) => (
                    <div key={condition.id} className="flex gap-2.5 py-2.5">
                      <LandingConditionIcon condition={condition} />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-x-2">
                          <span className={`text-[10px] font-medium ${landingConditionClass(condition)}`}>{condition.label}</span>
                          {condition.owner === "scm" && <span className="review-pill text-neutral-500">SCM-owned</span>}
                          {condition.state === "waived" && <span className="review-pill text-amber-300/80">waived by a human</span>}
                        </div>
                        <div className="mt-0.5 text-[10px] leading-4 text-neutral-600">{condition.detail}</div>
                        {webHref(condition.url) && (
                          <a href={webHref(condition.url)} target="_blank" rel="noreferrer" className="mt-1 inline-flex items-center gap-1 text-[10px] text-sky-300 hover:underline">
                            Inspect requirement <ExternalLink size={10} />
                          </a>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </section>

              {landing.work_items.length > 0 && (
                <section className="mt-5">
                  <div className="review-kicker mb-2">Work item</div>
                  <div className="space-y-1.5">
                    {landing.work_items.map((item) => (
                      <div key={item.id} className="rounded-md border border-neutral-800/70 bg-neutral-900/20 px-3 py-2">
                        {webHref(item.url) ? (
                          <a href={webHref(item.url)} target="_blank" rel="noreferrer" className="flex items-center justify-between gap-3 text-[10px] text-neutral-300 hover:text-white">
                            <span className="min-w-0 truncate">{item.title || item.external_id || "Associated work item"}</span>
                            <span className="flex shrink-0 items-center gap-1 text-neutral-600">{item.provider || "external"}<ExternalLink size={10} /></span>
                          </a>
                        ) : (
                          <div className="text-[10px] text-neutral-400">{item.title || item.external_id || "Associated work item"}</div>
                        )}
                      </div>
                    ))}
                  </div>
                </section>
              )}

              <section className="mt-5 rounded-md border border-neutral-800/80 bg-neutral-900/25 p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-[10px] font-semibold text-neutral-200">Merge when ready</div>
                    <div className="mt-1 text-[10px] leading-4 text-neutral-600">
                      {landing.landing.supported
                        ? `Delegates final landing to ${landing.landing.provider || "the connected SCM"}; LemonCrow does not merge or rebase Git itself.`
                        : "No connected SCM landing mechanism is available for this Change."}
                    </div>
                    {landing.landing.detail && (
                      <div className={`mt-1 text-[10px] leading-4 ${landing.landing.state === "failed" || landing.landing.state === "blocked" || landing.landing.semantic_stale ? "text-amber-300" : "text-neutral-500"}`}>
                        {landing.landing.detail}
                      </div>
                    )}
                    {webHref(landing.landing.url) && (
                      <a href={webHref(landing.landing.url)} target="_blank" rel="noreferrer" className="mt-1 inline-flex items-center gap-1 text-[10px] text-sky-300 hover:underline">
                        Open {landing.landing.provider || "SCM"} <ExternalLink size={10} />
                      </a>
                    )}
                  </div>
                  {landing.landing.armed ? (
                    <span className="review-pill shrink-0 text-emerald-300">armed</span>
                  ) : landing.landing.state === "merged" ? (
                    <span className="review-pill shrink-0 text-emerald-300">merged</span>
                  ) : landing.landing.semantic_stale ? (
                    <span className="review-pill shrink-0 text-amber-300">stale</span>
                  ) : landing.landing.state === "blocked" ? (
                    <span className="review-pill shrink-0 text-amber-300">disarmed</span>
                  ) : null}
                </div>
                {/* Cancelling is always safe, so a read-only sheet (a discarded or
                    no-longer-current Change) still withdraws an armed landing;
                    only arming needs the current, writable view. */}
                {landing.landing.supported && landing.landing.state !== "merged" && (landing.landing.armed || !readOnly) && (
                  <div className="mt-3 flex items-center gap-2">
                    {landing.landing.armed ? (
                      <button type="button" disabled={landingBusy} onClick={onCancelLanding} className="review-compact-action disabled:opacity-50">
                        {landingBusy ? "Cancelling…" : "Cancel merge when ready"}
                      </button>
                    ) : (
                      <button
                        type="button"
                        disabled={landingBusy || !landing.armable}
                        onClick={onArmLanding}
                        className="review-compact-action disabled:opacity-50"
                        title={!landing.armable ? "Resolve LemonCrow-owned blockers before arming landing." : "Explicitly arm provider-native landing."}
                      >
                        {landingBusy ? "Arming…" : landing.landing.semantic_stale ? "Re-arm Merge when ready" : "Arm Merge when ready"}
                      </button>
                    )}
                    {!landing.ready && landing.armable && !landing.landing.armed && (
                      <span className="text-[10px] text-neutral-600">SCM-owned conditions may remain pending; the provider will wait for them.</span>
                    )}
                  </div>
                )}
                {landingError && <div className="mt-2 text-[10px] text-rose-300">{landingError}</div>}
              </section>
            </>
          )}

          {!landing && (
            <section>
              <div className="review-kicker mb-1">Human review</div>
            {humanBlockers.length === 0 ? (
              <div className="flex items-center gap-2 rounded-md border border-emerald-900/35 bg-emerald-950/10 px-3 py-2.5 text-[10px] text-emerald-300">
                <CheckCircle2 size={12} />
                No unresolved human-review blockers on the current Snapshot.
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
          )}

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
                  {model.outcome.stale && <span className="review-pill text-amber-300">older Snapshot</span>}
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
