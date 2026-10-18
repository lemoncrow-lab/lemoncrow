import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReviewBrief from "./ReviewBrief";
import type {
  ReviewAiAssurance,
  ReviewDecisionState,
  ReviewProjection,
  ReviewProjectionDecision,
  ReviewProjectionItem,
  ReviewTarget,
} from "./types";

function assurance(overrides: Partial<ReviewAiAssurance> = {}): ReviewAiAssurance {
  return {
    identity: {
      driver: "openai",
      requested_model: "gpt-4.1",
      observed_models: ["gpt-4.1-2025-04-14"],
      observed_truncated: false,
      source: "observed",
      label: "gpt-4.1-2025-04-14",
    },
    independence: {
      status: "independent",
      independent: true,
      reason: "every reviewer model is outside the author's model family (anthropic, author claude/claude-sonnet-4-5)",
      author: { host: "claude", model: "claude-sonnet-4-5", family: "anthropic", label: "claude/claude-sonnet-4-5" },
      reviewers: [],
      reviewers_not_shown: 0,
      policy: {
        mode: "require",
        origin: { layer: "organization", source: "operator_review_policy" },
        paths: [],
        matched_paths: [],
        matched_path_count: 0,
      },
      requirement: "satisfied",
    },
    execution: { latest_mode: "executed", fresh_reruns: 0, fresh_rerun_runs: 0, latest_fresh_at: "", runs: 2 },
    fresh_rerun: {
      available: true,
      reason: "",
      effort: "thorough",
      entries: ["ai.investigators", "ai.hypotheses"],
      estimated_model_calls: 6,
      budget: { calls_used: 3, max_calls: 20, remaining: 17 },
      within_budget: true,
      cost_class: "model",
      execution_locus: "managed",
      metering_owner: "lemoncrow_managed",
      model_data_locus: "managed",
      provider: "openai",
      model: "gpt-4.1",
      reuses_cache: false,
    },
    ...overrides,
  };
}

function target(id: string, path: string, symbol: string): ReviewTarget {
  return {
    target_id: id,
    unit_key: `unit:${id}`,
    kind: "symbol",
    path,
    label: `${path}::${symbol}`,
    symbol,
    start_line: 10,
    end_line: 20,
    hunk_ordinals: [0],
    spans: [{ side: "new", start_line: 12, end_line: 12, hunk_ordinal: 0 }],
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: 0,
    attention_level: "normal",
    reasons: [],
    additions: 1,
    deletions: 0,
    fingerprint_method: "symbol_body_sha256",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
  };
}

const TARGETS = [
  target("t-resolve", "src/review/session.py", "resolve_review"),
  target("t-prune", "src/review/store.py", "prune_checkpoints"),
  target("t-fmt", "pkg/fmt.go", "f"),
  target("t-docs", "docs/notes.md", ""),
];

function item(id: string, options: Partial<ReviewProjectionItem> = {}): ReviewProjectionItem {
  return {
    id,
    key: id,
    kind: "area",
    title: id,
    before: "",
    after: "",
    rationale: "",
    rationale_origin: "none",
    source_label: "LemonCrow observation",
    producer: "lemoncrow",
    stale: false,
    attention: "understand",
    tier: 4,
    position: 0,
    primary_target_ids: [],
    related_target_ids: [],
    claim_ids: [],
    question_ids: [],
    concerns: [],
    unresolved_refs: [],
    ...options,
  };
}

function projection(options: Partial<ReviewProjection> = {}): ReviewProjection {
  return {
    projection_version: 1,
    review_id: "r1",
    revision_id: "rev-1",
    snapshot_number: 3,
    historical: false,
    plan: {
      schema_version: 1,
      input_digest: "abc",
      intents: [{ producer: "claude:cl-1", intent: "Reviews now remain with their branch." }],
      limitations: [],
    },
    actions: [],
    questions: [
      {
        id: "q:concern:branch-identity:0",
        reason: "claim_evidence_scope_mismatch",
        title: "Branch identity: legacy migration",
        detail: "Existing persisted reviews were not exercised.",
        item_ids: ["item:branch-identity"],
        target_ids: ["t-resolve"],
        actions: ["inspect_evidence", "ask", "open_implementation"],
        tier: 3,
      },
    ],
    items: [
      item("item:branch-identity", {
        key: "branch-identity",
        kind: "behavior",
        title: "Branch identity",
        before: "Branch changes could reuse the wrong review.",
        after: "Each branch resolves its own review.",
        rationale: "Preserve a continuous review across commits.",
        rationale_origin: "author",
        source_label: "Author's agent note",
        producer: "claude:cl-1",
        attention: "review_first",
        tier: 3,
        primary_target_ids: ["t-resolve"],
        claim_ids: ["claim:branch-identity"],
        question_ids: ["q:concern:branch-identity:0"],
        concerns: [{ scope: "legacy migration", reason: "Existing persisted reviews were not exercised." }],
      }),
      item("area:intent:storage", { title: "Storage", primary_target_ids: ["t-prune"], position: 1 }),
      item("group:routine", {
        kind: "mechanical",
        title: "Routine changes",
        attention: "routine",
        tier: 5,
        position: 2,
        primary_target_ids: ["t-fmt"],
        rationale: "Proven comment-only or formatting-only by a parser; collapsed until you open them.",
      }),
      item("area:intent:docs", { title: "Docs", attention: "done", tier: 6, position: 3, primary_target_ids: ["t-docs"] }),
    ],
    claims: [
      {
        id: "claim:branch-identity",
        statement: "Each branch resolves its own review.",
        source: "claude:cl-1",
        target_ids: ["t-resolve"],
        limitations: ["Not covered: legacy migration — Existing persisted reviews were not exercised."],
        evidence: [
          {
            key: "pytest review",
            title: "pytest review",
            status: "PASS",
            state: "current",
            snapshot_number: 3,
            mapping: "explicit",
            relevance: "Cited by the author",
          },
        ],
        summary: "partial",
        summary_text: "Partial evidence",
        result_line: "1 linked check passed on Snapshot 3",
      },
    ],
    decisions: [
      {
        key: "checkpoint-retention",
        producer: "claude:cl-1",
        question: "How long should committed checkpoints be retained?",
        options: [{ key: "indefinite", label: "Retain indefinitely" }, { key: "ninety-days", label: "Expire after 90 days" }],
        selected_option: "indefinite",
        reason_human_required: "The product requirement does not specify retention.",
        assumptions: [],
        contract_scope: [],
        relied_checks: [],
        target_ids: ["t-prune"],
        stale: false,
        read_only: true,
      },
    ],
    coverage: {
      target_count: 4,
      author_explained: 1,
      fallback_grouped: 2,
      other: 0,
      routine: 1,
      unresolved_refs: 0,
      unexplained_targets: 2,
      file_count: 4,
    },
    counts: { review_first: 1, understand: 1, routine: 1, done: 1, decisions: 1 },
    queue: ["t-resolve", "t-prune", "t-fmt", "t-docs"],
    ...options,
  };
}

function renderBrief(value: ReviewProjection, overrides: Partial<Parameters<typeof ReviewBrief>[0]> = {}) {
  const handlers = {
    onOpenTarget: vi.fn(),
    onOpenFiles: vi.fn(),
    onInspectEvidence: vi.fn(),
    onAsk: vi.fn(),
  };
  render(<ReviewBrief projection={value} loading={false} error="" targets={TARGETS} {...handlers} {...overrides} />);
  return handlers;
}

