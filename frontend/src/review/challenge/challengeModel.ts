// Challenge This Claim (N04): "is this explanation or finding actually true?"
// A pure chain over data the Reader already holds or reads through existing
// endpoints: claim -> source rationale -> relevant checks -> reproduction
// candidate -> actual observed result. Nothing here fetches, runs or asks a
// model. A reproduction is offered only through the existing check-recipe
// executor, only on explicit consent, and its outcome (pass/fail/not_run/
// unknown) stays separate from what it says about the claim. Evidence can
// falsify a claim; nothing here approves a Review. Claim text is data: it is
// quoted, never followed, and never changes what would run.
import type { AssumptionRow } from "../assumptions/assumptionsModel";
import type { BlindSpotItem } from "../blindSpots/blindSpotsModel";
import type { CheckDifferential, CheckRecipesProjection, CheckRecommendation, CheckVerdict } from "../checkRecipesApi";
import type { InvestigationCheckStatus, InvestigationContext, RelationshipProof } from "../investigation/investigationContext";
import type { RunPlanProjection } from "../runPlanApi";
import type { StaticFinding } from "../staticFindingsApi";
import type { ReviewProjection, ReviewTarget } from "../types";
import type { WhatIfModel, WhatIfRow } from "../whatIf/whatIfModel";
import type { XRay, XRayStep } from "../xray/xrayModel";

export type ChallengeOrigin = "blind_spot" | "assumption" | "what_if" | "xray";
/** `asserts`: the claim says something holds. `alleges`: it says something is wrong. */
export type ClaimStance = "asserts" | "alleges";
export type ChallengeOutcome = "supported" | "contradicted" | "inconclusive" | "could_not_run";
export type CheckOutcome = "pass" | "fail" | "not_run" | "unknown";

export const OUTCOME_LABEL: Record<ChallengeOutcome, string> = {
  supported: "Supported",
  contradicted: "Contradicted",
  inconclusive: "Inconclusive",
  could_not_run: "Could not run",
};

export const CHECK_OUTCOME_LABEL: Record<CheckOutcome, string> = {
  pass: "pass",
  fail: "fail",
  not_run: "not run",
  unknown: "unknown",
};

/** Claim text past this is cut with a count, never silently. */
export const MAX_CLAIM_TEXT = 2000;
export const PROOF_CHECK = "LemonCrow Proof";
const PROOF_TOOL = "lemoncrow-proof";

export interface ChallengeSource {
  label: string;
  detail: string;
  path: string;
  line: number;
  /** The changed target holding it, when one is loaded. */
  targetId: string | null;
  proof: RelationshipProof;
}

export interface ChallengeClaim {
  origin: ChallengeOrigin;
  /** The owning record's id on its surface (blind spot item, assumption row, What-If row, X-Ray step). */
  ref: string;
  /** Verbatim, bounded. Data, never an instruction. */
  text: string;
  textOmitted: number;
  saidBy: string;
  modelSuggested: boolean;
  stance: ClaimStance;
  /** A What-If overlay: not a fact about the current code. */
  hypothetical: boolean;
  targetIds: string[];
  /** Checks the claim's own record names (explicit mapping only). */
  namedChecks: string[];
  sources: ChallengeSource[];
  /** The Snapshot the claim was read on. */
  snapshotIdentity: string;
  /** An observation the subject already carries: an X-Ray link a recorded run traced. */
  trace: { evidenceRef: string; detail: string } | null;
}

export type Loadable<T> =
  | { state: "loading" }
  | { state: "ready"; value: T }
  | { state: "unavailable"; reason: string };

export interface ChallengeCheck {
  key: string;
  outcome: CheckOutcome;
  relation: "named_by_claim" | "same_code";
  current: boolean;
  snapshotNumber: number | null;
}

export interface ChallengeCommand {
  entryId: string;
  recipeId: string;
  title: string;
  command: string[];
  cwd: string;
  executor: string;
  recipeDigest: string;
  runPlanDigest: string;
  /** `named_by_claim` can support or contradict; `covers_source` only exercises the same code. */
  relation: "named_by_claim" | "covers_source";
  scope: string;
  selectedTests: string[];
  authorization: string;
  cost: string;
}

