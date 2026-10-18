import { useCallback, useEffect, useState } from "react";

import { Button as UiButton } from "../components/ui/button";
import { PanelCard } from "./ContextSections";
import {
  fetchCheckRecipes,
  postCheckRecipesRun,
  postCheckRequest,
  type CheckAttempt,
  type CheckDifferential,
  type CheckEntryRow,
  type CheckQualification,
  type CheckRecipesProjection,
  type CheckUnit,
} from "./checkRecipesApi";

const CLASS_LABEL: Record<string, string> = {
  introduced_failure: "Introduced failure",
  pre_existing_failure: "Pre-existing",
  fixed: "Fixed",
  new: "New test",
  removed: "Removed",
  newly_skipped: "Newly skipped",
  flaky: "Flaky",
  quarantined: "Quarantined",
  not_comparable: "Not comparable",
  not_run: "Not run on head",
  not_compared: "Head only",
  unchanged: "Unchanged",
};

const CLASS_TONE: Record<string, string> = {
  introduced_failure: "text-rose-300",
  flaky: "text-amber-300",
  not_comparable: "text-amber-300",
  not_run: "text-amber-300",
  removed: "text-amber-300",
  newly_skipped: "text-amber-300",
  fixed: "text-emerald-300",
  new: "text-sky-300",
};

const VERDICT_TONE: Record<string, string> = {
  PASS: "text-emerald-300",
  FAIL: "text-rose-300",
  UNKNOWN: "text-amber-300",
  NOT_RUN: "text-amber-300",
};

const VERDICT_LABEL: Record<string, string> = { PASS: "Passed", FAIL: "Failed", UNKNOWN: "Unknown", NOT_RUN: "Not run" };

const CLAIM_LABEL: Record<string, string> = {
  confirmed: "Confirmed",
  contradicted: "Contradicted",
  not_reproduced: "Not reproduced",
};

/** Counted, never raised: they are context, not deltas of this Change. */
const QUIET_CLASSES = ["pre_existing_failure", "quarantined", "unchanged"];

function marks(unit: CheckUnit): string {
  const base = unit.base_attempts.join(" ") || unit.base;
  const head = unit.head_attempts.join(" ") || unit.head;
  return `${base || "—"} → ${head || "—"}`;
}

/** Where a reused attempt actually ran: an earlier Snapshot or another Review's shared base run. */
function provenance(attempt: CheckAttempt): string {
  const origin = attempt.reused_from;
  if (!origin) return "";
  const side = attempt.side === "old" ? "Base" : "Head";
  const took = origin.duration_ms > 0 ? ` (${Math.round(origin.duration_ms / 100) / 10} s not re-run)` : "";
  return origin.kind === "snapshot"
    ? `${side} reused from Snapshot ${origin.snapshot}: inputs unchanged${took}`
    : `${side} shared from another Review on the same base, environment and recipe${took}`;
}

/** Test-impact selection and the on-demand base, as the run applied them. */
function EconomyLines({ result }: { result: CheckDifferential }) {
  const selection = result.economy?.selection;
  const base = result.economy?.base;
  const reused = result.attempts.map(provenance).filter((line) => line !== "");
  if (!selection && !base && reused.length === 0) return null;
  return (
    <div className="text-micro leading-relaxed text-muted-foreground" data-testid="check-economy">
      {selection && (
        <div data-testid="check-economy-selection" title={(selection.tests ?? []).join("\n")}>
          {selection.mode === "impact"
            ? `Ran ${selection.selected} of ${selection.total} test files selected by test impact: ${selection.reason}`
            : selection.reason}
        </div>
      )}
      {base && <div data-testid="check-economy-base">{base.reason}</div>}
      {[...new Set(reused)].map((line) => (
        <div key={line} data-testid="check-economy-reused">
          {line}
        </div>
      ))}
    </div>
  );
}

