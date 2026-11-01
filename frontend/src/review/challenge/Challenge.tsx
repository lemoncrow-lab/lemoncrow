import { useEffect, useMemo, useState, type ReactNode } from "react";
import { X } from "lucide-react";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetHeader, SheetLede, SheetTitle } from "../../components/ui/sheet";
import type { CodeTarget } from "../../lib/codeLinks";
import { fetchCheckRecipes, postCheckRecipesRun, type CheckRecipesProjection } from "../checkRecipesApi";
import { investigationContext, type InvestigationInput } from "../investigation/investigationContext";
import { fetchRunPlan, type RunPlanProjection } from "../runPlanApi";
import { fetchStaticFindings, type StaticFinding } from "../staticFindingsApi";
import type { EvidenceRow, ReviewProjection, ReviewTarget } from "../types";
import {
  CHECK_OUTCOME_LABEL,
  OUTCOME_LABEL,
  challengeChain,
  type ChallengeChain,
  type ChallengeClaim,
  type ChallengeCommand,
  type ChallengeObservation,
  type ChallengeRun,
  type Loadable,
} from "./challengeModel";
import "./challenge.css";

export interface ChallengeProps {
  claim: ChallengeClaim;
  /** The projection the Review shows now; a claim read on another Snapshot is stale against it. */
  projection: ReviewProjection;
  targets: readonly ReviewTarget[];
  checks?: readonly EvidenceRow[] | null;
  revision?: InvestigationInput["revision"];
  /** The session may request a check (not read-only, not historical). */
  canRun: boolean;
  run: ChallengeRun | null;
  onRun: (run: ChallengeRun) => void;
  onOpenTarget: (targetId: string) => void;
  onOpenCodeLocation?: (location: CodeTarget) => void;
  onClose: () => void;
}

const BASIS_LABEL: Record<ChallengeObservation["basis"], string> = {
  consented_run: "From the check you ran",
  recorded_proof: "From a recorded LemonCrow Proof",
  recorded_trace: "From a recorded run's trace",
  none: "Nothing ran",
};

const OUTCOME_TONE: Record<ChallengeObservation["outcome"], "success" | "destructive" | "warning" | "neutral"> = {
  supported: "success",
  contradicted: "destructive",
  inconclusive: "warning",
  could_not_run: "neutral",
};

const PROOF_LABEL: Record<string, string> = {
  snapshot: "Snapshot",
  observed: "Observed",
  current_index: "Working index",
  inferred: "Inferred",
  unknown: "Unknown",
};

function message(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason || "request failed");
}

/** Read-only loads through existing endpoints; each one that fails leaves inspection usable. */
function useLoads(reviewId: string, revisionId: string, wanted: boolean) {
  const [recipes, setRecipes] = useState<Loadable<CheckRecipesProjection>>({ state: "loading" });
  const [runPlan, setRunPlan] = useState<Loadable<RunPlanProjection>>({ state: "loading" });
  const [proofs, setProofs] = useState<Loadable<readonly StaticFinding[]>>({ state: "loading" });
  useEffect(() => {
    if (!wanted) {
      const skipped = { state: "unavailable" as const, reason: "not loaded for this claim" };
      setRecipes(skipped);
      setRunPlan(skipped);
      setProofs(skipped);
      return;
    }
    let active = true;
    const settle = <T,>(promise: Promise<T>, set: (value: Loadable<T>) => void) => {
      set({ state: "loading" });
      // The Promise boundary also catches a synchronous adapter failure.
      void Promise.resolve().then(() => promise).then(
        (value) => { if (active) set({ state: "ready", value }); },
        (reason: unknown) => { if (active) set({ state: "unavailable", reason: message(reason) }); },
      );
    };
    settle(fetchCheckRecipes(reviewId, revisionId), setRecipes);
    settle(fetchRunPlan(reviewId, revisionId), setRunPlan);
    settle(fetchStaticFindings(reviewId, revisionId, "").then((value) => value.findings), setProofs);
    return () => { active = false; };
  }, [reviewId, revisionId, wanted]);
  return useMemo(() => ({ recipes, runPlan, proofs }), [proofs, recipes, runPlan]);
}