export type ChallengeCandidate =
  | { kind: "loading" }
  | { kind: "runnable"; command: ChallengeCommand }
  | { kind: "inspect_only"; reason: string };

export interface ChallengeObservation {
  outcome: ChallengeOutcome;
  /** The check's own result, kept apart from what it says about the claim; null when no check produced it. */
  checkOutcome: CheckOutcome | null;
  basis: "consented_run" | "recorded_proof" | "recorded_trace" | "none";
  reason: string;
  evidenceId: string;
  snapshotIdentity: string;
  /** Recorded for a Snapshot the Review no longer shows. */
  stale: boolean;
}

/** One consented run of a candidate, as the existing executor answered it. */
export interface ChallengeRun {
  claimDigest: string;
  snapshotIdentity: string;
  revisionId: string;
  command: ChallengeCommand;
  /** The Snapshot the executor says it ran on; "" when it refused before running. */
  responseRevisionId: string;
  verdict: CheckVerdict | "";
  differential: CheckDifferential | null;
  /** The executor's refusal or failure, verbatim; "" when it answered. */
  error: string;
  notes: string[];
}

export interface ChallengeChain {
  claim: ChallengeClaim;
  digest: string;
  /** The Snapshot the Review shows now. */
  currentIdentity: string;
  stale: boolean;
  checks: ChallengeCheck[];
  /** Snapshot-wide checks not proven to touch this claim, counted only. */
  reviewLevelChecks: number;
  candidate: ChallengeCandidate;
  observed: ChallengeObservation | null;
}

// ---------- claims from each surface ----------

function bounded(text: string): { text: string; omitted: number } {
  return text.length > MAX_CLAIM_TEXT
    ? { text: text.slice(0, MAX_CLAIM_TEXT), omitted: text.length - MAX_CLAIM_TEXT }
    : { text, omitted: 0 };
}

function targetSources(ids: readonly string[], targets: readonly ReviewTarget[], label: string, detail: string, proof: RelationshipProof): ChallengeSource[] {
  const byId = new Map(targets.map((target) => [target.target_id, target]));
  return ids.map((id) => byId.get(id)).filter((target): target is ReviewTarget => Boolean(target)).map((target) => ({
    label,
    detail: detail || (target.symbol ? `${target.path} · ${target.symbol}` : target.path),
    path: target.path,
    line: target.start_line,
    targetId: target.target_id,
    proof,
  }));
}

function claim(base: Omit<ChallengeClaim, "text" | "textOmitted"> & { text: string }): ChallengeClaim | null {
  // No Challenge without at least a claim and a source to inspect.
  if (!base.text.trim() || base.sources.length === 0) return null;
  const { text, omitted } = bounded(base.text);
  return { ...base, text, textOmitted: omitted };
}

const AI_SOURCE = /\b(ai|model|llm|synthesis)\b/i;

