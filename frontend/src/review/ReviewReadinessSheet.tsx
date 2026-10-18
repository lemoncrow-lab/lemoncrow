import { Badge } from "../components/ui/badge";
import { Button as UiButton } from "../components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetHeader, SheetTitle } from "../components/ui/sheet";
import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, CircleDot, ExternalLink, X } from "lucide-react";

import { webHref } from "./safeLinks";
import { baseOnlyNote } from "./movementLabels";
import ReviewEvidenceFacts, { type ReviewEvidenceFact, type ReviewEvidenceFactState } from "./ReviewEvidenceFacts";
import ReviewPremergeChecks, { type PremergeActions } from "./ReviewPremergeChecks";
import type {
  EvidenceRow,
  ReviewAutoApprovalState,
  ReviewBlockingReason,
  ReviewBreakGlassRecord,
  ReviewLandingCondition,
  ReviewLandingReadiness,
  ReviewLandingScmState,
  ReviewMovementSummary,
  ReviewOutcome,
  ReviewProgress,
  ReviewRelationshipInfo,
} from "./types";

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
      const identity = item.title || item.external_id || item.relation || "Provider requirement";
      return {
        id: "scm-requirement:" + item.id,
        label: item.status ? identity + " · " + item.status : identity + " · status unknown",
        detail: (item.provider || "Connected provider") + " remains authoritative for this merge requirement.",
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
  targetCount: number;
  reviewedTargets: number;
  changedSinceReview: number;
  /** Base-only movement note for the "changed since" summary; empty when none. */
  baseOnlyNote?: string;
  needsChanges: number;
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
  movement,
}: {
  progress: ReviewProgress;
  openComments: number;
  commentsState?: "loading" | "ready" | "failed";
  verification: { pass: number; fail: number; not_run: number; unknown: number };
  evidence?: readonly EvidenceRow[];
  staleArtifacts?: number;
  additionalBlockers?: readonly ReviewReadinessBlocker[];
  outcome: ReviewOutcome | null;
  movement?: ReviewMovementSummary | null;
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
        ? "This check completed successfully for the current Snapshot."
        : item.status === "FAIL"
          ? "This check reported a failure for the current Snapshot."
          : item.status === "NOT_RUN"
            ? "This check did not run for the current Snapshot."
            : "The result of this check could not be determined."),
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
        label: `${verification.not_run} check${verification.not_run === 1 ? "" : "s"} did not run`,
        detail: "These checks did not run for the current Snapshot.",
        source: "verification",
        state: "not_run",
      });
    }
    if (verification.unknown > 0) {
      evidenceFacts.push({
        id: "evidence:unknown-summary",
        label: `${verification.unknown} check result${verification.unknown === 1 ? "" : "s"} could not be determined`,
        detail: "LemonCrow could not determine a current result for these checks.",
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
      detail: "Required checks either did not run or their current result could not be determined.",
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
    targetCount: progress.target_count,
    reviewedTargets: progress.reviewed,
    changedSinceReview: progress.changed_since_review,
    baseOnlyNote: baseOnlyNote(movement),
    needsChanges: progress.needs_changes,
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
  if (condition.state === "not_applicable") return "text-muted-foreground";
  if (condition.state === "waived") return "text-neutral-300";
  return "text-amber-300";
}

function instantLabel(value: string): string {
  return value.replace("T", " ").replace(/Z$/, " UTC");
}

/** Freshness of the external observations a condition relies on, as one caption line. */
function conditionFreshness(condition: ReviewLandingCondition): string {
  const external = (condition.evidence_refs ?? []).filter((ref) => ref.freshness_class === "external_observation");
  if (external.length === 0) return "";
  const lapsed = external.filter((ref) => ref.state !== "current");
  if (lapsed.length > 0) {
    return `${lapsed.length} external observation${lapsed.length === 1 ? " is" : "s are"} no longer current`;
  }
  const earliest = external.map((ref) => ref.expires_at).filter(Boolean).sort()[0];
  return earliest ? `External observation valid until ${instantLabel(earliest)}` : "";
}

function policyTitle(landing: ReviewLandingReadiness): string {
  const generation = landing.policy_generation;
  if (!generation) return "";
  const layers = generation.layers.map((layer) => `${layer.layer}: ${layer.source}${layer.state === "provisional" ? " (provisional)" : ""}`);
  return [`Evaluated against policy ${generation.digest}`, ...layers].join("\n");
}

function providerName(provider: string): string {
  if (provider === "github") return "GitHub";
  if (provider === "gitlab") return "GitLab";
  return provider || "the provider";
}

/** `Blocked: Finish review · Waiting for checks` (distinct actions, in the server's order). */
export function blockingSummary(reasons: readonly ReviewBlockingReason[]): string {
  const actions = [...new Set(reasons.map((row) => row.action).filter(Boolean))];
  if (actions.length === 0) return "";
  return (reasons.some((row) => row.state === "fail") ? "Blocked: " : "Waiting: ") + actions.join(" · ");
}

/** The provider-owned auto-merge / merge-queue state, or empty when the provider reports none. */
export function scmLandingText(scm: ReviewLandingScmState | undefined, provider: string): string {
  if (!scm) return "";
  const parts: string[] = [];
  if (scm.auto_merge) parts.push(`auto-merge ${scm.auto_merge}`);
  if (scm.queue_state) parts.push(`merge queue ${scm.queue_state.replaceAll("_", " ")}`);
  return parts.length ? `Managed by ${providerName(scm.managed_by || provider)}: ${parts.join(" · ")}` : "";
}

/** One line for the policy approval; empty when policy has neither decided nor approved. */
export function autoApprovalText(approval: ReviewAutoApprovalState | undefined, provider: string): string {
  if (!approval) return "";
  const name = providerName(provider);
  if (approval.state === "approved") return `Approved by organization policy on ${name}. Branch protection still applies.`;
  if (approval.state === "approving") return `Approved by organization policy; delivery to ${name} is owed and retried.`;
  if (approval.state === "withdrawn") return approval.detail || "The policy approval was withdrawn.";
  if (approval.state === "stale") return "Approved an earlier Snapshot; this one is decided again.";
  if (approval.state === "unknown") return approval.detail || "Policy approval state unavailable.";
  if (approval.eligible === false && approval.blocking?.length) return `Not auto-approved: ${approval.blocking.join(" · ")}`;
  if (approval.eligible === true) return "Eligible for policy approval.";
  return "";
}

function followUpText(record: ReviewBreakGlassRecord): string {
  const follow = record.follow_up;
  if (follow.state === "completed") return `Follow-up review by ${follow.completed_by || "a reviewer"}`;
  const due = follow.due_at ? instantLabel(follow.due_at) : "unknown";
  return follow.state === "overdue" ? `Follow-up review overdue (was due ${due})` : `Follow-up review due ${due}`;
}

function LandingConditionIcon({ condition }: { condition: ReviewLandingCondition }) {
  if (condition.state === "pass") return <CheckCircle2 size={12} className="mt-0.5 shrink-0 text-emerald-400" />;
  if (condition.state === "fail") return <AlertTriangle size={12} className="mt-0.5 shrink-0 text-rose-400" />;
  if (condition.state === "not_applicable") return <CircleDot size={11} className="mt-0.5 shrink-0 text-muted-foreground" />;
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
  verificationExtras,
  automation,
  deployments,
  premergeActions,
  recordSection,
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
  /** Rendered inside the existing Verification subsection (e.g. the manual checklist). */
  verificationExtras?: ReactNode;
  /** Automation rules and post-merge actions (never part of readiness). */
  automation?: ReactNode;
  /** Deployments of the merged Change (P15-DEPLOY; external observations, never part of readiness). */
  deployments?: ReactNode;
  /** Waive / re-bind / evaluate actions for pre-merge checks; omitted when the sheet cannot act. */
  premergeActions?: PremergeActions;
  /** Review record export (signed / UNSIGNED) for the decided Review; omitted when unavailable. */
  recordSection?: ReactNode;
  onArmLanding?(): void;
  onCancelLanding?(): void;
  onClose(): void;
}) {
  const verificationBlockers = new Set(["failed-checks", "unresolved-checks"]);
  const yourReviewBlockers = new Set([
    "unjudged",
    "changed",
    "needs-changes",
    "comments-unavailable",
    "comments",
    "outcome",
  ]);
  const yourBlockers = model.blockers.filter((item) => yourReviewBlockers.has(item.id));
  const teamOrExternalBlockers = model.blockers.filter(
    (item) => !yourReviewBlockers.has(item.id) && !verificationBlockers.has(item.id),
  );
  const scmFallbackBlockers = teamOrExternalBlockers.filter((item) => item.id.startsWith("scm-requirement:"));
  const teamBlockers = teamOrExternalBlockers.filter((item) => !item.id.startsWith("scm-requirement:"));
  const conditionLabel = (condition: ReviewLandingCondition) => {
    if (
      condition.owner === "scm"
      && condition.state !== "pass"
      && condition.state !== "not_applicable"
      && condition.state !== "waived"
      && /approv/i.test(condition.label)
    ) {
      return "Provider requires renewed approval";
    }
    return condition.label;
  };
  const blocking = landing && !landing.ready ? blockingSummary(landing.blocking_reasons ?? []) : "";
  const approvalText = landing ? autoApprovalText(landing.landing.auto_approval, landing.landing.provider) : "";
  const scmText = landing ? scmLandingText(landing.landing.scm, landing.landing.provider) : "";
  const breakGlass = landing?.landing.break_glass ?? [];

  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent className="w-[min(440px,100%)]">
        <SheetHeader className="items-center">
          <div className="min-w-0 flex-1">
            <SheetTitle className="text-body">Review Status</SheetTitle>
            {landing ? (
              <div
                className={`mt-0.5 text-caption ${
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
                    ? "Ready to merge"
                    : "Review ready · merge unavailable"
                  : `Not ready · ${landing.blocker_count} condition${landing.blocker_count === 1 ? "" : "s"}`}
                <span className="ml-2 font-mono text-muted-foreground">Snapshot {landing.snapshot_number}</span>
              </div>
            ) : (
              <div className={`mt-0.5 text-caption ${model.ready ? "text-emerald-300" : "text-muted-foreground"}`} title={model.detail}>{model.label}</div>
            )}
          </div>
          <SheetClose asChild>
            <UiButton variant="ghost" size="icon" type="button" aria-label="Close review status">
              <X size={14} />
            </UiButton>
          </SheetClose>
        </SheetHeader>

        <div className="min-h-0 flex-1 overflow-auto p-4">
          {!landing && landingError && (
            <div className="mb-4 rounded-md border border-amber-900/50 bg-amber-950/10 px-3 py-2.5 text-caption leading-4 text-amber-300" role="alert">
              {landingError}
              <div className="mt-1 text-muted-foreground">Readiness is not assumed from partial local state. Close and reopen this panel to retry.</div>
            </div>
          )}
          <section aria-labelledby="your-review-status">
            <div id="your-review-status" className="review-kicker mb-1">Your review</div>
            <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-muted-foreground">
              <span className="font-mono text-neutral-300">{model.reviewedTargets}/{model.targetCount} reviewed</span>
              {model.changedSinceReview > 0 && <span>{model.changedSinceReview} changed since your review</span>}
              {model.baseOnlyNote && <span data-testid="readiness-base-only">{model.baseOnlyNote}</span>}
              {model.needsChanges > 0 && <span>{model.needsChanges} need changes</span>}
              {model.openComments > 0 && <span>{model.openComments} unresolved feedback</span>}
            </div>
            {yourBlockers.length === 0 ? (
              <div className="flex items-center gap-2 rounded-md border border-emerald-900/35 bg-emerald-950/10 px-3 py-2.5 text-caption text-emerald-300">
                <CheckCircle2 size={12} />
                No unresolved blockers in your current review.
              </div>
            ) : (
              <div className="divide-y divide-neutral-900 border-y border-neutral-900">
                {yourBlockers.map((item) => (
                  <div key={item.id} className="flex gap-2.5 py-2.5">
                    {item.tone === "rose"
                      ? <AlertTriangle size={12} className="mt-0.5 shrink-0 text-rose-400" />
                      : <CircleDot size={11} className={`mt-0.5 shrink-0 ${toneClass(item.tone)}`} />}
                    <div className="min-w-0">
                      <div className={`text-caption font-medium ${toneClass(item.tone)}`}>{item.label}</div>
                      <div className="mt-0.5 text-caption leading-4 text-muted-foreground">{item.detail}</div>
                    </div>
                  </div>
                ))}
              </div>
            )}
            {model.outcome && (
              <div className="mt-3 rounded-md border border-border/70 bg-muted/25 px-3 py-2.5">
                <div className="flex items-center gap-2 text-caption">
                  <span className="text-muted-foreground">Latest outcome</span>
                  <span className="font-medium capitalize text-neutral-200">{model.outcome.outcome.replaceAll("_", " ")}</span>
                  {model.outcome.stale && <Badge tone="secondary" className="text-amber-300">older Snapshot</Badge>}
                </div>
                {model.outcome.summary && <div className="mt-1 text-caption leading-4 text-muted-foreground">{model.outcome.summary}</div>}
              </div>
            )}
            <div className="mt-4">
              <div className="mb-1 text-caption font-semibold text-neutral-300">Verification</div>
              <div className="mb-2 text-caption leading-4 text-muted-foreground">
                Only completed checks count as passing; checks that did not run or could not be read stay unresolved.
              </div>
              <ReviewEvidenceFacts facts={model.evidenceFacts} />
              {verificationExtras}
            </div>
          </section>

          {(extraSections || teamBlockers.length > 0) && (
            <section className="mt-5" aria-labelledby="team-review-status">
              <div id="team-review-status" className="review-kicker mb-1">Team review</div>
              {teamBlockers.length > 0 && (
                <div className="divide-y divide-neutral-900 border-y border-neutral-900">
                  {teamBlockers.map((item) => (
                    <div key={item.id} className="flex gap-2.5 py-2.5">
                      <CircleDot size={11} className={`mt-0.5 shrink-0 ${toneClass(item.tone)}`} />
                      <div className="min-w-0">
                        <div className={`text-caption font-medium ${toneClass(item.tone)}`}>{item.label}</div>
                        <div className="mt-0.5 text-caption leading-4 text-muted-foreground">{item.detail}</div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
              {extraSections}
            </section>
          )}

          {(landing || scmFallbackBlockers.length > 0) && (
            <section className="mt-5" aria-labelledby="merge-readiness-status">
              <div id="merge-readiness-status" className="review-kicker mb-2">Merge readiness</div>
              {blocking && (
                <div data-testid="readiness-blocking" className="mb-2 text-caption font-medium leading-4 text-amber-300">{blocking}</div>
              )}
              {!landing && scmFallbackBlockers.length > 0 && (
                <div className="divide-y divide-neutral-900 border-y border-neutral-900">
                  {scmFallbackBlockers.map((item) => (
                    <div key={item.id} className="flex gap-2.5 py-2.5">
                      <CircleDot size={11} className={`mt-0.5 shrink-0 ${toneClass(item.tone)}`} />
                      <div className="min-w-0">
                        <div className={`text-caption font-medium ${toneClass(item.tone)}`}>{item.label}</div>
                        <div className="mt-0.5 text-caption leading-4 text-muted-foreground">{item.detail}</div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
              {landing && <div className="divide-y divide-neutral-900 border-y border-neutral-900">
                {landing.conditions.map((condition) => (
                  <div key={condition.id} className="flex gap-2.5 py-2.5">
                    <LandingConditionIcon condition={condition} />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-x-2">
                        <span className={`text-caption font-medium ${landingConditionClass(condition)}`}>{conditionLabel(condition)}</span>
                        {condition.owner === "scm" && <Badge tone="secondary" className="text-muted-foreground">Provider</Badge>}
                        {condition.state === "waived" && <Badge tone="secondary" className="text-amber-300/80">waived by a human</Badge>}
                      </div>
                      {conditionLabel(condition) !== condition.label && (
                        <div className="mt-0.5 text-caption text-neutral-400">{condition.label}</div>
                      )}
                      <div className="mt-0.5 text-caption leading-4 text-muted-foreground">{condition.detail}</div>
                      {conditionFreshness(condition) && (
                        <div className="mt-0.5 font-mono text-caption leading-4 text-muted-foreground">{conditionFreshness(condition)}</div>
                      )}
                      {webHref(condition.url) && (
                        <a href={webHref(condition.url)} target="_blank" rel="noreferrer" className="mt-1 inline-flex items-center gap-1 text-caption text-sky-300 hover:underline">
                          Open requirement <ExternalLink size={10} />
                        </a>
                      )}
                    </div>
                  </div>
                ))}
              </div>}

              {approvalText && (
                <div data-testid="readiness-auto-approval" className="mt-3 rounded-md border border-border/70 bg-muted/15 px-3 py-2 text-caption">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-semibold text-neutral-300">Policy approval</span>
                    {landing?.landing.auto_approval?.state && landing.landing.auto_approval.state !== "none" && (
                      <Badge tone="secondary" className={landing.landing.auto_approval.state === "approved" ? "text-emerald-300" : "text-amber-300"}>
                        {landing.landing.auto_approval.state}
                      </Badge>
                    )}
                  </div>
                  <div className="mt-1 leading-4 text-muted-foreground">{approvalText}</div>
                </div>
              )}

              {landing?.policy_generation && (
                <details data-testid="review-policy-details" className="mt-3 rounded-md border border-border/70 bg-muted/15 px-3 py-2 text-caption">
                  <summary className="cursor-pointer select-none text-neutral-300">
                    Policy details{landing.policy_generation.authority === "provisional" ? " · provisional" : ""}
                  </summary>
                  <div className="mt-2 space-y-1 text-muted-foreground" title={policyTitle(landing)}>
                    <div>Generation <span className="font-mono">{landing.policy_generation.short}</span></div>
                    {landing.policy_generation.layers.map((layer) => (
                      <div key={`${layer.layer}:${layer.digest}`}>
                        <span className="font-mono">{layer.layer}</span> · {layer.source}
                      </div>
                    ))}
                  </div>
                </details>
              )}

              {landing?.premerge && (
                <details data-testid="review-premerge-details" className="mt-3 rounded-md border border-border/70 bg-muted/15 px-3 py-2 text-caption">
                  <summary className="cursor-pointer select-none text-neutral-300">Pre-merge check details</summary>
                  <ReviewPremergeChecks premerge={landing.premerge} readOnly={readOnly} actions={premergeActions} />
                </details>
              )}

              {landing && landing.work_items.length > 0 && (
                <div className="mt-4">
                  <div className="mb-2 text-caption font-semibold text-neutral-300">Work item</div>
                  <div className="space-y-1.5">
                    {landing.work_items.map((item) => (
                      <div key={item.id} className="rounded-md border border-border/70 bg-muted/20 px-3 py-2">
                        {webHref(item.url) ? (
                          <a href={webHref(item.url)} target="_blank" rel="noreferrer" className="flex items-center justify-between gap-3 text-caption text-neutral-300 hover:text-neutral-50">
                            <span className="min-w-0 truncate">{item.title || item.external_id || "Associated work item"}</span>
                            <span className="flex shrink-0 items-center gap-1 text-muted-foreground">{item.provider || "external"}<ExternalLink size={10} /></span>
                          </a>
                        ) : (
                          <div className="text-caption text-neutral-400">{item.title || item.external_id || "Associated work item"}</div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {landing && <div className="mt-4 rounded-md border border-border/80 bg-muted/25 p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-caption font-semibold text-neutral-200">Merge when ready</div>
                    <div className="mt-1 text-caption leading-4 text-muted-foreground">
                      {landing.landing.supported
                        ? `When enabled, ${landing.landing.provider || "the connected provider"} merges after its remaining requirements pass. LemonCrow does not merge or rebase Git itself.`
                        : "Automatic merge is unavailable for this Change."}
                    </div>
                    {landing.landing.detail && (
                      <div className={`mt-1 text-caption leading-4 ${landing.landing.state === "failed" || landing.landing.state === "blocked" || landing.landing.semantic_stale ? "text-amber-300" : "text-muted-foreground"}`}>
                        {landing.landing.detail}
                      </div>
                    )}
                    {landing.landing.provider_cancel === "pending" && (
                      <div className="mt-1 text-caption leading-4 text-amber-300">
                        Cancellation owed to {landing.landing.provider || "the provider"}: it is shown here until {landing.landing.provider || "the provider"} confirms it.
                      </div>
                    )}
                    {scmText && (
                      <div data-testid="readiness-scm-state" className="mt-1 text-caption leading-4 text-muted-foreground">{scmText}</div>
                    )}
                    {landing.landing.armed && landing.landing.policy_stale && (
                      <div className="mt-1 text-caption leading-4 text-amber-300">
                        Enabled under an earlier Review policy; LemonCrow rechecks it before merge can proceed.
                      </div>
                    )}
                    {webHref(landing.landing.url) && (
                      <a href={webHref(landing.landing.url)} target="_blank" rel="noreferrer" className="mt-1 inline-flex items-center gap-1 text-caption text-sky-300 hover:underline">
                        Open {landing.landing.provider || "provider"} <ExternalLink size={10} />
                      </a>
                    )}
                  </div>
                  {landing.landing.armed ? (
                    <Badge tone="secondary" className="shrink-0 text-emerald-300">enabled</Badge>
                  ) : landing.landing.state === "merged" ? (
                    <Badge tone="secondary" className="shrink-0 text-emerald-300">merged</Badge>
                  ) : landing.landing.semantic_stale ? (
                    <Badge tone="secondary" className="shrink-0 text-amber-300">stale</Badge>
                  ) : landing.landing.state === "blocked" ? (
                    <Badge tone="secondary" className="shrink-0 text-amber-300">disabled</Badge>
                  ) : null}
                </div>
                {landing.landing.supported && landing.landing.state !== "merged" && (landing.landing.armed || !readOnly) && (
                  <div className="mt-3 flex items-center gap-2">
                    {landing.landing.armed ? (
                      <UiButton variant="ghost" size="compact" type="button" disabled={landingBusy} onClick={onCancelLanding}>
                        {landingBusy ? "Cancelling…" : "Cancel merge when ready"}
                      </UiButton>
                    ) : (
                      <UiButton
                        variant="ghost"
                        size="compact"
                        type="button"
                        disabled={landingBusy || !landing.armable}
                        onClick={onArmLanding}
                        tooltip={!landing.armable ? "Resolve LemonCrow blockers before enabling merge when ready." : "Let the provider merge after its remaining requirements pass."}
                      >
                        {landingBusy ? "Enabling…" : landing.landing.semantic_stale ? "Re-enable merge when ready" : "Merge when ready"}
                      </UiButton>
                    )}
                    {!landing.ready && landing.armable && !landing.landing.armed && (
                      <span className="text-caption text-muted-foreground">Provider conditions may still be pending; the provider waits for them.</span>
                    )}
                  </div>
                )}
                {landingError && <div className="mt-2 text-caption text-rose-300">{landingError}</div>}
              </div>}

              {breakGlass.length > 0 && (
                <div data-testid="readiness-break-glass" className="mt-3 rounded-md border border-rose-900/40 bg-rose-950/10 px-3 py-2 text-caption">
                  <div className="font-semibold text-rose-300">Break-glass</div>
                  {breakGlass.map((record) => (
                    <div key={record.id} className="mt-1.5 leading-4">
                      <div className="text-neutral-200">
                        {record.actor_id} bypassed {record.bypassed.map((row) => row.label || row.id).join(", ") || "no condition"}
                      </div>
                      <div className="text-muted-foreground">{record.reason}</div>
                      {webHref(record.incident_url) && (
                        <a href={webHref(record.incident_url)} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-sky-300 hover:underline">
                          Incident <ExternalLink size={10} />
                        </a>
                      )}
                      <div className={record.follow_up.state === "overdue" ? "text-rose-300" : record.follow_up.state === "completed" ? "text-emerald-300" : "text-amber-300"}>
                        {followUpText(record)}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>
          )}
          {automation && (
            <details data-testid="review-automation-details" className="mt-5 rounded-md border border-border/70 bg-muted/15 px-3 py-2">
              <summary className="cursor-pointer select-none text-caption font-medium text-neutral-300">Automation</summary>
              {automation}
            </details>
          )}
          {deployments}

          {recordSection && (
            <details data-testid="review-record-details" className="mt-5 rounded-md border border-border/70 bg-muted/15 px-3 py-2">
              <summary className="cursor-pointer select-none text-caption font-medium text-neutral-300">Review record</summary>
              <section aria-label="Review record" className="pt-2">{recordSection}</section>
            </details>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
