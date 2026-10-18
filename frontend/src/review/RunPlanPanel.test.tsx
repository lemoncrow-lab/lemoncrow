import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import RunPlanPanel from "./RunPlanPanel";
import type { RunPlan, RunPlanEntry, RunPlanProjection } from "./runPlanApi";

const api = vi.hoisted(() => ({
  fetchRunPlan: vi.fn(),
  postRunSpecific: vi.fn(),
  startRunSpecific: vi.fn(),
}));

vi.mock("./runPlanApi", () => api);

function entry(id: string, options: Partial<RunPlanEntry> = {}): RunPlanEntry {
  return {
    id,
    family: id.split(".")[0] ?? "",
    title: id,
    status: "run",
    reason: "",
    origin: "preset",
    when: "push",
    sides: "head",
    paths: [],
    executor: "local",
    depth: "auto",
    required: false,
    escalate: false,
    cost_class: "low",
    degrade_tier: "base",
    estimated_minutes: 0,
    estimated_model_calls: 0,
    pulled_in_by: [],
    setup_hint: "",
    available: true,
    external: false,
    budget_reduced_from: "",
    ...options,
  };
}

function plan(entries: RunPlanEntry[], options: Partial<RunPlan> = {}): RunPlan {
  return {
    version: 1,
    digest: "abcdef0123456789".repeat(4),
    parent_digest: "",
    inputs_digest: "",
    review_id: "review-1",
    revision_id: "revision-1",
    trigger: "open",
    selection: { source: "preset", name: "auto", base: "auto" },
    run_on: "local",
    run_on_note: "",
    budget: { ceilings: {}, usage: {}, planned: { minutes: 1.5, model_calls: 5 }, limited: false, notes: [] },
    family_modes: {},
    entries,
    manual: [],
    errors: [],
    ...options,
  };
}

function projection(runPlan: RunPlan, options: Partial<RunPlanProjection> = {}): RunPlanProjection {
  return {
    plan: runPlan,
    settings: {
      effort: { effective_value: "auto", mode: "override", origin: { layer: "trusted_base_repository", source: "base" } },
      run_on: { effective_value: "local", mode: "inherit", origin: { layer: "built_in_default", source: "built_in_default" } },
      errors: [],
    },
    catalog: [{ id: "checks.lint", family: "checks", title: "Lint", cost_class: "low", available: true, external: false }],
    future_settings: { changed: false, applies_to: "", errors: [] },
    revision_id: "revision-1",
    historical: false,
    ...options,
  };
}

const ENTRIES = [
  entry("ai.investigators", { title: "AI investigators", estimated_model_calls: 4, estimated_minutes: 1.5 }),
  entry("checks.test", { title: "Tests", sides: "base+head", pulled_in_by: ["behavior.test_diff"], origin: "custom" }),
  entry("ai.hypotheses", { title: "AI hypotheses", status: "on_request", reason: "Auto escalates on risk" }),
  entry("visual.screenshot", { title: "Screenshots", status: "inapplicable", origin: "preset" }),
  entry("migrations.execute", {
    title: "Migrations",
    status: "inapplicable",
    origin: "custom",
    setup_hint: "No migrations directory in the trusted base.",
  }),
  entry("behavior.test_diff", { title: "Behavior diff", status: "limited_by_budget", budget_reduced_from: "" }),
];

