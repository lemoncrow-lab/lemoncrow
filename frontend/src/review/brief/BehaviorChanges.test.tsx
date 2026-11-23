import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ReviewBehaviorSummary, ReviewBehaviorTarget } from "../types";
import BehaviorChanges, { registerDeltaText } from "./BehaviorChanges";

function behaviorTarget(options: Partial<ReviewBehaviorTarget> = {}): ReviewBehaviorTarget {
  return {
    behavior_key: "beh:api:1", kind: "api_response", kind_label: "API response", producer: "lemoncrow.runtime_validation",
    title: "POST /session", summary: "Status 200 became 401", change: "changed", severity: "medium", critical: false,
    state: "unreviewed", changed_since_mark: false, reviewed_revision_id: "", fingerprint: "fp-1", environment_fingerprint: "",
    executor_class: "", paths: ["src/session.py"], target_ids: [], unverified_integrations: [],
    priority: { tier: 4, attention: "understand", outstanding: true, position: 0, reasons: [] }, bulk_eligible: true,
    ...options,
  } as ReviewBehaviorTarget;
}

function behaviorSummary(options: Partial<ReviewBehaviorSummary> = {}): ReviewBehaviorSummary {
  const targets = options.targets ?? [behaviorTarget()];
  return {
    contract_version: 2, revision_id: "rev-1", total: targets.length,
    progress: { total: targets.length, reviewed: 0, unreviewed: targets.length, changed_since_review: 0, needs_changes: 0, unknown: 0, critical: targets.filter((item) => item.critical).length, outstanding: targets.length },
    targets, unchanged: 1, execution_ran: true, coverage: null, checklist: null,
    narrative: { mode: "deterministic", statements: [], deterministic: [], synthesis: { status: "none", mode: "deterministic_only" } },
    degraded: [], producers: ["lemoncrow.runtime_validation"],
    ...options,
  } as ReviewBehaviorSummary;
}

const EVIDENCE = {
  digest: "d".repeat(64),
  base: { side: "base" as const, status: "200", digest: "b", summary: "ok body", evidence_ref: "artifact:base" },
  head: { side: "head" as const, status: "401", digest: "h", summary: "denied body", evidence_ref: "artifact:head" },
  refs: [{ ref: "artifact:head", kind: "artifact" as const, label: "Head response", digest: "h", side: "head", role: "response" }],
  facts: [{ name: "Status", value: "200 to 401" }],
} as unknown as NonNullable<ReviewBehaviorTarget["evidence"]>;