/** A Blind Spots row that is a claim or a hypothesis; other rows are gaps, not claims. */
export function claimFromBlindSpot(
  item: BlindSpotItem,
  projection: ReviewProjection,
  targets: readonly ReviewTarget[],
  snapshotIdentity: string,
): ChallengeClaim | null {
  if (item.category !== "untested_hypotheses") return null;
  const [, kind = "", ...rest] = item.id.split(":");
  const ref = rest.join(":");
  if (kind === "claim") {
    const row = projection.claims.find((entry) => entry.id === ref);
    if (!row) return null;
    return claim({
      origin: "blind_spot",
      ref: item.id,
      text: row.statement || item.title,
      saidBy: row.source ? `Claim · ${row.source}` : "Claim",
      modelSuggested: AI_SOURCE.test(row.source),
      stance: "asserts",
      hypothetical: false,
      targetIds: [...row.target_ids],
      namedChecks: row.evidence.filter((evidence) => evidence.mapping === "explicit").map((evidence) => evidence.key || evidence.title).filter(Boolean),
      sources: targetSources(row.target_ids, targets, "Claimed about", "", row.summary === "inferred" || row.summary === "inferred_failing" ? "inferred" : "snapshot"),
      snapshotIdentity,
      trace: null,
    });
  }
  if (kind === "question") {
    const row = projection.questions.find((entry) => entry.id === ref);
    if (!row) return null;
    const why = [row.detail, row.reason].filter(Boolean).join(" · ");
    return claim({
      origin: "blind_spot",
      ref: item.id,
      text: row.title,
      saidBy: row.origin === "ai" ? "AI hypothesis · unverified" : "Open question · LemonCrow",
      modelSuggested: row.origin === "ai",
      stance: "alleges",
      hypothetical: false,
      targetIds: [...row.target_ids],
      namedChecks: [],
      sources: [
        ...targetSources(row.target_ids, targets, "Raised about", "", "snapshot"),
        ...(why && row.target_ids.length > 0 ? [{ label: "Why it was raised", detail: why, path: "", line: 0, targetId: null, proof: "inferred" as const }] : []),
      ],
      snapshotIdentity,
      trace: null,
    });
  }
  return null;
}

export function claimFromAssumption(row: AssumptionRow, snapshotIdentity: string): ChallengeClaim | null {
  const origin = row.origins[0];
  return claim({
    origin: "assumption",
    ref: row.id,
    text: row.hypothesis,
    saidBy: origin === "model_suggested" ? "AI suggestion · unverified" : origin === "author_claim" ? `Author claim · ${row.signal}` : row.signal,
    modelSuggested: origin === "model_suggested",
    stance: "asserts",
    hypothetical: false,
    targetIds: [...row.targetIds],
    namedChecks: row.support.filter((support) => support.mapping === "explicit").map((support) => support.key),
    sources: [
      // The record that produced the assumption is its rationale, even when no code span is loaded.
      ...(row.signal || row.detail ? [{ label: "Signal", detail: [row.signal, row.detail].filter(Boolean).join(" · "), path: "", line: 0, targetId: null, proof: "inferred" as const }] : []),
      ...row.sources
        .filter((source) => source.snapshot_identity === snapshotIdentity)
        .map((source) => ({
          label: "Relied on in",
          detail: source.symbol ? `${source.path} · ${source.symbol}` : source.path,
          path: source.path,
          line: source.start_line,
          targetId: source.target_id,
          proof: "snapshot" as const,
        })),
    ],
    snapshotIdentity,
    trace: null,
  });
}

export function claimFromWhatIf(row: WhatIfRow, model: WhatIfModel, targetLabel: string): ChallengeClaim | null {
  const where = row.line > 0 ? `${row.path}:${row.line}` : row.path;
  return claim({
    origin: "what_if",
    ref: `${model.targetId}|${model.scenario.key}|${row.id}`,
    text: `If ${targetLabel}: ${model.scenario.label.toLowerCase()}, ${row.symbol || where} ${model.scenario.effect}.`,
    saidBy: "What-If overlay · simulated, not current code",
    modelSuggested: false,
    stance: "alleges",
    hypothetical: true,
    targetIds: [model.targetId, ...(row.openTargetId ? [row.openTargetId] : [])],
    namedChecks: [],
    sources: [{
      label: row.kind === "caller" ? "Unchanged caller" : row.kind === "test" ? "Test" : "Consumer",
      detail: [row.reason, row.uncertainty ? `Candidate: ${row.uncertainty}` : ""].filter(Boolean).join(" · "),
      path: row.path,
      line: row.line,
      targetId: row.openTargetId,
      proof: row.exactness === "snapshot" ? "snapshot" : row.exactness === "current_index" ? "current_index" : "unknown",
    }],
    snapshotIdentity: model.snapshotIdentity,
    trace: null,
  });
}

