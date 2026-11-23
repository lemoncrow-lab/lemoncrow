import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

import ReviewPremergeChecks from "./ReviewPremergeChecks";
import ReviewReadinessSheet, { deriveReviewReadiness } from "./ReviewReadinessSheet";
import type { ReviewIntentCapture, ReviewLandingReadiness, ReviewPremergeCheck, ReviewPremergeProjection } from "./types";

function check(overrides: Partial<ReviewPremergeCheck> & Pick<ReviewPremergeCheck, "id" | "title">): ReviewPremergeCheck {
  return {
    requirement_key: `premerge:${overrides.id}`,
    mode: "error",
    status: "FAIL",
    state: "fail",
    applicable: true,
    detail: "",
    evidence: [],
    evaluator: "deterministic",
    source: "trusted base",
    layer: "trusted_base",
    definition_digest: "d",
    blocking: true,
    would_block: false,
    waivable: false,
    waiver: null,
    waiver_note: "",
    model: null,
    ...overrides,
  };
}

function capture(overrides: Partial<ReviewIntentCapture> = {}): ReviewIntentCapture {
  return {
    evidence_id: "evd-1",
    revision_id: "rr-1",
    snapshot_number: 1,
    ref: "github:acme/shop#7",
    kind: "issue",
    origin: "provider_linked",
    role: "bound",
    title: "Checkout totals include tax",
    digest: "sha256:aaaa",
    state: "captured",
    truncated: false,
    source_revision: "",
    fetched_at: "",
    url: "https://github.com/acme/shop/issues/7",
    note: "",
    actor: "github",
    created_at: "",
    ...overrides,
  };
}

function premerge(overrides: Partial<ReviewPremergeProjection> = {}): ReviewPremergeProjection {
  const checks = overrides.checks ?? [
    check({ id: "linked-intent", title: "Linked issue or specification", waivable: true, detail: "No linked source." }),
    check({
      id: "description",
      title: "PR title and description",
      mode: "warning",
      blocking: false,
      would_block: true,
      detail: "description has 4 characters of text; at least 40 required",
    }),
  ];
  return {
    configured: true,
    checks,
    policy_errors: [],
    policy_generation: "policy:abc",
    evaluated_at: "2026-10-04T00:00:00Z",
    trial: { would_block: ["description"], summary: "1 warning-mode check(s) would block this Change if enforced." },
    snapshot_id: "rr-2",
    snapshot_number: 2,
    policy: {
      checks: [
        { id: "linked-intent", title: "Linked issue or specification", mode: "error", source: "", layer: "trusted_base", model_evaluated: false, waivable: true, waivable_by: [] },
        { id: "description", title: "PR title and description", mode: "warning", source: "", layer: "trusted_base", model_evaluated: false, waivable: false, waivable_by: [] },
      ],
      waivers: { allowed: ["release-captain"], max_hours: 48 },
      errors: [],
      sources: ["trusted base"],
    },
    intents: [],
    intent_changed: [],
    description: null,
    ai_unavailable: "",
    ...overrides,
  };
}

function landing(block: ReviewPremergeProjection): ReviewLandingReadiness {
  return {
    review_id: "review-1",
    snapshot_id: "rr-2",
    snapshot_number: 2,
    tree_fingerprint: "tree-2",
    source_fingerprint: "source-2",
    conditions: [
      { id: "premerge:linked-intent", label: "Linked issue or specification", state: "fail", detail: "No linked source.", owner: "lemoncrow", category: "premerge", url: "" },
    ],
    ready: false,
    armable: false,
    blocker_count: 1,
    local_blocker_count: 1,
    conditions_hash: "c",
    work_items: [],
    landing: { supported: true, provider: "github", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "" },
    premerge: block,
  };
}

const progress = { target_count: 1, reviewed: 1, changed_since_review: 0, needs_changes: 0, unreviewed: 0, unknown: 0, mechanical: 0 };
const model = () => deriveReviewReadiness({ progress, openComments: 0, verification: { pass: 0, fail: 0, not_run: 0, unknown: 0 }, outcome: null });

