import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetAiBudgetForTests } from "../aiBudget";
import { exportFeedback, fetchFinishReview, previewGitHubPublish, requestBrowserJson } from "../reviewApi";
import type { FinishHandoff, FinishOptions } from "../FinishSheet";
import type { Annotation, FeedbackExport, ReviewClosure, ReviewLandingReadiness, ReviewOutcomeKind, ReviewOverview, ReviewTarget } from "../types";
import { ReviewWrapScreen } from "./ReviewWrapScreen";
import { fetchReviewEnvironment } from "../reviewEnvironment";
import { resetSecondReviewerSupport, type SecondReviewerStatus } from "../second/secondApi";

vi.mock("../reviewEnvironment", () => ({ fetchReviewEnvironment: vi.fn(async () => ({ mode: "local" })), useReviewEnvironment: vi.fn(() => ({ mode: "local" })) }));
vi.mock("../reviewApi", () => ({ exportFeedback: vi.fn(), fetchFinishReview: vi.fn(), previewGitHubPublish: vi.fn(), requestBrowserJson: vi.fn(), fetchReviewRecordStatus: vi.fn(() => Promise.reject(new Error("no record in this test"))) }));
const apiBrowserJson = vi.mocked(requestBrowserJson);
const apiExport = vi.mocked(exportFeedback);
const apiClosure = vi.mocked(fetchFinishReview);
const apiGithub = vi.mocked(previewGitHubPublish);
const OVERVIEW = { title: "Review the change", session: { id: "review", ref: "shop", status: "open" }, revision: { id: "rev1", revision_number: 1 } } as ReviewOverview;
const CLOSURE = { status: "open", target_count: 2, reviewed_targets: 2, unreviewed_targets: 0, changed_since_review: 0, needs_changes: 0, unknown_targets: 0, open_comments: 0, orphaned_comments: 0, failed_verification: 0, unresolved_verification: 0, previous_revision_evidence: 0, discarded_verdicts: 0 } as ReviewClosure;
const EXPORT = { markdown: "## Review of Revision 1 — Request changes\n\n### MUST FIX · src/auth.py:L2\n\nFix it\n\nAdd a test that proves this.\n\nHuman review. Nothing here was decided by an AI.\n", revision_id: "rev1", feedback_hash: "digest", operation_id: "operation", open: 1, orphaned: 0, resolved: 0, annotation_versions: { ann1: 1 }, delivery: { kind: "agent", supported: true }, status: { unpublished: 1 } } as unknown as FeedbackExport;