export function claimFromXRayStep(step: XRayStep, xray: XRay, targets: readonly ReviewTarget[]): ChallengeClaim | null {
  if (!step.path) return null;
  const link = step.link;
  const sources: ChallengeSource[] = [{
    label: step.roleLabel,
    detail: [step.exactnessLabel, link?.orderLabel ?? ""].filter(Boolean).join(" · "),
    path: step.path,
    line: step.line,
    targetId: null,
    proof: step.exactness,
  }];
  if (link?.site && link.site.path) {
    sources.push({ label: "Relationship site", detail: link.exactnessLabel, path: link.site.path, line: link.site.line, targetId: null, proof: link.exactness });
  }
  return claim({
    origin: "xray",
    ref: `${xray.rootId}|${xray.side}|${step.key}`,
    text: link?.sentence || `${step.roleLabel}: ${step.title}`,
    saidBy: `Behavioral X-Ray · ${xray.side === "before" ? "Before" : "After"}`,
    modelSuggested: false,
    stance: "asserts",
    hypothetical: false,
    targetIds: targets.filter((target) => target.path === step.path).map((target) => target.target_id),
    namedChecks: [],
    sources,
    snapshotIdentity: xray.snapshot,
    trace: link && link.order === "observed" && link.trace && xray.side === "after"
      ? { evidenceRef: link.trace.evidenceRef || link.trace.recordId, detail: `A recorded run on this Snapshot called ${link.trace.callerFn} → ${link.trace.calleeFn}${link.trace.recipe ? ` (${link.trace.recipe})` : ""}.` }
      : null,
  });
}

// ---------- the chain ----------

/**
 * Stable identity of what is challenged: surface, record, words, sources and
 * named checks; not the Snapshot. Compared for equality in this tab only, so the
 * canonical form itself is the identity (claim text is already bounded).
 */
export function claimDigest(value: ChallengeClaim): string {
  return `claim:${JSON.stringify([
    value.origin,
    value.ref,
    value.text,
    value.textOmitted,
    value.stance,
    [...value.targetIds].sort(),
    [...value.namedChecks].sort(),
    value.sources.map((source) => [source.path, source.line, source.label]),
  ])}`;
}

function lower(status: InvestigationCheckStatus | CheckVerdict | string): CheckOutcome {
  const value = String(status).toUpperCase();
  return value === "PASS" ? "pass" : value === "FAIL" ? "fail" : value === "NOT_RUN" ? "not_run" : "unknown";
}

function named(value: ChallengeClaim, keys: readonly string[]): boolean {
  const wanted = new Set(value.namedChecks.map((key) => key.trim().toLowerCase()).filter(Boolean));
  return keys.some((key) => key && wanted.has(key.trim().toLowerCase()));
}

function globMatch(pattern: string, path: string): boolean {
  if (!pattern) return false;
  if (pattern === path) return true;
  if (pattern.endsWith("/")) return path.startsWith(pattern);
  if (!pattern.includes("*")) return false;
  const source = pattern.split("**").map((part) => part.split("*").map((piece) => piece.replace(/[.+?^${}()|[\]\\]/g, "\\$&")).join("[^/]*")).join(".*");
  return new RegExp(`^${source}$`).test(path);
}

function relevantChecks(value: ChallengeClaim, context: InvestigationContext): { checks: ChallengeCheck[]; reviewLevel: number } {
  const scope = new Set(value.targetIds);
  const checks: ChallengeCheck[] = [];
  let reviewLevel = 0;
  for (const check of context.checks) {
    const byName = named(value, [check.key]);
    const sameCode = check.target_ids.some((id) => scope.has(id));
    if (!byName && !sameCode) {
      if (check.target_ids.length === 0) reviewLevel += 1;
      continue;
    }
    checks.push({
      key: check.key,
      outcome: lower(check.status),
      relation: byName ? "named_by_claim" : "same_code",
      current: check.current,
      snapshotNumber: check.snapshot_number,
    });
  }
  return { checks, reviewLevel };
}

