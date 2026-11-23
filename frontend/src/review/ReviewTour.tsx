import { useEffect, useMemo, useState, type ReactNode } from "react";
import { ArrowLeft, ArrowRight, Code2, Map as MapIcon, Sparkles } from "lucide-react";

import { Badge } from "../components/ui/badge";
import { Button as UiButton } from "../components/ui/button";
import { isDialogOpen } from "../components/ui/dialog";
import type { ReviewSynthesisStatus, ReviewTarget, ReviewTourProjection } from "./types";

interface ReviewTourProps {
  reviewId: string;
  revisionId: string;
  tour: ReviewTourProjection;
  targets: readonly ReviewTarget[];
  /** Stable cohort identity carried in the URL for reload/back-forward resume. */
  activeCohortId?: string;
  onCohortChange?: (cohortId: string) => void;
  /** Esc and the empty state: back to the Overview. */
  onBack: () => void;
  /** Opens a part in the flow's Focus screen. */
  onOpenTarget: (targetId: string) => void;
  /** Opens the flow Map on the Dependencies lens, where the step's parts sit. */
  onShowOnMap: () => void;
  onAsk?: (targetId: string | null) => void;
  onSynthesize?: () => Promise<void>;
  /** The Steps / On the map switch; absent where the map cannot be offered. */
  viewSwitch?: ReactNode;
}

function positionKey(reviewId: string, revisionId: string): string {
  return "lc.review.guided.position." + reviewId + "." + revisionId;
}

function readPosition(reviewId: string, revisionId: string, tour: ReviewTourProjection): number {
  try {
    const raw = window.sessionStorage.getItem(positionKey(reviewId, revisionId)) ?? "";
    const byCohort = tour.steps.findIndex((step) => step.semantic_key === raw);
    if (byCohort >= 0) return byCohort;
    // Migrate the former numeric Tour position without making it Review state.
    const numeric = Number(raw);
    return Number.isSafeInteger(numeric) && numeric >= 0 && numeric < tour.steps.length ? numeric : 0;
  } catch {
    return 0;
  }
}

/** A file's several hunks read as the same row without their lines. */
function tourPartName(target: ReviewTarget): string {
  if (target.symbol) return target.path + " · " + target.symbol;
  if (target.kind === "hunk" && target.start_line > 0) {
    const lines = target.end_line > target.start_line ? `lines ${target.start_line}–${target.end_line}` : `line ${target.start_line}`;
    // The part before a file's first function reads as the top of the file, as All files names it.
    return `${target.path} · ${target.start_line <= 1 ? `top of file, ${lines}` : lines}`;
  }
  return target.path;
}

/** Plain words for where a step's text came from: the code alone, or AI wording over it. */
function statusText(status: ReviewSynthesisStatus | undefined): string {
  if (!status) return "Built from the code only";
  switch (status.mode) {
    case "cached_synthesis":
    case "synthesized":
      return "Guidance ready";
    case "skipped_by_budget":
      return "Guidance limited by budget";
    case "unavailable":
    case "error":
      return "Built from the code only · no AI wording";
    default:
      return "Built from the code only";
  }
}

export default function ReviewTour(props: ReviewTourProps) {
  // Tour navigation and pending synthesis belong to one immutable Snapshot.
  // A parent refresh must not paint the previous Snapshot's step, even for a
  // frame before passive effects run or when the new tour has equal length.
  return <ReviewTourForSnapshot key={`${props.reviewId}:${props.revisionId}`} {...props} />;
}

