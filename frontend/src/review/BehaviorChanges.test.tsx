import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ManualChecklist, registerDeltaText } from "./BehaviorChanges";
import ReviewBrief from "./ReviewBrief";
import ReviewReadinessSheet from "./ReviewReadinessSheet";
import {
  __setBootstrapForTest,
  fetchBehaviorTarget,
  postBehaviorJudgment,
  postManualCheckResult,
} from "./reviewApi";
import { isTinyMechanicalReview } from "./reviewSurfaceModel";
import type {
  ReviewBehaviorSummary,
  ReviewBehaviorTarget,
  ReviewManualChecklist,
  ReviewProjection,
  ReviewTarget,
} from "./types";

const CODE_TARGET: ReviewTarget = {
  target_id: "t-session",
  unit_key: "unit:t-session",
  kind: "symbol",
  path: "src/session.py",
  label: "src/session.py::refresh",
  symbol: "refresh",
  start_line: 1,
  end_line: 2,
  hunk_ordinals: [0],
  spans: [{ side: "new", start_line: 2, end_line: 2, hunk_ordinal: 0 }],
  state: "unreviewed",
  changed_since_mark: false,
  reviewed_revision_id: "",
  attention_rank: 0,
  attention_level: "mechanical",
  reasons: [],
  additions: 1,
  deletions: 1,
  fingerprint_method: "symbol_body_sha256",
  verification: { pass: 0, fail: 0, unknown: 0 },
  annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
};

function behaviorTarget(options: Partial<ReviewBehaviorTarget> = {}): ReviewBehaviorTarget {
  return {
    behavior_key: "beh:visual:abc",
    kind: "visual",
    kind_label: "Visual",
    producer: "lemoncrow.browser_checks",
    title: "Checkout",
    summary: "1.00% of pixels differ from the baseline",
    change: "changed",
    severity: "medium",
    critical: false,
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    fingerprint: "fp-1",
    environment_fingerprint: "",
    executor_class: "",
    paths: ["src/session.py"],
    target_ids: [],
    unverified_integrations: [],
    priority: { tier: 4, attention: "understand", outstanding: true, position: 0, reasons: [] },
    bulk_eligible: true,
    ...options,
  } as ReviewBehaviorTarget;
}

const EVIDENCE = {
  digest: "d".repeat(64),
  base: { side: "base" as const, status: "", digest: "b", summary: "baseline screenshot", evidence_ref: "artifact:ev-base" },
  head: { side: "head" as const, status: "", digest: "h", summary: "head screenshot · 1.00% changed", evidence_ref: "artifact:ev-head" },
  refs: [
    { ref: "artifact:ev-head", kind: "artifact" as const, label: "Checkout · head screenshot", digest: "h", side: "head", role: "screenshot", evidence_id: "ev-head", media_type: "image/png" },
  ],
  facts: [{ name: "Pixels changed", value: "1.00%" }],
};

function behavior(options: Partial<ReviewBehaviorSummary> = {}): ReviewBehaviorSummary {
  const targets = options.targets ?? [behaviorTarget()];
  return {
    contract_version: 1,
    revision_id: "rev-1",
    total: targets.length,
    progress: {
      total: targets.length,
      reviewed: 0,
      unreviewed: targets.length,
      changed_since_review: 0,
      needs_changes: 0,
      unknown: 0,
      critical: targets.filter((item) => item.critical).length,
      outstanding: targets.length,
    },
    targets,
    unchanged: 1,
    execution_ran: true,
    coverage: {
      runs: 2,
      changed_targets: 2,
      exercised: 1,
      never_executed_count: 1,
      never_executed: [{ target_id: "t-billing", path: "src/billing.py", label: "charge" }],
    },
    checklist: null,
    narrative: {
      mode: "deterministic",
      statements: [],
      deterministic: [],
      synthesis: { status: "none", mode: "deterministic_only" },
    },
    degraded: [],
    producers: ["lemoncrow.runtime_validation", "lemoncrow.browser_checks"],
    ...options,
  };
}

