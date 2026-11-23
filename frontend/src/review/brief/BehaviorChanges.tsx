import { useEffect, useRef, useState } from "react";

import { Button as UiButton } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { AgentActionList } from "../AgentActions";
import type {
  ReviewBehaviorEvidence,
  ReviewBehaviorObservation,
  ReviewBehaviorSummary,
  ReviewBehaviorTarget,
  ReviewExecutionCoverage,
  ReviewManualCheckItem,
  ReviewManualChecklist,
  ReviewStaticInventory,
} from "../types";
import { partCount } from "../vocabulary";

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
 * Rendered in the Brief's "Behavior changes" section and, for the files on the
 * walked path, under the X-Ray. A judgment is possible only after the evidence
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

/** The Reader's evidence-bound behavior actions; each is absent when read-only or historical. */
export interface BehaviorHandlers {
  onLoadTarget?: (behaviorKey: string) => Promise<ReviewBehaviorTarget>;
  onJudge?: (behaviorKey: string, state: BehaviorJudgmentState, evidenceDigest: string) => Promise<void>;
  /** The single narrative synthesis call. */
  onNarrate?: () => Promise<void>;
  onManualCheck?: (itemId: string, status: ManualCheckStatus, note: string) => Promise<void>;
}

export type ManualCheckStatus = "PASS" | "FAIL" | "UNKNOWN";