function target(id: string, category: "decision" | "quick_check" = "decision", state: ReviewTarget["state"] = "unreviewed"): ReviewTarget {
  return {
    target_id: id, unit_key: `sym:${id}`, kind: "symbol", path: "src/auth.py", label: id, symbol: id, start_line: 1,
    end_line: 3, hunk_ordinals: [0], spans: [], state, changed_since_mark: false, reviewed_revision_id: "rev1",
    attention_rank: 1, attention_level: "normal", reasons: [], additions: 1, deletions: 1, fingerprint_method: "ast",
    verification: { pass: 0, fail: 0, unknown: 0 }, annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    answer: { category, rule: "", rule_label: "", reason: "" },
    priority: { version: 1, tier: 3, attention: "routine", outstanding: true, position: 1, reasons: [] },
  };
}
const NOTE = {
  id: "ann1", review_id: "review", revision_id: "rev1", parent_id: "", source: "human", created_by_actor: "human",
  kind: "request_change", state: "open", body: "Fix it", anchored: true, anchor_exact: true, file_level: false,
  path: "src/auth.py", symbol: "auth", start_line: 2, end_line: 2, updated_at: "2026-11-06T00:00:00Z",
} as Annotation;
const SECOND = {
  state: "will_be_asked", role: "first", required: true, unavailable: "", area: "app/auth", decision_count: 1, revision_id: "rev1",
  reviewer: { id: "usr_maya", name: "Maya Chen", handle: "mchen" }, first_reviewer: null, first: null, second: null,
  signatures: { have: 0, needed: 2 }, verdict: "waiting",
} as SecondReviewerStatus;
const HANDOFF = { kind: "agent", available: true, reason: "", unsent: 1, label: "Claude Code" } as FinishHandoff;
function Location() { const value = useLocation(); return <output data-testid="wrap-location">{value.pathname + value.search}</output>; }
function show({
  targets = [target("auth")], notes = [], handoff = HANDOFF, complete = true, readOnly = false,
  onFinish = vi.fn(), recorded = null, currentOutcome = null, readiness = null,
}: {
  targets?: ReviewTarget[]; notes?: Annotation[]; handoff?: FinishHandoff; complete?: boolean;
  readOnly?: boolean; onFinish?: (outcome: ReviewOutcomeKind, summary: string, options: FinishOptions) => void;
  recorded?: ReviewOutcomeKind | null; currentOutcome?: null; readiness?: ReviewLandingReadiness | null;
} = {}) {
  return render(<MemoryRouter initialEntries={["/r/shop/wrap"]}><ReviewWrapScreen
    reviewRef="shop" apiReviewId="review" overview={OVERVIEW} targets={targets} annotations={notes}
    complete={complete} readOnly={readOnly} busy={false} handoff={handoff} onFinish={onFinish}
    recorded={recorded} currentOutcome={currentOutcome} readiness={readiness}
  /><Location /></MemoryRouter>);
}

beforeEach(() => {
  window.localStorage.clear();
  apiClosure.mockReset().mockResolvedValue(CLOSURE);
  apiExport.mockReset().mockImplementation(async (_id, options) => ({
    ...EXPORT, presentation: options?.presentation, markdown: EXPORT.markdown,
  }));
  apiGithub.mockReset();
  apiBrowserJson.mockReset().mockRejectedValue(new Error("no AI preferences in this test"));
});
afterEach(() => { cleanup(); resetAiBudgetForTests(); });

describe("Wrap up: merge readiness", () => {
  it("says in one line whether it can merge, and nothing before the server's readiness loads", async () => {
    const readiness = (ready: boolean, blocker_count: number) => ({
      ready, blocker_count, conditions: [{ id: "tests", label: "tests", state: ready ? "pass" : "fail", category: "verification" }],
    }) as unknown as ReviewLandingReadiness;
    show({ readiness: readiness(false, 1) });
    await waitFor(() => expect(apiClosure).toHaveBeenCalled());
    expect(screen.getByTestId("wrap-merge-readiness")).toHaveTextContent("Merge readinessChecks failed · 1 merge blocker");
    cleanup();
    show({ readiness: readiness(true, 0) });
    expect(screen.getByTestId("wrap-merge-readiness")).toHaveTextContent("Merge readinessReady");
    cleanup();
    show();
    expect(screen.queryByTestId("wrap-merge-readiness")).toBeNull();
  });
});

