import { useEffect, useMemo, useState } from "react";

import { Button as UiButton } from "../components/ui/button";
import { PanelCard } from "./ContextSections";
import {
  fetchRunPlan,
  postRunSpecific,
  startRunSpecific,
  type RunPlan,
  type RunPlanEntry,
  type RunPlanEscalation,
  type RunPlanProjection,
  type RunSpecificPreview,
} from "./runPlanApi";

const DEPTH_LABEL: Record<string, string> = { quick: "Quick", auto: "Auto", thorough: "Thorough", deep: "Deep", custom: "Custom" };

const STATUS_LABEL: Record<string, string> = {
  run: "Runs",
  on_request: "On request",
  skipped: "Skipped",
  inapplicable: "Nothing to act on",
  not_available: "Not available",
  limited_by_budget: "Limited by budget",
  satisfied: "Reused result",
};

const STATUS_TONE: Record<string, string> = {
  run: "text-emerald-300",
  satisfied: "text-emerald-300/80",
  on_request: "text-sky-300",
  limited_by_budget: "text-amber-300",
  not_available: "text-muted-foreground",
  skipped: "text-muted-foreground",
  inapplicable: "text-muted-foreground",
};

const ORIGIN_LABEL: Record<string, string> = {
  preset: "depth",
  custom: "custom",
  profile: "profile",
  policy_floor: "required by policy",
  manual: "added for this Review",
  family_override: "family override",
  escalation: "escalated",
};

const LAYER_LABEL: Record<string, string> = {
  built_in_default: "default",
  organization: "organization",
  team: "team",
  repository: "repository",
  trusted_base_repository: "repository config",
  directory: "directory config",
};

/** Origins that mean someone asked for this entry, so an inapplicable one is worth explaining. */
const EXPLICIT_ORIGINS = new Set(["custom", "profile", "policy_floor", "manual"]);

function selectionLabel(plan: RunPlan): string {
  const { source, name, base } = plan.selection;
  if (source === "custom") return `Custom (from ${DEPTH_LABEL[base ?? ""] ?? base})`;
  if (source === "profile") return `Profile ${name} (${DEPTH_LABEL[base ?? ""] ?? base})`;
  return DEPTH_LABEL[name ?? ""] ?? name ?? "Auto";
}

/** A path default chose this Change's starting depth: name the rule and where it comes from. */
function pathDefaultLabel(plan: RunPlan): string {
  const { path_default: depth, path_default_paths: paths, path_default_origin: origin } = plan.selection;
  if (!depth) return "";
  return `${DEPTH_LABEL[depth] ?? depth} for ${paths} (path default from ${origin})`;
}

/** Escalation states worth a line: what escalated, what budget or policy held back. */
const SHOWN_ESCALATION = new Set(["escalated", "limited_by_budget", "suppressed", "disabled"]);

function escalationOrigins(escalation: RunPlanEscalation): string {
  const origins = [...new Set([...escalation.triggers, ...escalation.suppressed].map((row) => row.origin).filter(Boolean))];
  return origins.length > 0 ? `thresholds from ${origins.join("; ")}` : "";
}

/** Per-entry escalation and rule-question lines (display only). */
function entryEscalationLine(entry: RunPlanEntry): string {
  const escalation = entry.escalation;
  if (!escalation) return "";
  const from = DEPTH_LABEL[escalation.from] ?? escalation.from;
  const to = DEPTH_LABEL[escalation.to] ?? escalation.to;
  if (escalation.state === "limited_by_budget") {
    return from && to && from !== to ? `Escalated from ${from} to ${to}, limited by budget` : "Escalation limited by budget";
  }
  return from && to ? `Escalated from ${from} to ${to}` : "Escalated";
}

function ruleChecksLine(entry: RunPlanEntry): string {
  const checks = entry.rule_checks;
  if (!checks) return "";
  const limited = checks.limited > 0 ? ` · ${checks.limited} limited by budget` : "";
  return `${checks.planned} rule question${checks.planned === 1 ? "" : "s"} · ceiling ${checks.ceiling} (${checks.ceiling_origin})${limited}`;
}

function cost(minutes: number, calls: number): string {
  const parts: string[] = [];
  if (minutes > 0) parts.push(`${Math.round(minutes * 10) / 10} min`);
  if (calls > 0) parts.push(`${calls} model call${calls === 1 ? "" : "s"}`);
  return parts.length > 0 ? parts.join(" · ") : "no compute";
}

