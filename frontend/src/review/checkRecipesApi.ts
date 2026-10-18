/**
 * Trusted-base check recipes for one exact Review Snapshot (Phase 9R items 1, 2, 5-8, 27, 29).
 *
 * Recipes come only from the trusted base; they run sandboxed on both sides when
 * the effective run plan schedules them, and their base-vs-head differential is
 * persisted as typed evidence. Autofix is a change proposal, never a mutation.
 */
import { requestBrowserJson } from "./reviewApi";
import type { RunPlanSelection } from "./runPlanApi";

export type CheckVerdict = "PASS" | "FAIL" | "UNKNOWN" | "NOT_RUN";
export type CheckDifferentialClass =
  | "introduced_failure"
  | "pre_existing_failure"
  | "fixed"
  | "new"
  | "removed"
  | "newly_skipped"
  | "flaky"
  | "quarantined"
  | "not_comparable"
  | "not_run"
  | "not_compared"
  | "unchanged";

export interface CheckRecipe {
  id: string;
  entry_id: string;
  kind: "test" | "lint" | "format" | "typecheck" | "build" | "precommit";
  kind_entry: string;
  title: string;
  command: string[];
  cwd: string;
  report: string;
  granularity: "test" | "recipe";
  definition: string;
  fix_command: string[];
  detect_changes: boolean;
  origin: "config" | "discovered";
  source: string;
  /** `recipe:sha256:…`: command + trusted-base config inputs (documented in recipes.py). */
  digest: string;
  fix_digest: string;
}

export interface CheckUnit {
  /** Test id, or "" for the recipe as a whole. */
  subject: string;
  class: CheckDifferentialClass;
  base: string;
  head: string;
  base_attempts: string[];
  head_attempts: string[];
  quarantine: string;
  file: string;
}

export interface CheckAttempt {
  side: "old" | "new";
  attempt: number;
  variant: "run" | "probe" | "fix";
  status: "passed" | "failed" | "not_run";
  reason: string;
  exit_code: number | null;
  duration_ms: number;
  environment: string;
  executor_class: string;
  evidence_id: string;
  evidence_ref: string;
  report: { format: string; tests: number; complete: boolean; error: string } | null;
  changed_paths: string[];
  reused: boolean;
  /** Where a reused run actually executed (R6): an earlier Snapshot or a shared base run. */
  reused_from?: CheckReusedFrom;
}

export type CheckReusedFrom =
  | { kind: "snapshot"; snapshot: number; revision_id: string; evidence_id: string; duration_ms: number }
  | { kind: "shared_base"; review_id: string; revision_id: string; evidence_id: string; duration_ms: number };

/** What the execution economy did for one recipe (R6 items 19 and 20). */
export interface CheckEconomy {
  selection?: {
    recipe_id: string;
    mode: "impact" | "all" | "none";
    reason: string;
    total: number;
    selected: number;
    /** The selected test files (first 200). */
    tests?: string[];
  };
  base?: { mode: "on_demand"; ran: boolean; reason: string };
}

export interface CheckAdequacy {
  test: string;
  file: string;
  verdict: "demonstrates" | "not_demonstrating" | "not_assessed";
  base_probe: string;
  head: string;
  reason: string;
}

export interface CheckClaim {
  claim: { key: string; command: string; status: string; origin: "development" | "caller_attested"; ref: string; at: string };
  recipe_id: string;
  verdict: "confirmed" | "contradicted" | "not_reproduced";
  sandbox: string;
  reason: string;
}

/** Gate H: whether a LemonCrow-executed result may gate readiness, or is advisory and why. */
export interface CheckQualification {
  state: "qualified" | "advisory" | "unmeasured";
  reason: string;
  detail: string;
  /** `report`: readiness unchanged, `would_be_advisory` records the effect; `enforce`: `advisory` applies. */
  mode: "report" | "enforce";
  advisory: boolean;
  would_be_advisory: boolean;
  subject: { kind: string; id: string; digest: string; environment_class: string; classifier?: string };
}