// lc-debt: claim-to-check matching is by name (recipe/entry id or title) or by run-plan path globs and the
// selected-test sample; a server-side claim->check link in the run plan would replace this heuristic.
function pickRecommendation(
  value: ChallengeClaim,
  recipes: CheckRecipesProjection,
  plan: RunPlanProjection | null,
): { rec: CheckRecommendation; relation: ChallengeCommand["relation"] } | null {
  const recs = recipes.recommended_checks?.recommendations ?? [];
  const byName = recs.find((rec) => named(value, [rec.recipe_id, rec.entry_id, rec.title, `${rec.entry_id}:${rec.recipe_id}`]));
  if (byName) return { rec: byName, relation: "named_by_claim" };
  const paths = value.sources.map((source) => source.path).filter(Boolean);
  const covers = recs.find((rec) => {
    const entry = plan?.plan.entries.find((row) => row.id === rec.entry_id);
    return paths.some((path) => (entry?.paths ?? []).some((pattern) => globMatch(pattern, path)) || rec.selected_tests_sample.includes(path));
  });
  return covers ? { rec: covers, relation: "covers_source" } : null;
}

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

function candidateFor(
  value: ChallengeClaim,
  context: InvestigationContext,
  input: Pick<ChallengeInput, "recipes" | "runPlan" | "canRun">,
  stale: boolean,
): ChallengeCandidate {
  const none = (reason: string): ChallengeCandidate => ({ kind: "inspect_only", reason });
  if (stale) return none("This claim was read on another Snapshot. Challenge it again on the Snapshot shown now.");
  if (value.hypothetical) return none("A hypothetical is not current code: a check would exercise the code as it is, not the change imagined here.");
  if (context.historical) return none("This is an earlier Snapshot; checks run only on the current one.");
  if (!input.canRun) return none("This view is read-only, so no check can be requested from it.");
  if (input.recipes.state === "loading") return { kind: "loading" };
  if (input.recipes.state === "unavailable") return none(`Check recipes are unavailable: ${input.recipes.reason}`);
  const recipes = input.recipes.value;
  if (recipes.revision_id !== context.revision_id) return none("Refused: the check plan returned is for a different Snapshot.");
  if (recipes.historical) return none("This is an earlier Snapshot; checks run only on the current one.");
  if (recipes.recipes.length === 0) {
    return none(recipes.base_state === "provisional"
      ? "The trusted base is not readable yet, so no check recipe is resolved."
      : "No check recipe in the trusted base, so there is no bounded reproduction to run.");
  }
  const plan = input.runPlan.state === "ready" && input.runPlan.value.revision_id === context.revision_id ? input.runPlan.value : null;
  const picked = pickRecommendation(value, recipes, plan);
  if (!picked) return none("No check in this Snapshot's run plan names this claim or covers its source.");
  const { rec, relation } = picked;
  if (!rec.can_run_now) return none(`${rec.title} is not runnable now: ${rec.reason || rec.run_plan_status.replaceAll("_", " ")}.`);
  const recipe = recipes.recipes.find((row) => row.id === rec.recipe_id);
  if (!recipe) return none(`${rec.title}: its recipe is not in the trusted base.`);
  if (rec.recipe_digest && recipe.digest !== rec.recipe_digest) return none(`Refused: ${rec.title}'s trusted-base recipe changed since the run plan suggested it.`);
  const entry = plan?.plan.entries.find((row) => row.id === rec.entry_id) ?? null;
  const cost = entry
    ? [
      `about ${entry.estimated_minutes} compute ${entry.estimated_minutes === 1 ? "minute" : "minutes"}`,
      plural(entry.estimated_model_calls, "model call"),
      entry.cost_class ? `cost class ${entry.cost_class}` : "",
      plan?.plan.budget.limited ? "this Review's budget is already limited" : "",
    ].filter(Boolean).join(" · ")
    : input.runPlan.state === "unavailable" ? `Not estimated: the run plan is unavailable (${input.runPlan.reason}).` : "Not estimated by the run plan.";
  const scope = rec.selection_mode === "impact"
    ? `${plural(rec.selected_test_count, "test file")} selected by test impact`
    : rec.selection_mode === "all" ? "The whole recipe" : "As the run plan schedules it";
  return {
    kind: "runnable",
    command: {
      entryId: rec.entry_id,
      recipeId: rec.recipe_id,
      title: rec.title,
      command: [...recipe.command],
      cwd: recipe.cwd || ".",
      executor: rec.executor || entry?.executor || "run plan executor",
      recipeDigest: recipe.digest,
      runPlanDigest: recipes.run_plan_digest,
      relation,
      scope: `${scope}${rec.coverage === "unknown" ? " · test-impact coverage unknown" : ""}${entry?.sides ? ` · sides: ${entry.sides}` : ""}`,
      selectedTests: rec.selected_tests_sample.slice(0, 10),
      authorization: [
        `Runs only when you confirm, on Snapshot ${context.snapshot_number ?? "?"}, through this Review's existing check-recipe executor.`,
        "The server re-checks the current Snapshot, run-plan admission, the trusted-base recipe digest and the compute budget, and refuses otherwise. Nothing is retried.",
        rec.manual_authorization_required ? "This entry needs manual authorization: confirming here is that authorization." : "",
      ].filter(Boolean).join(" "),
      cost,
    },
  };
}