describe("P2-4 Wrap up", () => {
  it("WRAP-4: cold load with unfinished decisions disables send and does not pretend an answer", async () => {
    const onFinish = vi.fn();
    show({ targets: [target("auth"), target("total")], onFinish });
    await waitFor(() => expect(apiClosure).toHaveBeenCalled());
    expect(screen.getByTestId("wrap-unanswered").textContent).toContain("2 decisions still need your answer");
    // The open decisions are listed once, above; "Your answers" does not repeat them.
    expect(screen.queryByText("Not answered")).toBeNull();
    expect(screen.getByTestId("wrap-no-answers")).toBeInTheDocument();
    expect(screen.getByTestId("wrap-send")).toBeDisabled();
    fireEvent.keyDown(window, { key: "Enter", metaKey: true });
    expect(screen.getByTestId("flow-toast").textContent).toContain("still need your answer");
    expect(onFinish).not.toHaveBeenCalled();
  });

  it("ST-1: an AI budget of $0 changes nothing on Wrap: no AI-off line, before or after the verdict (OWNER-1)", async () => {
    resetAiBudgetForTests({ monthly_budget_usd: 0, used_usd: 0, unreported_calls: 0, complete: true, resets_on: "2026-12-01", may_run: false, reason_code: "budget_zero", reason: "AI budget is $0" });
    show();
    await waitFor(() => expect(apiClosure).toHaveBeenCalled());
    expect(screen.queryByTestId("wrap-ai-off")).toBeNull();
    expect(screen.queryByText(/AI help is off/)).toBeNull();
    expect(screen.getByTestId("wrap-send")).toBeDisabled(); // held by the unanswered decision, not by AI
    cleanup();
    show({ recorded: "lgtm" });
    expect(screen.queryByText(/AI help is off|AI was off/)).toBeNull();
  });

  it("WRAP-1: Answer opens exact part with return link to Wrap up", async () => {
    show();
    await userEvent.click(within(screen.getByTestId("wrap-unanswered")).getByRole("button", { name: /Answer/ }));
    expect(screen.getByTestId("wrap-location").textContent).toBe("/r/shop/one/auth?from=wrap");
  });

  it("WRAP-2: completed decisions with no notes suggest approval and record without sending", async () => {
    const onFinish = vi.fn();
    show({ targets: [target("auth", "decision", "reviewed"), target("total", "quick_check", "later")],
      notes: [], handoff: { ...HANDOFF, unsent: 0 }, onFinish });
    const button = screen.getByTestId("wrap-send");
    await waitFor(() => expect(button).toBeEnabled());
    expect(button.textContent).toContain("Record approval");
    expect(screen.getByTestId("wrap-based-on").textContent).toContain("Based on your answers");
    expect(screen.getByTestId("wrap-summary").textContent).toBe("1 decision: 1 looks good · 1 quick check: 1 not checked yet");
    await userEvent.click(button);
    await waitFor(() => expect(onFinish).toHaveBeenCalledWith("lgtm", "", { send: false }));
  });

  it("WRAP-3: picking a verdict replaces the reason with Chosen by you", async () => {
    show({ targets: [target("auth", "decision", "reviewed")], notes: [], handoff: { ...HANDOFF, unsent: 0 } });
    expect(screen.getByTestId("wrap-based-on").textContent).toBe("Based on your answers · every decision looks good");
    await userEvent.click(within(screen.getByRole("group", { name: "Choose your verdict" })).getByRole("button", { name: "Request changes" }));
    expect(screen.getByTestId("wrap-based-on").textContent).toBe("Chosen by you");
    expect(screen.getAllByText("Chosen by you")).toHaveLength(1);
  });

  it("WRAP-1/3/8: requires human importance, renders server-previewed bytes and sends exact metadata", async () => {
    const onFinish = vi.fn();
    apiClosure.mockResolvedValue({ ...CLOSURE, target_count: 1 });
    show({ targets: [target("auth", "decision", "needs_changes")], notes: [NOTE], onFinish });
    await waitFor(() => expect(apiExport).toHaveBeenCalled());
    expect(screen.getByTestId("wrap-based-on").textContent).toContain("1 part needs a change");
    expect(screen.getByTestId("wrap-send")).toBeDisabled();
    expect(screen.getByTestId("wrap-footer").textContent).toContain("Set the importance of 1 note");
    await userEvent.click(within(await screen.findByTestId("wrap-note")).getByRole("button", { name: "Must fix" }));
    await waitFor(() => expect(screen.getByTestId("wrap-send")).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Preview exactly what is sent" }));
    expect(screen.getByTestId("wrap-exact-preview").textContent).toBe(EXPORT.markdown);
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Close" }));
    fireEvent.keyDown(window, { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(onFinish).toHaveBeenCalledWith(
      "changes_requested", "", {
        send: true, expectedFeedbackHash: "digest", presentation: {
          verdict: "changes_requested", importance: { ann1: "must_fix" }, test_with_fix: false, summary: "",
        },
      },
    ));
  });

  it("WRAP-8: a preview that names an agent session the Reader did not know yet sends by the preview's route, and tells the Reader", async () => {
    const onFinish = vi.fn();
    const onFeedbackPrepared = vi.fn();
    const onQueue = vi.fn(async () => ({ state: "pending" }) as unknown as import("../types").ReviewFlowSend);
    apiClosure.mockResolvedValue({ ...CLOSURE, target_count: 1 });
    // The Reader loaded before enrichment named the author's session, so it still thinks the notes are copied.
    render(<MemoryRouter initialEntries={["/r/shop/wrap"]}><ReviewWrapScreen
      reviewRef="shop" apiReviewId="review" overview={OVERVIEW} targets={[target("auth", "decision", "needs_changes")]} annotations={[NOTE]}
      complete readOnly={false} busy={false} handoff={{ ...HANDOFF, kind: "export", label: "the author" }} onFinish={onFinish}
      onQueue={onQueue} onFeedbackPrepared={onFeedbackPrepared} recorded={null} currentOutcome={null}
    /><Location /></MemoryRouter>);
    await userEvent.click(within(await screen.findByTestId("wrap-note")).getByRole("button", { name: "Must fix" }));
    await waitFor(() => expect(screen.getByTestId("wrap-send")).toBeEnabled());
    expect(onFeedbackPrepared).toHaveBeenCalledWith(expect.objectContaining({ delivery: EXPORT.delivery }));
    await userEvent.click(screen.getByTestId("wrap-send"));
    await waitFor(() => expect(onQueue).toHaveBeenCalledTimes(1));
    expect(onFinish).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByTestId("wrap-location").textContent).toBe("/r/shop/done"));
  });

  it("WRAP-1/3: mismatch requires a second human confirmation; bypass by keyboard is impossible", async () => {
    const onFinish = vi.fn();
    apiClosure.mockResolvedValue({ ...CLOSURE, target_count: 1 });
    show({ targets: [target("auth", "decision", "reviewed")], notes: [NOTE], onFinish });
    await userEvent.click(within(await screen.findByTestId("wrap-note")).getByRole("button", { name: "Must fix" }));
    await waitFor(() => expect(screen.getByTestId("wrap-send")).toBeEnabled());
    expect(screen.getByTestId("wrap-mismatch").textContent).toContain("Approve anyway?");
    await userEvent.click(screen.getByTestId("wrap-send"));
    expect(screen.getByRole("dialog", { name: "Confirm your verdict" })).toBeTruthy();
    expect(onFinish).not.toHaveBeenCalled();
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Go back" }));
    expect(onFinish).not.toHaveBeenCalled();
    await userEvent.click(screen.getByTestId("wrap-send"));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Confirm Approve" }));
    await waitFor(() => expect(onFinish).toHaveBeenCalledTimes(1));
  });

  it("WRAP-4: unverified preview, unread-only state and partial pagination prevent write", async () => {
    const onFinish = vi.fn();
    apiClosure.mockResolvedValue({ ...CLOSURE, target_count: 1 });
    apiExport.mockImplementation(async (_id, options) => (options?.presentation ? { ...EXPORT, revision_id: "old" } : EXPORT));
    show({ targets: [target("auth", "decision", "reviewed")], notes: [NOTE], complete: false, readOnly: true, onFinish });
    await waitFor(() => expect(screen.getByTestId("wrap-footer").textContent).toContain("Check the exact feedback"));
    expect(screen.getByTestId("wrap-send")).toBeDisabled();
    expect(screen.getByTestId("wrap-footer").textContent).toContain("read-only");
    expect(onFinish).not.toHaveBeenCalled();
  });

  it("WRAP-7: the second reviewer is named only when the server asks for one, and Request changes does not ask yet", async () => {
    const no = show({ targets: [target("auth", "decision", "needs_changes")], handoff: { ...HANDOFF, unsent: 0 } });
    expect(screen.queryByTestId("second-strip")).toBeNull();
    no.unmount();
    resetSecondReviewerSupport();
    vi.mocked(fetchReviewEnvironment).mockResolvedValue({ mode: "managed_shared" } as never);
    apiBrowserJson.mockReset().mockImplementation(async (path: string) => {
      if (String(path).includes("second-reviewer")) return SECOND as never;
      throw new Error("no AI preferences in this test");
    });
    show({ targets: [target("auth", "decision", "needs_changes")], handoff: { ...HANDOFF, unsent: 0 } });
    await waitFor(() => expect(screen.getByTestId("second-strip-line").textContent).toBe("Not needed yet · asked when you approve"));
  });

  describe("a recorded verdict goes to Done on a hosted server only (SPEC 7.1 P9-4 D)", () => {
    const hostedAs = (status: SecondReviewerStatus) => {
      resetSecondReviewerSupport();
      vi.mocked(fetchReviewEnvironment).mockResolvedValue({ mode: "managed_shared" } as never);
      apiBrowserJson.mockReset().mockImplementation(async (path: string) => {
        if (String(path).includes("second-reviewer")) return status as never;
        throw new Error("no AI preferences in this test");
      });
    };
    afterEach(() => { vi.mocked(fetchReviewEnvironment).mockResolvedValue({ mode: "local" } as never); resetSecondReviewerSupport(); });
    const decided = { targets: [target("auth", "decision", "reviewed")], handoff: { ...HANDOFF, unsent: 0 }, recorded: "lgtm" as ReviewOutcomeKind };

    it("hosted, to an agent or the author: a recorded verdict opens Done", async () => {
      hostedAs({ ...SECOND, state: "waiting" });
      show(decided);
      await waitFor(() => expect(screen.getByTestId("wrap-location").textContent).toBe("/r/shop/done"));
    });

    it("hosted, to a pull request: it stays on Wrap up, which says the second signature is waited for", async () => {
      hostedAs({ ...SECOND, state: "waiting" });
      apiGithub.mockReturnValue(new Promise(() => undefined) as never); // the publish preview is still being planned
      show({ ...decided, handoff: { ...HANDOFF, kind: "github", unsent: 0 } });
      await waitFor(() => expect(screen.getByTestId("second-strip-line").textContent).toBe("Waiting for Maya"));
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(screen.getByTestId("wrap-location").textContent).toBe("/r/shop/wrap");
    });

    it("local: a recorded verdict stays on Wrap up (its Done is the held send's)", async () => {
      show(decided);
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(screen.getByTestId("wrap-location").textContent).toBe("/r/shop/wrap");
    });
  });

  it("shows no signed or sent claim until the server records a verdict", () => {
    show({ targets: [target("auth", "decision", "reviewed")], handoff: { ...HANDOFF, unsent: 0 } });
    expect(screen.queryByTestId("wrap-recorded")).toBeNull();
    expect(screen.getByText(/Nothing has been sent yet/)).toBeTruthy();
  });

  it("handles a new text preview hash and refuses an unreviewed altered message", async () => {
    const onFinish = vi.fn();
    let count = 0;
    apiClosure.mockResolvedValue({ ...CLOSURE, target_count: 1 });
    apiExport.mockImplementation(async (_id, options) => ({ ...EXPORT, presentation: options?.presentation, feedback_hash: ++count >= 4 ? "changed" : "digest" }));
    show({ targets: [target("auth", "decision", "needs_changes")], notes: [NOTE], onFinish });
    await userEvent.click(within(await screen.findByTestId("wrap-note")).getByRole("button", { name: "Must fix" }));
    await waitFor(() => expect(screen.getByTestId("wrap-send")).toBeEnabled());
    await userEvent.click(screen.getByTestId("wrap-send"));
    await waitFor(() => expect(screen.getByTestId("flow-toast").textContent).toContain("Feedback changed"));
    expect(onFinish).not.toHaveBeenCalled();
  });

  it("requires the preview's note ids to equal the notes shown; an unseen server note blocks sending", async () => {
    apiClosure.mockResolvedValue({ ...CLOSURE, target_count: 1 });
    apiExport.mockImplementation(async (_id, options) => ({ ...EXPORT, presentation: options?.presentation, annotation_versions: { ann1: 1, ghost: 1 } }));
    show({ targets: [target("auth", "decision", "reviewed")], notes: [NOTE] });
    await userEvent.click(within(await screen.findByTestId("wrap-note")).getByRole("button", { name: "Suggestion" }));
    await waitFor(() => expect(screen.getByTestId("wrap-footer").textContent).toContain("Some unsent comments were not loaded"));
    expect(screen.getByTestId("wrap-send")).toBeDisabled();
  });

  it("lists only the server's notes: an already-sent note is not shown", async () => {
    apiClosure.mockResolvedValue({ ...CLOSURE, target_count: 1 });
    const sent = { ...NOTE, id: "sent1", body: "Already sent" } as Annotation;
    show({ targets: [target("auth", "decision", "reviewed")], notes: [NOTE, sent] });
    await waitFor(() => expect(screen.getAllByTestId("wrap-note")).toHaveLength(1));
    expect(screen.queryByText("Already sent")).toBeNull();
  });

  it("offers the test request only for agent destinations", async () => {
    apiClosure.mockResolvedValue({ ...CLOSURE, target_count: 1 });
    const view = show({ targets: [target("auth", "decision", "reviewed")], notes: [NOTE] });
    await waitFor(() => expect(screen.getByText("Ask for a test with each fix")).toBeTruthy());
    view.unmount();
    show({ targets: [target("auth", "decision", "reviewed")], notes: [NOTE], handoff: { ...HANDOFF, kind: "export" } });
    await waitFor(() => expect(apiExport).toHaveBeenCalled());
    expect(screen.queryByText("Ask for a test with each fix")).toBeNull();
  });

  it("a failed note-set load shows an error with a retry instead of waiting forever", async () => {
    apiClosure.mockResolvedValue({ ...CLOSURE, target_count: 1 });
    apiExport.mockRejectedValueOnce(new Error("offline"));
    show({ targets: [target("auth", "decision", "reviewed")], notes: [NOTE] });
    expect((await screen.findByRole("alert")).textContent).toContain("Couldn't load the notes to send");
    expect(screen.getByTestId("wrap-footer").textContent).not.toContain("Checking which notes");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getAllByTestId("wrap-note")).toHaveLength(1));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

// P6-3 (SPEC 4.17, 7.1 Q2): a pull request source replaces the agent destination, and the review is previewed as it will appear.
describe("P6-3 GitHub on Wrap up", () => {
  const GITHUB = { kind: "github", available: true, reason: "", unsent: 1, label: "PR #412" } as FinishHandoff;
  const PLAN = (over: Record<string, unknown> = {}): Awaited<ReturnType<typeof previewGitHubPublish>> => ({
    pr: { owner: "acme", repo: "payments", number: 412, url: "https://github.com/acme/payments/pull/412" },
    commit_id: "b52e7a9", event: "REQUEST_CHANGES", summary: "", body: "Human review. Nothing here was decided by an AI.",
    inline: [{ annotation_id: "ann1", path: "app/payments/webhooks.py", side: "RIGHT", start_line: 42, line: 42, body: "Cap retries by the spend budget." }],
    folded: [], notices: [], plan_hash: "plan", review_id: "review", revision_id: "rev1", operation_id: "op", head_sha: "b52e7a9",
    stale: false, empty: false, viewer: "srivera", author: "dkim", check_run: null, ...over,
  } as Awaited<ReturnType<typeof previewGitHubPublish>>);
  const decided = () => ({ targets: [target("auth", "decision", "needs_changes")], notes: [NOTE], handoff: GITHUB });
  const open = async () => {
    apiClosure.mockResolvedValue({ ...CLOSURE, target_count: 1 });
    show(decided());
    await userEvent.click(within(await screen.findByTestId("wrap-note")).getByRole("button", { name: "Must fix" }));
  };

  it("Q2: the destination is the pull request, with no agent session and no test request", async () => {
    apiGithub.mockResolvedValue(PLAN());
    await open();
    const destination = screen.getByTestId("wrap-destination");
    expect(destination.textContent).toContain("Post review to #412");
    expect(destination.textContent).not.toContain("Claude Code");
    expect(screen.queryByText("Ask for a test with each fix")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("wrap-send")).toBeEnabled());
    expect(screen.getByTestId("wrap-send").textContent).toContain("Post review to #412");
  });

  it("plans the review for the verdict on screen before anything is recorded", async () => {
    apiGithub.mockResolvedValue(PLAN());
    const onFinish = vi.fn();
    apiClosure.mockResolvedValue({ ...CLOSURE, target_count: 1 });
    show({ ...decided(), onFinish });
    await userEvent.click(within(await screen.findByTestId("wrap-note")).getByRole("button", { name: "Must fix" }));
    await waitFor(() => expect(apiGithub).toHaveBeenCalledWith("review", { outcome: "changes_requested", summary: "" }));
    await userEvent.click(screen.getByRole("button", { name: "Preview exactly what is sent" }));
    expect(onFinish).not.toHaveBeenCalled();
  });

  it("GH-1: the second tab draws the review, the inline comment on its line and the honest check-run line", async () => {
    apiGithub.mockResolvedValue(PLAN());
    await open();
    await userEvent.click(screen.getByRole("button", { name: "Preview exactly what is sent" }));
    await userEvent.click(await screen.findByRole("tab", { name: "As it will appear on GitHub" }));
    const page = await screen.findByTestId("gh-page");
    expect(page.textContent).toContain("srivera");
    expect(within(page).getByTestId("gh-verdict").textContent).toBe("Requested changes");
    expect(within(page).getByTestId("gh-body").textContent).toContain("Human review. Nothing here was decided by an AI.");
    expect(within(page).getByTestId("gh-comment").textContent).toContain("app/payments/webhooks.py:42");
    expect(within(page).queryByTestId("gh-ai")).toBeNull();
    expect(within(page).getByTestId("gh-no-check-run").textContent).toBe("No check run is posted from this machine.");
    expect(page.querySelector("img, svg")).toBeNull();
  });

  it("GH-1: a check run the server sends is drawn exactly, and an AI fix a note carries is disclosed", async () => {
    const run = { name: "LemonCrow review", text: "LemonCrow review · signed by @srivera · rev b52e7a9 · 1 of 2 signatures", state: "pending", label: "Pending · waiting for a second signature" };
    apiGithub.mockResolvedValue(PLAN({
      check_run: run,
      inline: [{ annotation_id: "ann1", path: "src/a.py", side: "RIGHT", start_line: 3, line: 3, body: "Guard it\n\nAI suggested fix (unverified; explicitly included by reviewer):\n-x\n+y", ai_fix: true }],
    }));
    await open();
    await userEvent.click(screen.getByRole("button", { name: "Preview exactly what is sent" }));
    await userEvent.click(await screen.findByRole("tab", { name: "As it will appear on GitHub" }));
    expect((await screen.findByTestId("gh-run-text")).textContent).toBe(run.text);
    expect(screen.getByTestId("gh-check-run").textContent).toContain("Pending · waiting for a second signature");
    expect(screen.queryByTestId("gh-no-check-run")).toBeNull();
    expect(screen.getByTestId("gh-ai").textContent).toContain("AI suggested fix, included by the reviewer");
  });

  it("GH-2: with no verdict the page tab asks for one and draws nothing", async () => {
    apiGithub.mockResolvedValue(PLAN({ event: "COMMENT" }));
    apiClosure.mockResolvedValue({ ...CLOSURE, target_count: 2 });
    show({ targets: [target("auth"), target("total")], notes: [NOTE], handoff: GITHUB });
    await waitFor(() => expect(apiGithub).toHaveBeenCalled());
    await userEvent.click(screen.getByRole("button", { name: "Preview exactly what is sent" }));
    await userEvent.click(await screen.findByRole("tab", { name: "As it will appear on GitHub" }));
    expect((await screen.findByTestId("gh-no-verdict")).textContent).toBe("Choose a verdict to see the review as it will appear.");
    expect(screen.queryByTestId("gh-page")).toBeNull();
  });

  it("What you send is the exact text of the plan, verdict first", async () => {
    apiGithub.mockResolvedValue(PLAN());
    await open();
    await userEvent.click(screen.getByRole("button", { name: "Preview exactly what is sent" }));
    expect((await screen.findByTestId("gh-send-text")).textContent).toBe(
      "Requested changes\n\nHuman review. Nothing here was decided by an AI.\n\napp/payments/webhooks.py:42\nCap retries by the spend budget.",
    );
  });

  it("a verdict GitHub will take as a comment is drawn as Commented, with the server's reason, and nothing claims the verdict", async () => {
    const notice = "GitHub does not let you approve or request changes on your own pull request; publishing as Comment.";
    apiGithub.mockResolvedValue(PLAN({ event: "COMMENT", notices: [notice] }));
    await open();
    await waitFor(() => expect(screen.getByTestId("wrap-send")).toBeEnabled());
    expect(screen.getByTestId("wrap-footer").textContent).toContain("GitHub shows Commented");
    expect(screen.getByText(/What happens next/).closest("section")?.textContent).toContain("GitHub will show the review as Commented.");
    await userEvent.click(screen.getByRole("button", { name: "Preview exactly what is sent" }));
    await userEvent.click(await screen.findByRole("tab", { name: "As it will appear on GitHub" }));
    expect((await screen.findByTestId("gh-verdict")).textContent).toBe("Commented");
    const notices = screen.getByTestId("gh-notices");
    expect(notices.textContent).toBe(notice);
    expect(notices.querySelector("[role]")).toBeNull();
  });

  it("a summary the server dropped (the verdict is already on GitHub) is explained, not silently gone", async () => {
    const notice = "Your verdict for this commit is already on GitHub; only new comments are published.";
    apiGithub.mockResolvedValue(PLAN({ event: "COMMENT", summary: "", body: "Human review. Nothing here was decided by an AI.", notices: [notice] }));
    await open();
    await userEvent.type(screen.getByRole("textbox"), "Ship it");
    await userEvent.click(screen.getByRole("button", { name: "Preview exactly what is sent" }));
    expect((await screen.findByTestId("gh-notices")).textContent).toBe(notice);
    expect((await screen.findByTestId("gh-send-text")).textContent).not.toContain("Ship it");
  });

  it("a verdict already on GitHub is not described as a comment: GitHub keeps showing it", async () => {
    apiGithub.mockResolvedValue(PLAN({
      event: "COMMENT", summary: "", verdict_on_github: true,
      notices: ["Your verdict for this commit is already on GitHub; only new comments are published."],
    }));
    await open();
    await waitFor(() => expect(screen.getByTestId("wrap-send")).toBeEnabled());
    expect(screen.getByTestId("wrap-footer").textContent).not.toContain("GitHub shows");
    expect(screen.getByText(/What happens next/).closest("section")?.textContent).not.toContain("GitHub will show");
    await userEvent.click(screen.getByRole("button", { name: "Preview exactly what is sent" }));
    expect((await screen.findByTestId("gh-notices")).textContent).toContain("already on GitHub");
  });

  it("when GitHub takes the verdict as chosen, the footer and next steps say nothing extra", async () => {
    apiGithub.mockResolvedValue(PLAN());
    await open();
    await waitFor(() => expect(screen.getByTestId("wrap-send")).toBeEnabled());
    expect(screen.getByTestId("wrap-footer").textContent).not.toContain("GitHub shows");
  });
});