function projection(options: Partial<ReviewProjection> = {}): ReviewProjection {
  return {
    projection_version: 1,
    review_id: "r1",
    revision_id: "rev-1",
    snapshot_number: 2,
    historical: false,
    plan: { schema_version: 1, input_digest: "abc", intents: [], limitations: [] },
    actions: [],
    questions: [],
    items: [],
    claims: [],
    decisions: [],
    coverage: {
      target_count: 1,
      author_explained: 0,
      fallback_grouped: 0,
      other: 0,
      routine: 0,
      unresolved_refs: 0,
      unexplained_targets: 0,
      file_count: 1,
    },
    counts: { review_first: 0, understand: 0, routine: 0, done: 0, decisions: 0 },
    queue: ["t-session"],
    ...options,
  };
}

function renderBrief(value: ReviewProjection, overrides: Partial<Parameters<typeof ReviewBrief>[0]> = {}) {
  return render(
    <ReviewBrief
      projection={value}
      loading={false}
      error=""
      targets={[CODE_TARGET]}
      onOpenTarget={vi.fn()}
      onOpenFiles={vi.fn()}
      onInspectEvidence={vi.fn()}
      {...overrides}
    />,
  );
}

function sectionLabels(container: HTMLElement): string[] {
  return [...container.querySelectorAll("section[aria-label]")].map((node) => node.getAttribute("aria-label") ?? "");
}

const BEHAVIOR_QUEUE_ENTRY = {
  id: "behavior:beh:visual:abc",
  reason: "behavior_changed",
  title: "Checkout",
  detail: "Visual changed; not reviewed yet",
  item_ids: [],
  target_ids: [],
  actions: ["inspect"],
  tier: 4,
};

