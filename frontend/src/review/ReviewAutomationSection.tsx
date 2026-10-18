/**
 * Automation inside Review Status (P12-AUTO): which organization rules apply,
 * why each would or would not fire (guards incl. provider branch protection),
 * and the optional post-merge actions. Nothing here moves readiness; a
 * post-merge action runs only after the provider reported the merge, and only
 * on an explicit click unless an organization rule enabled it.
 */
import { useCallback, useEffect, useState } from "react";
import { Badge } from "../components/ui/badge";
import { Button as UiButton } from "../components/ui/button";
import { CheckCircle2, CircleDot, XCircle } from "lucide-react";

import {
  fetchReviewAutomation,
  runPostMergeAction,
  type AutomationCheck,
  type BranchProtectionView,
  type PostMergeKindView,
  type ReviewAutomationView,
} from "./automationApi";

const PROTECTION_LABEL: Record<BranchProtectionView["requirements"], string> = {
  satisfied: "satisfied",
  unsatisfied: "not satisfied",
  unknown: "unknown (never treated as satisfied)",
};

function CheckRow({ check }: { check: AutomationCheck }) {
  return (
    <li className="flex gap-1.5 text-caption leading-4">
      {check.passed
        ? <CheckCircle2 size={11} className="mt-0.5 shrink-0 text-emerald-400" aria-label="holds" />
        : <XCircle size={11} className="mt-0.5 shrink-0 text-amber-300" aria-label="does not hold" />}
      <span className="min-w-0">
        <span className={check.passed ? "text-neutral-300" : "text-amber-300"}>{check.name}</span>
        <span className="text-muted-foreground"> · {check.detail}</span>
      </span>
    </li>
  );
}

function rerunnable(kind: PostMergeKindView): boolean {
  const status = kind.latest?.status;
  return !status || status === "failed" || status === "unavailable" || status === "refused";
}

