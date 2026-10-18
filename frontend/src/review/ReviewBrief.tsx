import { useMemo, useState, type ReactNode } from "react";

import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";

import { Select, SelectItem } from "../components/ui/select";
import { Textarea as UiTextarea } from "../components/ui/textarea";
import { ToggleGroup } from "../components/ui/toggle-group";
import BehaviorChanges, { ExecutionCoverageLine, type BehaviorJudgmentState } from "./BehaviorChanges";
import BriefArchitectureDelta from "./BriefArchitectureDelta";
import BriefProjectContext from "./ProjectHistory";
import { LARGE_REVIEW_TARGET_THRESHOLD } from "./reviewSurfaceModel";
import { DependencyReviewSummary, dependencyReviewRelevant } from "./ReviewFileSemantics";
import ReviewTriageLine from "./ReviewTriageLine";
import type {
  ReviewAiIndependence,
  ReviewAiReviewRunResult,
  ReviewArchitectureDelta,
  ReviewDependencyProjection,
  ReviewBehaviorTarget,
  ReviewDecisionEventInput,
  ReviewIndependenceRequirement,
  ReviewIndependenceStatus,
  ReviewProjection,
  ReviewProjectionClaim,
  ReviewProjectionDecision,
  ReviewProjectionItem,
  ReviewProjectionQuestion,
  ReviewProjectContext,
  ReviewTarget,
} from "./types";

/**
 * The Brief (spec §4.1): a concise handoff rendered in the server's canonical
 * order. Sections are filters over one queue; nothing here ranks. Decision
 * cards offer only the actions the server lists for the decision's derived
 * state (spec §5); without that state they stay read-only.
 */

const SECTION_LIMIT = 5;
const INTERNAL_ONLY_TEXT = "Internal implementation only — external behavior intended to remain unchanged.";
type ReviewDetail = "compact" | "standard" | "detailed";
const REVIEW_DETAIL_KEY = "lemoncrow.review.detail";

export interface ReviewBriefProps {
  projection: ReviewProjection | null;
  loading: boolean;
  error: string;
  targets: readonly ReviewTarget[];
  /** A scoped request: its items lead; the rest sit behind a visible "outside your request" group. */
  requestScope?: { inScopeTargetIds: ReadonlySet<string>; showOutside: boolean; onToggleOutside: () => void };
  onOpenTarget: (targetId: string) => void;
  onOpenFiles: () => void;
  onOpenTour?: () => void;
  onInspectEvidence: (targetId: string | null) => void;
  onAsk?: (targetId: string | null) => void;
  onDecision?: (key: string, input: ReviewDecisionEventInput) => Promise<void>;
  /** Explain unexplained change groups through the shared Review intelligence runtime. */
  onSynthesize?: () => Promise<void>;
  /** Most recent action result, shown only when it belongs to this Snapshot. */
  aiReviewRun?: ReviewAiReviewRunResult | null;
  /** Run a strictly deeper cost-aware AI pass; absent when read-only/historical. */
  onRunAiReview?: (effort: "thorough" | "deep") => Promise<ReviewAiReviewRunResult>;
  /** Reviewer/admin fresh re-run on the unchanged Snapshot (idempotent per action id); absent when read-only/historical. */
  onRerunAiReviewFresh?: (actionId: string) => Promise<ReviewAiReviewRunResult>;
  /** Stop only the current Snapshot's queued/running durable job. */
  onCancelAiReview?: (jobId: string) => Promise<void>;
  /** Fetch one behavior target's evidence (and the digest a judgment must echo). */
  onLoadBehaviorTarget?: (behaviorKey: string) => Promise<ReviewBehaviorTarget>;
  /** Judge a behavior target with the digest of the evidence rendered; absent when read-only. */
  onJudgeBehavior?: (behaviorKey: string, state: BehaviorJudgmentState, evidenceDigest: string) => Promise<void>;
  /** Run the single behavior-narrative synthesis call; absent when read-only. */
  onNarrateBehavior?: () => Promise<void>;
  /** Package-level dependency review for this Snapshot (the dependency projection). */
  dependencies?: ReviewDependencyProjection | null;
  /** Loads the deterministic architecture delta; absent when the change is too small to move it. */
  loadArchitectureDelta?: () => Promise<ReviewArchitectureDelta | null>;
  /** Opens the Map's Architecture Δ at a node, keeping the Brief on the backtrail. */
  onOpenArchitecture?: (nodeId: string) => void;
  /** PK2: reads landed project history for this Change (a plain GET); absent without Project Knowledge. */
  loadProjectContext?: () => Promise<ReviewProjectContext | null>;
  /** Interactive side-by-side preview entry, shown inside Behavior changes (renders nothing without a previewable app). */
  interactivePreview?: ReactNode;
  /** Live approval requests for risky agent actions (P12-AGENT); renders nothing until an agent asks. */
  agentApprovals?: ReactNode;
  /** P15-OBS: production problems and Revert / Fix forward links after the merge (renders nothing otherwise). */
  productionOutcomes?: ReactNode;
}

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

function targetLabel(target: ReviewTarget | undefined, fallback: string): string {
  if (!target) return fallback;
  return target.symbol ? `${target.path} · ${target.symbol}` : target.path;
}

function Section({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return (
    <section className="border-t border-border/70 py-3" aria-label={title}>
      <div className="mb-2 flex items-center gap-2">
        <h3 className="text-caption font-semibold uppercase tracking-wide text-neutral-400">{title}</h3>
        <span className="flex-1" />
        {action}
      </div>
      <div className="flex flex-col gap-2">{children}</div>
    </section>
  );
}

function Limited<T>({ rows, render }: { rows: readonly T[]; render: (row: T) => ReactNode }) {
  const [all, setAll] = useState(false);
  const shown = all ? rows : rows.slice(0, SECTION_LIMIT);
  return (
    <>
      {shown.map(render)}
      {!all && rows.length > SECTION_LIMIT && (
        <UiButton variant="ghost" size="compact" type="button" className="self-start" onClick={() => setAll(true)}>
          {`Show all ${rows.length}`}
        </UiButton>
      )}
    </>
  );
}

function TargetLinks({
  ids,
  byId,
  onOpenTarget,
}: {
  ids: readonly string[];
  byId: ReadonlyMap<string, ReviewTarget>;
  onOpenTarget: (targetId: string) => void;
}) {
  if (ids.length === 0) return null;
  return (
    <ul className="flex flex-col gap-0.5">
      {ids.map((id) => (
        <li key={id}>
          <UiButton
            unstyled
            type="button"
            className="break-all text-left font-mono text-caption text-sky-300/90 hover:text-sky-200 hover:underline"
            onClick={() => onOpenTarget(id)}
          >
            {targetLabel(byId.get(id), id)}
          </UiButton>
        </li>
      ))}
    </ul>
  );
}

function EvidenceSummary({ claim }: { claim: ReviewProjectionClaim }) {
  const tone = claim.summary === "contradictory"
    ? "text-rose-300"
    : claim.summary === "none" || claim.summary === "partial" || claim.summary === "inferred_failing"
      ? "text-amber-300/90"
      : "text-neutral-300";
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 text-caption">
      <span className={tone}>{claim.summary_text}</span>
      {claim.result_line && <span className="text-muted-foreground">{claim.result_line}</span>}
    </div>
  );
}

function QuestionRow({ question }: { question: ReviewProjectionQuestion }) {
  return (
    <div className="rounded border border-amber-900/50 bg-amber-950/15 px-2 py-1.5 text-caption">
      <div className="flex items-start gap-2">
        <span className="min-w-0 flex-1 font-medium text-amber-200">{question.title}</span>
        {question.origin === "ai" && (
          <span className="shrink-0 rounded border border-border/70 px-1 text-micro uppercase tracking-wide text-muted-foreground">AI question</span>
        )}
      </div>
      <div className="text-neutral-300">{question.detail}</div>
    </div>
  );
}

