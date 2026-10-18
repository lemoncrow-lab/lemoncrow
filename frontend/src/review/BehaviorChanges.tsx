import { useEffect, useRef, useState } from "react";

import { Button as UiButton } from "../components/ui/button";
import { Textarea as UiTextarea } from "../components/ui/textarea";
import { AgentActionList } from "./AgentActions";
import type {
  ReviewBehaviorEvidence,
  ReviewBehaviorObservation,
  ReviewBehaviorSummary,
  ReviewBehaviorTarget,
  ReviewExecutionCoverage,
  ReviewManualChecklist,
  ReviewStaticInventory,
} from "./types";

/** The Review's delta against the base side-effect register, or "" when there is none to show. */
export function registerDeltaText(inventory: ReviewStaticInventory | undefined): string {
  if (!inventory || inventory.status !== "ran") return "";
  const delta = inventory.register_delta ?? {};
  const added = delta.added ?? 0;
  const removed = delta.removed ?? 0;
  const changed = delta.changed ?? 0;
  if (added + removed + changed === 0) return "";
  const register = inventory.register ?? {};
  const base = register.base_sha ? ` at base ${register.base_sha.slice(0, 7)}` : "";
  const counts = [`${added} new`, `${removed} removed`, `${changed} changed`].join(", ");
  const dependencies = delta.judged_elsewhere?.dependency ?? 0;
  const elsewhere =
    dependencies > 0
      ? `; ${dependencies} dependency change${dependencies === 1 ? "" : "s"} judged in the dependency review`
      : "";
  const caveat =
    register.state === "truncated"
      ? " (partial register: some may already exist elsewhere)"
      : register.state === "changed_files_only"
        ? " (base register unavailable: compared with the changed files only)"
        : "";
  return `Side effects vs the register${base}: ${counts}${elsewhere}${caveat}.`;
}

/**
 * Behavior deltas as review targets (Phase 9R items 11, 12, 18, 38, 43).
 *
 * Rendered inside existing homes only: the Brief's "Behavior changes"
 * section, its "What was checked" footer, and the Review Status sheet's
 * Verification subsection. A judgment is possible only after the evidence
 * block for this Snapshot has been fetched and rendered; the request echoes
 * the digest of that block. Nothing about viewing is reported.
 */

const EXECUTOR_LABELS: Record<string, string> = {
  author_machine: "author machine",
  customer_worker: "customer worker",
  managed_sandbox: "managed sandbox",
};

const STATE_LABELS: Record<ReviewBehaviorTarget["state"], string> = {
  unreviewed: "Not judged",
  reviewed: "Reviewed",
  needs_changes: "Needs changes",
  changed_since_review: "Changed since your review",
  unknown: "Unknown",
};

export type BehaviorJudgmentState = "reviewed" | "needs_changes" | "unreviewed";

function Observation({ label, value }: { label: string; value: ReviewBehaviorObservation | null }) {
  return (
    <div className="min-w-0 flex-1 rounded border border-border/70 px-2 py-1.5">
      <div className="text-micro uppercase tracking-wide text-muted-foreground">{label}</div>
      {value ? (
        <>
          {value.status && <div className="font-mono text-caption text-neutral-200">{value.status}</div>}
          <div className="text-caption text-neutral-300">{value.summary || "observed"}</div>
          {value.evidence_ref && <div className="truncate font-mono text-micro text-muted-foreground">{value.evidence_ref}</div>}
        </>
      ) : (
        <div className="text-caption text-muted-foreground">No observation on this side</div>
      )}
    </div>
  );
}