/** What a consented run says about the claim; the check's own result is reported beside it, never folded in. */
export function observationFromRun(run: ChallengeRun, value: ChallengeClaim, currentIdentity: string): ChallengeObservation {
  const base = { basis: "consented_run" as const, evidenceId: run.differential?.evidence_id ?? "", snapshotIdentity: run.snapshotIdentity, stale: run.snapshotIdentity !== currentIdentity };
  if (run.error) return { ...base, outcome: "could_not_run", checkOutcome: null, reason: `The executor refused or failed: ${run.error}` };
  if (run.responseRevisionId !== run.revisionId) {
    return { ...base, outcome: "could_not_run", checkOutcome: null, evidenceId: "", reason: "Refused: the executor answered for a different Snapshot, so its result is not used." };
  }
  const checkOutcome = run.verdict ? lower(run.verdict) : "unknown";
  const result = run.differential;
  const attempts = result?.attempts ?? [];
  const timedOut = attempts.some((attempt) => /time[d ]?\s*out/i.test(attempt.reason));
  if (checkOutcome === "not_run") {
    const why = attempts.find((attempt) => attempt.status === "not_run" && attempt.reason)?.reason || run.notes[0] || "the executor recorded no run";
    return { ...base, outcome: "could_not_run", checkOutcome, reason: `The check did not run: ${why}.` };
  }
  if (checkOutcome === "unknown") return { ...base, outcome: "inconclusive", checkOutcome, reason: "The check recorded no result." };
  if (timedOut) return { ...base, outcome: "inconclusive", checkOutcome, reason: "A run timed out, so its result does not decide the claim." };
  const unit = result?.recipe_unit.class ?? "";
  if (unit === "flaky" || (result?.counts.flaky ?? 0) > 0) {
    return { ...base, outcome: "inconclusive", checkOutcome, reason: "The result changed between attempts (flaky), so it does not decide the claim." };
  }
  if (run.command.relation !== "named_by_claim") {
    return { ...base, outcome: "inconclusive", checkOutcome, reason: "This check exercises the claim's source, but the claim does not name it, so its result is not a verdict on the claim." };
  }
  if (value.stance === "asserts") {
    return checkOutcome === "pass"
      ? { ...base, outcome: "supported", checkOutcome, reason: "The check this claim names passed on this Snapshot." }
      : { ...base, outcome: "contradicted", checkOutcome, reason: "The check this claim names failed on this Snapshot." };
  }
  if (checkOutcome === "pass") return { ...base, outcome: "contradicted", checkOutcome, reason: "The check this claim names passed on this Snapshot, so the problem it alleges did not reproduce." };
  if (unit === "introduced_failure" || unit === "new" || result?.base_verdict === "PASS") {
    return { ...base, outcome: "supported", checkOutcome, reason: "The check this claim names fails on this Snapshot and passes on the base (or is new)." };
  }
  if (result?.base_verdict === "FAIL" || unit === "pre_existing_failure") {
    return { ...base, outcome: "inconclusive", checkOutcome, reason: "The check fails on the base too, so the failure is not shown to come from this change." };
  }
  return { ...base, outcome: "inconclusive", checkOutcome, reason: "The check fails on this Snapshot, but the base was not compared." };
}

