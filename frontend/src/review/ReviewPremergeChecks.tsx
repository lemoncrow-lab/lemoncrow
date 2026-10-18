import { useState } from "react";
import { AlertTriangle, CircleDot, ExternalLink } from "lucide-react";

import { Badge } from "../components/ui/badge";
import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";
import { Select, SelectItem } from "../components/ui/select";
import { Textarea as UiTextarea } from "../components/ui/textarea";
import { webHref } from "./safeLinks";
import type { ReviewPremergeCheck, ReviewPremergeProjection } from "./types";

export interface PremergeWaiverInput {
  check_id: string;
  reason: string;
  scope: "snapshot" | "review";
  hours?: number;
}

export interface PremergeActions {
  busy?: boolean;
  error?: string;
  onWaive?(input: PremergeWaiverInput): void;
  onRebind?(ref: string): void;
  onEvaluate?(): void;
}

function instant(value: string): string {
  return value.replace("T", " ").replace(/Z$/, " UTC");
}

function waiverPrincipals(premerge: ReviewPremergeProjection, check: ReviewPremergeCheck): string[] {
  const policy = premerge.policy.checks.find((item) => item.id === check.id);
  return policy?.waivable_by.length ? policy.waivable_by : premerge.policy.waivers.allowed;
}

function WaiverForm({
  check,
  maxHours,
  busy,
  onSubmit,
  onCancel,
}: {
  check: ReviewPremergeCheck;
  maxHours: number;
  busy: boolean;
  onSubmit(input: PremergeWaiverInput): void;
  onCancel(): void;
}) {
  const [reason, setReason] = useState("");
  const [scope, setScope] = useState<"snapshot" | "review">("snapshot");
  const [hours, setHours] = useState(String(Math.min(24, maxHours)));
  const parsedHours = Number(hours);
  const hoursValid = Number.isFinite(parsedHours) && parsedHours > 0 && parsedHours <= maxHours;
  const valid = reason.trim().length > 0 && hoursValid;
  return (
    <form
      className="mt-2 space-y-2 rounded-md border border-border/70 bg-muted/20 p-2.5"
      aria-label={`Waive ${check.title}`}
      onSubmit={(event) => {
        event.preventDefault();
        if (valid && !busy) onSubmit({ check_id: check.id, reason: reason.trim(), scope, hours: parsedHours });
      }}
    >
      <UiTextarea
        aria-label="Waiver reason"
        placeholder="Why this check may be overridden (recorded in the audit trail)"
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        rows={2}
      />
      <div className="flex flex-wrap items-center gap-2 text-caption text-muted-foreground">
        <Select value={scope} onValueChange={(value) => setScope(value === "review" ? "review" : "snapshot")} uiSize="xs" aria-label="Waiver scope">
          <SelectItem value="snapshot">This Snapshot only</SelectItem>
          <SelectItem value="review">Later Snapshots on the same paths</SelectItem>
        </Select>
        <label className="flex items-center gap-1">
          Expires in
          <UiInput
            aria-label="Waiver hours"
            className="h-8 w-16"
            inputMode="numeric"
            value={hours}
            onChange={(event) => setHours(event.target.value)}
          />
          h (max {maxHours})
        </label>
      </div>
      <div className="flex items-center gap-2">
        <UiButton variant="ghost" size="compact" type="submit" disabled={busy || !valid}>
          {busy ? "Waiving…" : "Record waiver"}
        </UiButton>
        <UiButton variant="ghost" size="compact" type="button" onClick={onCancel}>
          Cancel
        </UiButton>
      </div>
    </form>
  );
}

/**
 * Pre-merge checks inside Review Status. Blocking (error-mode) results are
 * readiness conditions and appear in the condition list; this adds what the
 * conditions cannot say: the warning-mode trial, linked-intent drift, waivers
 * and the waive action. Nothing here decides readiness.
 */