describe("Behavior changes in the Brief", () => {
  it("adds nothing when no behavior delta exists", () => {
    const bare = renderBrief(projection());
    const bareSections = sectionLabels(bare.container);
    const bareButtons = bare.container.querySelectorAll("header button").length;
    bare.unmount();

    const empty = renderBrief(projection({ behavior: behavior({ targets: [], coverage: null, execution_ran: false }) }));
    expect(sectionLabels(empty.container)).toEqual(bareSections);
    expect(empty.container.querySelectorAll("header button").length).toBe(bareButtons);
    expect(screen.queryByTestId("behavior-changes")).toBeNull();
    expect(screen.queryByText(/Behavior changes/)).toBeNull();
  });

  it("opens the interactive preview inside Behavior changes without a new section or header control", () => {
    const plain = renderBrief(projection({ behavior: behavior() }));
    const plainSections = sectionLabels(plain.container);
    const plainButtons = plain.container.querySelectorAll("header button").length;
    plain.unmount();

    const withPreview = renderBrief(projection({ behavior: behavior() }), {
      interactivePreview: <div data-testid="interactive-preview-slot">Interactive preview</div>,
    });
    expect(sectionLabels(withPreview.container)).toEqual(plainSections);
    expect(withPreview.container.querySelectorAll("header button").length).toBe(plainButtons);
    const section = screen.getByRole("region", { name: "Behavior changes · 1" });
    expect(within(section).getByTestId("interactive-preview-slot")).toBeInTheDocument();
    withPreview.unmount();

    // A historical Snapshot never offers a preview of an old revision.
    renderBrief(projection({ behavior: behavior(), historical: true }), {
      interactivePreview: <div data-testid="interactive-preview-slot">Interactive preview</div>,
    });
    expect(screen.queryByTestId("interactive-preview-slot")).toBeNull();
  });

  it("renders exactly one Brief section and no new header control when deltas exist", () => {
    const bare = renderBrief(projection());
    const bareSections = sectionLabels(bare.container);
    const bareButtons = bare.container.querySelectorAll("header button").length;
    bare.unmount();

    const shown = renderBrief(projection({ behavior: behavior(), actions: [BEHAVIOR_QUEUE_ENTRY] }));
    expect(sectionLabels(shown.container)).toEqual([...bareSections.slice(0, 1), "Behavior changes · 1", ...bareSections.slice(1)].filter(Boolean));
    expect(shown.container.querySelectorAll("header button").length).toBe(bareButtons);
    // A behavior queue entry is not a "Required check".
    expect(screen.queryByText(/Required checks/)).toBeNull();
    const section = screen.getByTestId("behavior-changes");
    expect(within(section).getByText("Visual · Checkout")).toBeInTheDocument();
    expect(within(section).getByText(/1 unchanged behavior collapsed/)).toBeInTheDocument();
  });

  it("enables judgment only after the evidence is rendered and sends that evidence's digest", async () => {
    const onLoadBehaviorTarget = vi.fn(async () => behaviorTarget({ evidence: EVIDENCE }));
    const onJudgeBehavior = vi.fn(async () => undefined);
    renderBrief(projection({ behavior: behavior() }), { onLoadBehaviorTarget, onJudgeBehavior });

    const reviewed = screen.getByRole("button", { name: "Mark reviewed" });
    expect(reviewed).toBeDisabled();
    expect(screen.getByRole("button", { name: "Needs changes" })).toBeDisabled();
    expect(screen.getByText("Show the evidence to judge this change.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Show evidence" }));
    const evidence = await screen.findByTestId("behavior-evidence");
    expect(within(evidence).getByText("baseline screenshot")).toBeInTheDocument();
    expect(within(evidence).getByText("artifact:ev-head", { selector: "span" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Mark reviewed" })).toBeEnabled());

    await userEvent.click(screen.getByRole("button", { name: "Mark reviewed" }));
    expect(onJudgeBehavior).toHaveBeenCalledWith("beh:visual:abc", "reviewed", EVIDENCE.digest);

    // Hiding the evidence withdraws the ability to judge.
    await userEvent.click(screen.getByRole("button", { name: "Hide evidence" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Mark reviewed" })).toBeDisabled());
  });

  it("labels critical changes as individually judged and is read-only on a historical Snapshot", () => {
    renderBrief(
      projection({ historical: true, behavior: behavior({ targets: [behaviorTarget({ critical: true, bulk_eligible: false })] }) }),
      { onLoadBehaviorTarget: vi.fn(), onJudgeBehavior: vi.fn(), onNarrateBehavior: vi.fn() },
    );
    expect(screen.getByText(/Critical · judged individually/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mark reviewed" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Show evidence" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Explain before/after" })).toBeNull();
  });

  it("shows an inferred narrative only with its provenance label", () => {
    renderBrief(
      projection({
        behavior: behavior({
          narrative: {
            mode: "inferred",
            statements: [{ text: "Checkout now renders a wider total.", behavior_keys: ["beh:visual:abc"], evidence_refs: ["artifact:ev-head"], origin: "inferred" }],
            deterministic: [],
            synthesis: { status: "ready" },
          },
        }),
      }),
    );
    const narrative = screen.getByTestId("behavior-narrative");
    expect(narrative).toHaveTextContent("Checkout now renders a wider total.");
    expect(narrative).toHaveTextContent("AI narrative · every statement cites the evidence it describes");
  });

  it("states changed-but-never-executed only when execution ran", async () => {
    const ran = renderBrief(projection({ behavior: behavior({ targets: [] }) }));
    await userEvent.click(screen.getByText("What was checked"));
    const coverage = screen.getByTestId("execution-coverage");
    expect(coverage).toHaveTextContent("1 of 2 changed areas exercised by 2 recorded runs");
    expect(coverage).toHaveTextContent("Changed but never executed: src/billing.py");
    ran.unmount();

    renderBrief(projection({ behavior: behavior({ targets: [], coverage: null, execution_ran: false }) }));
    expect(screen.queryByTestId("execution-coverage")).toBeNull();
    expect(screen.queryByText(/never executed/)).toBeNull();
  });

  it("keeps a behavior change from reading as a tiny mechanical review", () => {
    expect(isTinyMechanicalReview(projection(), [CODE_TARGET])).toBe(true);
    expect(isTinyMechanicalReview(projection({ behavior: behavior() }), [CODE_TARGET])).toBe(false);
  });
});

const CHECKLIST: ReviewManualChecklist = {
  required: true,
  policy_source: "base Snapshot .lemoncrow/review.yaml",
  policy_error: "",
  attestation_class: "human_observation",
  recorded: 1,
  items: [
    {
      item_id: "manual:stripe",
      title: "Stripe checkout",
      integration: "stripe",
      steps: ["Pay with a test card in staging"],
      behavior_keys: [],
      paths: ["src/session.py"],
      origin: "policy",
      required: true,
      result: {
        status: "PASS",
        note: "receipt arrived",
        attested_by: "alice",
        observed_at: "2026-10-03T00:00:00Z",
        evidence_id: "ev-1",
        attestation_class: "human_observation",
        label: "Human observation",
      },
    },
    {
      item_id: "manual:sso",
      title: "Single sign-on",
      integration: "sso",
      steps: ["Sign in through the IdP"],
      behavior_keys: [],
      paths: [],
      origin: "policy",
      required: true,
      result: null,
    },
  ],
};

describe("Manual verification checklist", () => {
  it("labels every result a human observation and records through the callback", async () => {
    const onRecord = vi.fn(async () => undefined);
    render(<ManualChecklist checklist={CHECKLIST} onRecord={onRecord} />);
    const list = screen.getByTestId("manual-checklist");
    expect(list).toHaveTextContent("required by repository policy");
    expect(list).toHaveTextContent("never as sandbox results");
    expect(screen.getByTestId("manual-check-manual:stripe")).toHaveTextContent("Human observation · PASS · alice · receipt arrived");
    expect(screen.getByTestId("manual-check-manual:sso")).toHaveTextContent("Not recorded");
    const sso = screen.getByTestId("manual-check-manual:sso");
    await userEvent.type(within(sso).getByRole("textbox"), "login works");
    await userEvent.click(within(sso).getByRole("button", { name: "Observed working" }));
    expect(onRecord).toHaveBeenCalledWith("manual:sso", "PASS", "login works");
  });

  it("is read-only without a recorder and lives inside the Status sheet's Verification subsection", () => {
    render(
      <ReviewReadinessSheet
        model={{
          ready: false,
          label: "Not ready",
          detail: "",
          reviewedTargets: 0,
          targetCount: 1,
          changedSinceReview: 0,
          needsChanges: 0,
          openComments: 0,
          blockers: [],
          evidenceFacts: [],
          outcome: null,
        } as unknown as Parameters<typeof ReviewReadinessSheet>[0]["model"]}
        verificationExtras={<ManualChecklist checklist={{ ...CHECKLIST, required: false }} />}
        onClose={vi.fn()}
      />,
    );
    const list = screen.getByTestId("manual-checklist");
    expect(list).toHaveTextContent("optional");
    expect(within(list).queryByRole("button")).toBeNull();
    const verification = screen.getByText("Verification").parentElement as HTMLElement;
    expect(within(verification).getByTestId("manual-checklist")).toBe(list);
  });
});

describe("behavior API helpers", () => {
  beforeEach(() => {
    __setBootstrapForTest({ token: "", reviewId: "" });
    window.history.replaceState(null, "", "/review");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubFetch(body: unknown) {
    const spy = vi.fn(async () => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", spy);
    return spy;
  }

  it("reads one target's evidence and echoes the digest in a judgment bound to the Snapshot", async () => {
    const spy = stubFetch({ revision_id: "rev-1", target: behaviorTarget({ evidence: EVIDENCE }), recorded: [], refused: [] });
    await fetchBehaviorTarget("r 1", "beh:visual:abc");
    await postBehaviorJudgment("r 1", "rev-1", { behavior_key: "beh:visual:abc", state: "reviewed", evidence_digest: EVIDENCE.digest });
    await postManualCheckResult("r 1", "rev-1", { item_id: "manual:sso", status: "UNKNOWN", note: "" });
    const calls = spy.mock.calls as unknown as [string, RequestInit][];
    expect(calls[0][0]).toBe("/api/reviews/r%201/behavior/targets/beh%3Avisual%3Aabc");
    expect(calls[1][0]).toBe("/api/reviews/r%201/behavior/marks");
    expect(JSON.parse(String(calls[1][1].body))).toEqual({
      expected_revision_id: "rev-1",
      judgments: [{ behavior_key: "beh:visual:abc", state: "reviewed", evidence_digest: EVIDENCE.digest }],
    });
    expect(calls[2][0]).toBe("/api/reviews/r%201/behavior/checklist");
    expect(JSON.parse(String(calls[2][1].body))).toEqual({
      expected_revision_id: "rev-1",
      item_id: "manual:sso",
      status: "UNKNOWN",
      note: "",
    });
  });
});

describe("Static side effects and agent actions in the Brief", () => {
  const sideEffect = behaviorTarget({
    behavior_key: "beh:side_effect:flag",
    kind: "side_effect",
    kind_label: "Side effect",
    producer: "lemoncrow.static_side_effects",
    title: "New environment variable: NEW_FLAG",
    change: "added",
    critical: false,
  });
  const weakening = behaviorTarget({
    behavior_key: "beh:verification_weakening:skip",
    kind: "verification_weakening",
    kind_label: "Verification weakened",
    producer: "lemoncrow.static_side_effects",
    title: "1 test newly skipped or disabled",
    critical: true,
    bulk_eligible: false,
  });
  const inventory = {
    status: "ran",
    reason: "",
    files_analyzed: 3,
    targets: 2,
    degraded: [],
    register: { state: "complete", base_sha: "abcdef0123456789", files_scanned: 12, identities: 9 },
    register_delta: { added: 2, removed: 1, changed: 0 },
  };

  it("states the delta against the base register and the auto-approval block", () => {
    renderBrief(
      projection({
        behavior: behavior({
          targets: [sideEffect, weakening],
          static_inventory: inventory,
          auto_approval: {
            blocked: true,
            blockers: [{ code: "verification_weakening", behavior_key: weakening.behavior_key, title: weakening.title, paths: ["tests/test_app.py"] }],
          },
        }),
      }),
    );
    const section = screen.getByTestId("behavior-changes");
    expect(within(section).getByTestId("side-effect-register")).toHaveTextContent(
      "Side effects vs the register at base abcdef0: 2 new, 1 removed, 0 changed.",
    );
    expect(within(section).getByTestId("auto-approval-blocked")).toHaveTextContent("Auto-approval blocked: 1 change");
    expect(within(section).getByText("Side effect · New environment variable: NEW_FLAG")).toBeInTheDocument();
  });

  it("caveats a partial or missing register and says nothing when the inventory did not run or is unchanged", () => {
    expect(registerDeltaText({ ...inventory, register: { ...inventory.register, state: "truncated" } })).toContain("partial register");
    expect(registerDeltaText({ ...inventory, register: { state: "changed_files_only", base_sha: "" } })).toBe(
      "Side effects vs the register: 2 new, 1 removed, 0 changed (base register unavailable: compared with the changed files only).",
    );
    expect(registerDeltaText({ ...inventory, status: "skipped" })).toBe("");
    expect(registerDeltaText({ ...inventory, register_delta: { added: 0, removed: 0, changed: 0 } })).toBe("");
    // A dependency is judged once, in the dependency review; the register line says where.
    expect(
      registerDeltaText({ ...inventory, register_delta: { added: 1, removed: 0, changed: 0, judged_elsewhere: { dependency: 2 } } }),
    ).toBe("Side effects vs the register at base abcdef0: 1 new, 0 removed, 0 changed; 2 dependency changes judged in the dependency review.");
    expect(registerDeltaText(undefined)).toBe("");

    renderBrief(projection({ behavior: behavior({ static_inventory: { ...inventory, register_delta: {} } }) }));
    expect(screen.queryByTestId("side-effect-register")).toBeNull();
    expect(screen.queryByTestId("auto-approval-blocked")).toBeNull();
    expect(screen.queryByTestId("agent-actions")).toBeNull();
  });

  it("shows consequential agent actions even when no behavior target exists", () => {
    renderBrief(
      projection({
        behavior: behavior({
          targets: [],
          coverage: null,
          execution_ran: false,
          agent_actions: [
            { category: "git_push", summary: "Git push: force push", host: "claude", session_id: "c1", at: "2026-09-27T10:05:00Z" },
          ],
        }),
      }),
    );
    const section = screen.getByTestId("behavior-changes");
    expect(screen.getByText("Behavior changes")).toBeInTheDocument();
    expect(within(section).queryByText(/judged/)).toBeNull();
    expect(within(section).getByText("Agent actions outside the repository")).toBeInTheDocument();
    expect(within(section).getByTestId("agent-actions")).toHaveTextContent("Git push: force push");
  });
});
