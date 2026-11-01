import { describe, expect, it } from "vitest";

import type { BlindSpotItem } from "../blindSpots/blindSpotsModel";
import type { CheckDifferential, CheckVerdict } from "../checkRecipesApi";
import { investigationContext } from "../investigation/investigationContext";
import type { StaticFinding } from "../staticFindingsApi";
import type { ReviewProjection } from "../types";
import type { WhatIfModel, WhatIfRow } from "../whatIf/whatIfModel";
import {
  challengeChain,
  claimDigest,
  claimFromBlindSpot,
  claimFromWhatIf,
  type ChallengeClaim,
  type ChallengeCommand,
  type ChallengeInput,
  type ChallengeRun,
} from "./challengeModel";
import { INJECTION, projection, recipes, RUN_PLAN, TARGETS } from "./fixtures";

function item(kind: string, ref: string, category: BlindSpotItem["category"] = "untested_hypotheses"): BlindSpotItem {
  return { id: `${category}:${kind}:${ref}`, category, title: ref, detail: "", anchor: { level: "review" }, decisionKey: null, actions: [] };
}

function input(claim: ChallengeClaim, overrides: Partial<ChallengeInput> = {}): ChallengeInput {
  const view = overrides.context ? undefined : projection();
  return {
    claim,
    context: overrides.context ?? investigationContext({ projection: view!, targets: TARGETS, checks: [] }),
    targets: TARGETS,
    recipes: { state: "ready", value: recipes() },
    runPlan: { state: "ready", value: RUN_PLAN },
    proofs: { state: "ready", value: [] },
    canRun: true,
    ...overrides,
  };
}

const IDENTITY = "review-fixture@rev-fixture";

function question(ref: string): ChallengeClaim {
  const claim = claimFromBlindSpot(item("question", ref), projection(), TARGETS, IDENTITY);
  if (!claim) throw new Error("no claim");
  return claim;
}

function differential(unit: CheckDifferential["recipe_unit"]["class"], base: CheckVerdict | "" = "", attempts: CheckDifferential["attempts"] = []): CheckDifferential {
  return {
    recipe: { id: "orders-dup", kind: "test", title: "Order duplicate tests", entry_id: "tests", kind_entry: "tests", digest: "recipe:sha256:r1", command: [], cwd: ".", origin: "config" },
    sides: ["base", "head"], granularity: "test", comparable: true, environment: { base: "", head: "", differences: [] }, executor_class: "author_machine",
    head_verdict: "FAIL", base_verdict: base, reasons: [],
    recipe_unit: { subject: "", class: unit, base: "", head: "", base_attempts: [], head_attempts: [], quarantine: "", file: "" },
    tests: [], tests_total: 1, counts: {}, attention: [], attempts, reruns: {}, adequacy: [], claims: [], autofix: {}, head_guard: "",
    evidence_id: "ev-run-1", observed_at: "",
  };
}

function run(claim: ChallengeClaim, command: ChallengeCommand, overrides: Partial<ChallengeRun> = {}): ChallengeRun {
  return {
    claimDigest: claimDigest(claim), snapshotIdentity: IDENTITY, revisionId: "rev-fixture", command,
    responseRevisionId: "rev-fixture", verdict: "FAIL", differential: differential("introduced_failure", "PASS"), error: "", notes: [], ...overrides,
  };
}

function runnable(claim: ChallengeClaim): ChallengeCommand {
  const chain = challengeChain(input(claim));
  if (chain.candidate.kind !== "runnable") throw new Error(`not runnable: ${JSON.stringify(chain.candidate)}`);
  return chain.candidate.command;
}