export interface CheckDifferential {
  recipe: { id: string; kind: string; title: string; entry_id: string; kind_entry: string; digest: string; command: string[]; cwd: string; origin: string };
  sides: string[];
  granularity: "test" | "recipe";
  /** `null` when one side did not run; `false` when the environment fingerprints differ. */
  comparable: boolean | null;
  environment: { base: string; head: string; differences: string[] };
  executor_class: string;
  head_verdict: CheckVerdict;
  base_verdict: CheckVerdict | "";
  reasons: string[];
  recipe_unit: CheckUnit;
  tests: CheckUnit[];
  tests_total: number;
  counts: Record<string, number>;
  /** Deltas only: what demands attention. */
  attention: CheckUnit[];
  attempts: CheckAttempt[];
  reruns: { limited_by_budget?: boolean; executed?: Record<string, number>; policy?: number };
  adequacy: CheckAdequacy[];
  claims: CheckClaim[];
  autofix: { status?: string; proposal_ids?: string[]; skipped?: string[]; reason?: string };
  head_guard: string;
  /** Absent when no selection or on-demand base applied. */
  economy?: CheckEconomy;
  /** Gate H qualification of this recipe's result; absent when none applies. */
  qualification?: CheckQualification;
  evidence_id: string;
  observed_at: string;
}

export interface CheckEntryRow {
  id: string;
  status: string;
  reason: string;
  sides: string;
  executor: string;
  required: boolean;
  recipes: string[];
  setup_hint: string;
  /** The plan entry's test-impact selection (R6 item 19). */
  selection?: RunPlanSelection | null;
  /** Head first: the base runs only to classify a head failure (R6 item 20). */
  base_on_demand?: boolean;
}

export interface CheckVerdictRow {
  stable_key: string;
  status: CheckVerdict;
  detail: string;
  evidence_id: string;
  executor_class: string;
  observed_at: string;
  qualification?: CheckQualification;
}

export interface CheckProposal {
  id: string;
  path: string;
  state: string;
  intent: string;
  created_by: string;
  patch_text: string;
  annotation_id: string;
}

export interface CheckRecipesProjection {
  revision_id: string;
  historical: boolean;
  run_plan_digest: string;
  recipes: CheckRecipe[];
  policy: { discover: boolean; reruns: number; quarantine: Array<{ test: string; recipe: string; reason: string }>; adequacy: boolean; autofix: boolean };
  errors: string[];
  base_state: "present" | "absent" | "provisional";
  entries: CheckEntryRow[];
  entry_digests: Record<string, string>;
  /** Head-side recipe edits: proposed policy for later Reviews, never executed here. */
  future_policy: { changed: boolean; applies_to?: string; changed_recipes?: Array<{ id: string }>; added?: CheckRecipe[]; removed?: string[]; errors?: string[] };
  results: CheckDifferential[];
  verdicts: CheckVerdictRow[];
  claims: CheckClaim[];
  proposals: CheckProposal[];
}

export interface CheckRunResponse {
  entry_id: string;
  revision_id: string;
  verdicts: Record<string, CheckVerdict>;
  executions: number;
  reused: number;
  /** Base runs reused from another Review on the same base, environment and recipe. */
  shared_base?: number;
  /** Head runs carried forward from an earlier Snapshot with unchanged inputs. */
  carried?: number;
  saved_seconds?: number;
  notes: string[];
  executor_class: string;
  run_on: string;
}

export type CheckRequestBody =
  | { kind: "proof"; recipe_id: string; subject?: string; path?: string; body?: string }
  | { kind: "autofix"; proposal_id: string; body?: string };

function base(reviewId: string): string {
  return `/api/reviews/${encodeURIComponent(reviewId)}/check-recipes`;
}

export function fetchCheckRecipes(reviewId: string, revisionId = ""): Promise<CheckRecipesProjection> {
  const query = revisionId ? `?revision_id=${encodeURIComponent(revisionId)}` : "";
  return requestBrowserJson<CheckRecipesProjection>(`${base(reviewId)}${query}`);
}

/** Execute one planned check entry on one exact Snapshot; `pick` adds it to the plan first. */
export function postCheckRecipesRun(reviewId: string, revisionId: string, entry: string, pick = false): Promise<CheckRunResponse> {
  return requestBrowserJson<CheckRunResponse>(`${base(reviewId)}/run`, {
    method: "POST",
    body: JSON.stringify({ revision_id: revisionId, entry, pick }),
  });
}

/** A proof request or autofix hand-off: reviewer feedback through the normal handoff. */
export function postCheckRequest(reviewId: string, expectedRevisionId: string, body: CheckRequestBody): Promise<{ kind: string; revision_id: string }> {
  return requestBrowserJson(`${base(reviewId)}/requests`, {
    method: "POST",
    body: JSON.stringify({ ...body, expected_revision_id: expectedRevisionId }),
  });
}