describe("ReviewBrief", () => {
  it("defers architecture Map work for a large change until the reviewer asks for it", async () => {
    const loadArchitectureDelta = vi.fn().mockResolvedValue(null);
    const targets = Array.from({ length: 500 }, (_, index) => ({
      ...TARGETS[0],
      target_id: `large-target-${index}`,
      unit_key: `large-unit-${index}`,
      path: `src/large/file-${index}.ts`,
    }));

    renderBrief(projection(), {
      targets,
      loadArchitectureDelta,
      onOpenArchitecture: vi.fn(),
    });
    await Promise.resolve();

    expect(loadArchitectureDelta).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Check architecture changes" }));
    expect(loadArchitectureDelta).toHaveBeenCalledTimes(1);
  });

  it("renders production outcomes after the merge, never on a historical Snapshot", () => {
    renderBrief(projection(), { productionOutcomes: <div data-testid="production-slot">After merge</div> });
    expect(screen.getByTestId("production-slot")).toHaveTextContent("After merge");
    cleanup();
    renderBrief({ ...projection(), historical: true }, { productionOutcomes: <div data-testid="production-slot" /> });
    expect(screen.queryByTestId("production-slot")).toBeNull();
  });

  it("shows Review priority and its explanation as one collapsed footer line without new controls", () => {
    renderBrief(projection());
    expect(screen.queryByTestId("brief-review-priority")).toBeNull();
    const controlsWithout = screen.getAllByRole("button").length;
    cleanup();
    const origin = { layer: "trusted_base_repository", source: "base Snapshot .lemoncrow/review.yaml", commit: "abc123" };
    renderBrief(projection({
      triage: {
        version: 1,
        review_id: "r1",
        revision_id: "rev-1",
        priority: "high",
        computed_priority: "high",
        criticality: "critical",
        severity_summary: { p0: 0, p1: 0, p2: 0, suggestion: 0, unrated: 0, stale: 0, below_threshold: 0, highest: null },
        finding_threshold: { value: "medium", label: "Medium+", origin },
        evidence_state: "missing",
        review_state: "not_reviewed",
        evidence_label: "Not reviewed · current evidence missing",
        release_urgency: "unknown",
        dependency_changes: 0,
        required_failures: 0,
        override: null,
        explanation: [{ field: "criticality", value: "critical", reason: "src/review/session.py matched src/** (strictest rule wins)", origin }],
        hidden_finding_ids: [],
        policy_state: "final",
        policy_errors: [],
        computed: true,
      },
    }));
    const line = screen.getByTestId("brief-review-priority");
    expect(line.closest("footer")).not.toBeNull();
    expect(line.querySelector("summary")?.textContent).toBe("Priority High · Critical criticality · Not reviewed · current evidence missing");
    expect(within(line).getByTestId("triage-explanation").textContent).toContain("trusted base · base Snapshot .lemoncrow/review.yaml@abc123");
    expect(screen.getAllByRole("button")).toHaveLength(controlsWithout);
  });

  it("changes presentation detail without another model run or hiding required checks", async () => {
    window.localStorage.setItem("lemoncrow.review.detail", "standard");
    const onRunAiReview = vi.fn();
    const value = projection({
      actions: [{
        id: "verify:required",
        reason: "required verification",
        title: "Required verification failed",
        detail: "Run the required contract check.",
        target_ids: ["t-resolve"],
        item_ids: ["item:branch-identity"],
        actions: ["Inspect evidence"],
        tier: 1,
      }],
      items: projection().items.map((item, index) => index === 0
        ? { ...item, before: "old behavior", after: "new behavior", rationale: "Detailed rationale" }
        : item),
    });
    renderBrief(value, { onRunAiReview });

    expect(screen.getByText(/Before: old behavior/)).toBeInTheDocument();
    expect(screen.getByTestId("review-detail-options")).not.toHaveAttribute("open");
    await userEvent.click(screen.getByText("View options · Standard"));
    await userEvent.click(screen.getByRole("radio", { name: "Compact" }));
    expect(screen.queryByText(/Before: old behavior/)).toBeNull();
    expect(screen.getByText("Required verification failed")).toBeInTheDocument();
    expect(onRunAiReview).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("radio", { name: "Detailed" }));
    expect(screen.getByText(/Why ·/)).toBeInTheDocument();
    expect(screen.getByText("Required verification failed")).toBeInTheDocument();
    expect(onRunAiReview).not.toHaveBeenCalled();
  });

  it("shows the short summary first and keeps review details open below it", () => {
    renderBrief(projection({
      summary: {
        paragraphs: ["Each branch retains its own review."],
        origin: "author",
        scope: "4 files · 4 changed targets",
        verification: "No current verification results recorded.",
        attention: "1 open decision",
        additional_explanations: 2,
      },
      synthesis: { status: "disabled", model: "", error: "AI explanations are disabled; set LEMONCROW_LLM_BACKEND to enable them" },
    }));
    expect(screen.getByRole("region", { name: "Change summary" })).toHaveTextContent("Each branch retains its own review.");
    expect(screen.getByTestId("review-brief")).toHaveTextContent("Human decisions · 1");
    expect(screen.getByText("AI explanations are unavailable. The Brief still uses recorded context and checks.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Explain unexplained changes" })).toBeNull();
    expect(screen.getByTestId("brief-item-item:branch-identity")).toBeInTheDocument();
    expect(screen.getByText(/View options ·/)).toBeInTheDocument();
  });

  it("labels AI text as inferred when a cached AI summary is present", () => {
    renderBrief(projection({
      summary: {
        paragraphs: ["Preserves review identity across branch switches."],
        origin: "inferred",
        scope: "4 files · 4 changed targets",
        verification: "Recorded checks: 1 passed, 0 failed.",
        attention: "",
        additional_explanations: 0,
      },
    }));
    expect(screen.getByText("AI summary · intent inferred")).toBeInTheDocument();
  });

  it("orders sections as the spec's handoff and omits nothing that has content", () => {
    renderBrief(projection());
    const headings = screen.getAllByRole("heading", { level: 3 }).map((node) => node.textContent);
    expect(headings).toEqual(["Human decisions · 1", "Review first · 1", "Understand · 1", "Routine · 1 change collapsed", "Reviewed · 1"]);
    expect(screen.getByText("Reviews now remain with their branch.")).toBeInTheDocument();
    expect(screen.getByText(/2 changed targets have no author explanation/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open files" })).toBeInTheDocument();
    expect(screen.getByText(/All code: 4 files, 4 targets/)).toBeInTheDocument();
  });

  it("renders captured author output through normal Brief intent and Understand items", async () => {
    const base = projection();
    renderBrief({
      ...base,
      plan: {
        ...base.plan,
        intents: [{ producer: "claude:plan-session", intent: "Keep review history isolated by branch", origin: "captured" }],
      },
      items: [
        ...base.items,
        item("agent:ctx-code", {
          kind: "behavior",
          title: "Implemented branch-local review identity",
          rationale: "Implemented branch-local review identity and retained previous marks.",
          rationale_origin: "author",
          source_label: "Captured author · Code",
          producer: "claude:code-session",
          attention: "understand",
          tier: 4,
          position: null,
        }),
      ],
    });

    expect(screen.getByText("Keep review history isolated by branch")).toBeInTheDocument();
    expect(screen.getByText("Captured plan · claude:plan-session")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Understand · 2" })).toBeInTheDocument();
    const card = screen.getByTestId("brief-item-agent:ctx-code");
    expect(within(card).getByText("Implemented branch-local review identity")).toBeInTheDocument();
    expect(within(card).getByText("Captured author · Code")).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Implemented branch-local review identity" }));
    expect(within(card).getByText(/retained previous marks/)).toBeInTheDocument();
    expect(screen.queryByText("Potential stale binding")).toBeNull();
  });

  it("shows author-proposed decisions read-only, without actions", () => {
    renderBrief(projection());
    const card = screen.getByTestId("brief-decision-checkpoint-retention");
    expect(within(card).getByText("Proposed by the author · read-only")).toBeInTheDocument();
    expect(within(card).getByText("Retain indefinitely")).toBeInTheDocument();
    expect(within(card).getByText("Expire after 90 days")).toBeInTheDocument();
    expect(within(card).getByText("The product requirement does not specify retention.")).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: /accept|request change|defer/i })).toBeNull();
  });

  it("labels evidence by who linked it and keeps the recorded result beneath", async () => {
    renderBrief(projection());
    const card = screen.getByTestId("brief-item-item:branch-identity");
    expect(within(card).getByText("Partial evidence")).toBeInTheDocument();
    expect(within(card).getByText("1 linked check passed on Snapshot 3")).toBeInTheDocument();
    expect(within(card).getByText("Author's agent note")).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: /Branch identity/ }));
    expect(within(card).getByText(/Not covered: legacy migration/)).toBeInTheDocument();
    expect(within(card).getByText("Cited by the author")).toBeInTheDocument();
  });

  it("jumps to the exact implementation target and runs question actions", async () => {
    const handlers = renderBrief(projection());
    const card = screen.getByTestId("brief-item-item:branch-identity");
    await userEvent.click(within(card).getByRole("button", { name: "Open code" }));
    expect(handlers.onOpenTarget).toHaveBeenCalledWith("t-resolve");
    await userEvent.click(within(card).getByRole("button", { name: "Inspect evidence" }));
    expect(handlers.onInspectEvidence).toHaveBeenCalledWith("t-resolve");
    await userEvent.click(within(card).getByRole("button", { name: "Ask" }));
    expect(handlers.onAsk).toHaveBeenCalledWith("t-resolve");
    await userEvent.click(screen.getByRole("button", { name: "Open files" }));
    expect(handlers.onOpenFiles).toHaveBeenCalled();
  });

  it("keeps Routine collapsed until asked", async () => {
    const handlers = renderBrief(projection());
    expect(screen.queryByRole("button", { name: /pkg\/fmt\.go/ })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    await userEvent.click(screen.getByRole("button", { name: /pkg\/fmt\.go/ }));
    expect(handlers.onOpenTarget).toHaveBeenCalledWith("t-fmt");
  });

  it("puts a failing required check first even with no guidance", () => {
    renderBrief(projection({
      plan: { schema_version: 1, input_digest: "x", intents: [], limitations: [] },
      questions: [],
      decisions: [],
      claims: [],
      items: [item("area:intent:storage", { title: "Storage", primary_target_ids: ["t-prune"] })],
      actions: [{
        id: "q:required:browser:checkout",
        reason: "required_browser_scenario_failed",
        title: "Required check failing",
        detail: "browser:checkout failed on Snapshot 3. No specific change item association is known.",
        item_ids: [],
        target_ids: [],
        actions: ["inspect_evidence"],
        tier: 1,
      }],
      counts: { review_first: 0, understand: 1, routine: 0, done: 0, decisions: 0 },
    }));
    const headings = screen.getAllByRole("heading", { level: 3 }).map((node) => node.textContent);
    expect(headings[0]).toBe("Required checks · 1");
    expect(screen.getByText(/No specific change item association is known/)).toBeInTheDocument();
  });

  it("shows captured author implementation context directly in Understand", () => {
    renderBrief(projection({
      questions: [],
      decisions: [],
      claims: [],
      items: [
        item("agent:ann-code", {
          kind: "behavior",
          title: "Implemented binding reuse",
          rationale: "Implemented binding reuse and kept the existing branch base fixed.",
          rationale_origin: "author",
          source_label: "Captured author · Code",
          producer: "codex:code-2",
          primary_target_ids: [],
          related_target_ids: [],
        }),
      ],
      counts: { review_first: 0, understand: 1, routine: 0, done: 0, decisions: 0 },
    }));

    expect(screen.getByRole("heading", { name: "Understand · 1" })).toBeInTheDocument();
    const card = screen.getByTestId("brief-item-agent:ann-code");
    expect(within(card).getByText("Implemented binding reuse")).toBeInTheDocument();
    expect(within(card).getByText("Implemented binding reuse and kept the existing branch base fixed.")).toBeInTheDocument();
    expect(within(card).getByText("Captured author · Code")).toBeInTheDocument();
    expect(within(card).queryByText(/no author explanation/i)).toBeNull();
  });

  it("keeps a tiny change small: no empty ceremony", () => {
    renderBrief(projection({
      plan: { schema_version: 1, input_digest: "x", intents: [], limitations: [] },
      questions: [],
      decisions: [],
      claims: [],
      items: [item("area:intent:storage", { title: "Storage", primary_target_ids: ["t-prune"] })],
      coverage: { target_count: 1, author_explained: 0, fallback_grouped: 1, other: 0, routine: 0, unresolved_refs: 0, unexplained_targets: 1, file_count: 1 },
      counts: { review_first: 0, understand: 1, routine: 0, done: 0, decisions: 0 },
      queue: ["t-prune"],
    }));
    const headings = screen.getAllByRole("heading", { level: 3 }).map((node) => node.textContent);
    expect(headings).toEqual(["Understand · 1"]);
    expect(screen.getByText("No open decisions")).toBeInTheDocument();
    expect(screen.getByText(/All code: 1 file, 1 target/)).toBeInTheDocument();
  });

  it("shows the most urgent five and a Show all control for the rest", async () => {
    const many = Array.from({ length: 7 }, (_, index) => item(`area:${index}`, { title: `Area ${index}`, position: index, primary_target_ids: ["t-prune"] }));
    renderBrief(projection({ items: many, questions: [], decisions: [], claims: [] }));
    expect(screen.getAllByTestId(/^brief-item-area:/)).toHaveLength(5);
    await userEvent.click(screen.getByRole("button", { name: "Show all 7" }));
    expect(screen.getAllByTestId(/^brief-item-area:/)).toHaveLength(7);
  });
});