describe("challengeModel", () => {
  it("offers the duplicate-order hypothesis a bounded check with its command, scope, cost and authorization before anything runs", () => {
    const claim = question("q-dup");
    expect(claim).toMatchObject({ stance: "alleges", modelSuggested: true, saidBy: "AI hypothesis · unverified" });
    expect(claim.sources.map((source) => source.path)).toContain("src/orders/checkout.py");
    const chain = challengeChain(input(claim));
    expect(chain.candidate.kind).toBe("runnable");
    const command = runnable(claim);
    expect(command).toMatchObject({ entryId: "tests", command: ["pytest", "tests/test_checkout.py"], relation: "covers_source" });
    expect(command.cost).toContain("about 2 compute minutes");
    expect(command.authorization).toMatch(/Runs only when you confirm/);
    expect(command.authorization).toMatch(/manual authorization/);
    // Nothing ran, so nothing is concluded.
    expect(chain.observed).toBeNull();

    // A check the claim does not name only exercises the same code: its failure is not a verdict.
    const covered = challengeChain(input(claim, { run: run(claim, command) }));
    expect(covered.observed).toMatchObject({ outcome: "inconclusive", checkOutcome: "fail", basis: "consented_run" });

    // Named by the claim, a failure introduced by this change supports it.
    const namedClaim = { ...claim, namedChecks: ["orders-dup"] };
    const named = runnable(namedClaim);
    expect(named.relation).toBe("named_by_claim");
    expect(challengeChain(input(namedClaim, { run: run(namedClaim, named) })).observed).toMatchObject({ outcome: "supported", checkOutcome: "fail", evidenceId: "ev-run-1" });
    // Failing on the base too is not this change's doing.
    expect(challengeChain(input(namedClaim, { run: run(namedClaim, named, { differential: differential("pre_existing_failure", "FAIL") }) })).observed?.outcome).toBe("inconclusive");
  });

  it("contradicts a deliberately false claim and keeps the original model suggestion visible", () => {
    const claim = { ...question("q-dup"), namedChecks: ["orders-dup"] };
    const command = runnable(claim);
    const chain = challengeChain(input(claim, { run: run(claim, command, { verdict: "PASS", differential: null }) }));
    expect(chain.observed).toMatchObject({ outcome: "contradicted", checkOutcome: "pass" });
    expect(chain.claim.text).toBe("Retrying checkout creates a duplicate order");
    expect(chain.claim.modelSuggested).toBe(true);

    // A recorded LemonCrow Proof at the claim's source is the observed result, its check outcome kept apart.
    const proof = {
      id: "f1", tool: "lemoncrow-proof", check: "LemonCrow Proof", rule_id: "Proof: contradicted", message: "Duplicate order: passes on head",
      path: "src/orders/checkout.py", start_line: 12, state: "current", snapshot_id: "rev-fixture", head_sha: "", check_status: "PASS", evidence_id: "ev-proof",
    } as StaticFinding;
    const recorded = challengeChain(input(question("q-dup"), { proofs: { state: "ready", value: [proof] } }));
    expect(recorded.observed).toMatchObject({ outcome: "contradicted", checkOutcome: "pass", basis: "recorded_proof", evidenceId: "ev-proof" });
    // A proof recorded on another commit is refused, not reused.
    const view = projection({ evaluation_timeline: { current: { revision_id: "rev-fixture", head_sha: "a".repeat(40), snapshot_number: 0, receipts: [] } } as unknown as ReviewProjection["evaluation_timeline"] });
    const context = investigationContext({ projection: view, targets: TARGETS, checks: [] });
    const wrongSha = challengeChain(input(question("q-dup"), { context, proofs: { state: "ready", value: [{ ...proof, head_sha: "b".repeat(40) }] } }));
    expect(wrongSha.observed).toMatchObject({ outcome: "could_not_run", basis: "recorded_proof" });
    expect(wrongSha.observed?.reason).toMatch(/^Refused/);
  });

  it("stays inspect-only with could_not_run and its reason when no bounded reproduction exists", () => {
    const none = challengeChain(input(question("q-dup"), { recipes: { state: "ready", value: recipes({ recipes: [], recommended_checks: undefined }) } }));
    expect(none.candidate).toEqual({ kind: "inspect_only", reason: "No check recipe in the trusted base, so there is no bounded reproduction to run." });
    expect(none.observed).toMatchObject({ outcome: "could_not_run", checkOutcome: null, basis: "none", reason: none.candidate.kind === "inspect_only" ? none.candidate.reason : "" });
    // Read-only views and a plan for another Snapshot do not run either.
    expect(challengeChain(input(question("q-dup"), { canRun: false })).candidate.kind).toBe("inspect_only");
    const other = challengeChain(input(question("q-dup"), { recipes: { state: "ready", value: recipes({ revision_id: "rev-other" }) } }));
    expect(other.candidate).toMatchObject({ kind: "inspect_only", reason: expect.stringMatching(/^Refused/) });
    // A What-If row is a hypothetical: no check can run it.
    const row = { id: "r1", kind: "caller", path: "src/app/entry.py", line: 7, symbol: "entry.main", depth: 1, origin: "simulated", unchanged: true, exactness: "snapshot", uncertainty: "", reason: "Calls submit_order", openTargetId: null, fromTargetId: "t-orders", coverage: [] } as WhatIfRow;
    const model = { snapshotIdentity: IDENTITY, targetId: "t-orders", scenario: { key: "returns_nullable", label: "Return value becomes nullable", effect: "would need to handle a missing (null/None) result" } } as WhatIfModel;
    const whatIf = claimFromWhatIf(row, model, "submit_order")!;
    expect(whatIf.hypothetical).toBe(true);
    expect(challengeChain(input(whatIf)).observed).toMatchObject({ outcome: "could_not_run", reason: expect.stringMatching(/hypothetical is not current code/) });
  });

  it("shows prompt-injection text as data and never lets it change what would run", () => {
    const claim = question("q-inject");
    expect(claim.text).toBe(INJECTION);
    const command = runnable(claim);
    expect(command.command).toEqual(["pytest", "tests/test_checkout.py"]);
    expect(JSON.stringify(command)).not.toContain("curl");
    expect(claimDigest(claim)).toMatch(/^claim:\[/);
    expect(claimDigest(claim)).toBe(claimDigest(question("q-inject")));
    expect(claimDigest(claim)).not.toBe(claimDigest(question("q-dup")));
  });

  it("reads as stale on a new Snapshot and never reuses the old run", () => {
    const claim = { ...question("q-dup"), namedChecks: ["orders-dup"] };
    const command = runnable(claim);
    const next = investigationContext({ projection: projection({ revision_id: "rev-2", snapshot_number: 1 }), targets: TARGETS, checks: [] });
    const chain = challengeChain(input(claim, { context: next, run: run(claim, command) }));
    expect(chain.stale).toBe(true);
    expect(chain.candidate.kind).toBe("inspect_only");
    expect(chain.observed).toMatchObject({ outcome: "supported", stale: true, snapshotIdentity: IDENTITY });
  });

  it("reports timeouts, flakes, refusals and a wrong Snapshot honestly, never as support", () => {
    const claim = { ...question("q-dup"), namedChecks: ["orders-dup"] };
    const command = runnable(claim);
    const outcome = (overrides: Partial<ChallengeRun>) => challengeChain(input(claim, { run: run(claim, command, overrides) })).observed;
    const timeout = { side: "new", attempt: 1, variant: "run", status: "failed", reason: "timed out after 600s", exit_code: null, duration_ms: 600000, environment: "", executor_class: "", evidence_id: "", evidence_ref: "", report: null, changed_paths: [] as string[], reused: false } as const;
    expect(outcome({ differential: differential("introduced_failure", "PASS", [timeout]) })?.outcome).toBe("inconclusive");
    expect(outcome({ differential: differential("flaky", "PASS") })?.outcome).toBe("inconclusive");
    expect(outcome({ verdict: "NOT_RUN", differential: null, notes: ["browser unavailable"] })).toMatchObject({ outcome: "could_not_run", checkOutcome: "not_run", reason: expect.stringContaining("browser unavailable") });
    expect(outcome({ verdict: "UNKNOWN", differential: null })?.outcome).toBe("inconclusive");
    expect(outcome({ error: "compute budget exhausted", verdict: "", responseRevisionId: "" })).toMatchObject({ outcome: "could_not_run", checkOutcome: null, reason: expect.stringContaining("compute budget exhausted") });
    expect(outcome({ responseRevisionId: "rev-other" })).toMatchObject({ outcome: "could_not_run", reason: expect.stringMatching(/^Refused/) });
  });

  it("offers Challenge only for rows with a claim and a source", () => {
    expect(claimFromBlindSpot(item("check", "FAIL:lint", "checks"), projection(), TARGETS, IDENTITY)).toBeNull();
    expect(claimFromBlindSpot(item("limit", "i1:0"), projection(), TARGETS, IDENTITY)).toBeNull();
    // A claim whose code is not loaded has nothing to inspect.
    expect(claimFromBlindSpot(item("claim", "c-idem"), projection(), [], IDENTITY)).toBeNull();
    expect(claimFromBlindSpot(item("claim", "c-idem"), projection(), TARGETS, IDENTITY)).toMatchObject({ stance: "asserts", namedChecks: ["orders-dup"] });
  });
});