function UnitRow({
  unit,
  recipeTitle,
  onProof,
}: {
  unit: CheckUnit;
  recipeTitle: string;
  onProof?: (unit: CheckUnit) => void;
}) {
  return (
    <div className="flex items-baseline gap-2 text-micro" data-testid={`check-unit-${unit.subject || "recipe"}`}>
      <span className={`shrink-0 ${CLASS_TONE[unit.class] ?? "text-muted-foreground"}`}>{CLASS_LABEL[unit.class] ?? unit.class}</span>
      <span className="min-w-0 flex-1 truncate font-mono text-neutral-300" title={unit.subject || recipeTitle}>
        {unit.subject || `${recipeTitle} (whole recipe)`}
      </span>
      <span className="shrink-0 font-mono text-muted-foreground">{marks(unit)}</span>
      {onProof && (
        <UiButton type="button" size="xs" variant="ghost" onClick={() => onProof(unit)}>
          Request proof
        </UiButton>
      )}
    </div>
  );
}

/** Gate H: an unqualified result is advisory (enforce) or would be (report); a qualified one says nothing. */
function QualificationLine({ qualification }: { qualification?: CheckQualification }) {
  if (!qualification || !qualification.would_be_advisory) return null;
  const enforced = qualification.mode === "enforce" && qualification.advisory;
  return (
    <div
      className={`text-micro leading-relaxed ${enforced ? "text-amber-300/80" : "text-muted-foreground"}`}
      data-testid="check-qualification"
      title={qualification.subject.environment_class}
    >
      {enforced ? "Advisory, not counted for readiness" : "Would be advisory under Gate H enforcement"} (
      {qualification.state}): {qualification.detail}
    </div>
  );
}

function DifferentialCard({
  result,
  editable,
  onProof,
  onSendAutofix,
}: {
  result: CheckDifferential;
  editable: boolean;
  onProof: (result: CheckDifferential, subject: string, file: string) => void;
  onSendAutofix: (proposalId: string) => void;
}) {
  const title = result.recipe.title || result.recipe.id;
  const quiet = QUIET_CLASSES.map((klass) => [klass, result.counts[klass] ?? 0] as const).filter(([, count]) => count > 0);
  const flagged = result.adequacy.filter((row) => row.verdict === "not_demonstrating");
  const proof = editable
    ? (unit: CheckUnit) => onProof(result, unit.subject, unit.file)
    : undefined;
  return (
    <div className="border-t border-border/60 py-2 first:border-t-0" data-testid={`check-result-${result.recipe.id}`}>
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-label text-neutral-200" title={result.recipe.command.join(" ")}>
          {title}
        </span>
        <span className={`text-caption ${VERDICT_TONE[result.head_verdict] ?? "text-muted-foreground"}`}>
          {VERDICT_LABEL[result.head_verdict] ?? result.head_verdict}
        </span>
      </div>
      <div className="mt-0.5 flex flex-wrap gap-x-2 text-micro text-muted-foreground">
        <span>{result.sides.includes("old") ? "base vs head" : "head only"}</span>
        <span>{result.granularity === "test" ? `${result.tests_total} tests` : "per recipe"}</span>
        {result.executor_class && <span>{result.executor_class.replace("_", " ")}</span>}
        {result.reruns.limited_by_budget && <span className="text-amber-300/80">reruns limited by budget</span>}
      </div>
      {result.comparable === false && (
        <div className="text-micro text-amber-300/80" data-testid="check-not-comparable">
          Not comparable: environments differ ({result.environment.differences.join(", ") || "fingerprint"})
        </div>
      )}
      {result.head_guard && <div className="text-micro text-amber-300/80">{result.head_guard}</div>}
      {result.reasons[0] && <div className="text-micro leading-relaxed text-muted-foreground">{result.reasons[0]}</div>}
      <QualificationLine qualification={result.qualification} />
      <EconomyLines result={result} />
      {result.attention.length > 0 && (
        <div className="mt-1 space-y-0.5">
          {result.attention.map((unit) => (
            <UnitRow
              key={`${unit.subject}:${unit.class}`}
              unit={unit}
              recipeTitle={title}
              onProof={unit.class === "introduced_failure" || unit.class === "new" || unit.class === "fixed" ? proof : undefined}
            />
          ))}
        </div>
      )}
      {quiet.length > 0 && (
        <div className="mt-1 text-micro text-muted-foreground" data-testid="check-quiet">
          {quiet.map(([klass, count]) => `${count} ${CLASS_LABEL[klass]?.toLowerCase() ?? klass}`).join(" · ")}
        </div>
      )}
      {flagged.map((row) => (
        <div key={row.test} className="mt-1 flex items-baseline gap-2 text-micro" data-testid="check-adequacy">
          <span className="shrink-0 text-amber-300">Doesn't demonstrate the change</span>
          <span className="min-w-0 flex-1 truncate font-mono text-neutral-300" title={row.reason}>{row.test}</span>
          {proof && (
            <UiButton type="button" size="xs" variant="ghost" onClick={() => onProof(result, row.test, row.file)}>
              Request proof
            </UiButton>
          )}
        </div>
      ))}
      {result.claims.map((row) => (
        <div key={row.claim.key} className="mt-1 text-micro" data-testid="check-claim">
          <span className={row.verdict === "contradicted" ? "text-rose-300" : row.verdict === "confirmed" ? "text-emerald-300" : "text-muted-foreground"}>
            Claim {CLAIM_LABEL[row.verdict]?.toLowerCase() ?? row.verdict}
          </span>{" "}
          <span className="text-muted-foreground">
            · {row.claim.origin === "development" ? "agent" : "caller-attested"} said {row.claim.status} for{" "}
            <span className="font-mono">{row.claim.command}</span>; {row.reason}
          </span>
        </div>
      ))}
      {result.autofix.status && (
        <div className="mt-1 text-micro text-muted-foreground" data-testid="check-autofix">
          Autofix: {result.autofix.reason ?? result.autofix.status}
          {editable &&
            (result.autofix.proposal_ids ?? []).map((id) => (
              <UiButton key={id} type="button" size="xs" variant="outline" className="ml-2" onClick={() => onSendAutofix(id)}>
                Send to agent
              </UiButton>
            ))}
        </div>
      )}
    </div>
  );
}