function decisionState(overrides: Partial<ReviewDecisionState> = {}): ReviewDecisionState {
  return {
    disposition: "open",
    validity: null,
    changed_inputs: [],
    outstanding: true,
    navigable: true,
    withdrawn: false,
    trigger: null,
    trigger_fired: false,
    accepted_option: "",
    latest_event_id: "",
    latest_action: "",
    latest_reason: "",
    event_snapshot_number: null,
    reason: "decision_open",
    detail: "Needs your decision",
    tier: 3,
    limitations: [],
    ...overrides,
  };
}

function actionable(
  overrides: Partial<ReviewProjectionDecision> = {},
  state: Partial<ReviewDecisionState> = {},
): ReviewProjectionDecision {
  return {
    ...projection().decisions[0],
    read_only: false,
    conflict: false,
    positions: [],
    actions: ["accept", "request_change", "ask", "defer"],
    relied_checks: ["browser:checkout"],
    state: decisionState(state),
    ...overrides,
  };
}

describe("ReviewBrief decisions", () => {
  it("accepts the chosen option, the author's choice preselected", async () => {
    const onDecision = vi.fn().mockResolvedValue(undefined);
    renderBrief(projection({ decisions: [actionable()] }), { onDecision });
    const card = screen.getByTestId("brief-decision-checkpoint-retention");
    expect(within(card).queryByText("Proposed by the author · read-only")).toBeNull();
    expect(within(card).getByRole("radio", { name: /Retain indefinitely/ }).getAttribute("aria-checked")).toBe("true");
    await userEvent.click(within(card).getByRole("radio", { name: /Expire after 90 days/ }));
    await userEvent.click(within(card).getByRole("button", { name: "Accept decision" }));
    expect(onDecision).toHaveBeenCalledWith("checkpoint-retention", { action: "accept", option_key: "ninety-days" });
  });

  it("offers Reconfirm only when the implementation changed", async () => {
    const onDecision = vi.fn().mockResolvedValue(undefined);
    const accepted = { disposition: "accepted" as const, validity: "current" as const, accepted_option: "indefinite", outstanding: false, navigable: false, reason: "", tier: 6 };
    const { unmount } = render(
      <ReviewBrief projection={projection({ decisions: [actionable({ actions: ["ask", "reopen"] }, accepted)] })} loading={false} error="" targets={TARGETS} onOpenTarget={vi.fn()} onOpenFiles={vi.fn()} onInspectEvidence={vi.fn()} onAsk={vi.fn()} onDecision={onDecision} />,
    );
    let card = screen.getByTestId("brief-decision-checkpoint-retention");
    expect(within(card).getByText("Accepted · Retain indefinitely")).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "Reconfirm" })).toBeNull();
    unmount();

    renderBrief(
      projection({
        decisions: [
          actionable(
            { actions: ["reconfirm", "accept", "request_change", "ask", "reopen"] },
            { ...accepted, validity: "implementation_changed_since_acceptance", outstanding: true, navigable: true, reason: "decision_implementation_changed", tier: 2, changed_inputs: ["unit:t-prune changed since acceptance"] },
          ),
        ],
      }),
      { onDecision },
    );
    card = screen.getByTestId("brief-decision-checkpoint-retention");
    expect(within(card).getByText("Code changed since acceptance")).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Reconfirm" }));
    expect(onDecision).toHaveBeenCalledWith("checkpoint-retention", { action: "reconfirm" });
  });

  it("names what changed when a decision needs reconsideration", () => {
    renderBrief(
      projection({
        decisions: [
          actionable(
            { actions: ["accept", "request_change", "ask", "defer", "reopen"] },
            { disposition: "accepted", validity: "needs_reconsideration", accepted_option: "indefinite", reason: "decision_scope_changed", tier: 2, changed_inputs: ["Check 'browser:checkout' is now FAIL (was PASS when accepted)"] },
          ),
        ],
      }),
      { onDecision: vi.fn() },
    );
    const card = screen.getByTestId("brief-decision-checkpoint-retention");
    expect(within(card).getByText("Needs reconsideration")).toBeInTheDocument();
    expect(within(card).getByText("Check 'browser:checkout' is now FAIL (was PASS when accepted)")).toBeInTheDocument();
  });

  it("defers with a reason and a typed trigger", async () => {
    const onDecision = vi.fn().mockResolvedValue(undefined);
    renderBrief(projection({ decisions: [actionable()] }), { onDecision });
    const card = screen.getByTestId("brief-decision-checkpoint-retention");
    await userEvent.click(within(card).getByRole("button", { name: "Defer" }));
    await userEvent.type(within(card).getByRole("textbox", { name: "Reason for deferring" }), "Waiting on product");
    await userEvent.click(within(card).getByRole("combobox", { name: "Revisit when" }));
    await userEvent.click(screen.getByRole("option", { name: "When browser:checkout has a result" }));
    await userEvent.click(within(card).getByRole("button", { name: "Defer decision" }));
    expect(onDecision).toHaveBeenCalledWith("checkpoint-retention", {
      action: "defer",
      reason: "Waiting on product",
      trigger: "check_available:browser:checkout",
    });
  });

  it("requests a change with written feedback", async () => {
    const onDecision = vi.fn().mockResolvedValue(undefined);
    renderBrief(projection({ decisions: [actionable()] }), { onDecision });
    const card = screen.getByTestId("brief-decision-checkpoint-retention");
    await userEvent.click(within(card).getByRole("button", { name: "Request change" }));
    await userEvent.type(within(card).getByRole("textbox", { name: "Requested change" }), "Expire after 90 days.");
    await userEvent.click(within(card).getByRole("button", { name: "Send request" }));
    expect(onDecision).toHaveBeenCalledWith("checkpoint-retention", { action: "request_change", feedback_body: "Expire after 90 days." });
  });

  it("lists unfired deferrals outside the main queue", () => {
    renderBrief(
      projection({
        decisions: [
          actionable(
            { actions: ["accept", "request_change", "ask", "reopen"] },
            { disposition: "deferred", navigable: false, reason: "", tier: 6, trigger: { kind: "new_snapshot", check_key: "", note: "" }, latest_reason: "Waiting on product" },
          ),
        ],
      }),
      { onDecision: vi.fn() },
    );
    const headings = screen.getAllByRole("heading", { level: 3 }).map((node) => node.textContent);
    expect(headings).toContain("Deferred decisions · 1");
    expect(headings.some((text) => text?.startsWith("Human decisions"))).toBe(false);
    expect(screen.getByText("Deferred until the next Snapshot · Waiting on product")).toBeInTheDocument();
  });

  it("shows each producer's position on a conflict", () => {
    renderBrief(
      projection({
        decisions: [
          actionable(
            {
              conflict: true,
              selected_option: "",
              positions: [
                { producer: "claude:cl-1", selected_option: "indefinite", option_keys: ["indefinite", "ninety-days"], assumptions: [] },
                { producer: "codex:cx-1", selected_option: "ninety-days", option_keys: ["indefinite", "ninety-days"], assumptions: [] },
              ],
            },
            { reason: "author_guidance_conflict", detail: "Authors disagree; choose an option" },
          ),
        ],
      }),
      { onDecision: vi.fn() },
    );
    const card = screen.getByTestId("brief-decision-checkpoint-retention");
    expect(within(card).getByText("Authors disagree")).toBeInTheDocument();
    expect(within(card).getByText("claude:cl-1 chose Retain indefinitely")).toBeInTheDocument();
    expect(within(card).getByText("codex:cx-1 chose Expire after 90 days")).toBeInTheDocument();
  });

  it("shows decision progress apart from code progress and keeps required checks separate", () => {
    renderBrief(
      projection({
        decisions: [actionable()],
        decision_progress: { total: 2, settled: 1, outstanding: 1, deferred: 0, withdrawn: 0 },
        actions: [
          { id: "decision:checkpoint-retention", reason: "decision_open", title: "How long?", detail: "Needs your decision", item_ids: [], target_ids: ["t-prune"], actions: ["accept"], tier: 3 },
        ],
      }),
      { onDecision: vi.fn() },
    );
    expect(screen.getByText("Decisions: 1 of 2 settled")).toBeInTheDocument();
    expect(screen.getByText("Code: 0 of 4 reviewed")).toBeInTheDocument();
    const headings = screen.getAllByRole("heading", { level: 3 }).map((node) => node.textContent);
    expect(headings.some((text) => text?.startsWith("Required checks"))).toBe(false);
  });
});

