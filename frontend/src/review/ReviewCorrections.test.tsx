/**
 * Controlled Review actions in the reader (Phase 12): which action addressed
 * which finding, Fix routed through a previewed action, honest unavailable
 * states, and expert actions reachable from the palette and the More menu.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps, ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";

import AnnotationCard from "./AnnotationCard";
import ReviewFixSheet from "./ReviewFixSheet";
import { FlowPaletteProvider, useFlowPaletteScreen } from "./palette/FlowPalette";
import { ReviewCorrectionsHost, ReviewCorrectionsProvider, useReviewCorrections, type ReviewCorrectionsContextValue } from "./reviewCorrections";
import { actionProvenance, correctionPaletteActions, latestActionBySubject } from "./reviewActionsModel";
import type { ReviewActionAvailability, ReviewActionPreview, ReviewActionRecord, ReviewActionStatus } from "./reviewActionsApi";
import type { Annotation } from "./types";

const actionsApi = vi.hoisted(() => ({
  fetchReviewActions: vi.fn(),
  previewReviewAction: vi.fn(),
  startReviewAction: vi.fn(),
  cancelReviewAction: vi.fn(),
}));
vi.mock("./reviewActionsApi", () => actionsApi);
vi.mock("./home/homeApi", () => {
  const source = { load: vi.fn(async () => ({ rows: [], teamsSupported: false, truncated: false, queueSupported: false })), snooze: vi.fn(), terminal: true };
  return { useHomeSource: () => source };
});

const EXACT = {
  tier: "exact_session" as const,
  mode: "resume" as const,
  available: true,
  host: "claude",
  session_id: "s-claude",
  target_ref: "claude:s-claude",
  label: "Claude s-claude",
  reason: "",
};

function record(overrides: Partial<ReviewActionRecord> = {}): ReviewActionRecord {
  return {
    id: "act-1",
    review_id: "review-1",
    kind: "fix",
    label: "Fix",
    code_changing: true,
    source_revision_id: "rrv-1",
    source_revision_number: 1,
    route: EXACT,
    status: "awaiting_snapshot",
    created_by: "local",
    created_at: "2026-10-03T10:00:00+00:00",
    updated_at: "2026-10-03T10:00:00+00:00",
    subjects: [{ kind: "ai_finding", id: "ann-ai", title: "Missing guard", path: "src/svc.py", start_line: 3, end_line: 3, revision_id: "rrv-1", state: "open" }],
    excluded: [],
    directive: "",
    handoff_digest: "d",
    identity: "i",
    resulting_revision_id: "",
    resulting_revision_number: null,
    result_certainty: "",
    message: "",
    remote_ref: "",
    events: [{ kind: "created", at: "", actor_type: "human", actor_id: "local", detail: "Fix", revision_id: "rrv-1" }],
    version: 1,
    rerun: null,
    ...overrides,
  };
}

function finding(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: "ann-ai",
    review_id: "review-1",
    revision_id: "rrv-1",
    parent_id: "",
    kind: "comment",
    state: "open",
    title: "Missing guard",
    body: "prices can be None here",
    created_by: "LemonCrow AI Review",
    created_by_actor: "agent",
    source: "ai_review",
    source_id: "finding-1",
    finding_disposition: "open",
    created_at: "",
    updated_at: "",
    anchor_method: "identical_blob",
    anchor_method_label: "file unchanged since the comment",
    anchor_exact: true,
    anchor_detail: "",
    anchored: true,
    path: "src/svc.py",
    side: "new",
    start_line: 3,
    end_line: 3,
    unit_key: "sym:abc",
    symbol: "total_price",
    origin_symbol: "",
    ...overrides,
  } as Annotation;
}

function mountCard(context: ReviewCorrectionsContextValue | null, onFindingFix = vi.fn(), comments = [finding()]) {
  const card = (
    <AnnotationCard
      comments={comments}
      all={comments}
      label="Comment"
      composing={false}
      busy={false}
      onSubmit={vi.fn()}
      onCancel={vi.fn()}
      onResolve={vi.fn()}
      onReopen={vi.fn()}
      onFindingDisposition={vi.fn()}
      onFindingFix={onFindingFix}
    />
  );
  render(context ? <ReviewCorrectionsProvider value={context}>{card}</ReviewCorrectionsProvider> : card);
  return onFindingFix;
}

function contextWith(actions: ReviewActionRecord[], overrides: Partial<ReviewCorrectionsContextValue> = {}): ReviewCorrectionsContextValue {
  return { byFinding: latestActionBySubject(actions), canFix: true, openFix: vi.fn(), cancel: vi.fn(), ...overrides };
}

afterEach(() => {
  Reflect.deleteProperty(navigator, "clipboard");
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe("action provenance", () => {
  it.each<ReviewActionStatus>(["dispatching", "awaiting_snapshot", "uncertain", "snapshot_recorded", "failed", "cancelled"])(
    "%s never claims the finding is resolved or fixed",
    (status) => {
      const text = actionProvenance(record({ status, resulting_revision_number: 2 })).text;
      expect(text).not.toMatch(/\bresolved\b|\bfixed\b/i);
    },
  );

  it("names the route, the resulting revision and re-review, and says failure leaves the finding unchanged", () => {
    expect(actionProvenance(record()).text).toBe("Fix sent to Claude s-claude · waiting for a new revision");
    expect(actionProvenance(record({ status: "snapshot_recorded", resulting_revision_number: 2, result_certainty: "exact" })).text)
      .toBe("Addressed by Fix → Revision 3 · re-review");
    expect(actionProvenance(record({ status: "snapshot_recorded", resulting_revision_number: 2, result_certainty: "unattributed", route: { ...EXACT, tier: "prompt_fallback", mode: "copy" } })).text)
      .toContain("not attributed to the fixer");
    expect(actionProvenance(record({ status: "failed", message: "session not found" })).text)
      .toBe("Fix failed: session not found · the finding is unchanged");
    expect(actionProvenance(record({ status: "cancelled" })).text).toBe("Fix cancelled · the finding is unchanged");
  });

  it("shows the newest action per finding", () => {
    const older = record({ id: "act-old", status: "failed", created_at: "2026-10-03T09:00:00+00:00" });
    const newer = record({ id: "act-new", created_at: "2026-10-03T11:00:00+00:00" });
    expect(latestActionBySubject([newer, older]).get("ann-ai")?.id).toBe("act-new");
  });
});

describe("finding cards show which action addressed which finding", () => {
  it("links the finding to its action and resulting revision without changing its disposition label", () => {
    const recorded = record({ status: "snapshot_recorded", resulting_revision_id: "rrv-2", resulting_revision_number: 2, result_certainty: "exact" });
    const other = record({ id: "act-2", subjects: [{ ...record().subjects[0], id: "ann-other" }] });
    mountCard(contextWith([recorded, other]), vi.fn(), [finding(), finding({ id: "ann-untouched", title: "Another finding" })]);
    const lines = screen.getAllByTestId("finding-action-provenance");
    expect(lines).toHaveLength(1);
    expect(lines[0].textContent).toContain("Addressed by Fix → Revision 3 · re-review");
    // The disposition is still the reviewer's: the finding reads Open, not Fixed.
    const actions = screen.getAllByLabelText("AI finding actions")[0];
    expect(within(actions).getByText("Open")).toBeTruthy();
  });

  it("routes Fix through the previewed action, not the legacy suggest-edit path", async () => {
    const openFix = vi.fn();
    const legacy = mountCard(contextWith([], { openFix }));
    await userEvent.click(screen.getByRole("button", { name: "Fix" }));
    expect(openFix).toHaveBeenCalledWith(["ann-ai"]);
    expect(legacy).not.toHaveBeenCalled();
  });

  it("disables Fix and offers Cancel while an action is in flight", async () => {
    const cancel = vi.fn();
    mountCard(contextWith([record()], { cancel }));
    expect(screen.getByTestId("finding-action-provenance").textContent).toContain("Fix sent to Claude s-claude · waiting for a new revision");
    expect((screen.getByRole("button", { name: "Fix" }) as HTMLButtonElement).disabled).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(cancel).toHaveBeenCalledWith("act-1");
  });

  it("keeps the legacy Fix where no writable action route exists", async () => {
    const legacy = mountCard(contextWith([], { canFix: false }));
    await userEvent.click(screen.getByRole("button", { name: "Fix" }));
    expect(legacy).toHaveBeenCalledTimes(1);
  });
});

function previewOf(overrides: Partial<ReviewActionPreview> = {}): ReviewActionPreview {
  return {
    revision_id: "rrv-1",
    kind: "fix_all",
    label: "Fix all",
    operation_id: "act-op",
    ready: true,
    included: [
      { kind: "ai_finding", id: "ann-a", title: "Finding A", path: "src/a.py", start_line: 4, end_line: 4, revision_id: "rrv-1", state: "open" },
    ],
    excluded: [
      { subject_id: "ann-b", kind: "ai_finding", code: "different_fixer", reason: "goes to Codex s-codex, not Claude s-claude; fix it as its own action", title: "Finding B" },
      { subject_id: "ann-h", kind: "human_comment", code: "human_feedback", reason: "human comments reach the fixer through Send feedback", title: "Rename this" },
    ],
    route: EXACT,
    candidates: [
      EXACT,
      { ...EXACT, tier: "preferred_agent", mode: "new_session", available: false, host: "", session_id: "", target_ref: "", label: "", reason: "no preferred fixing agent is set; run `lemoncrow settings set review.fix_agent <claude|codex|opencode>`" },
      { ...EXACT, tier: "prompt_fallback", mode: "copy", host: "", session_id: "", target_ref: "", label: "Copy the fix prompt" },
    ],
    handoff: "LemonCrow Review · correction handoff act-op · Revision 2\n\n## Authority\n...",
    handoff_digest: "digest-1",
    limit: 20,
    code_changing: true,
    ...overrides,
  };
}

function mountSheet(props: Partial<ComponentProps<typeof ReviewFixSheet>> = {}) {
  const onStarted = vi.fn();
  const onClose = vi.fn();
  render(
    <ReviewFixSheet
      reviewId="review-1"
      revisionId="rrv-1"
      request={{ kind: "fix_all", subject_ids: ["ann-a", "ann-b", "ann-h"] }}
      onStarted={onStarted}
      onClose={onClose}
      {...props}
    />,
  );
  return { onStarted, onClose };
}

describe("ReviewFixSheet", () => {
  it("shows the route order with honest reasons, what is excluded and why, and dispatches exactly the preview", async () => {
    const preview = previewOf();
    actionsApi.previewReviewAction.mockResolvedValue(preview);
    actionsApi.startReviewAction.mockResolvedValue({ action: record({ kind: "fix_all" }), replayed: false });
    const { onStarted } = mountSheet();

    const send = await screen.findByRole("button", { name: "Send to Claude s-claude" });
    expect(actionsApi.previewReviewAction).toHaveBeenCalledWith("review-1", "rrv-1", { kind: "fix_all", subject_ids: ["ann-a", "ann-b", "ann-h"], route: undefined, directive: "" });
    const routes = screen.getByRole("radiogroup", { name: "Fix route" });
    expect(within(routes).getByText("Authoring session")).toBeTruthy();
    expect(within(routes).getByText("no preferred fixing agent is set; run `lemoncrow settings set review.fix_agent <claude|codex|opencode>`")).toBeTruthy();
    expect((within(routes).getAllByRole("radio")[1] as HTMLInputElement).disabled).toBe(true);
    const excluded = screen.getByRole("region", { name: "Excluded" });
    expect(excluded.textContent).toContain("goes to Codex s-codex, not Claude s-claude");
    expect(excluded.textContent).toContain("human comments reach the fixer through Send feedback");
    expect((screen.getByLabelText("Your note to the fixer (optional)") as HTMLTextAreaElement).value).toBe("");

    await userEvent.click(send);
    expect(actionsApi.startReviewAction).toHaveBeenCalledWith(
      "review-1",
      { kind: "fix_all", subject_ids: ["ann-a", "ann-b", "ann-h"], route: undefined, directive: "" },
      preview,
      { copied: false },
    );
    await waitFor(() => expect(onStarted).toHaveBeenCalledTimes(1));
  });

  it("re-previews a reviewer note before it can be sent", async () => {
    actionsApi.previewReviewAction.mockResolvedValue(previewOf());
    mountSheet();
    await screen.findByRole("button", { name: "Send to Claude s-claude" });
    await userEvent.type(screen.getByLabelText("Your note to the fixer (optional)"), "Prefer a guard clause");
    expect(screen.queryByRole("button", { name: "Send to Claude s-claude" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Update preview" }));
    await waitFor(() => expect(actionsApi.previewReviewAction).toHaveBeenLastCalledWith(
      "review-1", "rrv-1", expect.objectContaining({ directive: "Prefer a guard clause" }),
    ));
    expect(await screen.findByRole("button", { name: "Send to Claude s-claude" })).toBeTruthy();
    expect(actionsApi.startReviewAction).not.toHaveBeenCalled();
  });

  it("copies the prompt first and only then records a copied handoff", async () => {
    const fallback = { ...EXACT, tier: "prompt_fallback" as const, mode: "copy" as const, host: "", session_id: "", target_ref: "", label: "Copy the fix prompt" };
    const preview = previewOf({ route: fallback, candidates: [{ ...EXACT, available: false, reason: "no exact authoring session" }, fallback] });
    actionsApi.previewReviewAction.mockResolvedValue(preview);
    actionsApi.startReviewAction.mockResolvedValue({ action: record({ route: fallback }), replayed: false });
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    mountSheet();
    await userEvent.click(await screen.findByRole("button", { name: "Copy prompt and record handoff" }));
    expect(writeText).toHaveBeenCalledWith(preview.handoff);
    expect(actionsApi.startReviewAction).toHaveBeenCalledWith("review-1", expect.anything(), preview, { copied: true });
  });

  it("does not record a handoff when the copy fails", async () => {
    const fallback = { ...EXACT, tier: "prompt_fallback" as const, mode: "copy" as const };
    actionsApi.previewReviewAction.mockResolvedValue(previewOf({ route: fallback, candidates: [fallback] }));
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    mountSheet();
    await userEvent.click(await screen.findByRole("button", { name: "Copy prompt and record handoff" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(actionsApi.startReviewAction).not.toHaveBeenCalled();
  });

  it("shows a refused preview and never offers to send", async () => {
    actionsApi.previewReviewAction.mockRejectedValue(new Error("Resolve merge conflicts is unavailable: LemonCrow does not observe this Change's mergeability yet"));
    mountSheet({ request: { kind: "resolve_merge_conflicts", subject_ids: [] } });
    expect((await screen.findByRole("alert")).textContent).toContain("does not observe this Change's mergeability");
    expect(screen.queryByRole("button", { name: /^Send to/ })).toBeNull();
  });
});

const CATALOG: ReviewActionAvailability[] = [
  { kind: "fix_all", label: "Fix all", available: true, reason: "", setup: "", subject_ids: ["ann-a", "ann-b"], code_changing: true },
  { kind: "fix_ci_failure", label: "Fix CI failure", available: false, reason: "no failing check on this revision", setup: "", subject_ids: [], code_changing: true },
  { kind: "resolve_merge_conflicts", label: "Resolve merge conflicts", available: false, reason: "LemonCrow does not observe this Change's mergeability yet", setup: "Resolve conflicts in your agent session, then capture a revision", subject_ids: [], code_changing: true },
  { kind: "create_follow_up_issue", label: "Create follow-up issue", available: false, reason: "no issue tracker is connected", setup: "Connect an issue tracker", subject_ids: [], code_changing: false },
];

describe("expert correction actions", () => {
  it("lists every catalog kind once, keeping unavailable ones visible with their reason", () => {
    const open = vi.fn();
    const entries = correctionPaletteActions(CATALOG, open);
    expect(entries.map((item) => item.label)).toEqual([
      "Fix all open findings (2)…",
      "Fix CI failure…",
      "Resolve merge conflicts…",
      "Create follow-up issue…",
    ]);
    expect(entries.map((item) => Boolean(item.disabled))).toEqual([false, true, true, true]);
    expect(entries[2].detail).toBe("LemonCrow does not observe this Change's mergeability yet · Resolve conflicts in your agent session, then capture a revision");
    entries[0].run();
    expect(open).toHaveBeenCalledWith("fix_all", ["ann-a", "ann-b"]);
  });

  it("offers Revert and Fix forward only when the server catalog lists a production problem", () => {
    const open = vi.fn();
    const entries = correctionPaletteActions(
      [
        { kind: "revert", label: "Revert", available: true, reason: "", setup: "", subject_ids: ["outcome:deployment:production"], code_changing: true },
        { kind: "fix_forward", label: "Fix forward", available: false, reason: "production has shown no problem with this merged Change", setup: "", subject_ids: [], code_changing: true },
      ],
      open,
    );
    expect(entries.map((item) => item.label)).toEqual(["Revert…", "Fix forward…"]);
    expect(entries[1].disabled).toBe(true);
    entries[0].run();
    expect(open).toHaveBeenCalledWith("revert", ["outcome:deployment:production"]);
    expect(correctionPaletteActions(CATALOG, open).some((item) => /Revert|Fix forward/.test(item.label))).toBe(false);
  });
});

describe("the Reader's corrections host", () => {
  const LISTING = { revision_id: "rrv-1", actions: [], catalog: CATALOG, fix_all_limit: 20 };

  // The Reader's own wiring: one useReviewCorrections, landed by ReviewCorrectionsHost around the flow screens.
  function Reader({ readOnly = false, children }: { readOnly?: boolean; children: ReactNode }) {
    const corrections = useReviewCorrections({ reviewId: "review-1", revisionId: "rrv-1", enabled: true, readOnly });
    return <ReviewCorrectionsHost corrections={corrections}>{children}</ReviewCorrectionsHost>;
  }

  function FlowScreen() {
    useFlowPaletteScreen({ context: { screen: "overview", actions: {} } });
    return <p>Overview</p>;
  }

  async function openPalette(readOnly = false) {
    const user = userEvent.setup();
    render(<MemoryRouter><FlowPaletteProvider><Reader readOnly={readOnly}><FlowScreen /></Reader></FlowPaletteProvider></MemoryRouter>);
    await waitFor(() => expect(actionsApi.fetchReviewActions).toHaveBeenCalledWith("review-1"));
    await act(async () => { await Promise.resolve(); });
    await user.keyboard("{Control>}k{/Control}");
    const palette = await screen.findByTestId("flow-palette");
    const option = (label: string) => within(palette).getAllByRole("option").find((item) => item.getAttribute("data-pal-item") === label);
    return { user, palette, option };
  }

  it("Fix on a finding card opens the preview sheet, which asks before anything is sent", async () => {
    actionsApi.fetchReviewActions.mockResolvedValue(LISTING);
    actionsApi.previewReviewAction.mockResolvedValue(previewOf({ kind: "fix", label: "Fix" }));
    const comments = [finding()];
    render(
      <Reader>
        <AnnotationCard comments={comments} all={comments} label="Comment" composing={false} busy={false} onSubmit={vi.fn()} onCancel={vi.fn()} onResolve={vi.fn()} onReopen={vi.fn()} onFindingDisposition={vi.fn()} />
      </Reader>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Fix" }));
    expect(await screen.findByRole("dialog", { name: "Fix" })).toBeInTheDocument();
    expect(actionsApi.previewReviewAction).toHaveBeenCalledWith("review-1", "rrv-1", expect.objectContaining({ kind: "fix", subject_ids: ["ann-ai"] }));
    expect(await screen.findByRole("button", { name: "Send to Claude s-claude" })).toBeInTheDocument();
    expect(actionsApi.startReviewAction).not.toHaveBeenCalled();
  });

  it("the ⌘K palette lists the correction actions; an unavailable one says why and does nothing, an available one opens its sheet", async () => {
    actionsApi.fetchReviewActions.mockResolvedValue(LISTING);
    actionsApi.previewReviewAction.mockResolvedValue(previewOf());
    const { user, palette, option } = await openPalette();
    expect(within(palette).getByRole("group", { name: "In this review" })).toBeInTheDocument();
    for (const label of ["Fix all open findings (2)…", "Fix CI failure…", "Resolve merge conflicts…", "Request split…"]) expect(option(label)).toBeTruthy();
    const ci = option("Fix CI failure…")!;
    expect(ci).toHaveAttribute("aria-disabled", "true");
    expect(ci).toHaveTextContent("no failing check on this revision");
    await user.click(ci);
    expect(screen.getByTestId("flow-palette")).toBeInTheDocument();
    expect(actionsApi.previewReviewAction).not.toHaveBeenCalled();
    await user.click(option("Fix all open findings (2)…")!);
    expect(await screen.findByRole("dialog", { name: "Fix all" })).toBeInTheDocument();
    expect(actionsApi.previewReviewAction).toHaveBeenCalledWith("review-1", "rrv-1", expect.objectContaining({ kind: "fix_all", subject_ids: ["ann-a", "ann-b"] }));
  });

  it("a read-only revision keeps the actions listed but off, and says where they work", async () => {
    actionsApi.fetchReviewActions.mockResolvedValue(LISTING);
    const { user, option } = await openPalette(true);
    const fixAll = option("Fix all open findings (2)…")!;
    expect(fixAll).toHaveAttribute("aria-disabled", "true");
    expect(fixAll).toHaveTextContent("Available on the current, writable revision");
    expect(option("Request split…")).toHaveAttribute("aria-disabled", "true");
    await user.click(fixAll);
    expect(actionsApi.previewReviewAction).not.toHaveBeenCalled();
  });
});
