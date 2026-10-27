import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import CheckRecipesPanel from "./CheckRecipesPanel";
import type { CheckAttempt, CheckDifferential, CheckRecipesProjection, CheckUnit } from "./checkRecipesApi";

const api = vi.hoisted(() => ({
  fetchCheckRecipes: vi.fn(),
  postCheckRecipesRun: vi.fn(),
  postCheckRequest: vi.fn(),
}));

vi.mock("./checkRecipesApi", () => api);
const evidenceApi = vi.hoisted(() => ({ fetchReviewEvidence: vi.fn() }));
vi.mock("./reviewApi", async (importOriginal) => ({
  ...await importOriginal<typeof import("./reviewApi")>(),
  fetchReviewEvidence: evidenceApi.fetchReviewEvidence,
}));
const actionApi = vi.hoisted(() => ({ fetchReviewActions: vi.fn() }));
vi.mock("./reviewActionsApi", async (importOriginal) => ({
  ...await importOriginal<typeof import("./reviewActionsApi")>(),
  fetchReviewActions: actionApi.fetchReviewActions,
}));

function unit(subject: string, klass: CheckUnit["class"], base: string, head: string, options: Partial<CheckUnit> = {}): CheckUnit {
  return { subject, class: klass, base, head, base_attempts: [], head_attempts: [], quarantine: "", file: "", ...options };
}

function attempt(side: CheckAttempt["side"], options: Partial<CheckAttempt> = {}): CheckAttempt {
  return {
    side,
    attempt: 1,
    variant: "run",
    status: "passed",
    reason: "",
    exit_code: 0,
    duration_ms: 1000,
    environment: "env:1",
    executor_class: "author_machine",
    evidence_id: `evidence-${side}`,
    evidence_ref: "",
    report: null,
    changed_paths: [],
    reused: false,
    ...options,
  };
}

function differential(options: Partial<CheckDifferential> = {}): CheckDifferential {
  return {
    recipe: {
      id: "unit",
      kind: "test",
      title: "Unit tests",
      entry_id: "checks.recipe:unit",
      kind_entry: "checks.test",
      digest: "recipe:sha256:" + "a".repeat(64),
      command: ["python3", "run_tests.py"],
      cwd: "",
      origin: "config",
    },
    sides: ["old", "new"],
    granularity: "test",
    comparable: true,
    environment: { base: "env:1", head: "env:1", differences: [] },
    executor_class: "managed_sandbox",
    head_verdict: "FAIL",
    base_verdict: "FAIL",
    reasons: ["1 introduced failure"],
    recipe_unit: unit("", "introduced_failure", "failed", "failed"),
    tests: [],
    tests_total: 6,
    counts: { introduced_failure: 1, pre_existing_failure: 1, quarantined: 1, unchanged: 2, new: 1 },
    attention: [
      unit("tests.test_calc.test_add", "introduced_failure", "passed", "failed", { file: "tests/test_calc.py" }),
      unit("tests.test_calc.test_mul", "new", "missing", "passed", { file: "tests/test_calc.py" }),
      unit("tests.test_calc.test_flaky", "flaky", "passed", "flaky", { head_attempts: ["F", "P"] }),
    ],
    attempts: [],
    reruns: { limited_by_budget: false },
    adequacy: [
      {
        test: "tests.test_calc.test_mul",
        file: "tests/test_calc.py",
        verdict: "not_demonstrating",
        base_probe: "passed",
        head: "passed",
        reason: "passes on the base too",
      },
    ],
    claims: [
      {
        claim: { key: "claim:1", command: "pytest -q", status: "PASS", origin: "development", ref: "", at: "" },
        recipe_id: "unit",
        verdict: "contradicted",
        sandbox: "FAIL",
        reason: "the sandbox run failed",
      },
    ],
    autofix: {},
    head_guard: "",
    evidence_id: "evidence-1",
    observed_at: "2026-10-04T00:00:00Z",
    ...options,
  };
}