describe("ReviewPremergeChecks inside Review Status", () => {
  it("renders inside the existing Review Status sheet with the warning-mode trial, not as a blocker", () => {
    render(<ReviewReadinessSheet model={model()} landing={landing(premerge())} onClose={vi.fn()} />);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Review Status")).toBeTruthy();
    expect(within(dialog).getByText("Pre-merge checks")).toBeTruthy();
    // The blocking check is one readiness condition; the trial is separate and labelled.
    expect(within(dialog).getAllByText("Linked issue or specification")).toHaveLength(1);
    expect(within(dialog).getByText("Warning-mode trial")).toBeTruthy();
    expect(within(dialog).getByText("would block if enforced")).toBeTruthy();
    expect(within(dialog).getByText("PR title and description")).toBeTruthy();
    expect(within(dialog).getByText("1 warning-mode check(s) would block this Change if enforced.")).toBeTruthy();
    expect(within(dialog).getByText("Not ready · 1 condition")).toBeTruthy();
  });

  it("renders nothing new when no pre-merge checks are configured", () => {
    const plain = landing(premerge());
    delete plain.premerge;
    render(<ReviewReadinessSheet model={model()} landing={plain} onClose={vi.fn()} />);
    expect(screen.queryByText("Pre-merge checks")).toBeNull();
  });

  it("records a reasoned, scoped, expiring waiver only through the explicit form", () => {
    const onWaive = vi.fn();
    render(<ReviewPremergeChecks premerge={premerge()} actions={{ onWaive }} />);
    expect(screen.getByText("Linked issue or specification may be waived by release-captain")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Waive…" }));
    const submit = screen.getByRole("button", { name: "Record waiver" });
    expect(submit).toBeDisabled(); // a waiver needs a reason
    fireEvent.change(screen.getByLabelText("Waiver reason"), { target: { value: "hotfix; issue filed after" } });
    fireEvent.change(screen.getByLabelText("Waiver hours"), { target: { value: "72" } });
    expect(submit).toBeDisabled(); // beyond the policy's max_hours
    fireEvent.change(screen.getByLabelText("Waiver hours"), { target: { value: "12" } });
    fireEvent.click(submit);
    expect(onWaive).toHaveBeenCalledWith({ check_id: "linked-intent", reason: "hotfix; issue filed after", scope: "snapshot", hours: 12 });
  });

  it("shows a waived check with its observed status, actor and policy generation, never as passed", () => {
    const waived = check({
      id: "linked-intent",
      title: "Linked issue or specification",
      state: "waived",
      blocking: false,
      waiver: {
        evidence_id: "evd-w",
        revision_id: "rr-2",
        snapshot_number: 2,
        requirement_key: "premerge:linked-intent",
        actor: "release-captain",
        reason: "hotfix",
        scope: "snapshot",
        expires_at: "2026-10-05T00:00:00Z",
        policy_generation: "policy:bf8dcc90a051ffff",
        policy_generation_short: "bf8dcc90a051",
        paths: ["pkg/alpha.py"],
        created_at: "",
      },
    });
    const lapsed = check({ id: "rollout-notes", title: "Rollout and backout notes", waiver_note: "The waiver was granted under another policy generation." });
    render(<ReviewPremergeChecks premerge={premerge({ checks: [waived, lapsed], trial: { would_block: [], summary: "" } })} actions={{ onWaive: vi.fn() }} />);
    const row = screen.getByText(/waived by release-captain/);
    expect(row.textContent).toContain("(failed)");
    expect(row.textContent).toContain("bf8dcc90a051");
    expect(row.textContent).toContain("this revision");
    expect(row.textContent).not.toContain("PASS");
    expect(screen.getByText(/another policy generation/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Waive…" })).toBeNull(); // a waived check is no longer waivable
  });

  it("names intent drift since its revision and re-binds only on an explicit action", () => {
    const onRebind = vi.fn();
    const changedNote = "Intent changed since Revision 2: Checkout totals include tax now reads differently (sha256:bbbb).";
    const block = premerge({
      intents: [{ ref: "github:acme/shop#7", kind: "issue", origin: "provider_linked", bound: capture(), latest: capture({ digest: "sha256:bbbb", role: "observed", snapshot_number: 2 }), changed: true, changed_note: changedNote }],
      intent_changed: [changedNote],
    });
    render(<ReviewPremergeChecks premerge={block} actions={{ onRebind }} />);
    expect(screen.getByRole("link", { name: /Checkout totals include tax/ })).toBeTruthy();
    expect(screen.getByText("issue · Revision 2")).toBeTruthy();
    expect(screen.getByText(changedNote)).toBeTruthy();
    expect(onRebind).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Re-bind" }));
    expect(onRebind).toHaveBeenCalledWith("github:acme/shop#7");
  });

  it("offers no waive, re-bind or evaluate action on a read-only sheet", () => {
    const block = premerge({
      intents: [{ ref: "github:acme/shop#7", kind: "issue", origin: "provider_linked", bound: capture(), latest: capture({ digest: "sha256:bbbb" }), changed: true, changed_note: "Intent changed since Revision 2." }],
    });
    render(<ReviewPremergeChecks premerge={block} readOnly actions={{ onWaive: vi.fn(), onRebind: vi.fn(), onEvaluate: vi.fn() }} />);
    expect(screen.queryByRole("button", { name: "Waive…" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Re-bind" })).toBeNull();
  });

  it("keeps natural-language checks NOT_RUN, naming why, and evaluates only on request", () => {
    const custom = check({ id: "no-secrets-in-logs", title: "No secrets in logs", status: "NOT_RUN", state: "pending", evaluator: "model" });
    const policyChecks = [{ id: "no-secrets-in-logs", title: "No secrets in logs", mode: "error" as const, source: "", layer: "trusted_base", model_evaluated: true, waivable: false, waivable_by: [] }];
    const base = premerge({ checks: [custom], trial: { would_block: [], summary: "" } });
    const block = { ...base, policy: { ...base.policy, checks: policyChecks } };
    const onEvaluate = vi.fn();
    const { rerender } = render(<ReviewPremergeChecks premerge={{ ...block, ai_unavailable: "Review Intelligence is unavailable (backend none)" }} actions={{ onEvaluate }} />);
    expect(screen.getByText(/stay NOT_RUN and are never passed/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Evaluate" })).toBeNull();
    rerender(<ReviewPremergeChecks premerge={block} actions={{ onEvaluate }} />);
    fireEvent.click(screen.getByRole("button", { name: "Evaluate" }));
    expect(onEvaluate).toHaveBeenCalledTimes(1);
  });

  it("shows run-plan budget limits as Limited by budget and offers no bypass", () => {
    const custom = check({ id: "no-secrets-in-logs", title: "No secrets in logs", status: "NOT_RUN", state: "pending", evaluator: "model", not_run_reason: "limited_by_budget" });
    const policyChecks = [{ id: "no-secrets-in-logs", title: "No secrets in logs", mode: "error" as const, source: "", layer: "trusted_base", model_evaluated: true, waivable: false, waivable_by: [] }];
    const base = premerge({ checks: [custom], trial: { would_block: [], summary: "" } });
    const runState = { entry: "ai.premerge_checks", status: "limited_by_budget", reason: "Limited by budget (model calls ceiling)", origin: "preset", depth: "auto", executor: "premerge_checks", plan_digest: "sha256:p" };
    const block = { ...base, policy: { ...base.policy, checks: policyChecks }, run: runState };
    const onEvaluate = vi.fn();
    const { rerender } = render(<ReviewPremergeChecks premerge={block} actions={{ onEvaluate }} />);
    expect(screen.getByText("Limited by budget")).toBeTruthy();
    expect(screen.getByText(/model calls ceiling\)\. Natural-language checks stay NOT_RUN and are never passed/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Evaluate" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Re-run" })).toBeNull();
    // Planned by the run plan: the explicit action is a re-run, not a first evaluation.
    rerender(<ReviewPremergeChecks premerge={{ ...block, run: { ...runState, status: "run", reason: "" } }} actions={{ onEvaluate }} />);
    expect(screen.queryByText("Limited by budget")).toBeNull();
    expect(screen.getByText(/The run plan evaluates natural-language checks/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Re-run" }));
    expect(onEvaluate).toHaveBeenCalledTimes(1);
  });

  it("shows an invalid pre-merge policy as an alert", () => {
    render(<ReviewPremergeChecks premerge={premerge({ policy_errors: ["trusted base: premerge has unknown setting 'x'"] })} />);
    expect(screen.getByRole("alert").textContent).toContain("premerge has unknown setting");
  });
});