describe("ReviewBrief scoped request", () => {
  const inScopeTargetIds = new Set(["t-resolve", "t-prune"]);
  const withProgress = () => projection({ decision_progress: { total: 1, settled: 0, outstanding: 1, deferred: 0, withdrawn: 0 } });

  it("leads with the request and keeps outside items in one visible group", async () => {
    const onToggleOutside = vi.fn();
    renderBrief(withProgress(), { requestScope: { inScopeTargetIds, showOutside: false, onToggleOutside } });

    expect(screen.getByText("Code: 0 of 2 reviewed in your request")).toBeInTheDocument();
    const outside = screen.getByRole("region", { name: "Outside your request · 2" });
    expect(within(outside).getByText("Docs")).toBeInTheDocument();
    expect(within(outside).getByText("Routine changes")).toBeInTheDocument();
    expect(within(outside).getByText("Not part of your review request; still open to inspect.")).toBeInTheDocument();
    // Outside items are listed once, in the outside group, never dropped.
    expect(screen.getAllByText("Docs")).toHaveLength(1);
    expect(screen.queryByRole("region", { name: /^Reviewed ·/ })).toBeNull();
    expect(screen.queryByRole("region", { name: /^Routine ·/ })).toBeNull();
    // In-scope items keep their place.
    expect(screen.getAllByText("Branch identity").length).toBeGreaterThan(0);
    expect(screen.getByText("Storage")).toBeInTheDocument();

    const show = within(outside).getByRole("button", { name: "Show outside your request" });
    expect(show.getAttribute("aria-pressed")).toBe("false");
    await userEvent.click(show);
    expect(onToggleOutside).toHaveBeenCalledTimes(1);
  });

  it("shows everything in place once the reviewer asks for outside items", () => {
    renderBrief(withProgress(), { requestScope: { inScopeTargetIds, showOutside: true, onToggleOutside: vi.fn() } });

    expect(screen.getByText("Code: 0 of 4 reviewed")).toBeInTheDocument();
    const outside = screen.getByRole("region", { name: "Outside your request · 2" });
    expect(within(outside).queryByText("Docs")).toBeNull();
    expect(within(outside).getByRole("button", { name: "Only your request" }).getAttribute("aria-pressed")).toBe("true");
    // Back in their own groups.
    expect(screen.getByRole("region", { name: "Reviewed · 1" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /^Routine ·/ })).toBeInTheDocument();
  });

  it("is unchanged without a scoped request", () => {
    renderBrief(withProgress());
    expect(screen.queryByRole("region", { name: /Outside your request/ })).toBeNull();
    expect(screen.getByText("Code: 0 of 4 reviewed")).toBeInTheDocument();
  });
});