function ReviewTourForSnapshot({
  reviewId,
  revisionId,
  tour,
  targets,
  activeCohortId,
  onCohortChange,
  onBack,
  onOpenTarget,
  onShowOnMap,
  onAsk,
  onSynthesize,
  viewSwitch,
}: ReviewTourProps) {
  const byId = useMemo(() => new Map(targets.map((target) => [target.target_id, target])), [targets]);
  const initialByUrl = activeCohortId ? tour.steps.findIndex((step) => step.semantic_key === activeCohortId) : -1;
  const [index, setIndex] = useState(() => initialByUrl >= 0 ? initialByUrl : readPosition(reviewId, revisionId, tour));
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const boundedIndex = Math.min(index, Math.max(0, tour.steps.length - 1));
  const step = tour.steps[boundedIndex];

  useEffect(() => {
    setIndex((current) => Math.min(current, Math.max(0, tour.steps.length - 1)));
  }, [tour.steps.length]);

  useEffect(() => {
    if (!activeCohortId) return;
    const next = tour.steps.findIndex((candidate) => candidate.semantic_key === activeCohortId);
    if (next >= 0) setIndex(next);
  }, [activeCohortId, tour.steps]);

  useEffect(() => {
    if (!step) return;
    try {
      window.sessionStorage.setItem(positionKey(reviewId, revisionId), step.semantic_key);
    } catch {
      // A blocked session store only makes resume URL/page-local.
    }
  }, [reviewId, revisionId, step]);

  const moveTo = (next: number) => {
    const bounded = Math.max(0, Math.min(tour.steps.length - 1, next));
    setIndex(bounded);
    const cohortId = tour.steps[bounded]?.semantic_key;
    if (cohortId) onCohortChange?.(cohortId);
  };

  useEffect(() => {
    if (!step) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || isDialogOpen()) return;
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable)) return;
      // Keys pressed inside the Ask rail belong to Ask: Escape there must not also leave the walkthrough.
      if (target?.closest("[data-review-rail]")) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "j" || event.key === "ArrowDown" || event.key === "]") {
        moveTo(boundedIndex + 1);
        event.preventDefault();
      } else if (event.key === "k" || event.key === "ArrowUp" || event.key === "[") {
        moveTo(boundedIndex - 1);
        event.preventDefault();
      } else if (event.key === "Enter" && step.implementation?.target_id) {
        // Enter on a focused button or link belongs to the control (SPEC 3.5).
        if (target?.closest("button, a, [role='button'], [role='link']")) return;
        onOpenTarget(step.implementation.target_id);
        event.preventDefault();
      } else if (event.key === "Escape") {
        onBack();
        event.preventDefault();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step, boundedIndex, tour.steps.length, onOpenTarget, onBack]);

  if (!step) {
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-1 items-center justify-center p-6 text-label text-muted-foreground">
        <div className="flex flex-col items-center gap-2">
          <span>This revision has no steps worth walking through.</span>
          <span className="flex gap-2">
            <UiButton variant="outline" size="compact" type="button" onClick={onBack}>Back to the Overview</UiButton>
            {viewSwitch}
          </span>
        </div>
      </div>
    );
  }

  const currentTargets = step.target_ids.map((id) => byId.get(id)).filter((value): value is ReviewTarget => Boolean(value));
  // No model is set up ("disabled"): a request could only answer the same, so nothing is offered.
  const canSynthesize = Boolean(onSynthesize)
    && (tour.synthesis?.candidates ?? 0) > 0
    && tour.synthesis?.status !== "pending"
    && tour.synthesis?.status !== "disabled"
    && tour.synthesis?.mode !== "skipped_by_budget";

  const synthesize = async () => {
    if (!onSynthesize) return;
    setBusy(true);
    setFailure("");
    try {
      await onSynthesize();
    } catch (reason) {
      setFailure(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col px-4 py-4" data-testid="review-tour" data-guided-cohort={step.semantic_key}>
      <header className="mb-4 flex flex-wrap items-center gap-2 border-b border-border/70 pb-3">
        <div className="min-w-0 flex-1">
          <div className="text-ui font-semibold text-foreground">
            {"Walkthrough · step " + (boundedIndex + 1) + " of " + tour.steps.length}
          </div>
          <div className="text-meta text-muted-foreground">{statusText(tour.synthesis)}</div>
        </div>
        {viewSwitch}
      </header>

      <article className="flex flex-col gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            {/* A step grouped by folder is titled with the folder; say what it is rather than show a bare path. */}
            <h2 className="text-title font-semibold text-foreground">{step.title.endsWith("/") ? `Changes in ${step.title}` : step.title}</h2>
            {step.change_type !== "current" && <Badge>{step.change_type}</Badge>}
            {step.stale && <Badge tone="p1">stale reference</Badge>}
          </div>
          <p className="mt-1 max-w-prose text-body leading-relaxed text-foreground">{step.why}</p>
          {step.why_origin === "inferred" && <p className="mt-1 text-caption text-muted-foreground">AI wording · exact parts retained</p>}
        </div>

        {step.decision_refs.length > 0 && (
          <div className="text-meta text-p1">{"Human decision: " + step.decision_refs.join(", ")}</div>
        )}
        {step.evidence_refs.length > 0 && (
          <div className="rounded-md border border-border px-2.5 py-2">
            <div className="mb-1 font-mono text-tag uppercase text-muted-foreground">Evidence</div>
            {step.evidence_refs.map((evidence) => (
              <div key={evidence.claim_id + ":" + evidence.key + ":" + evidence.state} className="flex flex-wrap gap-x-2 text-caption">
                <span className="font-mono text-subtle">{evidence.title || evidence.key}</span>
                <span className={evidence.status === "FAIL" ? "text-p0" : "text-muted-foreground"}>{evidence.status}</span>
                {evidence.state === "stale" && <span className="text-p1">earlier revision</span>}
              </div>
            ))}
          </div>
        )}

        {(currentTargets.length > 0 || step.stale_target_ids.length > 0) && (
          <div className="rounded-md border border-border">
            <div className="border-b border-border px-3 py-1.5 text-meta font-medium text-muted-foreground">
              {currentTargets.length + step.stale_target_ids.length === 1 ? "1 part" : `${currentTargets.length + step.stale_target_ids.length} parts`} in this step
            </div>
            <div className="max-h-[min(50vh,28rem)] overflow-y-auto">
              {currentTargets.map((target) => (
                <UiButton
                  key={target.target_id}
                  unstyled
                  type="button"
                  className="flex min-h-8 w-full items-center gap-2 border-b border-border px-3 text-left font-mono text-label text-foreground transition-colors duration-fast last:border-b-0 hover:bg-accent/40"
                  onClick={() => onOpenTarget(target.target_id)}
                >
                  <span className="min-w-0 flex-1 truncate">{tourPartName(target)}</span>
                  <ArrowRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                </UiButton>
              ))}
              {step.stale_target_ids.map((targetId) => (
                <div key={targetId} className="min-h-8 border-b border-border px-3 py-1.5 font-mono text-label text-p1 last:border-b-0">{targetId + " · no longer maps to a current part"}</div>
              ))}
            </div>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-1.5 border-t border-border/70 pt-3">
          <UiButton variant="ghost" size="compact" type="button" disabled={boundedIndex === 0} onClick={() => moveTo(boundedIndex - 1)}>
            <ArrowLeft className="size-3.5" aria-hidden="true" />
            Previous
          </UiButton>
          <UiButton variant="ghost" size="compact" type="button" disabled={boundedIndex >= tour.steps.length - 1} onClick={() => moveTo(boundedIndex + 1)}>
            Next
            <ArrowRight className="size-3.5" aria-hidden="true" />
          </UiButton>
          <span className="flex-1" />
          {step.implementation?.target_id && (
            <UiButton variant="outline" size="compact" type="button" onClick={() => onOpenTarget(step.implementation!.target_id)}>
              <Code2 className="size-3.5" aria-hidden="true" />
              Open this part
            </UiButton>
          )}
          <UiButton variant="ghost" size="compact" type="button" onClick={onShowOnMap}>
            <MapIcon className="size-3.5" aria-hidden="true" />
            Show on the Map
          </UiButton>
          {onAsk && <UiButton variant="outline" size="compact" type="button" onClick={() => onAsk(step.implementation?.target_id ?? step.target_ids[0] ?? null)}>Ask about this part</UiButton>}
          {canSynthesize && step.synthesis_needed && (
            <UiButton variant="ghost" size="compact" type="button" disabled={busy} onClick={() => void synthesize()}>
              <Sparkles className="mr-1 size-3.5" />{busy ? "Refining…" : "Refine explanation"}
            </UiButton>
          )}
        </div>
        {failure && <div role="alert" className="text-meta text-p0">{failure}</div>}
      </article>

      <footer className="mt-6 border-t border-border/70 pt-3 text-caption text-muted-foreground">
        The walkthrough only moves you between parts. Visiting a step never changes what you have reviewed.
      </footer>
    </div>
  );
}
