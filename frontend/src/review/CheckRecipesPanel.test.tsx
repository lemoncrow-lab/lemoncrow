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
    api.fetchCheckRecipes.mockReset();
    api.postCheckRecipesRun.mockReset();
    api.postCheckRequest.mockReset();
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
    await userEvent.click(within(autofix).getByRole("button", { name: "Send to agent" }));
    expect(api.postCheckRequest).toHaveBeenCalledWith("review-1", "revision-2", { kind: "autofix", proposal_id: "proposal-1" });

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