function projection(options: Partial<CheckRecipesProjection> = {}): CheckRecipesProjection {
  return {
    revision_id: "revision-2",
    historical: false,
    run_plan_digest: "digest",
    recipes: [],
    policy: { discover: true, reruns: 2, quarantine: [], adequacy: true, autofix: true },
    errors: [],
    base_state: "present",
    entries: [
      { id: "checks.test", status: "run", reason: "", sides: "base+head", executor: "local", required: false, recipes: ["unit"], setup_hint: "" },
      { id: "checks.lint", status: "inapplicable", reason: "", sides: "head", executor: "local", required: false, recipes: [], setup_hint: "" },
      { id: "checks.format", status: "on_request", reason: "", sides: "head", executor: "local", required: false, recipes: ["fmt"], setup_hint: "" },
    ],
    entry_digests: {},
    future_policy: { changed: false },
    results: [],
    verdicts: [],
    claims: [],
    proposals: [],
    ...options,
  } as CheckRecipesProjection;
}

const RECIPES = [{ id: "unit" }, { id: "fmt" }] as CheckRecipesProjection["recipes"];

describe("CheckRecipesPanel", () => {
  beforeEach(() => {
    evidenceApi.fetchReviewEvidence.mockReset();
    actionApi.fetchReviewActions.mockReset();
    actionApi.fetchReviewActions.mockResolvedValue({ revision_id: "revision-2", actions: [], catalog: [], fix_all_limit: 20 });
    api.fetchCheckRecipes.mockReset();
    api.postCheckRecipesRun.mockReset();
    api.postCheckRequest.mockReset();
  });

  it("shows only evidence-linked comparable test regressions as observations, not confirmed bugs", async () => {
    api.fetchCheckRecipes.mockResolvedValue(projection({
      recipes: RECIPES,
      observed_regressions: { schema_version: 1, rows: [{
        recipe_id: "unit", subject: "tests.test_calc::test_mul", test_path: "tests/test_calc.py",
        differential_evidence_id: "evidence-1", executor_class: "author_machine", independent_execution: false,
        scope: "exact_snapshot_base_vs_head", observation: "passed_on_base_failed_on_head",
        limitation: "An observed test regression, not a confirmed code defect.",
      }], omitted: 0 },
    }));
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);
    const failures = await screen.findByTestId("check-observed-regressions");
    expect(failures).toHaveTextContent("Observed test regressions · 1");
    expect(failures).toHaveTextContent("tests.test_calc::test_mul");
    expect(failures).toHaveTextContent("not automatically a confirmed code defect");
    expect(failures).toHaveTextContent("author machine observed");
  });

  it("opens only authenticated evidence from the exact Review and Snapshot", async () => {
    api.fetchCheckRecipes.mockResolvedValue(projection({
      observed_regressions: { schema_version: 1, rows: [{
        recipe_id: "unit", subject: "tests.test_calc::test_mul", test_path: "tests/test_calc.py",
        differential_evidence_id: "evidence-1", executor_class: "author_machine", independent_execution: false,
        scope: "exact_snapshot_base_vs_head", observation: "passed_on_base_failed_on_head",
        limitation: "An observed test regression, not a confirmed code defect.",
      }], omitted: 0 },
    }));
    evidenceApi.fetchReviewEvidence.mockResolvedValue({ revision_id: "revision-2", evidence: [{
      id: "evidence-1", review_id: "review-1", revision_id: "revision-2", revision_number: 2,
      status: "current", title: "Stored differential", kind: "test_report", source: "lemoncrow",
      path: "tests/test_calc.py", url: "", content_url: "/api/evidence/evidence-1/content", created_at: "2026-10-27T00:00:00Z",
      content_hash: "abc", mime_type: "application/json", bytes: 100, source_ref: "check", verification_status: "FAIL", detail: ""
    }] });
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);
    const button = await screen.findByRole("button", { name: "Inspect evidence for tests.test_calc::test_mul" });
    expect(evidenceApi.fetchReviewEvidence).not.toHaveBeenCalled();
    await userEvent.click(button);
    const detail = await screen.findByTestId("regression-evidence-detail");
    expect(detail).toHaveTextContent("Stored differential");
    expect(detail).toHaveTextContent("Snapshot 2");
    expect(evidenceApi.fetchReviewEvidence).toHaveBeenCalledWith("review-1", "revision-2");
    await userEvent.click(screen.getByRole("button", { name: "Inspect evidence for tests.test_calc::test_mul" }));
    expect(screen.queryByTestId("regression-evidence-detail")).toBeNull();
  });

  it("does not show wrong-Snapshot or wrong-Review evidence as current proof", async () => {
    api.fetchCheckRecipes.mockResolvedValue(projection({
      observed_regressions: { schema_version: 1, rows: [{
        recipe_id: "unit", subject: "test:checkout", test_path: "test_checkout.py", differential_evidence_id: "evidence-1",
        executor_class: "author_machine", independent_execution: false,
        scope: "exact_snapshot_base_vs_head", observation: "passed_on_base_failed_on_head",
        limitation: "Observed, not confirmed",
      }], omitted: 0 },
    }));
    evidenceApi.fetchReviewEvidence.mockResolvedValue({ revision_id: "revision-2", evidence: [{
      id: "evidence-1", review_id: "review-1", revision_id: "revision-1", status: "stale", title: "Old proof"
    }] });
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);
    await userEvent.click(await screen.findByRole("button", { name: "Inspect evidence for test:checkout" }));
    expect(await screen.findByText(/exact Snapshot evidence is unavailable/)).toBeInTheDocument();
    expect(screen.queryByText("Old proof")).toBeNull();
  });

  it("raises only the deltas and counts pre-existing and quarantined results", async () => {
    api.fetchCheckRecipes.mockResolvedValue(projection({ recipes: RECIPES, results: [differential()] }));
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);

    const card = await screen.findByTestId("check-result-unit");
    expect(api.fetchCheckRecipes).toHaveBeenCalledWith("review-1", "revision-2");
    expect(card).toHaveTextContent("Failed");
    expect(card).toHaveTextContent("base vs head");
    expect(within(card).getByTestId("check-unit-tests.test_calc.test_add")).toHaveTextContent("Introduced failure");
    expect(within(card).getByTestId("check-unit-tests.test_calc.test_flaky")).toHaveTextContent("F P");
    // Pre-existing, quarantined and unchanged tests are context: counted, never raised.
    expect(within(card).getByTestId("check-quiet")).toHaveTextContent("1 pre-existing · 1 quarantined · 2 unchanged");
    expect(within(card).queryByText("Pre-existing", { exact: true })).not.toBeInTheDocument();
    expect(within(card).getByTestId("check-adequacy")).toHaveTextContent("Doesn't demonstrate the change");
    expect(within(card).getByTestId("check-claim")).toHaveTextContent("Claim contradicted");
    // Inapplicable kinds with no recipe are not listed.
    expect(screen.queryByTestId("check-entry-checks.lint")).not.toBeInTheDocument();
  });

  it("names a Gate H advisory result in the recipe card without adding a control", async () => {
    const qualification = {
      state: "unmeasured" as const,
      reason: "unmeasured",
      detail: "no Gate H repeat-run measurement exists for this subject",
      mode: "report" as const,
      advisory: false,
      would_be_advisory: true,
      subject: { kind: "recipe", id: "checks.recipe:unit", digest: "recipe:sha256:a", environment_class: "author_machine@host:sha256:b" },
    };
    api.fetchCheckRecipes.mockResolvedValue(projection({ recipes: RECIPES, results: [differential({ qualification })] }));
    const { unmount } = render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);
    const card = await screen.findByTestId("check-result-unit");
    const buttons = within(card).queryAllByRole("button").length;
    expect(within(card).getByTestId("check-qualification")).toHaveTextContent(
      "Would be advisory under Gate H enforcement (unmeasured): no Gate H repeat-run measurement exists for this subject",
    );
    unmount();

    api.fetchCheckRecipes.mockResolvedValue(
      projection({ recipes: RECIPES, results: [differential({ qualification: { ...qualification, mode: "enforce", advisory: true } })] }),
    );
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);
    const enforced = await screen.findByTestId("check-result-unit");
    expect(within(enforced).getByTestId("check-qualification")).toHaveTextContent("Advisory, not counted for readiness (unmeasured)");
    // A reason line, not a new control.
    expect(within(enforced).queryAllByRole("button")).toHaveLength(buttons);
  });

  it("says nothing about Gate H for a qualified result", async () => {
    const qualified = {
      state: "qualified" as const,
      reason: "qualified",
      detail: "measured within the Gate H thresholds",
      mode: "enforce" as const,
      advisory: false,
      would_be_advisory: false,
      subject: { kind: "recipe", id: "checks.recipe:unit", digest: "d", environment_class: "e" },
    };
    api.fetchCheckRecipes.mockResolvedValue(projection({ recipes: RECIPES, results: [differential({ qualification: qualified })] }));
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);
    const card = await screen.findByTestId("check-result-unit");
    expect(within(card).queryByTestId("check-qualification")).not.toBeInTheDocument();
  });

  it("marks a fingerprint mismatch as not comparable", async () => {
    api.fetchCheckRecipes.mockResolvedValue(
      projection({
        recipes: RECIPES,
        results: [
          differential({
            comparable: false,
            environment: { base: "env:1", head: "env:2", differences: ["lockfile"] },
            attention: [unit("", "not_comparable", "passed", "failed")],
          }),
        ],
      }),
    );
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);

    expect(await screen.findByTestId("check-not-comparable")).toHaveTextContent("environments differ (lockfile)");
    expect(screen.getByTestId("check-unit-recipe")).toHaveTextContent("Not comparable");
  });

  it("runs a planned entry and adds an unplanned one to the plan before running it", async () => {
    api.fetchCheckRecipes.mockResolvedValue(projection({ recipes: RECIPES }));
    api.postCheckRecipesRun.mockResolvedValue({ entry_id: "checks.test", revision_id: "revision-2", verdicts: { "checks.test": "PASS" }, executions: 2, reused: 0, notes: [], executor_class: "author_machine", run_on: "local" });
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);

    const test = await screen.findByTestId("check-entry-checks.test");
    await userEvent.click(within(test).getByRole("button", { name: "Run" }));
    expect(api.postCheckRecipesRun).toHaveBeenCalledWith("review-1", "revision-2", "checks.test", false);
    expect(await screen.findByRole("status")).toHaveTextContent("checks.test: Passed (2 run, 0 reused)");
    await waitFor(() => expect(api.fetchCheckRecipes).toHaveBeenCalledTimes(2));

    api.postCheckRecipesRun.mockResolvedValue({ entry_id: "checks.format", revision_id: "revision-2", verdicts: { "checks.format": "FAIL" }, executions: 1, reused: 0, notes: [], executor_class: "author_machine", run_on: "local" });
    await userEvent.click(within(screen.getByTestId("check-entry-checks.format")).getByRole("button", { name: "Add & run" }));
    expect(api.postCheckRecipesRun).toHaveBeenLastCalledWith("review-1", "revision-2", "checks.format", true);
  });

  it("shows the test selection, the on-demand base and where reused runs came from", async () => {
    api.fetchCheckRecipes.mockResolvedValue(
      projection({
        recipes: RECIPES,
        entries: [
          { id: "checks.test", status: "run", reason: "", sides: "head", executor: "local", required: false, recipes: ["unit"], setup_hint: "", base_on_demand: true },
        ],
        results: [
          differential({
            sides: ["new"],
            economy: {
              selection: { recipe_id: "unit", mode: "impact", reason: "selected by the trusted base's import graph", total: 9, selected: 2, tests: ["tests/test_alpha.py", "tests/test_beta.py"] },
              base: { mode: "on_demand", ran: false, reason: "head passed: the base runs only to classify a head failure" },
            },
            attempts: [
              attempt("new", {
                reused: true,
                reused_from: { kind: "snapshot", snapshot: 1, revision_id: "revision-1", evidence_id: "evidence-9", duration_ms: 90000 },
              }),
            ],
          }),
          differential({
            recipe: { ...differential().recipe, id: "lint", title: "Lint" },
            economy: { selection: { recipe_id: "lint", mode: "all", reason: "full suite: no test-impact map for base aaaaaaaaaaaa (not built yet)", total: 0, selected: 0 } },
            attempts: [
              attempt("old", {
                reused: true,
                reused_from: { kind: "shared_base", review_id: "review-0", revision_id: "revision-0", evidence_id: "evidence-0", duration_ms: 0 },
              }),
              attempt("new"),
            ],
          }),
        ],
      }),
    );
    api.postCheckRecipesRun.mockResolvedValue({ entry_id: "checks.test", revision_id: "revision-2", verdicts: { "checks.test": "PASS" }, executions: 1, reused: 0, shared_base: 1, carried: 1, saved_seconds: 180, notes: [], executor_class: "author_machine", run_on: "local" });
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);

    const unitCard = await screen.findByTestId("check-result-unit");
    expect(unitCard).toHaveTextContent("head only");
    expect(within(unitCard).getByTestId("check-economy-selection")).toHaveTextContent(
      "Ran 2 of 9 test files selected by test impact: selected by the trusted base's import graph",
    );
    expect(within(unitCard).getByTestId("check-economy-base")).toHaveTextContent("head passed: the base runs only to classify a head failure");
    expect(within(unitCard).getByTestId("check-economy-reused")).toHaveTextContent("Head reused from Snapshot 1: inputs unchanged (90 s not re-run)");

    const lintCard = screen.getByTestId("check-result-lint");
    // A full run says why it was not narrowed.
    expect(within(lintCard).getByTestId("check-economy-selection")).toHaveTextContent("full suite: no test-impact map for base aaaaaaaaaaaa");
    expect(within(lintCard).getByTestId("check-economy-reused")).toHaveTextContent(
      "Base shared from another Review on the same base, environment and recipe",
    );
    expect(screen.getByTestId("check-entry-checks.test")).toHaveTextContent("head first");

    await userEvent.click(within(screen.getByTestId("check-entry-checks.test")).getByRole("button", { name: "Run" }));
    expect(await screen.findByRole("status")).toHaveTextContent("checks.test: Passed (1 run, 0 reused, 1 base shared, 1 carried forward)");
  });

  it("shows no economy line when nothing was narrowed, deferred or reused", async () => {
    api.fetchCheckRecipes.mockResolvedValue(projection({ recipes: RECIPES, results: [differential({ attempts: [attempt("old"), attempt("new")] })] }));
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);

    const card = await screen.findByTestId("check-result-unit");
    expect(within(card).queryByTestId("check-economy")).not.toBeInTheDocument();
  });

  it("renders persisted reviewer approval as sent, without a second action", async () => {
    api.fetchCheckRecipes.mockResolvedValue(
      projection({
        recipes: RECIPES,
        results: [differential({ autofix: { status: "proposed", proposal_ids: ["proposal-1"], reason: "format fix" } })],
        approved_autofix_ids: ["proposal-1"],
      }),
    );
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);
    const autofix = await screen.findByTestId("check-autofix");
    expect(autofix).toHaveTextContent("Approved · ready for agent");
    expect(within(autofix).queryByRole("button", { name: "Approve fix" })).toBeNull();
    expect(api.postCheckRequest).not.toHaveBeenCalled();
  });

  it("sends autofix proposals and proof requests to the agent as feedback", async () => {
    api.fetchCheckRecipes.mockResolvedValue(
      projection({
        recipes: RECIPES,
        results: [differential({ autofix: { status: "proposed", proposal_ids: ["proposal-1"], reason: "1 file reformatted" } })],
      }),
    );
    api.postCheckRequest.mockResolvedValue({ kind: "autofix", revision_id: "revision-2" });
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);

    const autofix = await screen.findByTestId("check-autofix");
    expect(autofix).toHaveTextContent("1 file reformatted");
    await userEvent.click(within(autofix).getByRole("button", { name: "Approve fix" }));
    expect(api.postCheckRequest).toHaveBeenCalledWith("review-1", "revision-2", { kind: "autofix", proposal_id: "proposal-1", approved: true });

    await userEvent.click(within(screen.getByTestId("check-adequacy")).getByRole("button", { name: "Request proof" }));
    expect(api.postCheckRequest).toHaveBeenLastCalledWith("review-1", "revision-2", {
      kind: "proof",
      recipe_id: "unit",
      subject: "tests.test_calc.test_mul",
      path: "tests/test_calc.py",
    });
    expect(await screen.findByRole("status")).toHaveTextContent("Proof requested as feedback for the agent");
  });

  it("is read-only on a historical Snapshot and shows head-side recipe edits as future policy", async () => {
    api.fetchCheckRecipes.mockResolvedValue(
      projection({
        historical: true,
        recipes: RECIPES,
        results: [differential({ autofix: { status: "proposed", proposal_ids: ["proposal-1"], reason: "proposed" } })],
        future_policy: { changed: true, applies_to: "later Reviews after this Change lands" },
      }),
    );
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-1" />);

    expect(await screen.findByTestId("check-future-policy")).toHaveTextContent("never run here");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("explains an empty or provisional trusted base and a failed load", async () => {
    api.fetchCheckRecipes.mockResolvedValueOnce(projection({ base_state: "provisional", entries: [] }));
    const { unmount } = render(<CheckRecipesPanel reviewId="review-1" />);
    expect(await screen.findByText(/trusted base is not readable yet/)).toBeInTheDocument();
    unmount();

    api.fetchCheckRecipes.mockRejectedValueOnce(new Error("boom"));
    render(<CheckRecipesPanel reviewId="review-1" />);
    expect(await screen.findByText("Check recipes unavailable: boom")).toBeInTheDocument();
  });
});