describe("RunPlanPanel", () => {
  beforeEach(() => {
    api.fetchRunPlan.mockReset();
    api.postRunSpecific.mockReset();
    api.startRunSpecific.mockReset();
  });

  it("shows what runs, why, the origin of the depth and the estimated cost", async () => {
    api.fetchRunPlan.mockResolvedValue(projection(plan(ENTRIES, { budget: { ...plan([]).budget, limited: true } })));
    render(<RunPlanPanel reviewId="review-1" revisionId="revision-1" />);

    const panel = await screen.findByTestId("run-plan-panel");
    expect(api.fetchRunPlan).toHaveBeenCalledWith("review-1", "revision-1");
    expect(panel).toHaveTextContent("Auto");
    expect(panel).toHaveTextContent("depth from repository config");
    expect(panel).toHaveTextContent("est. 1.5 min · 5 model calls");
    expect(screen.getByText("Limited by budget", { selector: "h3 ~ span" })).toBeInTheDocument();

    const tests = screen.getByTestId("run-plan-entry-checks.test");
    expect(tests).toHaveTextContent("Needed by behavior.test_diff");
    expect(tests).toHaveTextContent("base + head");
    expect(tests).toHaveTextContent("custom");
    expect(screen.getByTestId("run-plan-entry-ai.hypotheses")).toHaveTextContent("Auto escalates on risk");
    // Nothing to act on: hidden when only the depth selected it, explained when someone asked for it.
    expect(screen.queryByTestId("run-plan-entry-visual.screenshot")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-plan-entry-migrations.execute")).toHaveTextContent("No migrations directory");
    expect(screen.getByTestId("run-plan-entry-behavior.test_diff")).toHaveTextContent("Limited by budget");
  });

  it("keeps required work that will not run in view, with selection notes and metered compute", async () => {
    const entries = [
      ...ENTRIES,
      entry("check:security:sast", {
        title: "security:sast",
        status: "not_available",
        required: true,
        floor_kind: "check",
        origin: "policy_floor",
        available: false,
        reason: "required · no executor (required by organization Review policy required_checks)",
      }),
      entry("ai.investigators.capped", {
        title: "Capped AI",
        status: "limited_by_budget",
        required: true,
        floor_kind: "capability",
        origin: "policy_floor",
        reason: "Limited by budget: monthly managed spend cap reached",
      }),
    ];
    api.fetchRunPlan.mockResolvedValue(
      projection(plan(entries, { notes: ["profile docs: label rule inactive (no label source)"] }), {
        compute: {
          rows: [{ entry_id: "surface:checks", executor: "local", stage: "runtime_validation", compute_minutes: 2, runs: 2 }],
          totals: { compute_minutes: 2, runs: 2 },
          source: "local",
        },
      }),
    );
    render(<RunPlanPanel reviewId="review-1" />);

    const blockers = await screen.findByTestId("run-plan-required-blockers");
    expect(blockers).toHaveTextContent("Required, not running · 2");
    expect(within(blockers).getByTestId("run-plan-entry-check:security:sast")).toHaveTextContent("Required · no executor");
    expect(within(blockers).getByTestId("run-plan-entry-ai.investigators.capped")).toHaveTextContent(
      "monthly managed spend cap reached",
    );
    // An optional entry limited by budget stays with the other work that is not running.
    expect(within(blockers).queryByTestId("run-plan-entry-behavior.test_diff")).not.toBeInTheDocument();
    expect(screen.getByText("profile docs: label rule inactive (no label source)")).toBeInTheDocument();
    const compute = screen.getByTestId("run-plan-compute");
    expect(compute).toHaveTextContent("Compute used here: 2 min · 2 runs");
    expect(compute).toHaveTextContent("surface:checks · local · runtime validation · 2 min (2)");
  });

  it("says when an invalid trusted-base config stops every entry", async () => {
    const error = "trusted-base .lemoncrow/review.yaml is invalid: review.effort must be a depth";
    api.fetchRunPlan.mockResolvedValue(
      projection(plan([entry("ai.investigators", { status: "skipped", reason: `Review policy invalid: ${error}` })], {
        policy_state: "invalid",
        policy_error: error,
      })),
    );
    render(<RunPlanPanel reviewId="review-1" />);
    expect(await screen.findByTestId("run-plan-policy-invalid")).toHaveTextContent(
      `Review policy invalid: ${error}. Nothing runs until the trusted-base config is fixed.`,
    );
  });

  it("says head-side settings edits apply only to later Reviews", async () => {
    api.fetchRunPlan.mockResolvedValue(
      projection(plan(ENTRIES), { future_settings: { changed: true, applies_to: "later Reviews", errors: [] } }),
    );
    render(<RunPlanPanel reviewId="review-1" />);
    expect(await screen.findByText(/apply to later Reviews after it lands, not to this one/)).toBeInTheDocument();
  });

  it("Run specific previews the added cost before adding, then starts the added work", async () => {
    const user = userEvent.setup();
    const before = plan(ENTRIES);
    const added = entry("ai.hypotheses", { title: "AI hypotheses", origin: "manual", estimated_model_calls: 1 });
    const after = plan(
      ENTRIES.map((row) => (row.id === "ai.hypotheses" ? added : row)),
      { digest: "f".repeat(64) },
    );
    api.fetchRunPlan.mockResolvedValue(projection(before));
    api.postRunSpecific.mockImplementation(async (_id: string, _rev: string, _entries: unknown, confirm: boolean) => ({
      confirmed: confirm,
      preview: { changes: [added], incremental: { minutes: 0, model_calls: 1 }, limited_by_budget: [], rejected: [], plan: after },
      ...(confirm ? { plan: after, next: [{ entry: "ai.hypotheses", action: "ai_review", depth: "auto" }] } : {}),
    }));
    api.startRunSpecific.mockResolvedValue({ "ai.hypotheses": "started" });
    render(<RunPlanPanel reviewId="review-1" revisionId="revision-1" />);

    await user.click(await screen.findByRole("button", { name: "Run specific…" }));
    const picker = screen.getByRole("group", { name: "Add to this Review" });
    // Only work that could be added is offered: on-request entries and detected catalog entries.
    expect(within(picker).getByRole("button", { name: /AI hypotheses/ })).toHaveAttribute("aria-pressed", "false");
    expect(within(picker).getByRole("button", { name: /Lint/ })).toBeInTheDocument();
    expect(within(picker).queryByRole("button", { name: /Screenshots/ })).not.toBeInTheDocument();
    expect(within(picker).queryByRole("button", { name: /AI investigators/ })).not.toBeInTheDocument();

    await user.click(within(picker).getByRole("button", { name: /AI hypotheses/ }));
    expect(within(picker).getByRole("button", { name: /AI hypotheses/ })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "Preview cost" }));
    expect(await screen.findByTestId("run-specific-preview")).toHaveTextContent("Adds 1 model call");
    expect(api.postRunSpecific).toHaveBeenLastCalledWith("review-1", "revision-1", [{ id: "ai.hypotheses" }], false);

    await user.click(screen.getByRole("button", { name: "Add to this Review" }));
    await waitFor(() =>
      expect(api.postRunSpecific).toHaveBeenLastCalledWith("review-1", "revision-1", [{ id: "ai.hypotheses" }], true),
    );
    expect(api.startRunSpecific).toHaveBeenCalledWith("review-1", "revision-1", [
      { entry: "ai.hypotheses", action: "ai_review", depth: "auto" },
    ]);
    expect(await screen.findByRole("status")).toHaveTextContent("ai.hypotheses: started");
    expect(screen.getByTestId("run-plan-entry-ai.hypotheses")).toHaveTextContent("added for this Review");
  });

  it("offers no Run specific on a read-only or historical Snapshot", async () => {
    api.fetchRunPlan.mockResolvedValue(projection(plan(ENTRIES), { historical: true }));
    const { unmount } = render(<RunPlanPanel reviewId="review-1" />);
    expect(await screen.findByText("Plan for an earlier Snapshot.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run specific…" })).not.toBeInTheDocument();
    unmount();

    api.fetchRunPlan.mockResolvedValue(projection(plan(ENTRIES)));
    render(<RunPlanPanel reviewId="review-1" readOnly />);
    await screen.findByTestId("run-plan-panel");
    expect(screen.queryByRole("button", { name: "Run specific…" })).not.toBeInTheDocument();
  });

  it("shows a rejected pick's reason instead of adding it", async () => {
    const user = userEvent.setup();
    api.fetchRunPlan.mockResolvedValue(projection(plan(ENTRIES)));
    api.postRunSpecific.mockRejectedValue(new Error("manual pick ai.hypotheses: the ai family is off"));
    render(<RunPlanPanel reviewId="review-1" />);
    await user.click(await screen.findByRole("button", { name: "Run specific…" }));
    await user.click(screen.getByRole("button", { name: /AI hypotheses/ }));
    await user.click(screen.getByRole("button", { name: "Preview cost" }));
    expect(await screen.findByText("manual pick ai.hypotheses: the ai family is off")).toBeInTheDocument();
    expect(api.startRunSpecific).not.toHaveBeenCalled();
  });

  it("shows an entry reused from an equivalent result with its origin, never as a blocker", async () => {
    const reused = entry("checks.test", {
      title: "Affected tests",
      status: "satisfied",
      required: true,
      floor_kind: "capability",
      origin: "policy_floor",
      reason: "satisfied by provider CI CI / unit tests PASS on head bbbbbbbbbbbb; not re-run",
      satisfied_by: {
        entry_id: "checks.test",
        evidence_id: "ev-7",
        check_key: "provider_ci|github|actions||unit-tests",
        attestation_class: "provider_ci",
        status: "PASS",
        head_sha: "b".repeat(40),
        equivalence: "eq1;tool=github%3Aactions%3Aunit-tests;recipe=checks.test;head=bbb;config=;recipe_digest=",
        title: "CI / unit tests",
        observed_at: "2026-10-01T10:05:00Z",
        url: "https://ci.example.invalid/run/7",
      },
    });
    api.fetchRunPlan.mockResolvedValue(
      projection(
        plan([...ENTRIES, reused], {
          trigger: "push",
          fast_lane: {
            eligible: true,
            reasons: ["push with no risk signal, sensitive path or depth floor"],
            skipped: ["ai.investigators"],
          },
        }),
        { ci_equivalents: { rows: [], errors: ["ci_equivalents[1].provider is required for provider_ci"] } },
      ),
    );
    render(<RunPlanPanel reviewId="review-1" />);

    const group = await screen.findByTestId("run-plan-reused");
    expect(group).toHaveTextContent("Reused results · 1");
    const row = within(group).getByTestId("run-plan-entry-checks.test");
    expect(row).toHaveTextContent("Reused result");
    expect(screen.getByTestId("run-plan-satisfied-checks.test")).toHaveTextContent(
      "From provider CI · CI / unit tests · PASS · head bbbbbbbbbbbb",
    );
    expect(within(row).getByRole("link", { name: "run" })).toHaveAttribute("href", "https://ci.example.invalid/run/7");
    expect(screen.queryByTestId("run-plan-required-blockers")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-plan-fast-lane")).toHaveTextContent(
      "Fast lane on this push: push with no risk signal, sensitive path or depth floor · not running ai.investigators",
    );
    expect(screen.getByText("ci_equivalents[1].provider is required for provider_ci")).toBeInTheDocument();
  });

  it("shows each entry's test selection with its reason, the head-first base and compute saved versus spent", async () => {
    const selected = entry("checks.test", {
      title: "Affected tests",
      sides: "head",
      base_on_demand: true,
      estimated_minutes: 1,
      reason: "head first: the base runs only if the head fails, to tell pre-existing from introduced failures",
      selection: {
        map: "impact:sha256:" + "c".repeat(64),
        recipes: {
          unit: { recipe_id: "unit", mode: "impact", reason: "selected by the trusted base's import graph", total: 8, tests: ["tests/test_alpha.py"], input: "" },
          e2e: { recipe_id: "e2e", mode: "all", reason: "full suite: a global input changed (pyproject.toml)", total: 3, input: "" },
        },
      },
    });
    const required = entry("checks.recipe:smoke", {
      title: "Smoke",
      required: true,
      origin: "policy_floor",
      selection: {
        map: "",
        recipes: { smoke: { recipe_id: "smoke", mode: "all", reason: "full suite: selection never narrows policy-required work", total: 2, input: "" } },
      },
    });
    const skipped = entry("checks.recipe:docs", {
      title: "Docs tests",
      status: "skipped",
      reason: "no test reaches the change: documentation only",
      selection: {
        map: "",
        recipes: { docs: { recipe_id: "docs", mode: "none", reason: "no test reaches the change: documentation only", total: 4, input: "" } },
      },
    });
    api.fetchRunPlan.mockResolvedValue(
      projection(plan([selected, required, skipped]), {
        economy: {
          spent_minutes: 4,
          saved_minutes: 2.25,
          saved_measured_minutes: 1.5,
          saved_estimated_minutes: 0.75,
          rows: [
            { entry_id: "checks.test", reason: "base_shared", label: "Base result shared from another Review", basis: "measured", minutes: 1.5, runs: 2 },
            { entry_id: "checks.test", reason: "impact_selection", label: "Tests the change cannot affect", basis: "estimated", minutes: 0.75, runs: 1 },
          ],
        },
      }),
    );
    render(<RunPlanPanel reviewId="review-1" />);

    const row = await screen.findByTestId("run-plan-entry-checks.test");
    expect(row).toHaveTextContent("head first · base on demand");
    expect(row).not.toHaveTextContent("base + head");
    const lines = within(row).getAllByTestId("run-plan-selection-checks.test").map((node) => node.textContent);
    expect(lines).toEqual([
      "Test impact: unit: 1 of 8 test files · selected by the trusted base's import graph",
      "Test impact: e2e: full suite: a global input changed (pyproject.toml)",
    ]);
    expect(screen.getByTestId("run-plan-entry-checks.recipe:smoke")).toHaveTextContent(
      "Test impact: full suite: selection never narrows policy-required work",
    );
    // A skipped entry's reason is its selection; it is shown once.
    const docs = screen.getByTestId("run-plan-entry-checks.recipe:docs");
    expect(docs).toHaveTextContent("no test reaches the change: documentation only");
    expect(within(docs).queryByTestId("run-plan-selection-checks.recipe:docs")).not.toBeInTheDocument();

    const economy = screen.getByTestId("run-plan-economy");
    expect(economy).toHaveTextContent("Compute saved: 2.3 min · spent 4 min (0.8 min estimated)");
    expect(economy).toHaveTextContent("checks.test · Base result shared from another Review · 1.5 min measured (2 runs)");
    expect(economy).toHaveTextContent("checks.test · Tests the change cannot affect · 0.8 min estimated");
  });

  it("shows no compute saved line when nothing was saved", async () => {
    api.fetchRunPlan.mockResolvedValue(
      projection(plan(ENTRIES), {
        economy: { spent_minutes: 3, saved_minutes: 0, saved_measured_minutes: 0, saved_estimated_minutes: 0, rows: [] },
      }),
    );
    render(<RunPlanPanel reviewId="review-1" />);
    await screen.findByTestId("run-plan-panel");
    expect(screen.queryByTestId("run-plan-economy")).not.toBeInTheDocument();
  });

  it("names why a push keeps the full plan", async () => {
    api.fetchRunPlan.mockResolvedValue(
      projection(
        plan(ENTRIES, {
          trigger: "push",
          fast_lane: { eligible: false, reasons: ["sensitive paths changed: src/auth.py"], skipped: [] },
        }),
      ),
    );
    render(<RunPlanPanel reviewId="review-1" />);
    expect(await screen.findByTestId("run-plan-fast-lane")).toHaveTextContent(
      "Full plan on this push: sensitive paths changed: src/auth.py",
    );
  });

  it("shows why deep review escalated, from where, and each raised entry", async () => {
    const trigger = {
      class: "security_chains",
      label: "security chain at p1 or higher (py.sqli) in src/report.py",
      paths: ["src/report.py"],
      path_count: 1,
      origin: "organization policy",
    };
    const escalated = [
      entry("ai.investigators", {
        title: "AI investigators",
        depth: "thorough",
        origin: "escalation",
        reason: `escalated to thorough: ${trigger.label}`,
        escalation: { state: "escalated", from: "auto", to: "thorough", triggers: [trigger] },
      }),
      entry("ai.hypotheses", {
        title: "AI hypotheses",
        status: "limited_by_budget",
        origin: "escalation",
        reason: "Limited by budget: escalation does not fit; the deep-review ceiling allows 3 model call(s)",
        escalation: { state: "limited_by_budget", from: "", to: "", triggers: [trigger] },
      }),
    ];
    api.fetchRunPlan.mockResolvedValue(
      projection(
        plan(escalated, {
          escalation: {
            state: "limited_by_budget",
            to: "thorough",
            to_origin: "built-in default",
            reason: `Limited by budget: escalated to thorough: ${trigger.label}`,
            triggers: [trigger],
            suppressed: [],
            ceiling: { max_model_calls: 3, origin: "organization policy" },
            entries: ["ai.investigators", "ai.hypotheses"],
          },
        }),
      ),
    );
    render(<RunPlanPanel reviewId="review-1" />);

    const line = await screen.findByTestId("run-plan-escalation");
    // Budget-limited escalation is explicit, never silently gone.
    expect(line).toHaveAttribute("data-state", "limited_by_budget");
    expect(line).toHaveTextContent(`Deep review: Limited by budget: escalated to thorough: ${trigger.label}`);
    expect(line).toHaveTextContent("thresholds from organization policy");
    const investigators = screen.getByTestId("run-plan-entry-ai.investigators");
    expect(investigators).toHaveTextContent("escalated");
    expect(screen.getByTestId("run-plan-escalation-ai.investigators")).toHaveTextContent("Escalated from Auto to Thorough");
    expect(screen.getByTestId("run-plan-escalation-ai.hypotheses")).toHaveTextContent("Escalation limited by budget");
    expect(screen.getByTestId("run-plan-entry-ai.hypotheses")).toHaveTextContent("deep-review ceiling allows 3");
  });

  it("shows an author track record that is unknown past its bound, even when nothing escalated", async () => {
    const note =
      "author track record of claude/claude-sonnet-4-5 is unknown beyond the 200 most recent Reviews of the last 90 days";
    api.fetchRunPlan.mockResolvedValue(
      projection(
        plan(ENTRIES, {
          escalation: {
            state: "none",
            to: "thorough",
            to_origin: "built-in default",
            reason: "no deterministic escalation signal",
            triggers: [],
            suppressed: [],
            ceiling: { max_model_calls: null, origin: "built-in default" },
            entries: [],
            notes: [note],
          },
        }),
      ),
    );
    render(<RunPlanPanel reviewId="review-1" />);
    expect(await screen.findByTestId("run-plan-escalation-note")).toHaveTextContent(`Deep review: ${note}`);
    expect(screen.queryByTestId("run-plan-escalation")).not.toBeInTheDocument();
  });

  it("names the agent/model and the metric when a poor author track record escalates", async () => {
    const trigger = {
      class: "author_track_record",
      label:
        "author track record of claude/claude-sonnet-4-5: escaped defects: 2 Changes failed or were rolled back in production in Reviews of the last 90 days",
      paths: [],
      path_count: 0,
      origin: "built-in default",
    };
    api.fetchRunPlan.mockResolvedValue(
      projection(
        plan(
          [
            entry("ai.investigators", {
              title: "AI investigators",
              depth: "thorough",
              origin: "escalation",
              reason: `escalated to thorough: ${trigger.label}`,
              escalation: { state: "escalated", from: "auto", to: "thorough", triggers: [trigger] },
            }),
          ],
          {
            escalation: {
              state: "escalated",
              to: "thorough",
              to_origin: "built-in default",
              reason: `escalated to thorough: ${trigger.label}`,
              triggers: [trigger],
              suppressed: [],
              ceiling: { max_model_calls: null, origin: "built-in default" },
              entries: ["ai.investigators"],
            },
          },
        ),
      ),
    );
    render(<RunPlanPanel reviewId="review-1" />);
    const line = await screen.findByTestId("run-plan-escalation");
    expect(line).toHaveAttribute("data-state", "escalated");
    expect(line).toHaveTextContent("author track record of claude/claude-sonnet-4-5: escaped defects: 2 Changes");
    expect(screen.getByTestId("run-plan-escalation-ai.investigators")).toHaveTextContent("Escalated from Auto to Thorough");
  });

  it("says when deep review is disabled for the changed paths and shows nothing when no signal fired", async () => {
    const suppressed = {
      state: "suppressed" as const,
      to: "thorough" as const,
      to_origin: "built-in default",
      reason: "not escalated: deep review is disabled for src/legacy/billing.py (organization policy)",
      triggers: [],
      suppressed: [
        {
          class: "criticality",
          label: "path criticality high or higher: src/legacy/billing.py",
          paths: ["src/legacy/billing.py"],
          path_count: 1,
          origin: "built-in default",
          reason: "deep review is disabled for src/legacy/billing.py (organization policy)",
        },
      ],
      ceiling: { max_model_calls: null, origin: "built-in default" },
      entries: [],
    };
    api.fetchRunPlan.mockResolvedValue(projection(plan(ENTRIES, { escalation: suppressed })));
    const { unmount } = render(<RunPlanPanel reviewId="review-1" />);
    expect(await screen.findByTestId("run-plan-escalation")).toHaveTextContent(
      "Deep review: not escalated: deep review is disabled for src/legacy/billing.py (organization policy)",
    );
    unmount();

    api.fetchRunPlan.mockResolvedValue(
      projection(plan(ENTRIES, { escalation: { ...suppressed, state: "none", reason: "no deterministic escalation signal", suppressed: [] } })),
    );
    render(<RunPlanPanel reviewId="review-1" />);
    await screen.findByTestId("run-plan-panel");
    expect(screen.queryByTestId("run-plan-escalation")).not.toBeInTheDocument();
  });

  it("shows rule questions within their ceiling, the path default and an unavailable AI provider", async () => {
    const rules = entry("ai.rule_checks", {
      title: "Focused rule checks",
      estimated_model_calls: 2,
      reason: "Limited by budget: 2 of 3 rule questions fit; the rule-check ceiling is 2 model call(s) (organization policy); 1 not asked",
      rule_checks: {
        ceiling: 2,
        ceiling_origin: "organization policy",
        planned: 2,
        limited: 1,
        questions: [],
      },
    });
    api.fetchRunPlan.mockResolvedValue(
      projection(
        plan([...ENTRIES, rules], {
          selection: {
            source: "preset",
            name: "quick",
            base: "quick",
            path_default: "quick",
            path_default_paths: "docs/**",
            path_default_origin: "base Snapshot .lemoncrow/review.yaml",
          },
        }),
        { ai_availability: { available: false, reason: "no AI provider is configured: no API key", executor: "local" } },
      ),
    );
    render(<RunPlanPanel reviewId="review-1" />);

    expect(await screen.findByTestId("run-plan-rule-checks-ai.rule_checks")).toHaveTextContent(
      "2 rule questions · ceiling 2 (organization policy) · 1 limited by budget",
    );
    expect(screen.getByTestId("run-plan-path-default")).toHaveTextContent(
      "Starting depth: Quick for docs/** (path default from base Snapshot .lemoncrow/review.yaml)",
    );
    expect(screen.getByTestId("run-plan-ai-unavailable")).toHaveTextContent(
      "AI review unavailable: no AI provider is configured: no API key. Deterministic checks still run.",
    );
    // No new top-level control: the panel's only action stays Run specific….
    expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual(["Run specific…"]);
  });

  it("reports an unavailable plan without guessing", async () => {
    api.fetchRunPlan.mockRejectedValue(new Error("409"));
    render(<RunPlanPanel reviewId="review-1" />);
    expect(await screen.findByText("Run plan unavailable: 409")).toBeInTheDocument();
  });
});