/** Required work that will not run is never shown as an ordinary skip. */
function isRequiredBlocker(entry: RunPlanEntry): boolean {
  return (
    entry.required &&
    (entry.status === "limited_by_budget" ||
      (entry.status === "not_available" && (entry.floor_kind === "capability" || entry.floor_kind === "check")))
  );
}

function statusLabel(entry: RunPlanEntry): string {
  if (entry.required && entry.status === "not_available") return "Required · no executor";
  return STATUS_LABEL[entry.status] ?? entry.status;
}

/** Each recipe's test-impact selection, with its reason (impact, full suite or none). */
function selectionLines(entry: RunPlanEntry): string[] {
  const recipes = Object.values(entry.selection?.recipes ?? {});
  return recipes.map((row) => {
    const prefix = recipes.length > 1 ? `${row.recipe_id}: ` : "";
    if (row.mode === "impact") return `${prefix}${row.tests?.length ?? 0} of ${row.total} test files · ${row.reason}`;
    return `${prefix}${row.reason}`;
  });
}

function minutesLabel(minutes: number): string {
  return `${Math.round(minutes * 10) / 10} min`;
}

function EntryRow({ entry }: { entry: RunPlanEntry }) {
  const tone = isRequiredBlocker(entry) ? "text-amber-300" : (STATUS_TONE[entry.status] ?? "text-muted-foreground");
  return (
    <div className="border-t border-border/60 py-1.5 first:border-t-0" data-testid={`run-plan-entry-${entry.id}`}>
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-label text-neutral-200" title={entry.id}>
          {entry.title || entry.id}
        </span>
        <span className={`text-caption ${tone}`}>{statusLabel(entry)}</span>
      </div>
      <div className="mt-0.5 flex flex-wrap gap-x-2 text-micro text-muted-foreground">
        <span className="font-mono">{entry.id}</span>
        <span>{ORIGIN_LABEL[entry.origin] ?? entry.origin}</span>
        {entry.status === "run" && <span>{cost(entry.estimated_minutes, entry.estimated_model_calls)}</span>}
        {entry.sides === "base+head" && <span>base + head</span>}
        {entry.base_on_demand && <span>head first · base on demand</span>}
      </div>
      {selectionLines(entry)
        .filter((line) => line !== entry.reason)
        .map((line) => (
        <div key={line} className="text-micro leading-relaxed text-muted-foreground" data-testid={`run-plan-selection-${entry.id}`}>
          Test impact: {line}
        </div>
      ))}
      {entry.pulled_in_by.length > 0 && (
        <div className="text-micro text-muted-foreground">Needed by {entry.pulled_in_by.join(", ")}</div>
      )}
      {entry.budget_reduced_from && (
        <div className="text-micro text-amber-300/80">
          Reduced from {DEPTH_LABEL[entry.budget_reduced_from] ?? entry.budget_reduced_from} by budget
        </div>
      )}
      {entryEscalationLine(entry) && (
        <div
          className={`text-micro ${entry.escalation?.state === "limited_by_budget" ? "text-amber-300/80" : "text-sky-300/80"}`}
          data-testid={`run-plan-escalation-${entry.id}`}
        >
          {entryEscalationLine(entry)}
        </div>
      )}
      {ruleChecksLine(entry) && (
        <div className="text-micro text-muted-foreground" data-testid={`run-plan-rule-checks-${entry.id}`}>
          {ruleChecksLine(entry)}
        </div>
      )}
      {entry.status === "satisfied" && entry.satisfied_by && (
        <div className="text-micro text-muted-foreground" data-testid={`run-plan-satisfied-${entry.id}`}>
          From {entry.satisfied_by.attestation_class === "provider_ci" ? "provider CI" : "LemonCrow verification"} ·{" "}
          {entry.satisfied_by.title || entry.satisfied_by.check_key} · {entry.satisfied_by.status} · head{" "}
          <span className="font-mono" title={entry.satisfied_by.equivalence}>
            {entry.satisfied_by.head_sha.slice(0, 12)}
          </span>
          {entry.satisfied_by.url && (
            <>
              {" · "}
              <a className="underline" href={entry.satisfied_by.url} target="_blank" rel="noreferrer noopener">
                run
              </a>
            </>
          )}
        </div>
      )}
      {entry.reason && <div className="text-micro leading-relaxed text-muted-foreground">{entry.reason}</div>}
      {entry.status === "inapplicable" && entry.setup_hint && (
        <div className="text-micro leading-relaxed text-muted-foreground">{entry.setup_hint}</div>
      )}
    </div>
  );
}