function EntryAction({
  entry,
  busy,
  onRun,
}: {
  entry: CheckEntryRow;
  busy: boolean;
  onRun: (entry: string, pick: boolean) => void;
}) {
  if (entry.recipes.length === 0) return null;
  if (entry.status === "run") {
    return (
      <UiButton type="button" size="xs" variant="outline" disabled={busy} onClick={() => onRun(entry.id, false)}>
        Run
      </UiButton>
    );
  }
  if (entry.status === "on_request" || entry.status === "skipped" || entry.status === "not_planned") {
    return (
      <UiButton type="button" size="xs" variant="ghost" disabled={busy} onClick={() => onRun(entry.id, true)}>
        Add &amp; run
      </UiButton>
    );
  }
  return null;
}

/**
 * Trusted-base check recipes: planned sandboxed runs, the base-vs-head
 * differential (only deltas raised), claim reconciliation, test adequacy and
 * autofix proposals. Self-contained so it mounts in the Checks rail.
 */
export default function CheckRecipesPanel({
  reviewId,
  revisionId = "",
  readOnly = false,
}: {
  reviewId: string;
  revisionId?: string;
  readOnly?: boolean;
}) {
  const [data, setData] = useState<CheckRecipesProjection | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const load = useCallback(
    () =>
      fetchCheckRecipes(reviewId, revisionId).then(setData, (reason: unknown) =>
        setError(reason instanceof Error ? reason.message : "check recipes unavailable"),
      ),
    [reviewId, revisionId],
  );

  useEffect(() => {
    let cancelled = false;
    setError("");
    fetchCheckRecipes(reviewId, revisionId)
      .then((projection) => {
        if (!cancelled) setData(projection);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "check recipes unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, [reviewId, revisionId]);

  if (error) {
    return (
      <PanelCard title="Check recipes">
        <div className="text-caption text-muted-foreground">Check recipes unavailable: {error}</div>
      </PanelCard>
    );
  }
  if (!data) {
    return (
      <PanelCard title="Check recipes">
        <div className="text-caption text-muted-foreground">Loading check recipes…</div>
      </PanelCard>
    );
  }

  const snapshot = data;
  const editable = !readOnly && !snapshot.historical;
  const entries = snapshot.entries.filter((row) => row.recipes.length > 0);

  async function act(work: () => Promise<string>) {
    setBusy(true);
    setMessage("");
    try {
      setMessage(await work());
      await load();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "request failed");
    } finally {
      setBusy(false);
    }
  }

  function run(entry: string, pick: boolean) {
    void act(async () => {
      const response = await postCheckRecipesRun(reviewId, snapshot.revision_id, entry, pick);
      const verdict = response.verdicts[entry] ?? Object.values(response.verdicts)[0] ?? "NOT_RUN";
      const economy = [
        response.shared_base ? `${response.shared_base} base shared` : "",
        response.carried ? `${response.carried} carried forward` : "",
      ].filter((part) => part !== "");
      return `${entry}: ${VERDICT_LABEL[verdict] ?? verdict} (${response.executions} run, ${response.reused} reused${
        economy.length > 0 ? `, ${economy.join(", ")}` : ""
      })`;
    });
  }

  function requestProof(result: CheckDifferential, subject: string, path: string) {
    void act(async () => {
      await postCheckRequest(reviewId, snapshot.revision_id, { kind: "proof", recipe_id: result.recipe.id, subject, path });
      return "Proof requested as feedback for the agent";
    });
  }

  function sendAutofix(proposalId: string) {
    void act(async () => {
      await postCheckRequest(reviewId, snapshot.revision_id, { kind: "autofix", proposal_id: proposalId });
      return "Autofix proposal sent as feedback for the agent";
    });
  }

  return (
    <PanelCard
      title="Check recipes"
      action={<span className="font-mono text-caption text-muted-foreground">{snapshot.recipes.length}</span>}
    >
      <div className="space-y-2" data-testid="check-recipes-panel">
        {snapshot.recipes.length === 0 ? (
          <div className="text-caption text-muted-foreground">
            {snapshot.base_state === "provisional"
              ? "The trusted base is not readable yet, so no recipe is resolved."
              : "No check recipe in the trusted base. Add check_recipes to .lemoncrow/review.yaml."}
          </div>
        ) : (
          <div>
            {entries.map((entry) => (
              <div key={entry.id} className="flex items-baseline gap-2 py-0.5 text-micro" data-testid={`check-entry-${entry.id}`}>
                <span className="min-w-0 flex-1 truncate font-mono text-neutral-300" title={entry.reason}>
                  {entry.id}
                </span>
                <span className="text-muted-foreground">{entry.recipes.join(", ")}</span>
                <span className="text-muted-foreground">{entry.status.replaceAll("_", " ")}</span>
                {entry.base_on_demand && <span className="text-muted-foreground">head first</span>}
                {editable && <EntryAction entry={entry} busy={busy} onRun={run} />}
              </div>
            ))}
          </div>
        )}
        {snapshot.future_policy.changed && (
          <div className="text-micro leading-relaxed text-amber-300/80" data-testid="check-future-policy">
            This Change edits check recipes. Its edits are proposed policy for {snapshot.future_policy.applies_to}; they never run here.
          </div>
        )}
        {snapshot.errors.map((row) => (
          <div key={row} className="text-micro text-rose-300/80">{row}</div>
        ))}
        {snapshot.results.map((result) => (
          <DifferentialCard key={result.recipe.id} result={result} editable={editable} onProof={requestProof} onSendAutofix={sendAutofix} />
        ))}
        {message && <div className="text-micro text-neutral-300" role="status">{message}</div>}
      </div>
    </PanelCard>
  );
}
