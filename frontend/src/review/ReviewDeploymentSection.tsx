/**
 * Deployments of the merged Change inside Review Status (P15-DEPLOY): one
 * status line ("Deployed to staging · production canary 10%") and one row per
 * environment naming the deciding commit and why it counts. These are
 * external observations after the merge: never part of readiness, never
 * entered by hand. Without a deployment connector nothing renders.
 */
import { useEffect, useState } from "react";

import {
  fetchReviewDeployments,
  type DeploymentEnvironmentView,
  type DeploymentOutcomeState,
  type ReviewDeploymentsView,
} from "./deploymentsApi";

const STATE_TONE: Record<DeploymentOutcomeState, string> = {
  deployed: "text-emerald-300",
  deploying: "text-sky-300",
  failed: "text-rose-300",
  rolled_back: "text-rose-300",
  superseded: "text-muted-foreground",
  unknown: "text-amber-300",
};

function why(environment: DeploymentEnvironmentView): string {
  const commit = environment.commit.slice(0, 12);
  if (environment.state === "rolled_back") return `${commit} does not contain this change`;
  if (environment.relation === "exact") return `${commit} is the merged commit`;
  if (environment.relation === "descendant") return `${commit} contains this change`;
  return `${commit}: containment not proven`;
}

function sentenceCase(text: string): string {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

export default function ReviewDeploymentSection({ reviewId }: { reviewId: string }) {
  const [view, setView] = useState<ReviewDeploymentsView | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    fetchReviewDeployments(reviewId)
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

  if (error) {
    return (
      <section className="mt-5" aria-labelledby="review-deployment-status">
        <div id="review-deployment-status" className="review-kicker mb-1">Deployments</div>
        <div className="text-caption text-amber-300">Deployments are unavailable: {error}</div>
      </section>
    );
  }
  // No connector: nothing at all (not even an empty heading).
  if (!view || !view.configured) return null;
  const environments = view.environments ?? [];
  if (!view.merged && environments.length === 0) return null;
  const dropped = view.environments_dropped ?? 0;
  return (
    <section className="mt-5" aria-labelledby="review-deployment-status">
      <div id="review-deployment-status" className="review-kicker mb-1">Deployments</div>
      {view.status_line ? (
        <div className="text-label leading-5 text-neutral-200" data-testid="deployment-status-line">{view.status_line}</div>
      ) : (
        <div className="text-caption leading-4 text-muted-foreground">
          No deployment of {view.merged?.sha.slice(0, 12)} observed yet.
        </div>
      )}
      {environments.length > 0 && (
        <ul className="mt-1.5 space-y-1" aria-label="Deployment environments">
          {environments.map((environment) => (
            <li key={environment.key} className="text-caption leading-4">
              <span className={STATE_TONE[environment.state] ?? "text-neutral-300"}>{sentenceCase(environment.label)}</span>
              <span className="text-muted-foreground"> · {why(environment)}</span>
              {environment.production && <span className="text-muted-foreground"> · production</span>}
            </li>
          ))}
        </ul>
      )}
      {dropped > 0 && (
        <div className="mt-1 text-caption text-muted-foreground">{dropped} more environment{dropped === 1 ? "" : "s"} not shown.</div>
      )}
      <div className="mt-1 text-caption text-muted-foreground">
        Observed from {view.connector.connector.replace(/_/g, " ")} after the merge · never part of readiness.
      </div>
    </section>
  );
}