describe("trusted check recommendations", () => {
  const proposed = {
    entry_id: "checks.test", recipe_id: "unit", title: "Checkout unit tests",
    reason: "Planner selected impacted tests (1 recorded)", run_plan_status: "run",
    previous_verdict: "", prior_snapshot_verdict: "", required: false, can_run_now: true, manual_authorization_required: true,
    selection_mode: "impact" as const, selected_tests_sample: ["tests/test_checkout.py"],
    selected_test_count: 1, coverage: "known" as const, executor: "local", recipe_digest: "sha256:123",
  };

  beforeEach(() => {
    actionApi.fetchReviewActions.mockReset();
    actionApi.fetchReviewActions.mockResolvedValue({ revision_id: "revision-2", actions: [], catalog: [], fix_all_limit: 20 });
    api.fetchCheckRecipes.mockReset();
    api.postCheckRecipesRun.mockReset();
  });

  it("links a genuinely attributed correction to a pending formatter recheck without running anything", async () => {
    const format = {
      ...proposed, entry_id: "checks.recipe:fmt", recipe_id: "fmt", title: "Formatter",
      previous_verdict: "", prior_snapshot_verdict: "FAIL",
      reason: "Previous Snapshot had FAIL; this Snapshot has no corresponding check result",
      selection_mode: "all" as const, selected_test_count: 0, selected_tests_sample: [],
      coverage: "not_applicable" as const,
    };
    api.fetchCheckRecipes.mockResolvedValue(projection({
      recommended_checks: { schema_version: 1, previous_revision_id: "revision-1", recommendations: [format], omitted: 0, policy: "Manual only" },
    }));
    actionApi.fetchReviewActions.mockResolvedValue({
      revision_id: "revision-2", catalog: [], fix_all_limit: 20,
      actions: [{ kind: "fix", status: "snapshot_recorded", result_certainty: "exact",
        source_revision_id: "revision-1", resulting_revision_id: "revision-2",
        subjects: [{ kind: "approved_autofix", title: "Approved formatter correction · fmt" }] }],
    });
    api.postCheckRecipesRun.mockResolvedValue({ entry_id: "checks.recipe:fmt", revision_id: "revision-2", verdicts: { "checks.recipe:fmt": "PASS" }, executions: 1, reused: 0 });
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);
    const notice = await screen.findByTestId("agent-correction-recheck");
    expect(notice).toHaveTextContent("Agent correction captured · fresh verification pending");
    expect(notice).toHaveTextContent("Approved formatter correction · fmt");
    expect(api.postCheckRecipesRun).not.toHaveBeenCalled();
    await userEvent.click(within(await screen.findByTestId("check-recommendations")).getByRole("button", { name: "Reverify fresh" }));
    expect(api.postCheckRecipesRun).toHaveBeenCalledWith("review-1", "revision-2", "checks.recipe:fmt", false, "revision-1");
  });

  it("does not attribute correction when provenance is missing, unverifiable or from another Snapshot", async () => {
    api.fetchCheckRecipes.mockResolvedValue(projection({
      recommended_checks: { schema_version: 1, previous_revision_id: "revision-1", recommendations: [{ ...proposed, prior_snapshot_verdict: "FAIL" }], omitted: 0, policy: "Manual only" },
    }));
    actionApi.fetchReviewActions.mockResolvedValue({
      revision_id: "revision-2", catalog: [], fix_all_limit: 20,
      actions: [{ kind: "fix", status: "snapshot_recorded", result_certainty: "unattributed",
        source_revision_id: "revision-1", resulting_revision_id: "revision-2",
        subjects: [{ kind: "approved_autofix", title: "Unattributed" }] }],
    });
    const mounted = render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);
    await screen.findByTestId("check-recommendations");
    await waitFor(() => expect(actionApi.fetchReviewActions).toHaveBeenCalled());
    expect(screen.queryByTestId("agent-correction-recheck")).toBeNull();
    mounted.unmount();
    actionApi.fetchReviewActions.mockRejectedValue(new Error("no auth"));
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);
    await screen.findByTestId("check-recommendations");
    expect(screen.queryByTestId("agent-correction-recheck")).toBeNull();
    expect(api.postCheckRecipesRun).not.toHaveBeenCalled();
  });

  it("offers one explicit user action for a trusted planned check", async () => {
    api.fetchCheckRecipes.mockResolvedValue(projection({
      recipes: RECIPES,
      recommended_checks: { schema_version: 1, recommendations: [proposed], omitted: 0, policy: "Manual only" },
    }));
    api.postCheckRecipesRun.mockResolvedValue({
      entry_id: "checks.test", revision_id: "revision-2", verdicts: { "checks.test": "PASS" },
      executions: 1, reused: 0, notes: [], executor_class: "author_machine", run_on: "local",
    });
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);
    const suggestions = await screen.findByTestId("check-recommendations");
    expect(suggestions).toHaveTextContent("Suggested verification");
    expect(suggestions).toHaveTextContent("tests/test_checkout.py");
    expect(api.postCheckRecipesRun).not.toHaveBeenCalled();
    await userEvent.click(within(suggestions).getByRole("button", { name: "Run check" }));
    expect(api.postCheckRecipesRun).toHaveBeenCalledWith("review-1", "revision-2", "checks.test", false);
  });

  it("re-verifies a previous Snapshot failure with an explicit fresh, source-bound run", async () => {
    api.fetchCheckRecipes.mockResolvedValue(projection({
      recipes: RECIPES,
      recommended_checks: {
        schema_version: 1, previous_revision_id: "revision-1",
        recommendations: [{
          ...proposed, previous_verdict: "", prior_snapshot_verdict: "FAIL",
          reason: "Previous Snapshot had FAIL; this Snapshot has no corresponding check result",
        }], omitted: 0, policy: "Manual only",
      },
    }));
    api.postCheckRecipesRun.mockResolvedValue({
      entry_id: "checks.test", revision_id: "revision-2", verdicts: { "checks.test": "PASS" },
      executions: 1, reused: 0, notes: [], executor_class: "author_machine", run_on: "local",
    });
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);
    const suggestions = await screen.findByTestId("check-recommendations");
    expect(suggestions).toHaveTextContent("Previous Snapshot had FAIL");
    expect(api.postCheckRecipesRun).not.toHaveBeenCalled();
    await userEvent.click(within(suggestions).getByRole("button", { name: "Reverify fresh" }));
    expect(api.postCheckRecipesRun).toHaveBeenCalledWith(
      "review-1", "revision-2", "checks.test", false, "revision-1",
    );
  });

  it("does not execute a recommendation blocked by the effective plan or in a historical Review", async () => {
    api.fetchCheckRecipes.mockResolvedValue(projection({
      recipes: RECIPES,
      recommended_checks: {
        schema_version: 1,
        recommendations: [{ ...proposed, can_run_now: false, run_plan_status: "not_available", previous_verdict: "FAIL", reason: "Previous execution FAIL; inspect before retrying" }],
        omitted: 0, policy: "Manual only",
      },
    }));
    const mounted = render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" />);
    const suggestions = await screen.findByTestId("check-recommendations");
    expect(suggestions).toHaveTextContent("Previous execution FAIL");
    expect(within(suggestions).queryByRole("button", { name: "Run check" })).toBeNull();
    mounted.unmount();
    render(<CheckRecipesPanel reviewId="review-1" revisionId="revision-2" readOnly />);
    const readOnlySuggestions = await screen.findByTestId("check-recommendations");
    expect(within(readOnlySuggestions).queryByRole("button", { name: "Run check" })).toBeNull();
    expect(api.postCheckRecipesRun).not.toHaveBeenCalled();
  });
});
