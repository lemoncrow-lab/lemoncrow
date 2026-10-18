/**
 * Production after the merge (P15-OBS): predicted behavior vs what production
 * showed, incidents that may be related, and the Revert / Fix forward Changes
 * linked to this one. External observations after the merge: never part of
 * readiness. Status shows the whole line; the Brief shows only open problems.
 * Without a connector, or with nothing observed yet, nothing renders.
 */
import { useEffect, useState } from "react";

import {
  fetchReviewProduction,
  type ProductionChangeLink,
  type ProductionLinkRelation,
  type ProductionProblem,
  type ReviewProductionView,
} from "./productionApi";

const LINK_LABELS: Record<ProductionLinkRelation, string> = {
  reverted_by: "Reverted by",
  reverts: "Reverts",
  fixed_forward_by: "Fixed forward by",
  fixes_forward: "Fixes forward",
};

const MAX_ROWS = 5;

function ProblemRow({ problem }: { problem: ProductionProblem }) {
  return (
    <li className="text-caption leading-4">
      <span className={problem.advisory ? "text-amber-300" : "text-rose-300"}>{problem.title}</span>
      {problem.advisory && <span className="text-muted-foreground"> · advisory, correlation is not a cause</span>}
    </li>
  );
}

function LinkRow({ link }: { link: ProductionChangeLink }) {
  return (
    <li className="text-caption leading-4 text-neutral-300">
      {LINK_LABELS[link.relation] ?? link.relation} <span className="font-mono">{link.title || link.target_review_id}</span>
    </li>
  );
}

export default function ReviewProductionSection({ reviewId, variant }: { reviewId: string; variant: "status" | "brief" }) {
  const [view, setView] = useState<ReviewProductionView | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    fetchReviewProduction(reviewId)
      .then((next) => {
        if (!live) return;
        setView(next);
        setError("");
      })
      .catch((reason: unknown) => {
        if (live) setError(String(reason instanceof Error ? reason.message : reason));
      });
    return () => {
      live = false;
    };
  }, [reviewId]);

  const headingId = `review-production-${variant}`;
  if (error) {
    if (variant === "brief") return null;
    return (
      <section className="mt-5" aria-labelledby={headingId}>
        <div id={headingId} className="review-kicker mb-1">Production</div>
        <div className="text-caption text-amber-300">Production outcomes are unavailable: {error}</div>
      </section>
    );
  }
  // No connector: nothing at all (not even an empty heading).
  if (!view || !view.configured) return null;
  const problems = view.problems ?? [];
  const links = view.links ?? [];
  const observed = view.observability?.configured ? view.observability : null;
  const observations = observed?.observations ?? [];
  const concrete = problems.some((item) => !item.advisory);

  if (variant === "brief") {
    if (problems.length === 0 && links.length === 0) return null;
    return (
      <section aria-labelledby={headingId} data-testid="brief-production">
        <h2 id={headingId} className="text-label font-semibold text-foreground">After merge</h2>
        {problems.length > 0 && (
          <ul className="mt-1.5 space-y-1" aria-label="Production problems">
            {problems.slice(0, MAX_ROWS).map((problem) => <ProblemRow key={problem.id} problem={problem} />)}
          </ul>
        )}
        {problems.length > MAX_ROWS && (
          <div className="mt-1 text-caption text-muted-foreground">{problems.length - MAX_ROWS} more in Review Status.</div>
        )}
        {links.length > 0 && <ul className="mt-1.5 space-y-1" aria-label="Linked Changes">{links.map((link) => <LinkRow key={`${link.relation}:${link.target_review_id}`} link={link} />)}</ul>}
        {concrete && links.length === 0 && (
          <div className="mt-1 text-caption text-muted-foreground">Revert or Fix forward is in More actions; nothing runs without you.</div>
        )}
      </section>
    );
  }

  if (observations.length === 0 && problems.length === 0 && links.length === 0) return null;
  const rollback = view.rollback;
  return (
    <section className="mt-5" aria-labelledby={headingId}>
      <div id={headingId} className="review-kicker mb-1">Production</div>
      {observations.length > 0 && observed?.status_line && (
        <div className="text-label leading-5 text-neutral-200" data-testid="production-status-line">{observed.status_line}</div>
      )}
      {problems.length > 0 && (
        <ul className="mt-1.5 space-y-1" aria-label="Production problems">
          {problems.slice(0, MAX_ROWS).map((problem) => <ProblemRow key={problem.id} problem={problem} />)}
        </ul>
      )}
      {problems.length > MAX_ROWS && (
        <div className="mt-1 text-caption text-muted-foreground">{problems.length - MAX_ROWS} more problems not shown.</div>
      )}
      {links.length > 0 && <ul className="mt-1.5 space-y-1" aria-label="Linked Changes">{links.map((link) => <LinkRow key={`${link.relation}:${link.target_review_id}`} link={link} />)}</ul>}
      {concrete && rollback && (
        <div className="mt-1 text-caption text-muted-foreground">
          {rollback.decision.mode === "rule"
            ? `Automatic rollback (rule ${rollback.decision.rule_id}): ${rollback.reason}`
            : "Revert or Fix forward from More actions; nothing runs without you."}
        </div>
      )}
      <div className="mt-1 text-caption text-muted-foreground">Observed in production after the merge · never part of readiness.</div>
    </section>
  );
}
