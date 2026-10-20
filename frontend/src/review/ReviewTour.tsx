import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, ArrowRight, Code2, Sparkles, X } from "lucide-react";

import { Badge } from "../components/ui/badge";
import { Button as UiButton } from "../components/ui/button";
import type { ReviewSynthesisStatus, ReviewTarget, ReviewTourProjection } from "./types";

interface ReviewTourProps {
  reviewId: string;
  revisionId: string;
  tour: ReviewTourProjection;
  targets: readonly ReviewTarget[];
  /** Stable cohort identity carried in the URL for reload/back-forward resume. */
  activeCohortId?: string;
  onCohortChange?: (cohortId: string) => void;
  onExit: () => void;
  onBackToBrief: () => void;
  onOpenTarget: (targetId: string) => void;
  onInspectEvidence: (targetId: string | null) => void;
  onAsk?: (targetId: string | null) => void;
  onSynthesize?: () => Promise<void>;
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

function statusText(status: ReviewSynthesisStatus | undefined): string {
  if (!status) return "Deterministic guidance";
  switch (status.mode) {
    case "cached_synthesis":
    case "synthesized":
      return "Guidance ready";
    case "skipped_by_budget":
      return "Guidance limited by budget";
    case "unavailable":
    case "error":
      return "AI guidance unavailable";
    default:
      return "Deterministic guidance";
  }
}

export default function ReviewTour({
  reviewId,
  revisionId,
  tour,
  targets,
  activeCohortId,
  onCohortChange,
  onExit,
  onBackToBrief,
  onOpenTarget,
  onInspectEvidence,
  onAsk,
  onSynthesize,
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
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "j" || event.key === "ArrowDown" || event.key === "]") {
        moveTo(boundedIndex + 1);
        event.preventDefault();
      } else if (event.key === "k" || event.key === "ArrowUp" || event.key === "[") {
        moveTo(boundedIndex - 1);
        event.preventDefault();
      } else if (event.key === "Enter" && step.implementation?.target_id) {
        onOpenTarget(step.implementation.target_id);
        event.preventDefault();
      } else if (event.key === "Escape") {
        onBackToBrief();
        event.preventDefault();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step, boundedIndex, tour.steps.length, onOpenTarget, onBackToBrief]);

  if (!step) {
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-1 items-center justify-center p-6 text-label text-muted-foreground">
        <div className="flex flex-col items-center gap-2">
          <span>No consequential guided-review steps for this Snapshot.</span>
          <UiButton variant="outline" size="compact" type="button" onClick={onExit}>Open code</UiButton>
        </div>
      </div>
    );
  }

  const currentTargets = step.target_ids.map((id) => byId.get(id)).filter((value): value is ReviewTarget => Boolean(value));
  const canSynthesize = Boolean(onSynthesize)
    && (tour.synthesis?.candidates ?? 0) > 0
    && tour.synthesis?.status !== "pending"
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
            {"Guided review · step " + (boundedIndex + 1) + "/" + tour.steps.length}
          </div>
          <div className="text-meta text-muted-foreground">{statusText(tour.synthesis)}</div>
        </div>
        <UiButton variant="ghost" size="compact" type="button" onClick={onBackToBrief}>
          <ArrowLeft className="size-3.5" aria-hidden="true" />
          Back to Brief
        </UiButton>
        <UiButton variant="ghost" size="icon" type="button" aria-label="Exit guided review" tooltip="Exit guided review" onClick={onExit}>
          <X className="size-4" />
        </UiButton>
      </header>

      <article className="flex flex-col gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-title font-semibold text-foreground">{step.title}</h2>
            {step.change_type !== "current" && <Badge>{step.change_type}</Badge>}
            {step.stale && <Badge tone="p1">stale reference</Badge>}
          </div>
          <p className="mt-1 max-w-prose text-body leading-relaxed text-foreground">{step.why}</p>
          {step.why_origin === "inferred" && <p className="mt-1 text-caption text-muted-foreground">AI wording · exact targets retained</p>}
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
                {evidence.state === "stale" && <span className="text-p1">earlier Snapshot</span>}
              </div>
            ))}
          </div>
        )}

        {(currentTargets.length > 0 || step.stale_target_ids.length > 0) && (
          <div className="hunk">
            <div className="hunk-head">
              <span className="hunk-label">Targets · {currentTargets.length + step.stale_target_ids.length}</span>
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
                  <span className="min-w-0 flex-1 truncate">{target.symbol ? target.path + " · " + target.symbol : target.path}</span>
                  <ArrowRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                </UiButton>
              ))}
              {step.stale_target_ids.map((targetId) => (
                <div key={targetId} className="min-h-8 border-b border-border px-3 py-1.5 font-mono text-label text-p1 last:border-b-0">{targetId + " · no longer maps to a current target"}</div>
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
              Open code
            </UiButton>
          )}
          {onAsk && <UiButton variant="outline" size="compact" type="button" onClick={() => onAsk(step.implementation?.target_id ?? step.target_ids[0] ?? null)}>Ask</UiButton>}
          {step.evidence_refs.length > 0 && (
            <UiButton variant="ghost" size="compact" type="button" onClick={() => onInspectEvidence(step.implementation?.target_id ?? step.target_ids[0] ?? null)}>Inspect evidence</UiButton>
          )}
          {canSynthesize && step.synthesis_needed && (
            <UiButton variant="ghost" size="compact" type="button" disabled={busy} onClick={() => void synthesize()}>
              <Sparkles className="mr-1 size-3.5" />{busy ? "Refining…" : "Refine explanation"}
            </UiButton>
          )}
        </div>
        {failure && <div role="alert" className="text-meta text-p0">{failure}</div>}
      </article>

      <footer className="mt-6 border-t border-border/70 pt-3 text-caption text-muted-foreground">
        Guided Review is navigation only. Visiting a step never changes your durable Reviewed state.
      </footer>
    </div>
  );
}