/** Spec §9: a short delta since the previous Snapshot; nothing when nothing moved. */
function deltaLine(projection: ReviewProjection): string {
  const delta = projection.delta;
  if (!delta) return "";
  const items = delta.items ?? [];
  const behavior = items.filter((item) => item.kind === "behavior").map((item) => item.title);
  const affected = items.filter((item) => item.kind !== "behavior").map((item) => item.title);
  const parts = [
    behavior.length > 0 ? `behavior changed: ${behavior.join(", ")}` : "",
    affected.length > 0 ? `also affected: ${affected.join(", ")}` : "",
    delta.changed.length > 0 ? `${delta.changed.length} changed` : "",
    delta.added.length > 0 ? `${delta.added.length} new` : "",
    delta.removed > 0 ? `${delta.removed} removed` : "",
    delta.stale_evidence.length > 0 ? `evidence now stale: ${delta.stale_evidence.join(", ")}` : "",
    delta.reopened_decisions.length > 0 ? `decisions to revisit: ${delta.reopened_decisions.join(", ")}` : "",
  ].filter(Boolean);
  return parts.length > 0 ? `Since Snapshot ${delta.since_snapshot}: ${parts.join(" · ")}` : "";
}

/** A recorded time as UTC minutes; an unparseable value is shown as recorded. */
function ranAt(value: string): string {
  const time = new Date(value);
  if (Number.isNaN(time.getTime())) return value;
  return `${time.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

/** AI explanation status: explicit/on-demand only, with degradation visible. */
function SynthesisLine({
  projection,
  busy,
  error,
  onSynthesize,
}: {
  projection: ReviewProjection;
  busy: boolean;
  error: string;
  onSynthesize?: () => Promise<void>;
}) {
  const synthesis = projection.synthesis;
  if (!synthesis) return null;
  const missing = projection.coverage.unsynthesized_target_ids?.length ?? 0;
  const excluded = synthesis.excluded_by_policy ?? [];
  const pending = busy || synthesis.status === "pending";
  const budget = synthesis.budget;
  const spent = synthesis.mode === "skipped_by_budget"
    || (budget !== undefined && budget.calls_used >= budget.max_calls);
  const settled = synthesis.status === "none" || synthesis.status === "failed";
  const spentText = spent && settled && !pending
    ? "This review's AI analysis limit has been reached."
    : "";
  const unavailable = error || spentText || ((synthesis.status === "disabled" || synthesis.status === "failed") ? synthesis.error ?? "" : "");
  const offer = Boolean(onSynthesize) && !pending && !spent && settled && (synthesis.candidates ?? 0) > 0;
  const modeLabel = synthesis.mode === "cached_synthesis" || synthesis.mode === "synthesized"
    ? "AI explanations included"
    : "";
  const hasVisibleStatus = Boolean(
    modeLabel
    || (synthesis.status === "ready" && missing > 0)
    || excluded.length > 0
    || pending
    || unavailable
    || offer,
  );
  if (!hasVisibleStatus) return null;
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="brief-synthesis">
      {modeLabel && <span>{modeLabel}</span>}
      {synthesis.status === "ready" && missing > 0 && (
        <span>{"AI explanations: " + plural(missing, "changed target") + " " + (missing === 1 ? "has" : "have") + " no explanation."}</span>
      )}
      {excluded.length > 0 && <span>{excluded.length} target{excluded.length === 1 ? "" : "s"} excluded from AI synthesis by policy.</span>}
      {pending && <span role="status">Preparing explanations…</span>}
      {unavailable && <span>{synthesis.status === "disabled" && !synthesis.model ? "AI explanations are unavailable. The Brief still uses recorded context and checks." : "Explanation unavailable: " + unavailable}</span>}
      {offer && (
        <UiButton variant="outline" size="compact" type="button" onClick={() => void onSynthesize?.()}>
          Explain unexplained changes
        </UiButton>
      )}
    </div>
  );
}

const INDEPENDENCE_TEXT: Record<ReviewIndependenceStatus, string> = {
  independent: "independent of the author's model",
  not_independent: "not independent",
  unknown_author: "not established: the author's model is unknown",
  unreported_reviewer: "not established: the reviewer ran on an unreported default model",
  unverified_reviewer: "not established: the reviewer model was not observed",
  unknown_reviewer: "not established: the reviewer model family is unknown",
  not_applicable: "not applicable (human-authored change)",
  not_run: "not established: AI review has not run",
};

const REQUIREMENT_TEXT: Record<ReviewIndependenceRequirement, string> = {
  not_required: "not required",
  not_applicable: "not applicable",
  preferred_met: "preferred · met",
  preferred_unmet: "preferred · not met",
  satisfied: "required · satisfied",
  unsatisfied: "required · not satisfied",
};

/** The headline label for an active independent-reviewer policy; empty when the policy is off or does not apply. */
function independenceLabel(independence: ReviewAiIndependence | undefined): string {
  if (!independence || independence.policy.mode === "off" || independence.requirement === "not_applicable") return "";
  if (independence.requirement === "unsatisfied") return "Independent reviewer required · not satisfied";
  return independence.independent ? "Independent reviewer" : "Not independent";
}

function meteringText(owner: string): string {
  return owner === "lemoncrow_managed"
    ? "LemonCrow managed"
    : owner === "customer_provider"
      ? "Customer subscription/API"
      : "not reported by execution authority";
}

function modelDataText(locus: string): string {
  if (locus === "customer_local") return "Code and context stay in your environment for this model route.";
  if (locus === "external") return "Code and context may be sent to the configured external model provider.";
  if (locus === "managed") return "Code and context use the managed model route.";
  return "Data handling follows the configured model route.";
}

/** Why a queued hosted AI review has not started (RC8 wait reasons; never names another tenant). */
const WAIT_REASON_TEXT: Record<string, string> = {
  organization_model_budget: "waiting for your organization's share",
  repository_model_budget: "waiting for this repository's share",
  organization_deep_budget: "waiting for your organization's Deep share",
  repository_deep_budget: "waiting for this repository's Deep share",
  organization_model_ceiling: "at your organization's limit",
  repository_model_ceiling: "at this repository's limit",
  organization_deep_ceiling: "at your organization's Deep limit",
  repository_deep_ceiling: "at this repository's Deep limit",
  retry_backoff: "retrying shortly",
  capacity: "waiting for capacity",
};

function AiReviewLine({
  projection,
  busy,
  error,
  runResult,
  onRunAiReview,
  onRerunFresh,
  onCancelAiReview,
}: {
  projection: ReviewProjection;
  busy: boolean;
  error: string;
  runResult: ReviewAiReviewRunResult | null;
  onRunAiReview?: (effort: "thorough" | "deep") => Promise<ReviewAiReviewRunResult>;
  onRerunFresh?: (actionId: string) => Promise<ReviewAiReviewRunResult>;
  onCancelAiReview?: (jobId: string) => Promise<void>;
}) {
  const review = projection.ai_review;
  const [confirmEffort, setConfirmEffort] = useState<"thorough" | "deep" | null>(null);
  // One action id per opened confirmation: a double submit replays, it never spends twice.
  const [freshActionId, setFreshActionId] = useState("");
  if (!review) return null;
  const assurance = review.assurance;
  const independence = assurance?.independence;
  const identity = assurance?.identity ?? null;
  const freshPreview = assurance?.fresh_rerun;
  const headlineIndependence = independenceLabel(independence);
  const effectiveEffort = runResult?.effort ?? review.effort;
  const nextEffort = effectiveEffort === "deep" ? null : effectiveEffort === "thorough" ? "deep" : "thorough";
  const budget = runResult?.budget ?? review.budget;
  const skippedByBudget = runResult?.skipped_by_budget ?? review.skipped_by_budget ?? 0;
  const budgetSpent = budget.calls_used >= budget.max_calls;
  const statusCoverage = review.coverage;
  const runCoverage = runResult?.coverage;
  const excluded = runCoverage?.excluded_by_policy ?? statusCoverage?.excluded_by_policy ?? [];
  const humanTargets = runCoverage?.human_deterministic_targets ?? statusCoverage?.human_deterministic_targets ?? 0;
  const checkedTargets = runCoverage?.ai_checked_targets ?? statusCoverage?.ai_context_eligible_targets ?? 0;
  const unavailable = error || review.error || runResult?.provider_errors?.[0] || "";
  const execution = review.execution;
  const trace = execution?.trace;
  const provider = trace?.provider || execution?.provider || runResult?.reviewer.driver || review.provider || "";
  const model = trace?.model || execution?.model || runResult?.reviewer.model || review.model || "";
  const executionLocus = trace?.execution_locus || execution?.execution_locus || "";
  const meteringOwner = execution?.metering_owner || "";
  const modelDataLocus = trace?.model_data_locus || "";
  // Hosted: a durable job waiting for a slot says so (one chip with why), not "Running".
  const queued = !busy && execution?.status === "queued";
  const stopping = execution?.status === "cancel_requested" || Boolean(execution?.cancel_requested);
  const cancellable = execution?.job_id && (execution.status === "queued" || execution.status === "running");
  const waitReason = queued ? WAIT_REASON_TEXT[execution?.wait_reason ?? ""] ?? "" : "";
  const statusLabel = stopping
    ? "Stopping"
    : queued
      ? "Queued"
    : busy || review.status === "pending"
      ? "Running"
      : skippedByBudget > 0 || (budgetSpent && review.status !== "ready")
        ? "Limited by budget"
        : review.status === "skipped_unnecessary"
          ? "Not needed"
          : review.status === "ready"
            ? "Complete"
            : review.status === "failed" || review.status === "cancelled" || unavailable
              ? "Unavailable"
              : "Not run";
  const sourceLocus = modelDataLocus === "customer_local"
    ? "Source/context stays customer-local for the model route."
    : modelDataLocus === "external"
      ? "Source/context may leave the customer environment for the configured model route."
      : modelDataLocus === "managed"
        ? "Source/context uses the managed model-data locus."
        : "Model-data locus not reported by execution authority.";
  const meteringLabel = meteringText(meteringOwner);
  const runDeeper = async () => {
    if (!confirmEffort || !onRunAiReview) return;
    const effort = confirmEffort;
    setConfirmEffort(null);
    await onRunAiReview(effort);
  };
  const rerunFresh = async () => {
    if (!freshActionId || !onRerunFresh) return;
    const actionId = freshActionId;
    setFreshActionId("");
    try {
      await onRerunFresh(actionId);
    } catch {
      // surfaced on the AI Review line (a refused or failed re-run)
    }
  };
  const freshExecution = assurance?.execution;
  const ranFresh = runResult?.execution?.mode === "fresh_rerun" || freshExecution?.latest_mode === "fresh_rerun";

  return (
    <div className="space-y-2" data-testid="brief-ai-review">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-neutral-300">AI Review · {statusLabel}</span>
        {waitReason && (
          <span data-testid="ai-wait-reason" className="rounded-full border border-neutral-700 px-2 text-neutral-400">
            {waitReason}
          </span>
        )}
        {review.findings > 0 && <span>{plural(review.findings, "finding")}</span>}
        {review.status === "ready" && review.findings === 0 && <span>no confirmed findings</span>}
        {humanTargets > 0 && checkedTargets < humanTargets && <span>{checkedTargets}/{humanTargets} eligible targets checked</span>}
        {excluded.length > 0 && <span>{excluded.length} excluded by policy</span>}
        {headlineIndependence && (
          <span
            data-testid="ai-independence-label"
            className={independence?.independent ? "text-emerald-300/90" : "text-amber-300/90"}
          >
            {headlineIndependence}
          </span>
        )}
        {!busy && unavailable && (
          <span className="inline-flex flex-wrap gap-x-1 text-amber-300/90">
            <span>{unavailable}</span>
            <span>· deterministic Review remains available</span>
          </span>
        )}
        {!busy && !stopping && cancellable && onCancelAiReview && (
          <UiButton variant="ghost" size="compact" type="button" onClick={() => void onCancelAiReview(execution!.job_id!)}>
            Stop AI review
          </UiButton>
        )}
        {!busy && !budgetSpent && nextEffort && onRunAiReview && !confirmEffort && (
          <UiButton variant="outline" size="compact" type="button" onClick={() => setConfirmEffort(nextEffort)}>
            Run deeper review
          </UiButton>
        )}
      </div>

      {runResult?.depth_request && runResult.depth_request.state !== "honored" && runResult.depth_request.reason && (
        <div className="text-caption text-amber-300/90" data-testid="ai-depth-request">
          Requested {runResult.depth_request.requested}: {runResult.depth_request.reason}
        </div>
      )}

      {confirmEffort && (
        <div role="group" aria-label="Confirm deeper review" className="rounded-md border border-amber-900/45 bg-amber-950/10 p-2.5 text-caption">
          <div className="font-medium text-neutral-200">Confirm deeper review</div>
          <div className="mt-1 grid gap-0.5 text-muted-foreground">
            <div>Depth: <span className="capitalize text-neutral-300">{confirmEffort}</span></div>
            <div>AI usage: <span className="text-neutral-300">{budget.calls_used}/{budget.max_calls} calls used</span></div>
            <div>{modelDataText(modelDataLocus)}</div>
          </div>
          <div className="mt-2 flex gap-1.5">
            <UiButton variant="default" size="compact" type="button" onClick={() => void runDeeper()}>
              Run {confirmEffort} review
            </UiButton>
            <UiButton variant="ghost" size="compact" type="button" onClick={() => setConfirmEffort(null)}>Cancel</UiButton>
          </div>
        </div>
      )}

      <details className="text-caption text-muted-foreground" data-testid="ai-run-details">
        <summary className="cursor-pointer">Run details</summary>
        <div className="mt-1 grid gap-0.5 pl-2">
          <div>Effective depth: <span className="capitalize text-neutral-300">{effectiveEffort}</span></div>
          <div>Budget: <span className="text-neutral-300">{budget.calls_used}/{budget.max_calls} model calls</span></div>
          <div>Execution locus: <span className="text-neutral-300">{executionLocus || "not reported"}</span></div>
          <div>Model data locus: <span className="text-neutral-300">{modelDataLocus || "not reported"}</span></div>
          <div>Metering owner: <span className="text-neutral-300">{meteringLabel}</span></div>
          {provider && <div>Provider: <span className="text-neutral-300">{provider}</span></div>}
          {identity ? (
            <>
              <div data-testid="ai-requested-model">
                Requested model:{" "}
                <span className="text-neutral-300">{identity.requested_model || "provider default (unreported)"}</span>
              </div>
              <div data-testid="ai-observed-model">
                Observed model:{" "}
                <span className="text-neutral-300">
                  {identity.source === "observed" || identity.source === "unverified"
                    ? identity.label
                    : "not reported by the provider"}
                </span>
              </div>
            </>
          ) : (
            model && <div>Model: <span className="text-neutral-300">{model}</span></div>
          )}
          {independence && (
            <div data-testid="ai-independence">
              Independence:{" "}
              <span className="text-neutral-300">{INDEPENDENCE_TEXT[independence.status] ?? independence.status}</span>
              {independence.author && <span> · author {independence.author.label}</span>}
              {independence.reason && <div className="pl-2">{independence.reason}</div>}
            </div>
          )}
          {independence && independence.policy.mode !== "off" && (
            <div data-testid="ai-independence-policy">
              Independent reviewer policy:{" "}
              <span className="text-neutral-300">
                {REQUIREMENT_TEXT[independence.requirement] ?? independence.requirement}
              </span>
              <span> · from {independence.policy.origin.source || independence.policy.origin.layer}</span>
            </div>
          )}
          {(ranFresh || (freshExecution?.fresh_reruns ?? 0) > 0) && (
            <div data-testid="ai-execution-mode">
              Execution:{" "}
              <span className="text-neutral-300">
                {ranFresh ? "fresh re-run" : "executed"}
                {(freshExecution?.fresh_reruns ?? 0) > 0 && ` · ${plural(freshExecution!.fresh_reruns, "explicit fresh re-run")} on this Snapshot`}
              </span>
            </div>
          )}
          {runResult && runResult.cache_reuse > 0 && <div>Cache reuse: <span className="text-neutral-300">{runResult.cache_reuse}</span></div>}
          {(runResult?.rule_checks?.length ?? 0) > 0 && (
            <div data-testid="ai-rule-checks">
              Rule checks:{" "}
              <span className="text-neutral-300">
                {Object.entries(
                  runResult!.rule_checks!.reduce<Record<string, number>>((counts, row) => {
                    counts[row.state] = (counts[row.state] ?? 0) + 1;
                    return counts;
                  }, {}),
                )
                  .map(([state, count]) => `${count} ${state.replaceAll("_", " ")}`)
                  .join(" · ")}
              </span>
            </div>
          )}
          {execution?.cache_status && <div>Cache: <span className="text-neutral-300">{execution.cache_status}</span></div>}
          {trace?.provider_calls != null && <div>Provider calls: <span className="text-neutral-300">{trace.provider_calls}</span></div>}
          {trace?.provider_cost != null && <div>Provider cost: <span className="text-neutral-300">{trace.provider_cost} {trace.currency || ""}</span></div>}
          {excluded.length > 0 && <div>Content exclusions: <span className="text-neutral-300">{excluded.length}</span></div>}
          {(trace?.tools?.length ?? 0) > 0 && <div>Tools: <span className="text-neutral-300">{trace!.tools!.join(", ")}</span></div>}
          {(trace?.external_domains?.length ?? 0) > 0 && <div>External domains: <span className="text-neutral-300">{trace!.external_domains!.join(", ")}</span></div>}
          {(trace?.evidence_refs?.length ?? 0) > 0 && <div>Evidence consulted: <span className="text-neutral-300">{trace!.evidence_refs!.length}</span></div>}
          <div>{sourceLocus}</div>
          {onRerunFresh && freshPreview && !busy && !freshActionId && (
            freshPreview.available ? (
              <UiButton
                variant="outline"
                size="compact"
                type="button"
                className="mt-1 self-start"
                onClick={() => setFreshActionId(globalThis.crypto.randomUUID())}
              >
                Re-run fresh
              </UiButton>
            ) : (
              freshPreview.reason && <div data-testid="ai-fresh-unavailable">Fresh re-run: {freshPreview.reason}</div>
            )
          )}
          {freshActionId && freshPreview && (
            <div
              role="group"
              aria-label="Confirm fresh re-run"
              className="mt-1 rounded-md border border-amber-900/45 bg-amber-950/10 p-2.5"
            >
              <div className="font-medium text-neutral-200">Confirm fresh re-run</div>
              <div className="mt-1 grid gap-0.5">
                <div>Runs AI review again on this unchanged Snapshot; cached results are not reused.</div>
                <div>Depth: <span className="capitalize text-neutral-300">{freshPreview.effort || "not planned"}</span></div>
                <div>Estimated AI calls: <span className="text-neutral-300">{freshPreview.estimated_model_calls}</span></div>
                <div>
                  AI usage:{" "}
                  <span className="text-neutral-300">
                    {freshPreview.budget.calls_used}/{freshPreview.budget.max_calls} calls used · {freshPreview.budget.remaining} remaining
                  </span>
                </div>
                {!freshPreview.within_budget && (
                  <div className="text-amber-300/90" data-testid="ai-fresh-over-budget">
                    The remaining Review budget may not cover it: calls past the budget are not made.
                  </div>
                )}
                <div>{modelDataText(freshPreview.model_data_locus)}</div>
              </div>
              <div className="mt-2 flex gap-1.5">
                <UiButton variant="default" size="compact" type="button" onClick={() => void rerunFresh()}>
                  Re-run fresh now
                </UiButton>
                <UiButton variant="ghost" size="compact" type="button" onClick={() => setFreshActionId("")}>
                  Cancel
                </UiButton>
              </div>
            </div>
          )}
        </div>
      </details>

      {excluded.length > 0 && (
        <details className="text-caption text-muted-foreground">
          <summary className="cursor-pointer">What AI could not inspect</summary>
          <div className="mt-1 space-y-0.5 pl-2">
            {excluded.slice(0, 12).map((row) => (
              <div key={`${row.path}:${row.mode}`}><span className="font-mono">{row.path}</span> · {row.mode.replaceAll("_", " ")}</div>
            ))}
            {excluded.length > 12 && <div>+{excluded.length - 12} more excluded targets</div>}
          </div>
        </details>
      )}
    </div>
  );
}

function WhatWasChecked({
  projection,
  targets,
}: {
  projection: ReviewProjection;
  targets: readonly ReviewTarget[];
}) {
  const ai = projection.ai_review;
  const execution = projection.behavior?.coverage ?? null;
  const aiShown = Boolean(ai && ai.findings === 0);
  if (!aiShown && !execution) return null;
  const evidence = projection.claims.flatMap((claim) => claim.evidence);
  const byStatus = evidence.reduce<Record<string, number>>((counts, row) => {
    const key = row.status || "UNKNOWN";
    counts[key] = (counts[key] ?? 0) + 1;
    return counts;
  }, {});
  const exclusions = (aiShown && ai?.coverage?.excluded_by_policy) || [];
  const reviewed = targets.filter((target) => target.state === "reviewed").length;
  const aiStatus = ai?.status === "ready"
    ? "Complete"
    : ai?.status === "skipped_unnecessary"
      ? "Not needed"
      : ai?.status === "pending"
        ? "Running"
        : ai?.status === "failed" || ai?.status === "cancelled"
          ? "Unavailable"
          : "Not run";
  const eligible = ai?.coverage?.ai_context_eligible_targets ?? 0;
  const human = ai?.coverage?.human_deterministic_targets ?? targets.length;
  const targetLabel = targets.length === 1 ? "target" : "targets";
  const exclusionLabel = exclusions.length === 1 ? "target" : "targets";
  const checkSummary = Object.entries(byStatus)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([status, count]) => status + " " + count)
    .join(" · ");

  return (
    <details data-testid="what-was-checked" className="rounded border border-border/70 px-2.5 py-2">
      <summary className="cursor-pointer select-none font-medium text-neutral-300">What was checked</summary>
      <div className="mt-2 space-y-1 text-caption text-muted-foreground">
        <div>Deterministic Review: <span className="text-neutral-300">{targets.length + " exact Snapshot " + targetLabel}</span></div>
        {aiShown && <div>AI Review: <span className="text-neutral-300">{aiStatus}</span>{human > 0 ? " · " + eligible + "/" + human + " eligible targets" : ""}</div>}
        <div>Reviewer inspection: <span className="text-neutral-300">{reviewed + "/" + targets.length + " targets reviewed"}</span></div>
        <div>Recorded checks: {checkSummary || "none attached to the current claims."}</div>
        <ExecutionCoverageLine coverage={execution} />
        {exclusions.length > 0 && (
          <div className="text-amber-300/90">
            {"AI exclusions: " + exclusions.length + " " + exclusionLabel + " " + (exclusions.length === 1 ? "was" : "were") + " not sent to the model by policy."}
          </div>
        )}
        {byStatus.NOT_RUN && <div>NOT_RUN means the verification did not run; it is not a pass.</div>}
        {byStatus.UNKNOWN && <div>UNKNOWN means the verification result is unavailable; it is not a pass.</div>}
      </div>
    </details>
  );
}

function PolicyLine({ projection }: { projection: ReviewProjection }) {
  const policy = projection.policy;
  if (!policy) return null;
  const suggestions = policy.suggestions.filter((item) => item.state === "suggested");
  if (
    policy.future_changes.length === 0
    && suggestions.length === 0
    && policy.authority.rules.length === 0
    && policy.authority.ai_content_access.length === 0
    && policy.authority.errors.length === 0
  ) return null;
  return (
    <details data-testid="brief-review-policy">
      <summary className="cursor-pointer">
        Review policy · {policy.authority.rules.length} active rule{policy.authority.rules.length === 1 ? "" : "s"}
        {policy.future_changes.length > 0 ? ` · ${policy.future_changes.length} future change${policy.future_changes.length === 1 ? "" : "s"}` : ""}
        {suggestions.length > 0 ? ` · ${suggestions.length} suggestion${suggestions.length === 1 ? "" : "s"}` : ""}
      </summary>
      <div className="mt-1 space-y-1 pl-2">
        {policy.future_changes.map((change) => (
          <div key={`${change.category}:${change.path}`}>
            <span className="font-mono">{change.path}</span> · future policy only; does not govern this Review
          </div>
        ))}
        {suggestions.map((suggestion) => (
          <div key={suggestion.id}>
            Suggested rule · {suggestion.candidate_statement} · {suggestion.supporting_count} human comments · not active
          </div>
        ))}
        {policy.authority.errors.map((error) => <div key={error} className="text-amber-300/90">Policy authority degraded: {error}</div>)}
      </div>
    </details>
  );
}

function ItemCard({
  item,
  claims,
  questions,
  byId,
  expanded,
  detailLevel,
  onToggle,
  onOpenTarget,
  onInspectEvidence,
  onAsk,
}: {
  item: ReviewProjectionItem;
  claims: readonly ReviewProjectionClaim[];
  questions: readonly ReviewProjectionQuestion[];
  byId: ReadonlyMap<string, ReviewTarget>;
  expanded: boolean;
  detailLevel: ReviewDetail;
  onToggle: () => void;
  onOpenTarget: (targetId: string) => void;
  onInspectEvidence: (targetId: string | null) => void;
  onAsk?: (targetId: string | null) => void;
}) {
  const first = item.primary_target_ids[0] ?? item.related_target_ids[0] ?? null;
  const reviewFirst = item.attention === "review_first";
  return (
    <article
      data-testid={`brief-item-${item.id}`}
      data-cohort-id={item.key || item.id}
      className={`rounded-md border px-2.5 py-2 ${reviewFirst ? "border-amber-900/60" : "border-border/80"}`}
    >
      <div className="flex items-start gap-2">
        <UiButton
          unstyled
          type="button"
          aria-expanded={expanded}
          onClick={onToggle}
          className="min-w-0 flex-1 text-left text-label font-medium text-neutral-100 hover:text-neutral-50"
        >
          {item.title}
        </UiButton>
        <span className="shrink-0 rounded border border-border/70 px-1 text-micro uppercase tracking-wide text-muted-foreground">
          {item.rationale_origin === "inferred" ? `${item.source_label} · intent inferred` : item.source_label}
        </span>
      </div>
      {item.stale && <div className="text-caption text-amber-300/90">Written for earlier content</div>}
      {item.internal_only && <div className="mt-1 text-caption text-neutral-300">{INTERNAL_ONLY_TEXT}</div>}
      {detailLevel !== "compact" && (item.before || item.after) && (
        <div className="mt-1 text-caption text-neutral-300">
          {item.before && <span className="text-muted-foreground">Before: {item.before} </span>}
          {item.after && <span>After: {item.after}</span>}
        </div>
      )}
      {detailLevel !== "compact" && (item.kind !== "behavior" || !first) && item.rationale && (
        <div className={`mt-1 text-caption ${item.rationale_origin === "inferred" ? "text-neutral-300" : "text-muted-foreground"}`}>{item.rationale}</div>
      )}
      {detailLevel !== "compact" && (item.limitations ?? []).map((text) => <div key={text} className="text-micro text-muted-foreground">{text}</div>)}
      {item.kind === "area" || item.kind === "other" ? (
        <div className="mt-1 text-caption text-muted-foreground">
          {plural(item.primary_target_ids.length, "target")} · no author explanation
        </div>
      ) : null}
      {claims.map((claim) => <EvidenceSummary key={claim.id} claim={claim} />)}
      {questions.length > 0 && <div className="mt-1.5 flex flex-col gap-1">{questions.map((question) => <QuestionRow key={question.id} question={question} />)}</div>}
      {(expanded || detailLevel === "detailed") && (
        <div className="mt-2 flex flex-col gap-2 border-t border-border/60 pt-2 text-caption">
          {item.rationale && item.kind === "behavior" && first && (
            <div>
              <span className="text-muted-foreground">Why · {item.source_label}: </span>
              <span className="text-neutral-300">{item.rationale}</span>
            </div>
          )}
          {claims.map((claim) => (
            <div key={claim.id} className="flex flex-col gap-1">
              {claim.evidence.length > 0 && (
                <ul className="flex flex-col gap-0.5">
                  {claim.evidence.map((link) => (
                    <li key={`${link.key}:${link.state}:${link.snapshot_number ?? ""}`} className="flex flex-wrap gap-x-2">
                      <span className="font-mono text-neutral-300">{link.title}</span>
                      <span className={link.status === "FAIL" ? "text-rose-300" : "text-neutral-400"}>{link.status}</span>
                      {link.snapshot_number !== null && <span className="text-muted-foreground">Snapshot {link.snapshot_number}{link.state === "stale" ? " (earlier)" : ""}</span>}
                      {link.observed_at && <span className="text-muted-foreground">{`Ran ${ranAt(link.observed_at)}`}</span>}
                      {link.source && <span className="text-muted-foreground">{`Recorded by ${link.source}`}</span>}
                      {link.environment && Object.keys(link.environment).length > 0 && (
                        <span className="text-muted-foreground">
                          {Object.entries(link.environment).map(([name, value]) => `${name}: ${value}`).join(" · ")}
                        </span>
                      )}
                      <span className="text-muted-foreground">{link.relevance}</span>
                    </li>
                  ))}
                </ul>
              )}
              {claim.limitations.map((text) => <div key={text} className="text-amber-300/80">{text}</div>)}
            </div>
          ))}
          {item.unresolved_refs.length > 0 && (
            <div className="text-muted-foreground">
              {item.unresolved_refs.map((ref) => (
                <div key={`${ref.ref.path}:${ref.ref.symbol}:${ref.outcome}`}>
                  <span className="font-mono">{ref.ref.path}{ref.ref.symbol ? ` · ${ref.ref.symbol}` : ""}</span>: {ref.outcome.replaceAll("_", " ")}
                </div>
              ))}
            </div>
          )}
          <TargetLinks ids={item.primary_target_ids} byId={byId} onOpenTarget={onOpenTarget} />
          {item.related_target_ids.length > 0 && (
            <div>
              <div className="text-muted-foreground">Also touches</div>
              <TargetLinks ids={item.related_target_ids} byId={byId} onOpenTarget={onOpenTarget} />
            </div>
          )}
        </div>
      )}
      {first && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {reviewFirst && (
            <>
              <UiButton variant="outline" size="compact" type="button" onClick={() => onInspectEvidence(first)}>Inspect evidence</UiButton>
              {onAsk && <UiButton variant="outline" size="compact" type="button" onClick={() => onAsk(first)}>Ask</UiButton>}
            </>
          )}
          <UiButton variant={reviewFirst ? "default" : "ghost"} size="compact" type="button" onClick={() => onOpenTarget(first)}>
            Open code
          </UiButton>
        </div>
      )}
    </article>
  );
}

function optionLabel(decision: ReviewProjectionDecision, key: string): string {
  return decision.options.find((option) => option.key === key)?.label ?? key;
}

function triggerText(trigger: { kind: string; check_key: string; note: string } | null): string {
  if (!trigger) return "Deferred";
  if (trigger.kind === "new_snapshot") return "Deferred until the next Snapshot";
  if (trigger.kind === "check_available") return `Deferred until ${trigger.check_key} has a result`;
  return trigger.note ? `Deferred until: ${trigger.note}` : "Deferred until you reopen it";
}

function statusLine(decision: ReviewProjectionDecision): { text: string; tone: string } | null {
  const state = decision.state;
  if (!state) return null;
  if (state.withdrawn) return { text: "Withdrawn by the author", tone: "text-muted-foreground" };
  if (state.disposition === "open") {
    if (decision.conflict) return { text: "Authors disagree", tone: "text-amber-300" };
    return { text: "Open", tone: "text-amber-300" };
  }
  if (state.disposition === "change_requested") {
    return state.navigable && state.reason
      ? { text: "Requested change resolved; decide again", tone: "text-amber-300" }
      : { text: "Change requested", tone: "text-rose-300" };
  }
  if (state.disposition === "deferred") {
    const base = triggerText(state.trigger);
    const text = state.latest_reason ? `${base} · ${state.latest_reason}` : base;
    return { text: state.trigger_fired ? `${text} · due now` : text, tone: "text-neutral-400" };
  }
  const accepted = `Accepted · ${optionLabel(decision, state.accepted_option)}`;
  if (state.validity === "implementation_changed_since_acceptance") {
    return { text: "Code changed since acceptance", tone: "text-amber-300" };
  }
  if (state.validity === "needs_reconsideration") return { text: "Needs reconsideration", tone: "text-rose-300" };
  if (state.validity === "unverified") return { text: `${accepted} · unverified`, tone: "text-amber-300/90" };
  return { text: accepted, tone: "text-emerald-300/90" };
}

type DecisionForm = "" | "request_change" | "defer";

function DecisionCard({
  decision,
  byId,
  onOpenTarget,
  onAsk,
  onDecision,
}: {
  decision: ReviewProjectionDecision;
  byId: ReadonlyMap<string, ReviewTarget>;
  onOpenTarget: (targetId: string) => void;
  onAsk?: (targetId: string | null) => void;
  onDecision?: (key: string, input: ReviewDecisionEventInput) => Promise<void>;
}) {
  const actions = new Set(decision.state && onDecision ? decision.actions ?? [] : []);
  const actionable = [...actions].some((action) => action !== "ask");
  const [chosen, setChosen] = useState(decision.state?.accepted_option || decision.selected_option || "");
  const [form, setForm] = useState<DecisionForm>("");
  const [text, setText] = useState("");
  const [trigger, setTrigger] = useState("manual");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const status = statusLine(decision);

  const run = async (input: ReviewDecisionEventInput) => {
    if (!onDecision) return;
    setBusy(true);
    setFailure("");
    try {
      await onDecision(decision.key, input);
      setForm("");
      setText("");
    } catch (reason) {
      setFailure(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };
  const toggle = (next: DecisionForm) => {
    setText("");
    setForm((current) => (current === next ? "" : next));
  };

  return (
    <article data-testid={`brief-decision-${decision.key}`} className="rounded-md border border-border/80 px-2.5 py-2">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1 text-label font-medium text-neutral-100">{decision.question || decision.key}</div>
        {!decision.state && (
          <span className="shrink-0 rounded border border-border/70 px-1 text-micro uppercase tracking-wide text-muted-foreground">
            Proposed by the author · read-only
          </span>
        )}
      </div>
      {status && <div className={`text-caption ${status.tone}`}>{status.text}</div>}
      {decision.state && decision.state.changed_inputs.length > 0 && (
        <ul className="mt-0.5 list-disc pl-4 text-caption text-neutral-400">
          {decision.state.changed_inputs.map((line) => <li key={line}>{line}</li>)}
        </ul>
      )}
      {decision.state?.limitations.map((line) => (
        <div key={line} className="text-caption text-muted-foreground">{line}</div>
      ))}
      {decision.stale && <div className="text-caption text-amber-300/90">Written for earlier content</div>}
      {decision.conflict && decision.positions && decision.positions.length > 0 && (
        <ul className="mt-1 flex flex-col gap-0.5 text-caption text-neutral-300">
          {decision.positions.map((position) => (
            <li key={position.producer}>{`${position.producer} chose ${optionLabel(decision, position.selected_option)}`}</li>
          ))}
        </ul>
      )}
      {actions.has("accept") ? (
        <ToggleGroup
          aria-label="Options"
          className="mt-1 flex-col items-start gap-1"
          itemClassName="h-auto min-h-7 py-0.5 text-left"
          disabled={busy}
          value={chosen}
          onChange={setChosen}
          options={decision.options.map((option) => ({
            value: option.key,
            label: (
              <span className="flex items-center gap-1.5">
                <span>{option.label}</span>
                {option.key === decision.selected_option && <span className="text-sky-300/90">author's choice</span>}
              </span>
            ),
          }))}
        />
      ) : (
        <ul className="mt-1 flex flex-col gap-0.5 text-caption">
          {decision.options.map((option) => (
            <li key={option.key} className={option.key === decision.selected_option ? "text-neutral-100" : "text-muted-foreground"}>
              <span>{option.label}</span>
              {option.key === decision.selected_option && <span className="ml-2 text-sky-300/90">author's choice</span>}
            </li>
          ))}
        </ul>
      )}
      {decision.reason_human_required && (
        <div className="mt-1 text-caption">
          <span className="text-muted-foreground">Why a human decides: </span>
          <span className="text-neutral-300">{decision.reason_human_required}</span>
        </div>
      )}
      <div className="mt-1.5">
        <TargetLinks ids={decision.target_ids} byId={byId} onOpenTarget={onOpenTarget} />
      </div>
      {actionable && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {actions.has("reconfirm") && (
            <UiButton variant="default" size="compact" type="button" disabled={busy} onClick={() => void run({ action: "reconfirm" })}>
              Reconfirm
            </UiButton>
          )}
          {actions.has("accept") && (
            <UiButton
              variant={actions.has("reconfirm") ? "outline" : "default"}
              size="compact"
              type="button"
              disabled={busy || !chosen}
              onClick={() => void run({ action: "accept", option_key: chosen })}
            >
              Accept decision
            </UiButton>
          )}
          {actions.has("request_change") && (
            <UiButton variant="outline" size="compact" type="button" disabled={busy} aria-expanded={form === "request_change"} onClick={() => toggle("request_change")}>
              Request change
            </UiButton>
          )}
          {actions.has("ask") && onAsk && (
            <UiButton variant="ghost" size="compact" type="button" onClick={() => onAsk(decision.target_ids[0] ?? null)}>Ask</UiButton>
          )}
          {actions.has("defer") && (
            <UiButton variant="ghost" size="compact" type="button" disabled={busy} aria-expanded={form === "defer"} onClick={() => toggle("defer")}>
              Defer
            </UiButton>
          )}
          {actions.has("reopen") && (
            <UiButton variant="ghost" size="compact" type="button" disabled={busy} onClick={() => void run({ action: "reopen" })}>
              Reopen
            </UiButton>
          )}
        </div>
      )}
      {form === "request_change" && (
        <div className="mt-1.5 flex flex-col gap-1">
          <UiTextarea uiSize="xs" aria-label="Requested change" rows={3} value={text} onChange={(event) => setText(event.target.value)} />
          <UiButton variant="default" size="compact" type="button" className="self-start" disabled={busy || !text.trim()} onClick={() => void run({ action: "request_change", feedback_body: text.trim() })}>
            Send request
          </UiButton>
        </div>
      )}
      {form === "defer" && (
        <div className="mt-1.5 flex flex-col gap-1">
          <UiInput uiSize="xs" aria-label="Reason for deferring" value={text} onChange={(event) => setText(event.target.value)} />
          <Select uiSize="xs" aria-label="Revisit when" value={trigger} onValueChange={setTrigger}>
            <SelectItem value="manual">When I reopen it</SelectItem>
            <SelectItem value="new_snapshot">At the next Snapshot</SelectItem>
            {decision.relied_checks.map((key) => (
              <SelectItem key={key} value={`check_available:${key}`}>{`When ${key} has a result`}</SelectItem>
            ))}
          </Select>
          <UiButton variant="default" size="compact" type="button" className="self-start" disabled={busy || !text.trim()} onClick={() => void run({ action: "defer", reason: text.trim(), trigger })}>
            Defer decision
          </UiButton>
        </div>
      )}
      {failure && <div role="alert" className="mt-1 text-caption text-rose-300">{failure}</div>}
    </article>
  );
}

export default function ReviewBrief({
  projection,
  loading,
  error,
  targets,
  requestScope,
  onOpenTarget,
  onOpenFiles,
  onOpenTour,
  onInspectEvidence,
  onAsk,
  onDecision,
  onSynthesize,
  aiReviewRun,
  onRunAiReview,
  onRerunAiReviewFresh,
  onCancelAiReview,
  onLoadBehaviorTarget,
  onJudgeBehavior,
  onNarrateBehavior,
  dependencies,
  loadArchitectureDelta,
  onOpenArchitecture,
  loadProjectContext,
  interactivePreview,
  agentApprovals,
  productionOutcomes,
}: ReviewBriefProps) {
  const [expandedId, setExpandedId] = useState("");
  const [detailLevel, setDetailLevelState] = useState<ReviewDetail>(() => {
    if (typeof window === "undefined") return "standard";
    const saved = window.localStorage.getItem(REVIEW_DETAIL_KEY);
    return saved === "compact" || saved === "detailed" ? saved : "standard";
  });
  const setDetailLevel = (next: ReviewDetail) => {
    setDetailLevelState(next);
    if (typeof window !== "undefined") window.localStorage.setItem(REVIEW_DETAIL_KEY, next);
  };
  const [synthesizing, setSynthesizing] = useState(false);
  const [synthesisError, setSynthesisError] = useState("");
  const [aiReviewBusy, setAiReviewBusy] = useState(false);
  const [aiReviewError, setAiReviewError] = useState("");
  const [routineOpen, setRoutineOpen] = useState(false);
  const [doneOpen, setDoneOpen] = useState(false);

  const byId = useMemo(() => new Map(targets.map((target) => [target.target_id, target])), [targets]);

  if (!projection) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 p-6 text-label text-muted-foreground" role="status">
        <span>{error ? `Brief unavailable: ${error}` : loading ? "Preparing brief…" : "Brief unavailable."}</span>
        <UiButton variant="ghost" size="compact" type="button" onClick={onOpenFiles}>Open code</UiButton>
      </div>
    );
  }

  const claimsById = new Map(projection.claims.map((claim) => [claim.id, claim]));
  const questionsByItem = new Map<string, ReviewProjectionQuestion[]>();
  const orphanQuestions: ReviewProjectionQuestion[] = [];
  const itemIds = new Set(projection.items.map((item) => item.id));
  for (const question of projection.questions) {
    const owners = question.item_ids.filter((id) => itemIds.has(id));
    if (owners.length === 0) orphanQuestions.push(question);
    for (const id of owners) questionsByItem.set(id, [...(questionsByItem.get(id) ?? []), question]);
  }
  // A scoped request leads with its own items; an item whose every primary
  // target is outside the request moves to one visible group, never away.
  const scopeFocused = Boolean(requestScope && !requestScope.showOutside);
  const outsideRequest = (item: ReviewProjectionItem) => Boolean(
    requestScope
      && item.primary_target_ids.length > 0
      && item.primary_target_ids.every((id) => !requestScope.inScopeTargetIds.has(id)),
  );
  const outsideItems = requestScope ? projection.items.filter(outsideRequest) : [];
  const queueItems = scopeFocused ? projection.items.filter((item) => !outsideRequest(item)) : projection.items;
  const byAttention = (attention: ReviewProjectionItem["attention"]) => queueItems.filter((item) => item.attention === attention);
  const reviewFirst = byAttention("review_first");
  const understand = byAttention("understand");
  const routine = byAttention("routine");
  const done = byAttention("done");
  const routineTargets = routine.flatMap((item) => item.primary_target_ids);
  const coverage = projection.coverage;
  // Decisions and behavior targets have their own sections; the rest are checks.
  const requiredChecks = projection.actions.filter(
    (action) => !action.id.startsWith("decision:") && !action.id.startsWith("behavior:"),
  );
  const behavior =
    projection.behavior && (projection.behavior.total > 0 || (projection.behavior.agent_actions?.length ?? 0) > 0)
      ? projection.behavior
      : null;
  const activeDecisions = projection.decisions.filter(
    (decision) => !decision.state || (!decision.state.withdrawn && (decision.state.disposition !== "deferred" || decision.state.navigable)),
  );
  const deferredDecisions = projection.decisions.filter(
    (decision) => decision.state && !decision.state.withdrawn && decision.state.disposition === "deferred" && !decision.state.navigable,
  );
  const withdrawnDecisions = projection.decisions.filter((decision) => decision.state?.withdrawn);
  const openDecisions = activeDecisions.filter((decision) => !decision.state || decision.state.outstanding);
  const progress = projection.decision_progress;
  const codeTargets = scopeFocused && requestScope
    ? targets.filter((target) => requestScope.inScopeTargetIds.has(target.target_id))
    : targets;
  const reviewedCode = codeTargets.filter((target) => target.state === "reviewed").length;
  const collapsedSupport = projection.material?.files.filter((file) => file.collapsed_default) ?? [];
  const decisionCard = (decision: ReviewProjectionDecision) => (
    <DecisionCard key={decision.key} decision={decision} byId={byId} onOpenTarget={onOpenTarget} onAsk={onAsk} onDecision={onDecision} />
  );

  const card = (item: ReviewProjectionItem) => (
    <ItemCard
      key={item.id}
      item={item}
      claims={item.claim_ids.map((id) => claimsById.get(id)).filter((claim): claim is ReviewProjectionClaim => Boolean(claim))}
      questions={questionsByItem.get(item.id) ?? []}
      byId={byId}
      expanded={expandedId === item.id}
      detailLevel={detailLevel}
      onToggle={() => setExpandedId((current) => current === item.id ? "" : item.id)}
      onOpenTarget={onOpenTarget}
      onInspectEvidence={onInspectEvidence}
      onAsk={onAsk}
    />
  );

  return (
    <div
      className="mx-auto flex w-full max-w-3xl flex-col px-4 py-4 outline-none"
      data-testid="review-brief"
      data-review-view-root="brief"
      tabIndex={-1}
    >
      <header className="flex flex-col gap-1 pb-3">
        {!projection.summary && projection.plan.intents.map((intent) => (
          <div key={intent.producer}>
            <p className="text-body text-neutral-100">{intent.intent}</p>
            {intent.origin === "captured" && (
              <p className="text-micro text-muted-foreground">{`Captured plan · ${intent.producer}`}</p>
            )}
          </div>
        ))}
        {!projection.summary && openDecisions.length === 0 && <p className="text-caption text-muted-foreground">No open decisions</p>}
        {progress && progress.total > 0 && (
          <p className="flex gap-3 text-caption text-muted-foreground">
            <span>{`Decisions: ${progress.settled} of ${progress.total} settled`}</span>
            <span>{`Code: ${reviewedCode} of ${codeTargets.length} reviewed${scopeFocused ? " in your request" : ""}`}</span>
          </p>
        )}
        {projection.snapshot_number !== null && (
          <p className="text-caption text-muted-foreground">Snapshot {projection.snapshot_number}{projection.historical ? " · historical" : ""}</p>
        )}
        {deltaLine(projection) && <p className="text-caption text-amber-200/90">{deltaLine(projection)}</p>}
      </header>

      {projection.summary && (
        <section aria-label="Change summary" className="flex flex-col gap-3 pb-4">
          {projection.summary.statements?.length
            ? projection.summary.statements.map((statement) => (
                <div key={statement.text}>
                  <p className="max-w-prose text-body leading-relaxed text-foreground">{statement.text}</p>
                  {statement.origin === "inferred" && statement.target_ids.length > 0 && (
                    <div className="mt-0.5 text-micro text-muted-foreground">
                      {"AI statement · " + statement.target_ids.length + " exact target" + (statement.target_ids.length === 1 ? "" : "s")}
                    </div>
                  )}
                </div>
              ))
            : projection.summary.paragraphs.map((paragraph) => (
                <p key={paragraph} className="max-w-prose text-body leading-relaxed text-foreground">{paragraph}</p>
              ))}
          <p className="text-caption text-muted-foreground">
            {projection.summary.origin === "inferred" ? "AI summary · intent inferred" : projection.summary.origin === "mixed" ? "Author context and AI explanations" : projection.summary.origin === "author" ? "From author context" : "From the changed files"}
          </p>
          <p className="text-caption text-neutral-300">{projection.summary.verification}</p>
          {projection.summary.attention && <p className="text-caption text-amber-200">{projection.summary.attention}</p>}
          <p className="text-caption text-muted-foreground">{projection.summary.scope}</p>
          {projection.material && projection.material.total_changed_lines > 0 && (
            <p className="text-caption text-muted-foreground" data-testid="review-change-size">
              {projection.material.implementation_changed_lines === projection.material.total_changed_lines
                ? `${projection.material.total_changed_lines} changed implementation lines`
                : `${projection.material.implementation_changed_lines} implementation lines · ${projection.material.total_changed_lines} total changed lines`}
            </p>
          )}
          <div className="flex flex-wrap gap-1.5">
            {onOpenTour && (projection.tour?.step_count ?? 0) > 0 && <UiButton variant="default" size="compact" onClick={onOpenTour}>Start guided review</UiButton>}
            <UiButton variant={onOpenTour && (projection.tour?.step_count ?? 0) > 0 ? "outline" : "default"} size="compact" onClick={onOpenFiles}>Open code</UiButton>
          </div>
        </section>
      )}

      {/* §5.1 item 2: large changes defer Map projection until explicit interest so it cannot compete with first-useful paint. */}
      {loadArchitectureDelta && onOpenArchitecture && (
        <BriefArchitectureDelta
          load={loadArchitectureDelta}
          onOpen={onOpenArchitecture}
          eager={targets.length <= LARGE_REVIEW_TARGET_THRESHOLD}
        />
      )}

      {/* PK2: landed history, shown only when Project Knowledge has something relevant. */}
      {loadProjectContext && <BriefProjectContext load={loadProjectContext} />}

      {!projection.historical && productionOutcomes && <div className="border-t border-border/70 py-3 empty:hidden">{productionOutcomes}</div>}

      {requiredChecks.length > 0 && (
        <Section title={`Required checks · ${requiredChecks.length}`}>
          {requiredChecks.map((action) => (
            <div key={action.id} className="rounded-md border border-rose-900/60 bg-rose-950/20 px-2.5 py-2 text-caption">
              <div className="font-medium text-rose-200">{action.title}</div>
              <div className="text-neutral-300">{action.detail}</div>
              <UiButton variant="outline" size="compact" type="button" className="mt-1.5" onClick={() => onInspectEvidence(null)}>
                Inspect evidence
              </UiButton>
            </div>
          ))}
        </Section>
      )}

      {!projection.historical && agentApprovals && <div className="border-t border-border/70 py-3 empty:hidden">{agentApprovals}</div>}

      {behavior && (
        <Section title={behavior.total > 0 ? `Behavior changes · ${behavior.total}` : "Behavior changes"}>
          <BehaviorChanges
            behavior={behavior}
            revisionId={projection.revision_id}
            onLoadTarget={projection.historical ? undefined : onLoadBehaviorTarget}
            onJudge={projection.historical ? undefined : onJudgeBehavior}
            onNarrate={projection.historical ? undefined : onNarrateBehavior}
          />
          {!projection.historical && interactivePreview}
        </Section>
      )}

      {activeDecisions.length > 0 && (
        <Section title={`Human decisions · ${activeDecisions.length}`}>
          <Limited rows={activeDecisions} render={decisionCard} />
        </Section>
      )}

      {deferredDecisions.length > 0 && (
        <Section title={`Deferred decisions · ${deferredDecisions.length}`}>
          <Limited rows={deferredDecisions} render={decisionCard} />
        </Section>
      )}

      {reviewFirst.length + orphanQuestions.length > 0 && (
        <Section title={`Review first · ${reviewFirst.length + orphanQuestions.length}`}>
          {orphanQuestions.map((question) => <QuestionRow key={question.id} question={question} />)}
          <Limited rows={reviewFirst} render={card} />
        </Section>
      )}

      {understand.length > 0 && (
        <Section title={`Understand · ${understand.length}`}>
          <Limited rows={understand} render={card} />
        </Section>
      )}

      {dependencyReviewRelevant(dependencies) && (
        <Section title={`Dependencies · ${dependencies.summary.semantic_changes}`}>
          <DependencyReviewSummary review={dependencies} limit={SECTION_LIMIT} onOpenTarget={onOpenTarget} />
        </Section>
      )}

      {routine.length > 0 && (
        <Section
          title={`Routine · ${plural(routineTargets.length, "change")} collapsed`}
          action={(
            <UiButton variant="ghost" size="compact" type="button" aria-expanded={routineOpen} onClick={() => setRoutineOpen((open) => !open)}>
              {routineOpen ? "Hide" : "Show"}
            </UiButton>
          )}
        >
          {routine.map((item) => item.rationale && <p key={item.id} className="text-caption text-muted-foreground">{item.rationale}</p>)}
          {routineOpen && <TargetLinks ids={routineTargets} byId={byId} onOpenTarget={onOpenTarget} />}
        </Section>
      )}

      {done.length > 0 && (
        <Section
          title={`Reviewed · ${done.length}`}
          action={(
            <UiButton variant="ghost" size="compact" type="button" aria-expanded={doneOpen} onClick={() => setDoneOpen((open) => !open)}>
              {doneOpen ? "Hide reviewed" : "Show reviewed"}
            </UiButton>
          )}
        >
          {doneOpen && <Limited rows={done} render={card} />}
        </Section>
      )}

      {requestScope && outsideItems.length > 0 && (
        <Section
          title={`Outside your request · ${outsideItems.length}`}
          action={(
            <UiButton
              variant="ghost"
              size="compact"
              type="button"
              aria-pressed={requestScope.showOutside}
              onClick={requestScope.onToggleOutside}
            >
              {requestScope.showOutside ? "Only your request" : "Show outside your request"}
            </UiButton>
          )}
        >
          <p className="text-caption text-muted-foreground" data-testid="brief-outside-request">
            {requestScope.showOutside
              ? "Shown in place above, alongside your request."
              : "Not part of your review request; still open to inspect."}
          </p>
          {!requestScope.showOutside && <Limited rows={outsideItems} render={card} />}
        </Section>
      )}

      {withdrawnDecisions.length > 0 && (
        <Section title={`Withdrawn by the author · ${withdrawnDecisions.length}`}>
          <Limited rows={withdrawnDecisions} render={decisionCard} />
        </Section>
      )}

      <footer className="flex flex-col gap-1 border-t border-border/70 pt-3 text-caption text-muted-foreground">
        <details data-testid="review-detail-options" className="rounded border border-border/70 px-2 py-1">
          <summary className="cursor-pointer select-none text-neutral-400">
            View options · {detailLevel === "compact" ? "Compact" : detailLevel === "detailed" ? "Detailed" : "Standard"}
          </summary>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <ToggleGroup
              value={detailLevel}
              onChange={(value) => {
                if (value === "compact" || value === "standard" || value === "detailed") setDetailLevel(value);
              }}
              options={[
                { value: "compact", label: "Compact" },
                { value: "standard", label: "Standard" },
                { value: "detailed", label: "Detailed" },
              ]}
              variant="segment"
              size="xs"
              aria-label="Review detail"
            />
            <span className="text-micro text-muted-foreground">Changes presentation only.</span>
          </div>
        </details>
        {collapsedSupport.length > 0 && (
          <details data-testid="supporting-material" className="rounded border border-border/70 px-2 py-1">
            <summary className="cursor-pointer select-none text-neutral-400">
              Supporting material · {plural(collapsedSupport.length, "file")}
            </summary>
            <div className="mt-1 space-y-0.5">
              {collapsedSupport.map((file) => (
                <div key={file.path} className="flex min-w-0 gap-2">
                  <span className="min-w-0 flex-1 truncate font-mono">{file.path}</span>
                  <span>{file.role.replace("_", " ")}</span>
                  <span>{file.changed_lines} lines</span>
                </div>
              ))}
              <div>Still included in the complete Code view and Review scope.</div>
            </div>
          </details>
        )}
        {!projection.summary && coverage.unexplained_targets > 0 && (
          <span>Analysis coverage: {plural(coverage.unexplained_targets, "changed target")} {coverage.unexplained_targets === 1 ? "has" : "have"} no author explanation.</span>
        )}
        {coverage.unresolved_refs > 0 && (
          <span>{plural(coverage.unresolved_refs, "author reference")} could not be matched to this Snapshot.</span>
        )}
        {projection.plan.limitations.map((text) => <span key={text}>{text}</span>)}
        <AiReviewLine
          projection={projection}
          busy={aiReviewBusy}
          error={aiReviewError}
          runResult={aiReviewRun?.revision_id === projection.revision_id ? aiReviewRun : null}
          onRunAiReview={onRunAiReview && !projection.historical ? async (effort) => {
            setAiReviewBusy(true);
            setAiReviewError("");
            try {
              return await onRunAiReview(effort);
            } catch (err) {
              setAiReviewError(err instanceof Error ? err.message : String(err));
              throw err;
            } finally {
              setAiReviewBusy(false);
            }
          } : undefined}
          onCancelAiReview={onCancelAiReview && !projection.historical ? async (jobId) => {
            setAiReviewBusy(true);
            setAiReviewError("");
            try {
              await onCancelAiReview(jobId);
            } catch (err) {
              setAiReviewError(err instanceof Error ? err.message : String(err));
            } finally {
              setAiReviewBusy(false);
            }
          } : undefined}
          onRerunFresh={onRerunAiReviewFresh && !projection.historical ? async (actionId) => {
            setAiReviewBusy(true);
            setAiReviewError("");
            try {
              return await onRerunAiReviewFresh(actionId);
            } catch (err) {
              setAiReviewError(err instanceof Error ? err.message : String(err));
              throw err;
            } finally {
              setAiReviewBusy(false);
            }
          } : undefined}
        />
        <WhatWasChecked projection={projection} targets={targets} />
        <PolicyLine projection={projection} />
        <ReviewTriageLine triage={projection.triage} />
        <SynthesisLine
          projection={projection}
          busy={synthesizing}
          error={synthesisError}
          onSynthesize={onSynthesize && !projection.historical ? async () => {
            setSynthesizing(true);
            setSynthesisError("");
            try {
              await onSynthesize();
            } catch (err) {
              setSynthesisError(err instanceof Error ? err.message : String(err));
            } finally {
              setSynthesizing(false);
            }
          } : undefined}
        />
        <div className="flex items-center gap-2">
          <span>All code: {plural(coverage.file_count, "file")}, {plural(coverage.target_count, "target")}</span>
          <UiButton variant="outline" size="compact" type="button" onClick={onOpenFiles}>Open files</UiButton>
          {/* Spec §8: the full inventory stays one explicit step away, never the default. */}
          <UiButton variant="ghost" size="compact" type="button" onClick={() => onInspectEvidence(null)}>All evidence</UiButton>
        </div>
      </footer>
    </div>
  );
}