function Observation({ label, value }: { label: string; value: ReviewBehaviorObservation | null }) {
  return (
    <div className="min-w-0 flex-1 rounded border border-border/70 px-2 py-1.5">
      <div className="text-micro uppercase tracking-wide text-muted-foreground">{label}</div>
      {value ? (
        <>
          {value.status && <div className="font-mono text-caption text-foreground">{value.status}</div>}
          <div className="text-caption text-subtle">{value.summary || "observed"}</div>
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
              <dd className="text-subtle">{fact.value}</dd>
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

export function BehaviorRow({
  target,
  onLoadTarget,
  onJudge,
}: {
  target: ReviewBehaviorTarget;
  onLoadTarget?: (behaviorKey: string) => Promise<ReviewBehaviorTarget>;
  onJudge?: (behaviorKey: string, state: BehaviorJudgmentState, evidenceDigest: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<ReviewBehaviorTarget | null>(null);
  const [displayedDigest, setDisplayedDigest] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const evidenceNode = useRef<HTMLDivElement | null>(null);
  // BehaviorChanges mounts this row under its immutable evidence identity.
  // It cannot carry a prior Snapshot's evidence or mark eligibility.

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
      const loaded = await onLoadTarget(target.behavior_key);
      if (loaded.behavior_key !== target.behavior_key
        || loaded.fingerprint !== target.fingerprint
        || loaded.environment_fingerprint !== target.environment_fingerprint
        || (target.evidence?.digest && loaded.evidence?.digest !== target.evidence.digest)) {
        throw new Error("Behavior evidence changed or belongs to another part. Refresh the Review before judging.");
      }
      setDetail(loaded);
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
      className={`rounded-md border px-2.5 py-2 text-caption ${target.critical ? "border-p0-line bg-p0-bg" : "border-border/70"}`}
      data-testid={`behavior-target-${target.behavior_key}`}
      data-behavior-key={target.behavior_key}
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="font-medium text-foreground">{`${target.kind_label} · ${target.title}`}</div>
          <div className="text-subtle">{target.summary}</div>
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
      {error && <div className="mt-1 text-p0" role="alert">{error}</div>}
    </div>
  );
}

export function executionCoverageText(coverage: ReviewExecutionCoverage): string {
  return `${coverage.exercised} of ${partCount(coverage.changed_targets)} exercised by ${coverage.runs} recorded run${coverage.runs === 1 ? "" : "s"}`;
}

export default function BehaviorChanges({
  behavior,
  revisionId,
  onLoadTarget,
  onJudge,
  onNarrate,
  onManualCheck,
}: {
  behavior: ReviewBehaviorSummary;
  revisionId: string;
  onLoadTarget?: (behaviorKey: string) => Promise<ReviewBehaviorTarget>;
  /** Absent when read-only (historical Snapshot, comparison). */
  onJudge?: (behaviorKey: string, state: BehaviorJudgmentState, evidenceDigest: string) => Promise<void>;
  /** The single narrative synthesis call; absent when read-only. */
  onNarrate?: () => Promise<void>;
  /** Records what a person observed for one checklist item; absent when read-only. */
  onManualCheck?: BehaviorHandlers["onManualCheck"];
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
        <p className="text-caption text-p1" data-testid="auto-approval-blocked">
          {`Auto-approval blocked: ${blockers.length} change${blockers.length === 1 ? "" : "s"} (weakened verification, agent instructions or critical side effects) need${blockers.length === 1 ? "s" : ""} a human judgment.`}
        </p>
      )}
      {inferred.length > 0 && (
        <div className="flex flex-col gap-1" data-testid="behavior-narrative">
          {inferred.map((statement) => (
            <p key={statement.text} className="max-w-prose text-body leading-relaxed text-foreground">{statement.text}</p>
          ))}
          <p className="text-micro text-muted-foreground">AI interpretation · not a verified runtime observation. Check the underlying evidence before making a decision.</p>
        </div>
      )}
      {(canNarrate || narrateError) && (
        <div className="flex items-center gap-2 text-caption text-muted-foreground">
          <span>Explain the before/after evidence below; an AI interpretation is not runtime proof.</span>
          {canNarrate && (
            <UiButton variant="ghost" size="compact" type="button" disabled={narrating} onClick={() => void narrate()}>
              {narrating ? "Explaining…" : "Explain before/after"}
            </UiButton>
          )}
          {narrateError && <span className="text-p0" role="alert">{narrateError}</span>}
        </div>
      )}
      {behavior.targets.map((target) => (
        <BehaviorRow
          key={`${revisionId}:${target.behavior_key}:${target.fingerprint}:${target.environment_fingerprint}:${target.evidence?.digest ?? ""}:${target.title}:${target.summary}`}
          target={target}
          onLoadTarget={onLoadTarget}
          onJudge={onJudge}
        />
      ))}
      {behavior.total > behavior.targets.length && (
        <p className="text-caption text-muted-foreground">{`${behavior.total - behavior.targets.length} more behavior changes are not listed here.`}</p>
      )}
      {behavior.coverage && (
        <p className="text-caption text-muted-foreground">{"Execution: " + executionCoverageText(behavior.coverage) + "."}</p>
      )}
      {behavior.checklist && <ManualChecklist checklist={behavior.checklist} onRecord={onManualCheck} />}
      {behavior.degraded.map((note) => (
        <p key={note} className="text-caption text-p1">{behaviorNoteText(note)}</p>
      ))}
      {agentActions.length > 0 && (
        <div className="mt-1">
          <div className="mb-1 text-caption font-semibold text-subtle">Agent actions outside the repository</div>
          <AgentActionList rows={agentActions} />
        </div>
      )}
    </div>
  );
}

/** A degraded note without the internal producer id it is keyed by. */
export function behaviorNoteText(note: string): string {
  return note.replace(/^[a-z_]+(?:\.[a-z_]+)+: /, "");
}

const MANUAL_OUTCOMES = [
  { status: "PASS", label: "Worked", recorded: "worked" },
  { status: "FAIL", label: "Failed", recorded: "failed" },
  { status: "UNKNOWN", label: "Could not verify", recorded: "could not verify" },
] as const;

function ManualCheckRow({ item, onRecord }: { item: ReviewManualCheckItem; onRecord?: BehaviorHandlers["onManualCheck"] }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const result = item.result;
  const recorded = MANUAL_OUTCOMES.find((outcome) => outcome.status === result?.status)?.recorded ?? result?.status;
  const record = async (status: ManualCheckStatus) => {
    if (!onRecord) return;
    setBusy(true);
    setError("");
    try {
      await onRecord(item.item_id, status, note.trim());
      setNote("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="rounded-md border border-border p-2" data-testid="manual-check">
      <div className="text-body font-medium text-foreground">
        {item.title}
        {item.required ? <span className="ml-2 text-caption text-p1">Required</span> : null}
      </div>
      <ol className="mt-1 list-decimal pl-4 text-caption text-muted-foreground">
        {item.steps.map((step) => <li key={step}>{step}</li>)}
      </ol>
      {item.paths.length > 0 && <p className="mt-1 text-micro text-muted-foreground">{item.paths.join(", ")}</p>}
      {result && (
        <p className="mt-1 text-caption text-foreground" data-testid="manual-check-result">
          {`${result.label}: ${recorded}${result.attested_by ? ` · ${result.attested_by}` : ""}${result.note ? ` · ${result.note}` : ""}`}
        </p>
      )}
      {onRecord && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Input
            className="max-w-xs"
            aria-label={`What you saw: ${item.title}`}
            placeholder="What you saw (optional)"
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
          {MANUAL_OUTCOMES.map((outcome) => (
            <UiButton key={outcome.status} variant="ghost" size="compact" type="button" disabled={busy} onClick={() => void record(outcome.status)}>
              {outcome.label}
            </UiButton>
          ))}
        </div>
      )}
      {error && <div className="mt-1 text-p0" role="alert">{error}</div>}
    </li>
  );
}

/** What only a person can check on this revision; a result is recorded as that person's observation, never as a run. */
function ManualChecklist({ checklist, onRecord }: { checklist: ReviewManualChecklist; onRecord?: BehaviorHandlers["onManualCheck"] }) {
  return (
    <div className="mt-1 flex flex-col gap-1" data-testid="manual-checklist">
      <div className="text-caption font-semibold text-subtle">
        {`Check by hand · ${checklist.recorded} of ${checklist.items.length} recorded`}
      </div>
      {checklist.policy_error && <p className="text-caption text-p1">{checklist.policy_error}</p>}
      {checklist.items.length > 0 && (
        <ul className="flex flex-col gap-1">
          {checklist.items.map((item) => <ManualCheckRow key={item.item_id} item={item} onRecord={onRecord} />)}
        </ul>
      )}
    </div>
  );
}