/**
 * The Snapshot's effective run plan: what runs, why, at what estimated cost.
 *
 * Self-contained (fetches its own data) so it mounts in the Checks rail with
 * one line. Executors consume exactly this persisted plan (same digest).
 */
export default function RunPlanPanel({
  reviewId,
  revisionId = "",
  readOnly = false,
}: {
  reviewId: string;
  revisionId?: string;
  readOnly?: boolean;
}) {
  const [data, setData] = useState<RunPlanProjection | null>(null);
  const [error, setError] = useState("");
  const [picking, setPicking] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [preview, setPreview] = useState<RunSpecificPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [started, setStarted] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    setError("");
    fetchRunPlan(reviewId, revisionId)
      .then((projection) => {
        if (!cancelled) setData(projection);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "run plan unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, [reviewId, revisionId]);

  const plan = data?.plan ?? null;
  const groups = useMemo(() => {
    const entries = plan?.entries ?? [];
    return {
      // Required work that will not run stays in view: readiness waits on it.
      blocked: entries.filter(isRequiredBlocker),
      runs: entries.filter((row) => row.status === "run"),
      reused: entries.filter((row) => row.status === "satisfied"),
      onRequest: entries.filter((row) => row.status === "on_request"),
      notRunning: entries.filter(
        (row) =>
          !isRequiredBlocker(row) &&
          (row.status === "limited_by_budget" ||
            row.status === "not_available" ||
            row.status === "skipped" ||
            // Nothing to act on stays out of sight unless someone asked for it.
            (row.status === "inapplicable" && EXPLICIT_ORIGINS.has(row.origin))),
      ),
    };
  }, [plan]);

  const candidates = useMemo(() => {
    if (!data || !plan) return [];
    const inPlan = new Map(plan.entries.map((row) => [row.id, row]));
    const rows: Array<{ id: string; title: string }> = [];
    for (const row of plan.entries) {
      if ((row.status === "on_request" || row.status === "skipped") && row.available) rows.push({ id: row.id, title: row.title });
    }
    for (const row of data.catalog) {
      if (!inPlan.has(row.id) && row.available) rows.push({ id: row.id, title: row.title });
    }
    return rows;
  }, [data, plan]);

  if (error) {
    return (
      <PanelCard title="Effective run plan">
        <div className="text-caption text-muted-foreground">Run plan unavailable: {error}</div>
      </PanelCard>
    );
  }
  if (!data || !plan) {
    return (
      <PanelCard title="Effective run plan">
        <div className="text-caption text-muted-foreground">Loading run plan…</div>
      </PanelCard>
    );
  }

  const effortOrigin = data.settings.effort.origin.layer;
  const editable = !readOnly && !data.historical;

  async function previewPicks() {
    if (!data) return;
    setBusy(true);
    setActionError("");
    try {
      const response = await postRunSpecific(reviewId, data.revision_id, picked.map((id) => ({ id })), false);
      setPreview(response.preview);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "preview failed");
    } finally {
      setBusy(false);
    }
  }

  async function confirmPicks() {
    if (!data) return;
    setBusy(true);
    setActionError("");
    try {
      const response = await postRunSpecific(reviewId, data.revision_id, picked.map((id) => ({ id })), true);
      if (response.plan) setData({ ...data, plan: response.plan });
      setStarted(await startRunSpecific(reviewId, data.revision_id, response.next ?? []));
      setPicked([]);
      setPreview(null);
      setPicking(false);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "could not add");
    } finally {
      setBusy(false);
    }
  }

  return (
    <PanelCard
      title="Effective run plan"
      action={
        plan.budget.limited ? (
          <span className="text-caption text-amber-300">Limited by budget</span>
        ) : (
          <span className="font-mono text-micro text-muted-foreground" title={plan.digest}>
            {plan.digest.slice(0, 12)}
          </span>
        )
      }
    >
      <div className="space-y-2" data-testid="run-plan-panel">
        <div className="text-caption text-neutral-300">
          {selectionLabel(plan)}
          <span className="text-muted-foreground">
            {" "}
            · depth from {LAYER_LABEL[effortOrigin] ?? effortOrigin} · runs on {plan.run_on} · est. {cost(plan.budget.planned.minutes, plan.budget.planned.model_calls)}
          </span>
        </div>
        {pathDefaultLabel(plan) && (
          <div className="text-micro text-muted-foreground" data-testid="run-plan-path-default">
            Starting depth: {pathDefaultLabel(plan)}
          </div>
        )}
        {data.ai_availability && !data.ai_availability.available && (
          <div className="text-micro leading-relaxed text-amber-300/80" data-testid="run-plan-ai-unavailable">
            AI review unavailable: {data.ai_availability.reason}. Deterministic checks still run.
          </div>
        )}
        {plan.run_on_note && <div className="text-micro text-amber-300/80">{plan.run_on_note}</div>}
        {plan.policy_error && (
          <div className="text-micro leading-relaxed text-rose-300" data-testid="run-plan-policy-invalid">
            Review policy invalid: {plan.policy_error}. Nothing runs until the trusted-base config is fixed.
          </div>
        )}
        {data.historical && <div className="text-micro text-muted-foreground">Plan for an earlier Snapshot.</div>}
        {data.future_settings.changed && (
          <div className="text-micro leading-relaxed text-sky-300/80">
            This Change edits Review settings. They apply to later Reviews after it lands, not to this one.
          </div>
        )}
        {[...plan.budget.notes, ...plan.errors, ...data.settings.errors].map((note) => (
          <div key={note} className="text-micro leading-relaxed text-amber-300/80">
            {note}
          </div>
        ))}
        {(plan.notes ?? []).map((note) => (
          <div key={note} className="text-micro leading-relaxed text-muted-foreground">
            {note}
          </div>
        ))}
        {plan.fast_lane && (
          <div className="text-micro leading-relaxed text-muted-foreground" data-testid="run-plan-fast-lane">
            {plan.fast_lane.eligible
              ? `Fast lane on this push: ${plan.fast_lane.reasons.join("; ")}${
                  plan.fast_lane.skipped.length > 0 ? ` · not running ${plan.fast_lane.skipped.join(", ")}` : ""
                }`
              : `Full plan on this push: ${plan.fast_lane.reasons.join("; ")}`}
          </div>
        )}
        {plan.escalation && SHOWN_ESCALATION.has(plan.escalation.state) && (
          <div
            className={`text-micro leading-relaxed ${
              plan.escalation.state === "limited_by_budget"
                ? "text-amber-300/80"
                : plan.escalation.state === "escalated"
                  ? "text-sky-300/80"
                  : "text-muted-foreground"
            }`}
            data-testid="run-plan-escalation"
            data-state={plan.escalation.state}
          >
            Deep review: {plan.escalation.reason}
            {escalationOrigins(plan.escalation) && (
              <span className="text-muted-foreground"> · {escalationOrigins(plan.escalation)}</span>
            )}
          </div>
        )}
        {(plan.escalation?.notes ?? []).map((note) => (
          <div key={note} className="text-micro leading-relaxed text-muted-foreground" data-testid="run-plan-escalation-note">
            Deep review: {note}
          </div>
        ))}
        {(data.ci_equivalents?.errors ?? []).map((note) => (
          <div key={note} className="text-micro leading-relaxed text-amber-300/80">
            {note}
          </div>
        ))}
        {data.compute && data.compute.totals.runs > 0 && (
          <details data-testid="run-plan-compute">
            <summary className="cursor-pointer text-micro text-muted-foreground">
              Compute used here: {Math.round(data.compute.totals.compute_minutes * 100) / 100} min · {data.compute.totals.runs} run
              {data.compute.totals.runs === 1 ? "" : "s"}
            </summary>
            {data.compute.rows.map((row) => (
              <div key={`${row.entry_id}|${row.executor}|${row.stage}`} className="text-micro text-muted-foreground">
                <span className="font-mono">{row.entry_id}</span> · {row.executor} · {row.stage.replace(/_/g, " ")} ·{" "}
                {Math.round(row.compute_minutes * 100) / 100} min ({row.runs})
              </div>
            ))}
          </details>
        )}

        {data.economy && data.economy.rows.length > 0 && (
          <details data-testid="run-plan-economy">
            <summary className="cursor-pointer text-micro text-muted-foreground">
              Compute saved: {minutesLabel(data.economy.saved_minutes)} · spent {minutesLabel(data.economy.spent_minutes)}
              {data.economy.saved_estimated_minutes > 0 && ` (${minutesLabel(data.economy.saved_estimated_minutes)} estimated)`}
            </summary>
            {data.economy.rows.map((row) => (
              <div key={`${row.entry_id}|${row.reason}`} className="text-micro text-muted-foreground">
                <span className="font-mono">{row.entry_id}</span> · {row.label} · {minutesLabel(row.minutes)} {row.basis}
                {row.runs > 1 ? ` (${row.runs} runs)` : ""}
              </div>
            ))}
          </details>
        )}

        {groups.blocked.length > 0 && (
          <div data-testid="run-plan-required-blockers">
            <div className="text-micro uppercase tracking-wider text-amber-300">Required, not running · {groups.blocked.length}</div>
            {groups.blocked.map((entry) => (
              <EntryRow key={entry.id} entry={entry} />
            ))}
          </div>
        )}
        {groups.runs.length > 0 && (
          <div>
            <div className="text-micro uppercase tracking-wider text-muted-foreground">Runs · {groups.runs.length}</div>
            {groups.runs.map((entry) => (
              <EntryRow key={entry.id} entry={entry} />
            ))}
          </div>
        )}
        {groups.reused.length > 0 && (
          <div data-testid="run-plan-reused">
            <div className="text-micro uppercase tracking-wider text-muted-foreground">
              Reused results · {groups.reused.length}
            </div>
            {groups.reused.map((entry) => (
              <EntryRow key={entry.id} entry={entry} />
            ))}
          </div>
        )}
        {groups.onRequest.length > 0 && (
          <div>
            <div className="text-micro uppercase tracking-wider text-muted-foreground">On request · {groups.onRequest.length}</div>
            {groups.onRequest.map((entry) => (
              <EntryRow key={entry.id} entry={entry} />
            ))}
          </div>
        )}
        {groups.notRunning.length > 0 && (
          <details>
            <summary className="cursor-pointer text-micro uppercase tracking-wider text-muted-foreground">
              Not running · {groups.notRunning.length}
            </summary>
            {groups.notRunning.map((entry) => (
              <EntryRow key={entry.id} entry={entry} />
            ))}
          </details>
        )}

        {Object.keys(started).length > 0 && (
          <div className="space-y-0.5" role="status">
            {Object.entries(started).map(([entry, outcome]) => (
              <div key={entry} className="text-micro text-muted-foreground">
                <span className="font-mono">{entry}</span>: {outcome}
              </div>
            ))}
          </div>
        )}

        {editable && candidates.length > 0 && !picking && (
          <UiButton type="button" size="xs" variant="outline" onClick={() => setPicking(true)}>
            Run specific…
          </UiButton>
        )}
        {editable && picking && (
          <fieldset className="space-y-1 border-t border-border pt-2">
            <legend className="text-micro uppercase tracking-wider text-muted-foreground">Add to this Review</legend>
            {candidates.map((row) => {
              const selected = picked.includes(row.id);
              return (
                <UiButton
                  key={row.id}
                  type="button"
                  size="xs"
                  variant={selected ? "default" : "outline"}
                  aria-pressed={selected}
                  className="w-full justify-start"
                  onClick={() => {
                    setPreview(null);
                    setPicked((current) => (selected ? current.filter((id) => id !== row.id) : [...current, row.id]));
                  }}
                >
                  <span className="truncate">{row.title || row.id}</span>
                  <span className="font-mono text-micro opacity-70">{row.id}</span>
                </UiButton>
              );
            })}
            {preview && (
              <div className="text-caption text-neutral-300" data-testid="run-specific-preview">
                Adds {cost(preview.incremental.minutes, preview.incremental.model_calls)}
                {preview.changes.length > 0 && (
                  <span className="text-muted-foreground">
                    {" "}
                    ({preview.changes.map((row) => `${row.id}: ${STATUS_LABEL[row.status] ?? row.status}`).join(", ")})
                  </span>
                )}
                {preview.limited_by_budget.length > 0 && (
                  <div className="text-micro text-amber-300/80">Limited by budget: {preview.limited_by_budget.join(", ")}</div>
                )}
              </div>
            )}
            {actionError && <div className="text-micro text-rose-300">{actionError}</div>}
            <div className="flex gap-2">
              {preview ? (
                <UiButton type="button" size="xs" variant="default" disabled={busy} onClick={() => void confirmPicks()}>
                  Add to this Review
                </UiButton>
              ) : (
                <UiButton type="button" size="xs" disabled={busy || picked.length === 0} onClick={() => void previewPicks()}>
                  Preview cost
                </UiButton>
              )}
              <UiButton
                type="button"
                size="xs"
                variant="ghost"
                onClick={() => {
                  setPicking(false);
                  setPicked([]);
                  setPreview(null);
                  setActionError("");
                }}
              >
                Cancel
              </UiButton>
            </div>
          </fieldset>
        )}
      </div>
    </PanelCard>
  );
}