export default function ReviewAutomationSection({ reviewId, readOnly = false }: { reviewId: string; readOnly?: boolean }) {
  const [view, setView] = useState<ReviewAutomationView | null>(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState("");
  const load = useCallback(async () => {
    try {
      setView(await fetchReviewAutomation(reviewId));
      setError("");
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    }
  }, [reviewId]);
  useEffect(() => { void load(); }, [load]);

  const run = async (kind: string) => {
    setRunning(kind);
    setError("");
    try {
      await runPostMergeAction(reviewId, kind);
      await load();
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setRunning("");
    }
  };

  if (!view) {
    return error ? (
      <section className="mt-5" aria-labelledby="review-automation-status">
        <div id="review-automation-status" className="review-kicker mb-1">Automation</div>
        {/* Not an alert: automation is an optional follow-up and never blocks the Review. */}
        <div className="text-caption text-amber-300">Automation is unavailable: {error}</div>
      </section>
    ) : null;
  }
  const protection = view.facts.branch_protection;
  const evaluations = new Map(view.evaluations.map((item) => [item.rule_id, item]));
  return (
    <section className="mt-5" aria-labelledby="review-automation-status">
      <div id="review-automation-status" className="review-kicker mb-1">Automation</div>
      {view.policy.rules.length === 0 ? (
        <div className="text-caption leading-4 text-muted-foreground">
          No organization automation rule applies. State-changing follow-ups need an explicit human action.
        </div>
      ) : (
        <ul className="divide-y divide-neutral-900 border-y border-neutral-900" aria-label="Automation rules">
          {view.policy.rules.map((rule) => {
            const evaluation = evaluations.get(rule.id);
            return (
              <li key={rule.id} className="py-2.5">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="text-caption font-medium text-neutral-200">{rule.action_label}</span>
                  <span className="font-mono text-caption text-muted-foreground">{rule.id}</span>
                  {rule.reserved && <Badge tone="secondary" className="text-muted-foreground">evaluated only</Badge>}
                  {!rule.enabled && <Badge tone="secondary" className="text-muted-foreground">disabled</Badge>}
                  {evaluation && (
                    <Badge tone="secondary" className={evaluation.would_fire ? "text-emerald-300" : "text-amber-300"}>
                      {evaluation.would_fire ? "would fire" : "would not fire"}
                    </Badge>
                  )}
                </div>
                <div className="mt-0.5 text-caption leading-4 text-muted-foreground">
                  Enabled by {rule.enabled_by || "nobody"} · acts as {rule.acts_as.subject} ({rule.acts_as.role}) · needs {rule.permission}
                </div>
                {evaluation && (
                  <>
                    <div className="mt-0.5 text-caption leading-4 text-neutral-400">{evaluation.reason}</div>
                    <ul className="mt-1 space-y-0.5" aria-label={`Checks for ${rule.id}`}>
                      {[...evaluation.guards, ...evaluation.conditions].map((check) => (
                        <CheckRow key={`${check.kind}:${check.name}`} check={check} />
                      ))}
                    </ul>
                  </>
                )}
              </li>
            );
          })}
        </ul>
      )}
      <div className="mt-1 text-caption leading-4 text-muted-foreground">Rules: {view.policy.edit_source}</div>
      {view.policy.errors.length > 0 && (
        <div role="alert" className="mt-1 text-caption text-amber-300">{view.policy.errors.join(" · ")}</div>
      )}

      <div className="mt-3 rounded-md border border-border/70 bg-muted/20 px-3 py-2" aria-label="Provider branch protection">
        <div className="text-caption font-semibold text-neutral-300">Provider branch protection</div>
        <div className="mt-0.5 text-caption leading-4 text-muted-foreground">
          Requirements {PROTECTION_LABEL[protection.requirements]} · required reviews {PROTECTION_LABEL[protection.required_reviews]}
          {protection.changes_requested ? " · changes requested" : ""}
        </div>
        <div className="mt-0.5 text-caption leading-4 text-muted-foreground">
          {protection.detail} No rule can bypass branch protection or required reviews.
        </div>
      </div>

      <div className="mt-3">
        <div className="text-caption font-semibold text-neutral-300">After merge</div>
        <div className="mt-0.5 text-caption leading-4 text-muted-foreground">
          {view.post_merge.reason} Never part of readiness.
        </div>
        <ul className="mt-1.5 space-y-1.5" aria-label="Post-merge actions">
          {view.post_merge.kinds.map((kind) => (
            <li key={kind.kind} className="flex items-start justify-between gap-3 rounded-md border border-border/60 px-3 py-2">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2 text-caption">
                  <CircleDot size={10} className="shrink-0 text-muted-foreground" />
                  <span className="font-medium text-neutral-200">{kind.label}</span>
                  {kind.latest && (
                    <Badge tone="secondary" className={kind.latest.status === "completed" ? "text-emerald-300" : "text-amber-300"}>
                      {kind.latest.status}
                    </Badge>
                  )}
                </div>
                {kind.latest && (
                  <div className="mt-0.5 text-caption leading-4 text-muted-foreground">
                    {kind.latest.initiated_by === "rule" ? `By rule ${kind.latest.rule_id}` : `By ${kind.latest.actor_id}`}
                    {kind.latest.detail ? ` · ${kind.latest.detail}` : ""}
                    {kind.latest.status === "completed" && (kind.latest.readiness_unchanged ? " · readiness record unchanged" : " · readiness record changed")}
                  </div>
                )}
              </div>
              {!readOnly && kind.runnable && rerunnable(kind) && (
                <UiButton
                  variant="ghost"
                  size="compact"
                  type="button"
                  disabled={running !== ""}
                  onClick={() => void run(kind.kind)}
                  aria-label={`Run ${kind.label}`}
                >
                  {running === kind.kind ? "Running…" : kind.latest ? "Run again" : "Run"}
                </UiButton>
              )}
            </li>
          ))}
        </ul>
      </div>
      {error && <div role="alert" className="mt-2 text-caption text-rose-300">{error}</div>}
    </section>
  );
}