const PROOF_OUTCOME = /\bProof:\s*(supported|contradicted|inconclusive|could_not_run)\b/i;

/** A `LemonCrow Proof` reproduction recorded on this exact Snapshot at the claim's own source. */
function recordedProof(value: ChallengeClaim, proofs: readonly StaticFinding[], context: InvestigationContext, targets: ReadonlyMap<string, { start: number; end: number }>): ChallengeObservation | null {
  for (const finding of proofs) {
    if (finding.tool !== PROOF_TOOL && finding.check !== PROOF_CHECK) continue;
    if (finding.state !== "current" || finding.snapshot_id !== context.revision_id) continue;
    const at = value.sources.find((source) => {
      if (!source.path || source.path !== finding.path) return false;
      const span = source.targetId ? targets.get(source.targetId) : null;
      return span ? finding.start_line >= span.start && finding.start_line <= span.end : source.line === finding.start_line;
    });
    if (!at) continue;
    const match = PROOF_OUTCOME.exec(`${finding.rule_id} ${finding.message}`);
    if (!match) continue;
    const base = { basis: "recorded_proof" as const, evidenceId: finding.evidence_id, snapshotIdentity: context.snapshot_identity, stale: false };
    if (context.head_sha && finding.head_sha && finding.head_sha !== context.head_sha) {
      return { ...base, outcome: "could_not_run", checkOutcome: null, reason: `Refused: the recorded proof ran on commit ${finding.head_sha.slice(0, 12)}, not this Snapshot's head.` };
    }
    return {
      ...base,
      outcome: match[1].toLowerCase() as ChallengeOutcome,
      checkOutcome: lower(finding.check_status),
      reason: `${PROOF_CHECK} at ${finding.path}:${finding.start_line}: ${finding.message}`,
    };
  }
  return null;
}

export interface ChallengeInput {
  claim: ChallengeClaim;
  /** The Review-subject context of the Snapshot shown now. */
  context: InvestigationContext;
  targets: readonly ReviewTarget[];
  recipes: Loadable<CheckRecipesProjection>;
  runPlan: Loadable<RunPlanProjection>;
  proofs: Loadable<readonly StaticFinding[]>;
  /** The session may request a check here (not read-only, not historical). */
  canRun: boolean;
  run?: ChallengeRun | null;
}

/** Build the chain; pure and deterministic for identical inputs. */
export function challengeChain(input: ChallengeInput): ChallengeChain {
  const { claim: value, context } = input;
  const digest = claimDigest(value);
  const currentIdentity = context.snapshot_identity;
  const stale = value.snapshotIdentity !== currentIdentity;
  const { checks, reviewLevel } = relevantChecks(value, context);
  const candidate = candidateFor(value, context, input, stale);
  const spans = new Map(input.targets.map((target) => [target.target_id, { start: target.start_line, end: target.end_line }]));

  let observed: ChallengeObservation | null = null;
  const run = input.run && input.run.claimDigest === digest ? input.run : null;
  if (run) observed = observationFromRun(run, value, currentIdentity);
  else if (!stale && input.proofs.state === "ready") observed = recordedProof(value, input.proofs.value, context, spans);
  if (!observed && !stale && value.trace) {
    observed = {
      outcome: "supported",
      checkOutcome: null,
      basis: "recorded_trace",
      reason: `${value.trace.detail} The trace observed the call, not that it is correct.`,
      evidenceId: value.trace.evidenceRef,
      snapshotIdentity: currentIdentity,
      stale: false,
    };
  }
  if (!observed && candidate.kind === "inspect_only") {
    observed = { outcome: "could_not_run", checkOutcome: null, basis: "none", reason: candidate.reason, evidenceId: "", snapshotIdentity: value.snapshotIdentity, stale };
  }
  return { claim: value, digest, currentIdentity, stale, checks, reviewLevelChecks: reviewLevel, candidate, observed };
}