describe("BehaviorChanges", () => {
  it("draws the manual checklist and records what the person saw as their observation", async () => {
    const onManualCheck = vi.fn(async () => undefined);
    const item = {
      item_id: "manual:payments", title: "Payments sandbox", integration: "payments", steps: ["Charge a test card"],
      behavior_keys: [], paths: ["src/pay.py"], origin: "policy" as const, required: true, result: null,
    };
    const checklist = { required: true, policy_source: "base", policy_error: "", attestation_class: "human_observation" as const, recorded: 0, items: [item] };
    const { rerender } = render(<BehaviorChanges behavior={behaviorSummary({ checklist })} revisionId="rev-1" onManualCheck={onManualCheck} />);

    const row = screen.getByTestId("manual-check");
    expect(within(row).getByText("Charge a test card")).toBeInTheDocument();
    expect(within(row).getByText("Required")).toBeInTheDocument();
    await userEvent.type(within(row).getByRole("textbox", { name: "What you saw: Payments sandbox" }), " card declined ");
    await userEvent.click(within(row).getByRole("button", { name: "Failed" }));
    await waitFor(() => expect(onManualCheck).toHaveBeenCalledWith("manual:payments", "FAIL", "card declined"));

    const result = { status: "FAIL", note: "card declined", attested_by: "local", observed_at: "", evidence_id: "e", attestation_class: "human_observation" as const, label: "Human observation" as const };
    rerender(<BehaviorChanges behavior={behaviorSummary({ checklist: { ...checklist, recorded: 1, items: [{ ...item, result }] } })} revisionId="rev-1" />);
    expect(screen.getByText("Check by hand · 1 of 1 recorded")).toBeInTheDocument();
    expect(screen.getByTestId("manual-check-result")).toHaveTextContent("Human observation: failed · local · card declined");
    expect(screen.queryByRole("button", { name: "Failed" })).not.toBeInTheDocument();
  });

  it("does not show internal producer ids in a degraded note", () => {
    render(<BehaviorChanges behavior={behaviorSummary({ degraded: ["lemoncrow.static_side_effects: side-effect register truncated"] })} revisionId="rev-1" />);
    expect(screen.getByText("side-effect register truncated")).toBeInTheDocument();
  });

  it("allows a judgment only once the evidence is on screen, and sends that evidence's digest", async () => {
    const onLoadTarget = vi.fn(async () => behaviorTarget({ evidence: EVIDENCE }));
    const onJudge = vi.fn(async () => undefined);
    render(<BehaviorChanges behavior={behaviorSummary()} revisionId="rev-1" onLoadTarget={onLoadTarget} onJudge={onJudge} />);

    expect(screen.getByRole("button", { name: "Mark reviewed" })).toBeDisabled();
    expect(screen.getByText("Show the evidence to judge this change.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show evidence" }));
    const evidence = await screen.findByTestId("behavior-evidence");
    expect(within(evidence).getByText("ok body")).toBeInTheDocument();
    expect(within(evidence).getByText("denied body")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Mark reviewed" })).toBeEnabled());

    await userEvent.click(screen.getByRole("button", { name: "Mark reviewed" }));
    expect(onJudge).toHaveBeenCalledWith("beh:api:1", "reviewed", EVIDENCE.digest);

    await userEvent.click(screen.getByRole("button", { name: "Hide evidence" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Mark reviewed" })).toBeDisabled());
  });

  it("refuses evidence that belongs to another fingerprint", async () => {
    const onJudge = vi.fn(async () => undefined);
    render(<BehaviorChanges behavior={behaviorSummary()} revisionId="rev-1" onLoadTarget={async () => behaviorTarget({ fingerprint: "other", evidence: EVIDENCE })} onJudge={onJudge} />);
    await userEvent.click(screen.getByRole("button", { name: "Show evidence" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Refresh the Review before judging");
    expect(screen.queryByTestId("behavior-evidence")).toBeNull();
    expect(screen.getByRole("button", { name: "Mark reviewed" })).toBeDisabled();
  });

  it("offers no judgment when read-only and labels a critical change", () => {
    render(<BehaviorChanges behavior={behaviorSummary({ targets: [behaviorTarget({ critical: true })] })} revisionId="rev-1" />);
    expect(screen.queryByRole("button", { name: "Mark reviewed" })).toBeNull();
    expect(screen.getByText(/Critical · judged individually/)).toBeInTheDocument();
  });

  it("labels an AI narrative as interpretation, never as an observation", () => {
    const narrative = { mode: "inferred", statements: [{ text: "Sign-in now rejects expired tokens.", behavior_keys: [], evidence_refs: [], origin: "inferred" }], deterministic: [], synthesis: { status: "ready" } } as ReviewBehaviorSummary["narrative"];
    render(<BehaviorChanges behavior={behaviorSummary({ narrative })} revisionId="rev-1" />);
    expect(screen.getByTestId("behavior-narrative")).toHaveTextContent("not a verified runtime observation");
  });

  it("states the register delta and its caveat, and nothing when the inventory did not run", () => {
    const inventory = { status: "ran", reason: "", register: { base_sha: "abcdef123", state: "truncated" }, register_delta: { added: 1, removed: 0, changed: 2 }, degraded: [], files_analyzed: 1, targets: 1 };
    expect(registerDeltaText(inventory)).toBe("Side effects vs the register at base abcdef1: 1 new, 0 removed, 2 changed (partial register: some may already exist elsewhere).");
    expect(registerDeltaText({ ...inventory, status: "skipped" })).toBe("");
  });
});