export function BehaviorEvidenceBlock({ evidence }: { evidence: ReviewBehaviorEvidence }) {
  return (
    <div className="mt-2 flex flex-col gap-1.5" data-testid="behavior-evidence" data-evidence-digest={evidence.digest}>
      <div className="flex gap-1.5">
        <Observation label="Before · base" value={evidence.base} />
        <Observation label="After · head" value={evidence.head} />
      </div>
      {evidence.facts.length > 0 && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-caption">
          {evidence.facts.map((fact) => (
            <div key={fact.name} className="contents">
              <dt className="text-muted-foreground">{fact.name}</dt>
              <dd className="text-neutral-300">{fact.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {evidence.refs.length > 0 && (
        <ul className="flex flex-col gap-0.5 text-micro text-muted-foreground">
          {evidence.refs.map((ref) => (
            <li key={ref.ref} className="flex min-w-0 gap-2">
              <span className="min-w-0 flex-1 truncate">{ref.label}</span>
              <span className="shrink-0 font-mono">{ref.ref}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function BehaviorRow({
  target,
  revisionId,
  onLoadTarget,
  onJudge,
}: {
  target: ReviewBehaviorTarget;
  revisionId: string;
  onLoadTarget?: (behaviorKey: string) => Promise<ReviewBehaviorTarget>;
  onJudge?: (behaviorKey: string, state: BehaviorJudgmentState, evidenceDigest: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<ReviewBehaviorTarget | null>(null);
  const [displayedDigest, setDisplayedDigest] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const evidenceNode = useRef<HTMLDivElement | null>(null);
  const identity = `${revisionId}:${target.fingerprint}:${target.title}:${target.summary}`;

  // Another Snapshot or another observation: what was shown no longer applies.
  useEffect(() => {
    setDetail(null);
    setDisplayedDigest("");
  }, [identity]);

  // The digest becomes usable only once the block it digests is in the DOM.
  useEffect(() => {
    const digest = detail?.evidence?.digest ?? "";
    setDisplayedDigest(open && digest && evidenceNode.current?.querySelector("[data-testid='behavior-evidence']") ? digest : "");
  }, [open, detail]);

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (!next || detail || !onLoadTarget) return;
    setBusy(true);
    setError("");
    try {
      setDetail(await onLoadTarget(target.behavior_key));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const judge = async (state: BehaviorJudgmentState) => {
    if (!onJudge || !displayedDigest) return;
    setBusy(true);
    setError("");
    try {
      await onJudge(target.behavior_key, state, displayedDigest);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const executor = EXECUTOR_LABELS[target.executor_class] ?? "executor not reported";
  const judgeable = Boolean(displayedDigest);
  return (
    <div
      className={`rounded-md border px-2.5 py-2 text-caption ${target.critical ? "border-rose-900/60 bg-rose-950/15" : "border-border/70"}`}
      data-testid={`behavior-target-${target.behavior_key}`}
      data-behavior-key={target.behavior_key}
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="font-medium text-neutral-100">{`${target.kind_label} · ${target.title}`}</div>
          <div className="text-neutral-300">{target.summary}</div>
          <div className="text-micro text-muted-foreground">
            {[STATE_LABELS[target.state] ?? target.state, target.critical ? "Critical · judged individually" : "", executor]
              .filter(Boolean)
              .join(" · ")}
          </div>
        </div>
        {onLoadTarget && (
          <UiButton variant="ghost" size="compact" type="button" aria-expanded={open} onClick={() => void toggle()}>
            {open ? "Hide evidence" : "Show evidence"}
          </UiButton>
        )}
      </div>
      {open && (
        <div ref={evidenceNode}>
          {busy && !detail && <div className="mt-1 text-muted-foreground" role="status">Loading evidence…</div>}
          {detail?.evidence && <BehaviorEvidenceBlock evidence={detail.evidence} />}
        </div>
      )}
      {onJudge && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <UiButton
            variant="outline"
            size="compact"
            type="button"
            disabled={!judgeable || busy}
            onClick={() => void judge("reviewed")}
          >
            Mark reviewed
          </UiButton>
          <UiButton
            variant="ghost"
            size="compact"
            type="button"
            disabled={!judgeable || busy}
            onClick={() => void judge("needs_changes")}
          >
            Needs changes
          </UiButton>
          {!judgeable && <span className="text-micro text-muted-foreground">Show the evidence to judge this change.</span>}
        </div>
      )}
      {error && <div className="mt-1 text-rose-300" role="alert">{error}</div>}
    </div>
  );
}

export function executionCoverageText(coverage: ReviewExecutionCoverage): string {
  return `${coverage.exercised} of ${coverage.changed_targets} changed areas exercised by ${coverage.runs} recorded run${coverage.runs === 1 ? "" : "s"}`;
}

/** "Changed but never executed" -- present only when something was executed against this Snapshot. */
export function ExecutionCoverageLine({ coverage }: { coverage: ReviewExecutionCoverage | null | undefined }) {
  if (!coverage) return null;
  const names = coverage.never_executed.slice(0, 5).map((row) => row.path || row.label);
  const more = coverage.never_executed_count - names.length;
  return (
    <div data-testid="execution-coverage">
      {"Execution: " + executionCoverageText(coverage) + "."}
      {coverage.never_executed_count > 0 && (
        <div className="text-amber-300/90">
          {"Changed but never executed: " + names.join(", ") + (more > 0 ? ` and ${more} more` : "")}
        </div>
      )}
    </div>
  );
}

export default function BehaviorChanges({
  behavior,
  revisionId,
  onLoadTarget,
  onJudge,
  onNarrate,
}: {
  behavior: ReviewBehaviorSummary;
  revisionId: string;
  onLoadTarget?: (behaviorKey: string) => Promise<ReviewBehaviorTarget>;
  /** Absent when read-only (historical Snapshot, comparison). */
  onJudge?: (behaviorKey: string, state: BehaviorJudgmentState, evidenceDigest: string) => Promise<void>;
  /** The single narrative synthesis call; absent when read-only. */
  onNarrate?: () => Promise<void>;
}) {
  const [narrating, setNarrating] = useState(false);
  const [narrateError, setNarrateError] = useState("");
  const narrative = behavior.narrative;
  const inferred = narrative?.mode === "inferred" ? narrative.statements : [];
  const synthesisStatus = String(narrative?.synthesis?.status ?? "");
  const canNarrate = Boolean(onNarrate) && narrative?.mode === "deterministic" && synthesisStatus !== "disabled";
  const registerText = registerDeltaText(behavior.static_inventory);
  const blockers = behavior.auto_approval?.blockers ?? [];
  const agentActions = behavior.agent_actions ?? [];
  const narrate = async () => {
    if (!onNarrate) return;
    setNarrating(true);
    setNarrateError("");
    try {
      await onNarrate();
    } catch (err) {
      setNarrateError(err instanceof Error ? err.message : String(err));
    } finally {
      setNarrating(false);
    }
  };
  return (
    <div className="flex flex-col gap-2" data-testid="behavior-changes">
      {behavior.total > 0 && (
        <p className="text-caption text-muted-foreground">
          {`${behavior.progress.reviewed} of ${behavior.total} judged`}
          {behavior.progress.critical > 0 ? ` · ${behavior.progress.critical} critical` : ""}
          {behavior.unchanged > 0 ? ` · ${behavior.unchanged} unchanged behavior${behavior.unchanged === 1 ? "" : "s"} collapsed` : ""}
        </p>
      )}
      {registerText && (
        <p className="text-caption text-muted-foreground" data-testid="side-effect-register">{registerText}</p>
      )}
      {blockers.length > 0 && (
        <p className="text-caption text-amber-300/90" data-testid="auto-approval-blocked">
          {`Auto-approval blocked: ${blockers.length} change${blockers.length === 1 ? "" : "s"} (weakened verification, agent instructions or critical side effects) need${blockers.length === 1 ? "s" : ""} a human judgment.`}
        </p>
      )}
      {inferred.length > 0 && (
        <div className="flex flex-col gap-1" data-testid="behavior-narrative">
          {inferred.map((statement) => (
            <p key={statement.text} className="max-w-prose text-body leading-relaxed text-foreground">{statement.text}</p>
          ))}
          <p className="text-micro text-muted-foreground">AI narrative · every statement cites the evidence it describes</p>
        </div>
      )}
      {(canNarrate || narrateError) && (
        <div className="flex items-center gap-2 text-caption text-muted-foreground">
          <span>Before/after narrative: the observed changes below.</span>
          {canNarrate && (
            <UiButton variant="ghost" size="compact" type="button" disabled={narrating} onClick={() => void narrate()}>
              {narrating ? "Explaining…" : "Explain before/after"}
            </UiButton>
          )}
          {narrateError && <span className="text-rose-300" role="alert">{narrateError}</span>}
        </div>
      )}
      {behavior.targets.map((target) => (
        <BehaviorRow
          key={target.behavior_key}
          target={target}
          revisionId={revisionId}
          onLoadTarget={onLoadTarget}
          onJudge={onJudge}
        />
      ))}
      {behavior.total > behavior.targets.length && (
        <p className="text-caption text-muted-foreground">{`${behavior.total - behavior.targets.length} more behavior changes are listed in the review queue.`}</p>
      )}
      {behavior.coverage && (
        <p className="text-caption text-muted-foreground">{"Execution: " + executionCoverageText(behavior.coverage) + "."}</p>
      )}
      {behavior.degraded.map((note) => (
        <p key={note} className="text-caption text-amber-300/90">{note}</p>
      ))}
      {agentActions.length > 0 && (
        <div className="mt-1">
          <div className="mb-1 text-caption font-semibold text-neutral-300">Agent actions outside the repository</div>
          <AgentActionList rows={agentActions} />
        </div>
      )}
    </div>
  );
}

const RESULT_CHOICES = [
  { status: "PASS", label: "Observed working" },
  { status: "FAIL", label: "Observed broken" },
  { status: "UNKNOWN", label: "Could not verify" },
] as const;

/** Manual verification of integrations the sandbox cannot exercise; results are human observations. */
export function ManualChecklist({
  checklist,
  onRecord,
}: {
  checklist: ReviewManualChecklist;
  onRecord?: (itemId: string, status: "PASS" | "FAIL" | "UNKNOWN", note: string) => Promise<void>;
}) {
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const record = async (itemId: string, status: "PASS" | "FAIL" | "UNKNOWN") => {
    if (!onRecord) return;
    setBusy(itemId);
    setError("");
    try {
      await onRecord(itemId, status, notes[itemId] ?? "");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };
  return (
    <div className="mt-3" data-testid="manual-checklist">
      <div className="mb-1 flex items-center gap-2 text-caption font-semibold text-neutral-300">
        <span>Manual verification</span>
        <span className="font-normal text-muted-foreground">{checklist.required ? "required by repository policy" : "optional"}</span>
      </div>
      <div className="mb-2 text-caption leading-4 text-muted-foreground">
        Integrations the sandbox cannot exercise. Results are recorded as human observations, never as sandbox results.
      </div>
      {checklist.policy_error && <div className="mb-2 text-caption text-amber-300" role="alert">{checklist.policy_error}</div>}
      <ul className="flex flex-col gap-2">
        {checklist.items.map((item) => (
          <li key={item.item_id} className="rounded-md border border-border/70 px-2.5 py-2 text-caption" data-testid={`manual-check-${item.item_id}`}>
            <div className="font-medium text-neutral-200">{item.title}</div>
            <ol className="ml-4 list-decimal text-neutral-300">
              {item.steps.map((step) => <li key={step}>{step}</li>)}
            </ol>
            <div className="mt-1 text-muted-foreground">
              {item.result
                ? `${item.result.label} · ${item.result.status}${item.result.attested_by ? ` · ${item.result.attested_by}` : ""}${item.result.note ? ` · ${item.result.note}` : ""}`
                : "Not recorded"}
            </div>
            {onRecord && (
              <div className="mt-1.5 flex flex-col gap-1.5">
                <UiTextarea
                  aria-label={`What you observed for ${item.title}`}
                  value={notes[item.item_id] ?? ""}
                  onChange={(event) => setNotes((current) => ({ ...current, [item.item_id]: event.target.value }))}
                  rows={2}
                />
                <div className="flex flex-wrap gap-1.5">
                  {RESULT_CHOICES.map((choice) => (
                    <UiButton
                      key={choice.status}
                      variant="outline"
                      size="compact"
                      type="button"
                      disabled={busy === item.item_id}
                      onClick={() => void record(item.item_id, choice.status)}
                    >
                      {choice.label}
                    </UiButton>
                  ))}
                </div>
              </div>
            )}
          </li>
        ))}
      </ul>
      {error && <div className="mt-1 text-caption text-rose-300" role="alert">{error}</div>}
    </div>
  );
}