describe("ReviewBrief AI explanations", () => {
  const ready = { status: "ready" as const, model: "openai:m", error: "", candidates: 2, rejected_count: 0, unsynthesized: 1 };

  it("labels inferred explanations", () => {
    const base = projection();
    renderBrief({
      ...base,
      items: base.items.map((row) =>
        row.id === "area:intent:storage"
          ? {
              ...row,
              rationale: "Prunes checkpoints older than the retention window.",
              rationale_origin: "inferred" as const,
              source_label: "AI explanation",
              limitations: ["Intent inferred from the changed code; not stated by the author."],
            }
          : row,
      ),
      questions: [
        ...base.questions,
        {
          id: "q:ai:implementation_choice_unrecorded:0",
          reason: "implementation_choice_unrecorded",
          title: "Unrecorded choice: retention",
          detail: "The code appears to pick a retention window.",
          item_ids: ["area:intent:storage"],
          target_ids: ["t-prune"],
          actions: [],
          tier: 3,
          origin: "ai",
        },
      ],
      coverage: { ...base.coverage, synthesized: 1, unsynthesized_target_ids: ["t-docs"] },
      synthesis: ready,
    });
    const card = screen.getByTestId("brief-item-area:intent:storage");
    expect(within(card).getByText("AI explanation · intent inferred")).toBeInTheDocument();
    expect(within(card).getByText("Prunes checkpoints older than the retention window.")).toBeInTheDocument();
    expect(within(card).getByText("Intent inferred from the changed code; not stated by the author.")).toBeInTheDocument();
    expect(within(card).getByText("AI question")).toBeInTheDocument();
    expect(screen.getByText("AI explanations: 1 changed target has no explanation.")).toBeInTheDocument();
  });

  it("requests synthesis on demand and shows pending", async () => {
    const onSynthesize = vi.fn().mockResolvedValue(undefined);
    const { unmount } = render(
      <ReviewBrief
        projection={projection({ synthesis: { ...ready, status: "none", unsynthesized: 0 } })}
        loading={false}
        error=""
        targets={TARGETS}
        onOpenTarget={vi.fn()}
        onOpenFiles={vi.fn()}
        onInspectEvidence={vi.fn()}
        onAsk={vi.fn()}
        onSynthesize={onSynthesize}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Explain unexplained changes" }));
    expect(onSynthesize).toHaveBeenCalledTimes(1);
    unmount();
    renderBrief(projection({ synthesis: { ...ready, status: "pending", unsynthesized: 0 } }), { onSynthesize });
    expect(screen.getByText("Preparing explanations…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Explain unexplained changes" })).toBeNull();
  });

  it("shows an honest unavailable state", () => {
    const onSynthesize = vi.fn();
    const disabled = "AI explanations are disabled; set LEMONCROW_LLM_BACKEND to enable them";
    const { unmount } = render(
      <ReviewBrief
        projection={projection({ synthesis: { ...ready, status: "disabled", model: "", error: disabled } })}
        loading={false}
        error=""
        targets={TARGETS}
        onOpenTarget={vi.fn()}
        onOpenFiles={vi.fn()}
        onInspectEvidence={vi.fn()}
        onAsk={vi.fn()}
        onSynthesize={onSynthesize}
      />,
    );
    expect(screen.getByText("AI explanations are unavailable. The Brief still uses recorded context and checks.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Explain unexplained changes" })).toBeNull();
    unmount();
    renderBrief(projection({ synthesis: { ...ready, status: "failed", error: "RuntimeError: backend down" } }), { onSynthesize });
    expect(screen.getByText("Explanation unavailable: RuntimeError: backend down")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Explain unexplained changes" })).toBeInTheDocument();
  });
});

describe("ReviewBrief snapshot delta", () => {
  it("shows what changed since the previous snapshot", () => {
    renderBrief(
      projection({
        delta: {
          since_snapshot: 2,
          added: ["t-docs"],
          changed: ["t-resolve", "t-prune"],
          removed: 1,
          stale_evidence: ["unit tests"],
          reopened_decisions: ["retention"],
        },
      }),
    );
    expect(
      screen.getByText("Since Snapshot 2: 2 changed · 1 new · 1 removed · evidence now stale: unit tests · decisions to revisit: retention"),
    ).toBeInTheDocument();
  });

  it("says nothing when the previous snapshot left everything as it was", () => {
    renderBrief(projection({ delta: { since_snapshot: 2, added: [], changed: [], removed: 0, stale_evidence: [], reopened_decisions: [] } }));
    expect(screen.queryByText(/Since Snapshot/)).toBeNull();
  });
});

describe("ReviewBrief large reviews", () => {
  it("a long section shows every item after Show all", async () => {
    const many = Array.from({ length: 250 }, (_, index) =>
      item(`area:${index}`, { title: `Area ${index}`, position: index, primary_target_ids: ["t-prune"] }),
    );
    renderBrief(projection({ items: many, questions: [], decisions: [], claims: [] }));
    expect(screen.getAllByTestId(/^brief-item-area:/)).toHaveLength(5);
    await userEvent.click(screen.getByRole("button", { name: "Show all 250" }));
    expect(screen.getAllByTestId(/^brief-item-area:/)).toHaveLength(250);
  });
});

describe("ReviewBrief evidence and scope (spec §3, §8, §9, §10)", () => {
  it("names the changed behavior and the other affected items in the delta", () => {
    renderBrief(
      projection({
        delta: {
          since_snapshot: 2,
          added: ["t-docs"],
          changed: ["t-resolve", "t-prune"],
          removed: 1,
          stale_evidence: ["unit tests"],
          reopened_decisions: ["retention"],
          items: [
            { id: "item:branch-identity", title: "Branch identity", kind: "behavior", changed: ["t-resolve"], added: [] },
            { id: "area:intent:storage", title: "Storage", kind: "area", changed: ["t-prune"], added: [] },
            { id: "area:intent:docs", title: "Docs", kind: "area", changed: [], added: ["t-docs"] },
          ],
        },
      }),
    );
    expect(
      screen.getByText(
        "Since Snapshot 2: behavior changed: Branch identity · also affected: Storage, Docs · 2 changed · 1 new · 1 removed · evidence now stale: unit tests · decisions to revisit: retention",
      ),
    ).toBeInTheDocument();
  });

  it("says when, by whom and where each linked check ran", async () => {
    const base = projection();
    const [claim] = base.claims;
    renderBrief(projection({
      claims: [{
        ...claim,
        evidence: [
          { ...claim.evidence[0], observed_at: "2026-09-28T10:05:00+00:00", source: "ci", environment: {} },
          {
            key: "browser:login",
            title: "browser:login",
            status: "PASS",
            state: "current",
            snapshot_number: 3,
            mapping: "explicit",
            relevance: "Cited by the author",
            observed_at: "2026-09-28T11:00:00+00:00",
            source: "agent",
            environment: { browser: "chromium", os: "linux" },
          },
        ],
      }],
    }));
    const card = screen.getByTestId("brief-item-item:branch-identity");
    await userEvent.click(within(card).getByRole("button", { name: /Branch identity/ }));
    expect(within(card).getByText("Ran 2026-09-28 10:05 UTC")).toBeInTheDocument();
    expect(within(card).getByText("Recorded by ci")).toBeInTheDocument();
    expect(within(card).getByText("Recorded by agent")).toBeInTheDocument();
    expect(within(card).getByText("browser: chromium · os: linux")).toBeInTheDocument();
  });

  it("offers the whole evidence inventory only on request", async () => {
    const handlers = renderBrief(projection());
    await userEvent.click(screen.getByRole("button", { name: "All evidence" }));
    expect(handlers.onInspectEvidence).toHaveBeenCalledWith(null);
  });

  it("states an internal-only change as the author's claim", () => {
    const [first, ...rest] = projection().items;
    renderBrief(projection({ items: [{ ...first, before: "", after: "", internal_only: true }, ...rest] }));
    const card = screen.getByTestId("brief-item-item:branch-identity");
    expect(within(card).getByText("Internal implementation only — external behavior intended to remain unchanged.")).toBeInTheDocument();
    expect(within(card).getByText("Author's agent note")).toBeInTheDocument();
  });

  it("stops offering explanations once the Review's AI budget is spent", () => {
    renderBrief(
      projection({
        synthesis: { status: "none", model: "m", error: "", candidates: 2, rejected_count: 0, unsynthesized: 0, budget: { calls_used: 3, max_calls: 3 } },
      }),
      { onSynthesize: vi.fn() },
    );
    expect(screen.getByText("Explanation unavailable: This review's AI analysis limit has been reached.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Explain unexplained changes" })).toBeNull();
  });
});