export default function ReviewPremergeChecks({
  premerge,
  readOnly = false,
  actions,
}: {
  premerge: ReviewPremergeProjection;
  readOnly?: boolean;
  actions?: PremergeActions;
}) {
  const [waiving, setWaiving] = useState("");
  const busy = Boolean(actions?.busy);
  const interactive = !readOnly && actions !== undefined;
  const trial = premerge.checks.filter((check) => check.would_block);
  const waivable = premerge.checks.filter((check) => check.mode === "error" && check.waivable);
  const waived = premerge.checks.filter((check) => check.waiver !== null);
  const lapsed = premerge.checks.filter((check) => check.waiver === null && check.waiver_note);
  const notRun = premerge.checks.filter((check) => check.status === "NOT_RUN" && check.applicable);
  const modelChecks = new Set(premerge.policy.checks.filter((item) => item.model_evaluated).map((item) => item.id));
  const evaluable = notRun.some((check) => modelChecks.has(check.id));
  const run = premerge.run ?? null;
  const limited = run?.status === "limited_by_budget";
  // The plan decided not to run them (budget, family off, inapplicable): no button bypasses that.
  const blockedByPlan = limited || ["skipped", "inapplicable", "not_available"].includes(run?.status ?? "");
  const planned = run?.status === "run";
  let notRunText = "Natural-language checks are not in this Snapshot's run plan; evaluate them on request.";
  if (premerge.ai_unavailable) notRunText = `${premerge.ai_unavailable}. Natural-language checks stay NOT_RUN and are never passed.`;
  else if (limited) notRunText = `${run?.reason || "Limited by budget"}. Natural-language checks stay NOT_RUN and are never passed.`;
  else if (blockedByPlan) notRunText = `Not run: ${run?.reason || run?.status}. Natural-language checks stay NOT_RUN and are never passed.`;
  else if (planned) notRunText = "The run plan evaluates natural-language checks on this Snapshot; they stay NOT_RUN until a result is recorded.";
  else if (!run) notRunText = "Natural-language checks have not run on this Snapshot; they stay NOT_RUN until evaluated.";

  return (
    <div className="mt-4" aria-labelledby="premerge-checks-heading">
      <div id="premerge-checks-heading" className="mb-1 text-caption font-semibold text-neutral-300">
        Pre-merge checks
      </div>
      <div className="mb-2 text-caption leading-4 text-muted-foreground">
        Blocking checks are listed with the conditions above. A waived check keeps its observed result.
      </div>

      {premerge.policy_errors.length > 0 && (
        <div className="mb-2 rounded-md border border-amber-900/50 bg-amber-950/10 px-3 py-2 text-caption leading-4 text-amber-300" role="alert">
          Pre-merge policy is invalid, so this Change is blocked until it is fixed:
          <ul className="mt-1 list-disc pl-4">
            {premerge.policy_errors.map((error) => <li key={error}>{error}</li>)}
          </ul>
        </div>
      )}

      {premerge.intents.length > 0 && (
        <div className="mb-2 space-y-1.5">
          {premerge.intents.map((intent) => (
            <div key={intent.ref} className="rounded-md border border-border/70 bg-muted/20 px-3 py-2">
              <div className="flex items-center justify-between gap-3 text-caption">
                {webHref(intent.bound.url) ? (
                  <a href={webHref(intent.bound.url)} target="_blank" rel="noreferrer" className="flex min-w-0 items-center gap-1 text-neutral-300 hover:text-neutral-50">
                    <span className="truncate">{intent.bound.title || intent.ref}</span>
                    <ExternalLink size={10} className="shrink-0" />
                  </a>
                ) : (
                  <span className="min-w-0 truncate text-neutral-300">{intent.bound.title || intent.ref}</span>
                )}
                <span className="shrink-0 font-mono text-muted-foreground">
                  {intent.kind}
                  {intent.bound.snapshot_number !== null ? ` · Snapshot ${intent.bound.snapshot_number}` : ""}
                </span>
              </div>
              {!intent.bound.digest && (
                <div className="mt-0.5 text-caption leading-4 text-muted-foreground">{intent.bound.note || "Reference only: its text was not captured."}</div>
              )}
              {intent.changed && (
                <div className="mt-1 flex items-start justify-between gap-2">
                  <div className="text-caption leading-4 text-amber-300">{intent.changed_note}</div>
                  {interactive && actions?.onRebind && (
                    <UiButton variant="ghost" size="compact" type="button" disabled={busy} onClick={() => actions.onRebind?.(intent.ref)}>
                      Re-bind
                    </UiButton>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {trial.length > 0 && (
        <div className="mb-2">
          <div className="text-caption font-medium text-neutral-300">Warning-mode trial</div>
          <div className="mb-1 text-caption leading-4 text-muted-foreground">{premerge.trial.summary}</div>
          <div className="divide-y divide-neutral-900 border-y border-neutral-900">
            {trial.map((check) => (
              <div key={check.id} className="flex gap-2.5 py-2">
                <CircleDot size={11} className="mt-0.5 shrink-0 text-sky-300" />
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-x-2">
                    <span className="text-caption font-medium text-neutral-300">{check.title}</span>
                    <Badge tone="secondary" className="text-sky-300">would block if enforced</Badge>
                    <span className="font-mono text-caption text-muted-foreground">{check.status}</span>
                  </div>
                  <div className="mt-0.5 text-caption leading-4 text-muted-foreground">{check.detail}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {evaluable && (
        <div className="mb-2 flex items-start justify-between gap-2 text-caption leading-4 text-muted-foreground">
          <span>
            {limited && !premerge.ai_unavailable && (
              <Badge tone="secondary" className="mr-1.5 text-amber-300">Limited by budget</Badge>
            )}
            {notRunText}
          </span>
          {interactive && actions?.onEvaluate && !premerge.ai_unavailable && !blockedByPlan && (
            <UiButton variant="ghost" size="compact" type="button" disabled={busy} onClick={() => actions.onEvaluate?.()}>
              {busy ? "Evaluating…" : planned ? "Re-run" : "Evaluate"}
            </UiButton>
          )}
        </div>
      )}

      {waived.map((check) => check.waiver && (
        <div key={check.id} className="mb-1.5 flex gap-2.5 text-caption leading-4">
          <CircleDot size={11} className="mt-0.5 shrink-0 text-neutral-400" />
          <div className="min-w-0 text-muted-foreground">
            <span className="text-neutral-300">{check.title}</span> ({check.status}) waived by {check.waiver.actor} until{" "}
            {instant(check.waiver.expires_at)} · {check.waiver.scope === "review" ? "same-path Snapshots" : "this Snapshot"} · policy{" "}
            <span className="font-mono">{check.waiver.policy_generation_short}</span>: {check.waiver.reason}
          </div>
        </div>
      ))}
      {lapsed.map((check) => (
        <div key={check.id} className="mb-1.5 flex gap-2.5 text-caption leading-4 text-amber-300">
          <AlertTriangle size={12} className="mt-0.5 shrink-0" />
          <span>{check.title}: {check.waiver_note}</span>
        </div>
      ))}

      {waivable.length > 0 && (
        <div className="space-y-1.5">
          {waivable.map((check) => (
            <div key={check.id}>
              <div className="flex items-center justify-between gap-2 text-caption">
                <span className="min-w-0 text-muted-foreground">
                  {check.title} may be waived by {waiverPrincipals(premerge, check).join(", ") || "no one"}
                </span>
                {interactive && actions?.onWaive && waiving !== check.id && (
                  <UiButton variant="ghost" size="compact" type="button" disabled={busy} onClick={() => setWaiving(check.id)}>
                    Waive…
                  </UiButton>
                )}
              </div>
              {interactive && actions?.onWaive && waiving === check.id && (
                <WaiverForm
                  check={check}
                  maxHours={premerge.policy.waivers.max_hours}
                  busy={busy}
                  onSubmit={(input) => {
                    actions.onWaive?.(input);
                    setWaiving("");
                  }}
                  onCancel={() => setWaiving("")}
                />
              )}
            </div>
          ))}
        </div>
      )}

      {actions?.error && <div className="mt-2 text-caption text-rose-300" role="alert">{actions.error}</div>}
    </div>
  );
}