/**
 * Challenge This Claim (N04): the chain claim -> source rationale -> relevant
 * checks -> reproduction candidate -> observed result. Inspection never needs
 * a run; a run happens only on the explicit confirmation below, through the
 * Review's existing check-recipe executor, once.
 */
export default function Challenge(props: ChallengeProps) {
  const { claim, projection, targets, checks, canRun, run, revision } = props;
  const context = useMemo(
    () => investigationContext({ projection, targets, checks: checks ?? null, revision }),
    [checks, projection, revision, targets],
  );
  const stale = claim.snapshotIdentity !== context.snapshot_identity;
  const loads = useLoads(projection.review_id, projection.revision_id, !stale && !claim.hypothetical);
  const [busy, setBusy] = useState(false);
  const chain = useMemo(
    () => challengeChain({ claim, context, targets, canRun, run, ...loads }),
    [canRun, claim, context, loads, run, targets],
  );

  const execute = async (command: ChallengeCommand) => {
    if (busy || run) return;
    setBusy(true);
    const record: ChallengeRun = {
      claimDigest: chain.digest,
      snapshotIdentity: context.snapshot_identity,
      revisionId: context.revision_id,
      command,
      responseRevisionId: "",
      verdict: "",
      differential: null,
      error: "",
      notes: [],
    };
    try {
      // pick=false: only an entry the effective run plan already schedules runs; the plan is not amended.
      const response = await postCheckRecipesRun(projection.review_id, context.revision_id, command.entryId, false);
      record.responseRevisionId = response.revision_id;
      record.verdict = response.verdicts[command.entryId] ?? response.verdicts[command.recipeId] ?? "";
      record.notes = response.notes.slice(0, 5);
      if (response.revision_id === context.revision_id) {
        try {
          const after = await fetchCheckRecipes(projection.review_id, context.revision_id);
          if (after.revision_id === context.revision_id) {
            record.differential = after.results.find((row) => row.recipe.id === command.recipeId) ?? null;
            if (!record.verdict && record.differential) record.verdict = record.differential.head_verdict;
          }
        } catch {
          // The verdict stands without the differential; its evidence link is simply absent.
        }
      }
    } catch (reason) {
      record.error = message(reason);
    } finally {
      setBusy(false);
    }
    props.onRun(record);
  };

  return (
    <Sheet open onOpenChange={(open) => { if (!open) props.onClose(); }}>
      <SheetContent className="w-[min(600px,100%)]" aria-label="Challenge this claim" data-testid="challenge" data-stale={stale ? "true" : "false"}>
        <SheetHeader>
          <SheetTitle>Challenge this claim</SheetTitle>
          <SheetClose asChild>
            <Button variant="ghost" size="icon" type="button" aria-label="Close the challenge">
              <X size={14} />
            </Button>
          </SheetClose>
        </SheetHeader>
        <SheetLede>
          Is it actually true? Evidence can falsify a claim; it never approves the Review. Nothing runs unless you confirm below.
        </SheetLede>
        <div className="ch-body">
          {stale && (
            <p className="ch-stale" role="status" data-testid="challenge-stale">
              <Badge tone="warning">Stale</Badge>
              <span>{`Read on ${claim.snapshotIdentity || "another Snapshot"}; this Review now shows ${context.snapshot_identity}. Nothing below describes the Snapshot shown now.`}</span>
            </p>
          )}
          <ol className="ch-chain" data-testid="challenge-chain">
            <ClaimStep chain={chain} />
            <SourcesStep chain={chain} props={props} />
            <ChecksStep chain={chain} />
            <CandidateStep chain={chain} busy={busy} ran={Boolean(run && run.claimDigest === chain.digest)} onRun={execute} />
            <ObservedStep chain={chain} />
          </ol>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function Step({ index, title, testId, children }: { index: number; title: string; testId: string; children: ReactNode }) {
  return (
    <li className="ch-step" data-testid={testId}>
      <h3 className="ch-step-title"><span className="ch-step-index" aria-hidden="true">{index}</span>{title}</h3>
      <div className="ch-step-body">{children}</div>
    </li>
  );
}

function ClaimStep({ chain }: { chain: ChallengeChain }) {
  const { claim } = chain;
  return (
    <Step index={1} title="Claim" testId="challenge-claim">
      <div className="ch-tags">
        <Badge tone={claim.modelSuggested ? "ai" : "secondary"}>{claim.saidBy}</Badge>
        {claim.hypothetical && <Badge tone="info">Hypothetical</Badge>}
        <Badge tone="neutral">{claim.stance === "alleges" ? "Alleges a problem" : "Asserts it holds"}</Badge>
      </div>
      {/* Rendered as text: claim words are data, never markup or instructions. */}
      <blockquote className="ch-quote" data-testid="challenge-claim-text">{claim.text}</blockquote>
      {claim.textOmitted > 0 && <p className="ch-note">{`${claim.textOmitted} more characters not shown.`}</p>}
      <p className="ch-note">Quoted as written. LemonCrow does not follow instructions in it, and it never changes what would run.</p>
    </Step>
  );
}

function SourcesStep({ chain, props }: { chain: ChallengeChain; props: ChallengeProps }) {
  return (
    <Step index={2} title="Source rationale" testId="challenge-sources">
      <ul className="ch-list">
        {chain.claim.sources.map((source, index) => {
          const where = source.path ? (source.line > 0 ? `${source.path}:${source.line}` : source.path) : "";
          const open = !chain.stale && (source.targetId
            ? () => props.onOpenTarget(source.targetId!)
            : source.path && props.onOpenCodeLocation
              ? () => props.onOpenCodeLocation!({ path: source.path, line: Math.max(1, source.line), endLine: Math.max(1, source.line) })
              : null);
          return (
            <li key={`${source.label}:${where}:${index}`} className="ch-row">
              <div className="ch-row-head">
                <span className="ch-row-label">{source.label}</span>
                <Badge tone="neutral">{PROOF_LABEL[source.proof] ?? source.proof}</Badge>
              </div>
              {where && <div className="ch-mono">{where}</div>}
              {source.detail && <div className="ch-note">{source.detail}</div>}
              {open && (
                <Button variant="outline" size="compact" type="button" className="self-start" onClick={() => { props.onClose(); open(); }}>
                  Open source
                </Button>
              )}
            </li>
          );
        })}
      </ul>
    </Step>
  );
}

function ChecksStep({ chain }: { chain: ChallengeChain }) {
  return (
    <Step index={3} title="Relevant checks" testId="challenge-checks">
      {chain.checks.length === 0 ? (
        <p className="ch-note">No recorded check is linked to this claim. That is not a pass.</p>
      ) : (
        <ul className="ch-list">
          {chain.checks.map((check) => (
            <li key={`${check.relation}:${check.key}`} className="ch-row ch-row-inline">
              <span className="ch-mono">{check.key}</span>
              <Badge tone="neutral" data-outcome={check.outcome}>{CHECK_OUTCOME_LABEL[check.outcome]}</Badge>
              <span className="ch-note">
                {check.relation === "named_by_claim" ? "named by the claim" : "on the same code, not named by the claim"}
                {check.current ? "" : check.snapshotNumber !== null ? ` · from Snapshot ${check.snapshotNumber}` : " · from an earlier Snapshot"}
              </span>
            </li>
          ))}
        </ul>
      )}
      {chain.reviewLevelChecks > 0 && (
        <p className="ch-note">{`${chain.reviewLevelChecks} Snapshot-wide ${chain.reviewLevelChecks === 1 ? "check is" : "checks are"} not linked to this claim and not counted here.`}</p>
      )}
    </Step>
  );
}

function CandidateStep({ chain, busy, ran, onRun }: { chain: ChallengeChain; busy: boolean; ran: boolean; onRun: (command: ChallengeCommand) => void }) {
  const candidate = chain.candidate;
  return (
    <Step index={4} title="Reproduction candidate" testId="challenge-candidate">
      {candidate.kind === "loading" && <p className="ch-note" role="status">Looking for a bounded check in this Snapshot's run plan…</p>}
      {candidate.kind === "inspect_only" && (
        <div className="ch-inspect" data-testid="challenge-inspect-only">
          <Badge tone="neutral">Inspect only</Badge>
          <p className="ch-note">{candidate.reason}</p>
        </div>
      )}
      {candidate.kind === "runnable" && (
        <div className="ch-consent" data-testid="challenge-consent" role="group" aria-label="Run consent">
          <p className="ch-row-label">{candidate.command.title}</p>
          <dl className="ch-facts">
            <dt>Command</dt>
            <dd><code className="ch-mono" data-testid="challenge-command">{candidate.command.command.join(" ")}</code></dd>
            <dt>Working directory</dt>
            <dd className="ch-mono">{candidate.command.cwd}</dd>
            <dt>Environment</dt>
            <dd>{`${candidate.command.executor} · trusted-base recipe ${candidate.command.recipeDigest.slice(0, 24)}`}</dd>
            <dt>Scope</dt>
            <dd>
              {candidate.command.scope}
              {candidate.command.selectedTests.length > 0 && <span className="ch-mono ch-block">{candidate.command.selectedTests.join(", ")}</span>}
            </dd>
            <dt>Expected cost</dt>
            <dd data-testid="challenge-cost">{candidate.command.cost}</dd>
            <dt>Authorization</dt>
            <dd>{candidate.command.authorization}</dd>
            <dt>Link to the claim</dt>
            <dd>{candidate.command.relation === "named_by_claim"
              ? "The claim names this check, so its result can support or contradict the claim."
              : "It covers the claim's source but the claim does not name it: whatever it shows stays inconclusive for the claim."}</dd>
          </dl>
          {ran ? (
            <p className="ch-note">Ran once on this Snapshot. Run it again from Check recipes if you need another result.</p>
          ) : (
            <Button variant="outline" size="compact" type="button" className="self-start" disabled={busy} onClick={() => onRun(candidate.command)}>
              {busy ? "Running…" : "Confirm and run"}
            </Button>
          )}
        </div>
      )}
    </Step>
  );
}

function ObservedStep({ chain }: { chain: ChallengeChain }) {
  const observed = chain.observed;
  return (
    <Step index={5} title="Observed result" testId="challenge-observed">
      {!observed ? (
        <p className="ch-note">Not run yet. Nothing is concluded about this claim.</p>
      ) : (
        <div className="ch-result" data-outcome={observed.outcome} data-stale={observed.stale ? "true" : "false"}>
          <div className="ch-tags">
            <Badge tone={OUTCOME_TONE[observed.outcome]} data-testid="challenge-outcome">{OUTCOME_LABEL[observed.outcome]}</Badge>
            <Badge tone="neutral" data-testid="challenge-check-outcome">
              {observed.checkOutcome ? `Check: ${CHECK_OUTCOME_LABEL[observed.checkOutcome]}` : "No check outcome"}
            </Badge>
            {observed.stale && <Badge tone="warning">Stale</Badge>}
          </div>
          <p className="ch-note">{observed.reason}</p>
          <p className="ch-note">
            {BASIS_LABEL[observed.basis]}
            {observed.evidenceId ? <span className="ch-mono">{` · evidence ${observed.evidenceId}`}</span> : null}
            {` · ${observed.snapshotIdentity}`}
          </p>
          {observed.outcome === "contradicted" && (
            <p className="ch-note">The original claim stays quoted above: a contradiction is recorded beside it, never in place of it.</p>
          )}
        </div>
      )}
    </Step>
  );
}