describe("ReviewBrief AI review", () => {
  it("shows passive effort/budget status and runs a stricter pass", async () => {
    const onRunAiReview = vi.fn().mockResolvedValue({
      revision_id: "rev-1",
      status: "completed",
      effort: "thorough",
      candidates: 1,
      hypotheses: 1,
      findings: { confirmed: 0, not_confirmed: 1, unknown: 0, annotations_created: 0 },
      annotation_ids: [],
      budget: { calls_used: 3, max_calls: 20 },
      provider_calls: 1,
      cache_reuse: 1,
      skipped_by_budget: 0,
      provider_errors: [],
      reviewer: { driver: "openai", model: "review-model", independent: true },
      provider_cost: null,
      currency: null,
    });
    renderBrief(
      projection({
        ai_review: {
          status: "ready",
          effort: "auto",
          candidates: 1,
          findings: 0,
          provider: "openai",
          model: "review-model",
          budget: { calls_used: 2, max_calls: 20 },
        },
      }),
      { onRunAiReview },
    );
    const ai = screen.getByTestId("brief-ai-review");
    expect(ai).toHaveTextContent("AI Review · Complete");
    expect(ai).toHaveTextContent("no confirmed findings");
    expect(screen.getByText("Run details")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run deeper review" }));
    expect(onRunAiReview).not.toHaveBeenCalled();
    const deeper = screen.getByRole("group", { name: "Confirm deeper review" });
    expect(deeper).toHaveTextContent("Depth: thorough");
    expect(deeper).toHaveTextContent("AI usage: 2/20 calls used");
    expect(deeper).toHaveTextContent("Data handling follows the configured model route.");
    expect(deeper).not.toHaveTextContent(/execution locus|metering owner|model data locus/i);
    await userEvent.click(screen.getByRole("button", { name: "Run thorough review" }));
    expect(onRunAiReview).toHaveBeenCalledWith("thorough");
  });

  it.each([
    ["repository_model_ceiling", "at this repository's limit"],
    ["organization_model_budget", "waiting for your organization's share"],
    ["capacity", "waiting for capacity"],
  ])("shows a queued hosted review as Queued with one chip saying why (%s)", (waitReason, text) => {
    renderBrief(
      projection({
        ai_review: {
          status: "pending",
          effort: "thorough",
          candidates: 1,
          findings: 0,
          budget: { calls_used: 0, max_calls: 20 },
          execution: { status: "queued", job_id: "job_1", wait_reason: waitReason },
        },
      }),
    );
    const line = screen.getByTestId("brief-ai-review");
    expect(line).toHaveTextContent("AI Review · Queued");
    expect(line).not.toHaveTextContent("Running");
    expect(screen.getByTestId("ai-wait-reason")).toHaveTextContent(text);
  });

  it("shows no wait chip once the hosted job runs", () => {
    renderBrief(
      projection({
        ai_review: {
          status: "pending",
          effort: "thorough",
          candidates: 1,
          findings: 0,
          budget: { calls_used: 0, max_calls: 20 },
          execution: { status: "running", job_id: "job_1" },
        },
      }),
    );
    expect(screen.getByTestId("brief-ai-review")).toHaveTextContent("AI Review · Running");
    expect(screen.queryByTestId("ai-wait-reason")).not.toBeInTheDocument();
  });

  it("shows authoritative execution/data-boundary details and metering ownership", async () => {
    renderBrief(
      projection({
        ai_review: {
          status: "ready",
          effort: "thorough",
          candidates: 1,
          findings: 1,
          budget: { calls_used: 3, max_calls: 20 },
          execution: {
            status: "succeeded",
            execution_locus: "customer",
            metering_owner: "customer_provider",
            provider: "ollama",
            model: "qwen",
            cache_status: "fresh",
            trace: {
              execution_locus: "customer",
              execution_class: "review_intelligence",
              worker_id: "worker-1",
              provider: "ollama",
              model: "qwen",
              model_data_locus: "customer_local",
              tools: ["read", "code_search"],
              evidence_refs: ["target:t1"],
              external_domains: [],
              provider_calls: 1,
              cache_status: "fresh",
            },
          },
        },
      }),
    );

    await userEvent.click(screen.getByText("Run details"));
    const details = screen.getByTestId("ai-run-details");
    expect(details).toHaveTextContent("Execution locus: customer");
    expect(details).toHaveTextContent("Model data locus: customer_local");
    expect(details).toHaveTextContent("Metering owner: Customer subscription/API");
    expect(details).toHaveTextContent("Provider: ollama");
    expect(details).toHaveTextContent("Model: qwen");
    expect(details).toHaveTextContent("Tools: read, code_search");
    expect(details).toHaveTextContent("Evidence consulted: 1");
    expect(details).toHaveTextContent("Source/context stays customer-local for the model route.");
  });

  it("shows the Snapshot-bound action result supplied by the Reader", async () => {
    renderBrief(
      projection({
        ai_review: {
          status: "ready",
          effort: "auto",
          candidates: 1,
          findings: 0,
          budget: { calls_used: 2, max_calls: 20 },
        },
      }),
      {
        aiReviewRun: {
          revision_id: "rev-1",
          status: "completed",
          effort: "thorough",
          candidates: 1,
          hypotheses: 1,
          findings: { confirmed: 0, not_confirmed: 1, unknown: 0, annotations_created: 0 },
          annotation_ids: [],
          budget: { calls_used: 3, max_calls: 20 },
          provider_calls: 0,
          cache_reuse: 1,
          skipped_by_budget: 0,
          provider_errors: [],
          reviewer: { driver: "openai", model: "review-model", independent: true },
          provider_cost: null,
          currency: null,
        },
      },
    );
    expect(screen.getByTestId("brief-ai-review")).toHaveTextContent("AI Review · Complete");
    await userEvent.click(screen.getByText("Run details"));
    expect(screen.getByTestId("ai-run-details")).toHaveTextContent("Effective depth: thorough");
    expect(screen.getByTestId("ai-run-details")).toHaveTextContent("Budget: 3/20 model calls");
    expect(screen.getByTestId("ai-run-details")).toHaveTextContent("Cache reuse: 1");
    expect(screen.queryByTestId("ai-execution-mode")).toBeNull();
  });

  const readyReview = (extra: Partial<NonNullable<ReviewProjection["ai_review"]>> = {}) =>
    projection({
      ai_review: {
        status: "ready",
        effort: "thorough",
        candidates: 1,
        findings: 0,
        budget: { calls_used: 3, max_calls: 20 },
        ...extra,
      },
    });

  it("shows requested vs observed model and a satisfied independent-reviewer requirement", async () => {
    renderBrief(readyReview({ assurance: assurance() }));
    expect(screen.getByTestId("ai-independence-label")).toHaveTextContent("Independent reviewer");
    await userEvent.click(screen.getByText("Run details"));
    expect(screen.getByTestId("ai-requested-model")).toHaveTextContent("Requested model: gpt-4.1");
    expect(screen.getByTestId("ai-observed-model")).toHaveTextContent("Observed model: gpt-4.1-2025-04-14");
    expect(screen.getByTestId("ai-independence")).toHaveTextContent(
      "Independence: independent of the author's model · author claude/claude-sonnet-4-5",
    );
    expect(screen.getByTestId("ai-independence-policy")).toHaveTextContent(
      "Independent reviewer policy: required · satisfied · from operator_review_policy",
    );
  });

  it("labels an unreported default model and an unsatisfied requirement visibly, never as independent", async () => {
    renderBrief(
      readyReview({
        assurance: assurance({
          identity: {
            driver: "claude",
            requested_model: "",
            observed_models: [],
            observed_truncated: false,
            source: "unreported_default",
            label: "provider default (unreported)",
          },
          independence: {
            ...assurance().independence,
            status: "unreported_reviewer",
            independent: false,
            reason: "claude ran on its unreported default model, which cannot prove independence",
            requirement: "unsatisfied",
          },
        }),
      }),
    );
    expect(screen.getByTestId("ai-independence-label")).toHaveTextContent("Independent reviewer required · not satisfied");
    await userEvent.click(screen.getByText("Run details"));
    expect(screen.getByTestId("ai-requested-model")).toHaveTextContent("Requested model: provider default (unreported)");
    expect(screen.getByTestId("ai-observed-model")).toHaveTextContent("Observed model: not reported by the provider");
    expect(screen.getByTestId("ai-independence")).toHaveTextContent("unreported default model, which cannot prove independence");
    expect(screen.getByTestId("ai-independence-policy")).toHaveTextContent("required · not satisfied");
  });

  it("labels a preferred-but-unmet result and shows nothing when the policy is off", () => {
    const unmet = assurance({
      independence: {
        ...assurance().independence,
        status: "not_independent",
        independent: false,
        requirement: "preferred_unmet",
        policy: { ...assurance().independence.policy, mode: "prefer" },
      },
    });
    renderBrief(readyReview({ assurance: unmet }));
    expect(screen.getByTestId("ai-independence-label")).toHaveTextContent("Not independent");
    cleanup();
    const off = assurance({
      independence: { ...assurance().independence, requirement: "not_required", policy: { ...assurance().independence.policy, mode: "off" } },
    });
    renderBrief(readyReview({ assurance: off }));
    expect(screen.queryByTestId("ai-independence-label")).toBeNull();
  });

  it("previews a fresh re-run at the action boundary and submits one action id only on confirm", async () => {
    const onRerunAiReviewFresh = vi.fn().mockResolvedValue({
      revision_id: "rev-1",
      status: "completed",
      effort: "thorough",
      candidates: 1,
      hypotheses: 1,
      findings: { confirmed: 0, not_confirmed: 1, unknown: 0, annotations_created: 0 },
      annotation_ids: [],
      budget: { calls_used: 9, max_calls: 20 },
      provider_calls: 6,
      cache_reuse: 0,
      skipped_by_budget: 0,
      provider_errors: [],
      reviewer: { driver: "openai", model: "gpt-4.1", independent: true },
      execution: { mode: "fresh_rerun" },
    });
    // The id is minted when a confirmation opens; Cancel discards it.
    const minted = vi
      .spyOn(globalThis.crypto, "randomUUID")
      .mockReturnValueOnce("open-1-0-0-0")
      .mockReturnValueOnce("open-2-0-0-0");
    renderBrief(readyReview({ assurance: assurance() }), { onRerunAiReviewFresh });
    await userEvent.click(screen.getByText("Run details"));
    // Inside Run details: no new top-level control.
    const rerun = within(screen.getByTestId("ai-run-details")).getByRole("button", { name: "Re-run fresh" });
    expect(rerun).toBeInTheDocument();
    await userEvent.click(rerun);
    expect(onRerunAiReviewFresh).not.toHaveBeenCalled();
    const confirm = screen.getByRole("group", { name: "Confirm fresh re-run" });
    expect(confirm).toHaveTextContent("Runs AI review again on this unchanged Snapshot; cached results are not reused.");
    expect(confirm).toHaveTextContent("Depth: thorough");
    expect(confirm).toHaveTextContent("Estimated AI calls: 6");
    expect(confirm).toHaveTextContent("AI usage: 3/20 calls used · 17 remaining");
    expect(confirm).toHaveTextContent("Code and context use the managed model route.");
    expect(confirm).not.toHaveTextContent(/cost class|execution locus|metering owner|model data locus/i);
    expect(within(confirm).queryByTestId("ai-fresh-over-budget")).toBeNull();
    await userEvent.click(within(confirm).getByRole("button", { name: "Cancel" }));
    expect(onRerunAiReviewFresh).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Re-run fresh" }));
    await userEvent.click(screen.getByRole("button", { name: "Re-run fresh now" }));
    expect(onRerunAiReviewFresh).toHaveBeenCalledTimes(1);
    const [actionId] = onRerunAiReviewFresh.mock.calls[0] as [string];
    expect(actionId).toMatch(/^[A-Za-z0-9._:-]{1,128}$/);
    expect(actionId).toBe("open-2-0-0-0");
    expect(minted).toHaveBeenCalledTimes(2);
    minted.mockRestore();
    expect(screen.queryByRole("group", { name: "Confirm fresh re-run" })).toBeNull();
  });

  it("warns before a fresh re-run past the remaining budget and says why one is unavailable", async () => {
    const over = assurance();
    over.fresh_rerun = { ...over.fresh_rerun, within_budget: false, budget: { calls_used: 18, max_calls: 20, remaining: 2 } };
    renderBrief(readyReview({ assurance: over }), { onRerunAiReviewFresh: vi.fn() });
    await userEvent.click(screen.getByText("Run details"));
    await userEvent.click(screen.getByRole("button", { name: "Re-run fresh" }));
    expect(screen.getByTestId("ai-fresh-over-budget")).toHaveTextContent("calls past the budget are not made");
    cleanup();

    const pending = assurance();
    pending.fresh_rerun = { ...pending.fresh_rerun, available: false, reason: "AI review has not settled on this Snapshot yet" };
    renderBrief(readyReview({ assurance: pending }), { onRerunAiReviewFresh: vi.fn() });
    await userEvent.click(screen.getByText("Run details"));
    expect(screen.queryByRole("button", { name: "Re-run fresh" })).toBeNull();
    expect(screen.getByTestId("ai-fresh-unavailable")).toHaveTextContent("Fresh re-run: AI review has not settled on this Snapshot yet");
    cleanup();

    // Read-only or historical: the action is absent.
    renderBrief({ ...readyReview({ assurance: assurance() }), historical: true }, { onRerunAiReviewFresh: vi.fn() });
    await userEvent.click(screen.getByText("Run details"));
    expect(screen.queryByRole("button", { name: "Re-run fresh" })).toBeNull();
  });

  it("distinguishes a fresh re-run from cache reuse in Run details", async () => {
    renderBrief(
      readyReview({
        assurance: assurance({
          execution: { latest_mode: "fresh_rerun", fresh_reruns: 2, fresh_rerun_runs: 4, latest_fresh_at: "2026-10-04T10:00:00Z", runs: 6 },
        }),
      }),
    );
    await userEvent.click(screen.getByText("Run details"));
    expect(screen.getByTestId("ai-execution-mode")).toHaveTextContent(
      "Execution: fresh re-run · 2 explicit fresh re-runs on this Snapshot",
    );
  });

  it("surfaces a refused fresh re-run on the AI Review line", async () => {
    const onRerunAiReviewFresh = vi.fn().mockRejectedValue(new Error("permission denied: review:judge"));
    renderBrief(readyReview({ assurance: assurance() }), { onRerunAiReviewFresh });
    await userEvent.click(screen.getByText("Run details"));
    await userEvent.click(screen.getByRole("button", { name: "Re-run fresh" }));
    await userEvent.click(screen.getByRole("button", { name: "Re-run fresh now" }));
    expect(await screen.findByText("permission denied: review:judge")).toBeInTheDocument();
  });

  it("says why a requested depth was raised to a floor and how the rule checks went", async () => {
    renderBrief(
      projection({
        ai_review: {
          status: "ready",
          effort: "thorough",
          candidates: 1,
          findings: 0,
          budget: { calls_used: 2, max_calls: 20 },
        },
      }),
      {
        aiReviewRun: {
          revision_id: "rev-1",
          status: "degraded",
          effort: "deep",
          candidates: 1,
          hypotheses: 1,
          findings: { confirmed: 0, not_confirmed: 1, unknown: 0, annotations_created: 0 },
          annotation_ids: [],
          budget: { calls_used: 4, max_calls: 20 },
          provider_calls: 2,
          cache_reuse: 0,
          skipped_by_budget: 1,
          provider_errors: [],
          reviewer: { driver: "openai", model: "review-model", independent: true },
          provider_cost: null,
          currency: null,
          depth_request: {
            entry: "ai.investigators",
            requested: "thorough",
            effective: "deep",
            state: "raised",
            reason:
              "raised to deep: a per-Review request cannot go below the min_effort floor deep for src/payments/** (organization policy)",
          },
          rule_checks: [
            { ref: "rule:a", stable_key: "a", state: "answered", reason: "", answer: "satisfied" },
            { ref: "rule:b", stable_key: "b", state: "limited_by_budget", reason: "Limited by budget" },
          ],
        },
      },
    );
    expect(screen.getByTestId("ai-depth-request")).toHaveTextContent(
      "Requested thorough: raised to deep: a per-Review request cannot go below the min_effort floor deep for src/payments/** (organization policy)",
    );
    await userEvent.click(screen.getByText("Run details"));
    expect(screen.getByTestId("ai-rule-checks")).toHaveTextContent("Rule checks: 1 answered · 1 limited by budget");
  });

  it("ignores an action result from a different Snapshot", () => {
    renderBrief(
      projection({
        ai_review: {
          status: "ready",
          effort: "auto",
          candidates: 1,
          findings: 0,
          budget: { calls_used: 2, max_calls: 20 },
        },
      }),
      {
        aiReviewRun: {
          revision_id: "old-revision",
          status: "degraded",
          effort: "auto",
          candidates: 1,
          hypotheses: 0,
          findings: { confirmed: 0, not_confirmed: 0, unknown: 0, annotations_created: 0 },
          annotation_ids: [],
          budget: { calls_used: 20, max_calls: 20 },
          provider_calls: 0,
          cache_reuse: 0,
          skipped_by_budget: 1,
          provider_errors: ["old provider error"],
          reviewer: { driver: "", model: "", independent: false },
          provider_cost: null,
          currency: null,
        },
      },
    );
    expect(screen.getByTestId("brief-ai-review")).toHaveTextContent("2/20 model calls");
    expect(screen.queryByText(/old provider error/)).toBeNull();
  });

  it("does not offer a deeper action at Deep or for historical Review", () => {
    const onRunAiReview = vi.fn();
    const deep = projection({
      ai_review: {
        status: "ready",
        effort: "deep",
        candidates: 2,
        findings: 1,
        budget: { calls_used: 8, max_calls: 20 },
      },
    });
    const { unmount } = render(
      <ReviewBrief
        projection={deep}
        loading={false}
        error=""
        targets={TARGETS}
        onOpenTarget={vi.fn()}
        onOpenFiles={vi.fn()}
        onInspectEvidence={vi.fn()}
        onAsk={vi.fn()}
        onRunAiReview={onRunAiReview}
      />,
    );
    expect(screen.queryByRole("button", { name: "Run deeper review" })).toBeNull();
    unmount();
    renderBrief({ ...deep, historical: true, ai_review: { ...deep.ai_review!, effort: "auto" } }, { onRunAiReview: undefined });
    expect(screen.queryByRole("button", { name: "Run deeper review" })).toBeNull();
  });

  it("keeps human coverage distinct from AI coverage when policy excludes targets", async () => {
    renderBrief(
      projection({
        ai_review: {
          status: "ready",
          effort: "auto",
          candidates: 1,
          findings: 0,
          budget: { calls_used: 1, max_calls: 20 },
          coverage: {
            human_deterministic_targets: 4,
            ai_context_eligible_targets: 2,
            excluded_count: 2,
            excluded_by_policy: [
              { target_id: "t-secret", path: "src/secrets/key.py", mode: "ai_denied", reason: "AI content access policy denies this path" },
              { target_id: "t-local", path: "customer/private.py", mode: "customer_local_only", reason: "requires local route" },
            ],
          },
        },
      }),
    );
    const status = screen.getByTestId("brief-ai-review");
    expect(status).toHaveTextContent("2/4 eligible targets checked");
    expect(status).toHaveTextContent("2 excluded by policy");
    await userEvent.click(screen.getByText("What AI could not inspect"));
    expect(screen.getByText(/src\/secrets\/key.py/)).toBeInTheDocument();
    expect(screen.getByText(/customer\/private.py/)).toBeInTheDocument();
    expect(screen.queryByText(/everything checked/i)).toBeNull();
  });

  it("explains exactly what was checked on a zero-finding Review without upgrading UNKNOWN or NOT_RUN to pass", async () => {
    const base = projection();
    const reviewedTargets = TARGETS.map((target, index) => (
      index < 2 ? { ...target, state: "reviewed" as const } : target
    ));
    render(
      <ReviewBrief
        projection={projection({
          claims: [{
            ...base.claims[0],
            evidence: [
              { ...base.claims[0].evidence[0], status: "PASS" },
              { ...base.claims[0].evidence[0], key: "unknown-check", title: "unknown-check", status: "UNKNOWN" },
              { ...base.claims[0].evidence[0], key: "not-run-check", title: "not-run-check", status: "NOT_RUN" },
            ],
          }],
          ai_review: {
            status: "ready",
            effort: "auto",
            candidates: 2,
            findings: 0,
            budget: { calls_used: 1, max_calls: 20 },
            coverage: {
              human_deterministic_targets: 4,
              ai_context_eligible_targets: 3,
              excluded_count: 1,
              excluded_by_policy: [
                { target_id: "t-docs", path: "docs/notes.md", mode: "ai_denied", reason: "AI content access policy denies this path" },
              ],
            },
          },
        })}
        loading={false}
        error=""
        targets={reviewedTargets}
        onOpenTarget={vi.fn()}
        onOpenFiles={vi.fn()}
        onInspectEvidence={vi.fn()}
        onAsk={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByText("What was checked"));
    const checked = screen.getByTestId("what-was-checked");
    expect(checked).toHaveTextContent("Deterministic Review: 4 exact Snapshot targets");
    expect(checked).toHaveTextContent("AI Review: Complete · 3/4 eligible targets");
    expect(checked).toHaveTextContent("Reviewer inspection: 2/4 targets reviewed");
    expect(checked).toHaveTextContent("NOT_RUN 1");
    expect(checked).toHaveTextContent("UNKNOWN 1");
    expect(checked).toHaveTextContent("NOT_RUN means the verification did not run; it is not a pass.");
    expect(checked).toHaveTextContent("UNKNOWN means the verification result is unavailable; it is not a pass.");
    expect(checked).toHaveTextContent("1 target was not sent to the model by policy.");
  });

  it("only exposes explicit stop for a live durable AI job and never as approval", async () => {
    const onCancelAiReview = vi.fn().mockResolvedValue(undefined);
    const pending = projection({ ai_review: {
      status: "pending", effort: "auto", candidates: 1, findings: 0,
      budget: { calls_used: 0, max_calls: 4 },
      execution: { status: "queued", job_id: "ai-job-1", revision_id: "rev-1", wait_reason: "capacity" },
    } });
    renderBrief(pending, { onCancelAiReview });
    expect(screen.getByTestId("brief-ai-review")).toHaveTextContent("Queued");
    await userEvent.click(screen.getByRole("button", { name: "Stop AI review" }));
    expect(onCancelAiReview).toHaveBeenCalledWith("ai-job-1");
    cleanup();
    renderBrief({ ...pending, ai_review: {
      ...pending.ai_review!, execution: {
        status: "cancel_requested", job_id: "ai-job-1", revision_id: "rev-1", cancel_requested: true,
      },
    } }, { onCancelAiReview });
    expect(screen.queryByRole("button", { name: "Stop AI review" })).toBeNull();
    expect(screen.getByTestId("brief-ai-review")).toHaveTextContent("Stopping");
    cleanup();
    renderBrief(pending);
    expect(screen.queryByRole("button", { name: "Stop AI review" })).toBeNull();
  });

  it("surfaces budget skips and provider degradation without a scorecard", async () => {
    renderBrief(
      projection({
        ai_review: {
          status: "failed",
          effort: "thorough",
          candidates: 3,
          findings: 0,
          error: "provider unavailable",
          budget: { calls_used: 20, max_calls: 20 },
          skipped_by_budget: 2,
        },
      }),
    );
    expect(screen.getByTestId("brief-ai-review")).toHaveTextContent("AI Review · Limited by budget");
    await userEvent.click(screen.getByText("Run details"));
    expect(screen.getByTestId("ai-run-details")).toHaveTextContent("Budget: 20/20 model calls");
    expect(screen.getByText("provider unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/confidence/i)).toBeNull();
  });
});
