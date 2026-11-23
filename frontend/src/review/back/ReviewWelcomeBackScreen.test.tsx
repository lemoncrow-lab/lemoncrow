import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReviewWelcomeBackScreen } from "./ReviewWelcomeBackScreen";
import type { Annotation, ReviewOverview, ReviewTarget } from "../types";

const api = vi.hoisted(() => ({
  exportFeedback: vi.fn(),
  fetchFinishReview: vi.fn(),
  fetchHistoricalAnnotations: vi.fn(),
  fetchReviewOutcome: vi.fn(),
  fetchReviewRecordFile: vi.fn(),
  fetchReviewRecordPdf: vi.fn(),
  fetchReviewRecordStatus: vi.fn(),
  fetchReviewRecordVerify: vi.fn(),
  fetchReviewVerification: vi.fn(),
  fetchRevisionComparison: vi.fn(),
  postAnnotation: vi.fn(),
  postMark: vi.fn(),
  postReviewOutcome: vi.fn(),
}));
vi.mock("../reviewApi", async (importOriginal) => ({
  ...api, sourceCompareHref: (await importOriginal<typeof import("../reviewApi")>()).sourceCompareHref,
}));
const frontier = vi.hoisted(() => ({ fetchFrontierNotes: vi.fn(async (_id: string): Promise<string[]> => []) }));
vi.mock("./frontierApi", () => frontier);
vi.mock("@pierre/diffs", () => import("../focus/pierreTestMock").then((m) => m.pierreCore));
vi.mock("@pierre/diffs/react", () => import("../focus/pierreTestMock").then((m) => m.pierreReact));

const overview = { title: "Example review", session: { id: "r1" }, revision: { id: "rev2", revision_number: 1, provenance_host: "claude_code" } } as ReviewOverview;
function Where() { return <output data-testid="where">{useLocation().pathname}</output>; }
const target = { target_id: "auth", unit_key: "auth", path: "a.py", symbol: "auth",
  state: "unreviewed", reviewed_revision_id: "", changed_since_mark: false,
} as ReviewTarget;
const annotation = { id: "note1", unit_key: "auth", path: "a.py", symbol: "auth",
  body: "Keep the permission check", kind: "request_change", source: "human", parent_id: "",
} as Annotation;

const revision = {
  from_revision: { id: "rev1" }, to_revision: { id: "rev2" },
  units: [{ unit_key: "auth", status: "changed" }],
  code_available: true, code_files: [{ path: "a.py", patch: "@@ -1 +1 @@\n-allow_all\n+check_permission" }],
};

type MountOptions = Partial<React.ComponentProps<typeof ReviewWelcomeBackScreen>> & { notes?: Annotation[] };
function mount({ notes = [annotation], ...rest }: MountOptions = {}) {
  api.fetchHistoricalAnnotations.mockResolvedValue({ annotations: notes });
  render(<MemoryRouter>
    <ReviewWelcomeBackScreen reviewRef="example" apiReviewId="r1" revision={2}
      overview={overview} targets={[target]} readOnly={false}
      onDownloadRecord={async () => "Downloaded"} {...rest} />
    <Where />
  </MemoryRouter>);
}

describe("P2-6 Welcome back, real API-driven decisions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchRevisionComparison.mockResolvedValue(revision);
    api.fetchReviewOutcome.mockResolvedValue({ current: null });
    api.fetchReviewRecordStatus.mockResolvedValue({ records: [], signing: { available: false } });
    api.fetchReviewVerification.mockImplementation(async (_id: string, snapshot: string) => ({
      snapshot_id: snapshot, checks: [],
    }));
    api.postMark.mockResolvedValue({ downgraded: false });
    api.postAnnotation.mockResolvedValue({ annotation: {} });
    api.fetchFinishReview.mockResolvedValue({ status: "open", target_count: 1 });
    api.postReviewOutcome.mockResolvedValue({ current: { revision_id: "rev2", outcome: "lgtm", stale: false } });
  });
  afterEach(() => cleanup());

  it("keeps approval gated until the reviewer confirms the returned part, then records revision-specific approval", async () => {
    mount();
    expect(await screen.findByText("Keep the permission check")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Approve revision 2" })).toBeNull();
    expect(screen.getByText(/Not verified · no test is tied to this note/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Looks fixed/ }));
    await waitFor(() => expect(api.postMark).toHaveBeenCalledWith("r1", "auth", "reviewed", "rev2"));
    const approval = await screen.findByRole("button", { name: "Approve revision 2" });
    fireEvent.click(approval);
    await waitFor(() => expect(api.postReviewOutcome).toHaveBeenCalledWith(
      "r1", "lgtm", expect.stringContaining("Revision 2"), "rev2",
    ));
    expect(await screen.findByTestId("back-approved")).toHaveProperty("textContent", expect.stringContaining("LemonCrow never merges for you."));
    expect(screen.getByText("Nothing was merged. Your approval is recorded.")).toBeTruthy();
    // Approved: the outcome comes first; the pre-approval banner and the answer buttons are gone.
    expect(screen.queryByTestId("back-banner")).toBeNull();
    expect(screen.queryByRole("button", { name: /Looks fixed/ })).toBeNull();
    const panel = screen.getByTestId("back-approved");
    expect(panel.compareDocumentPosition(screen.getByTestId("back-part")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(panel).getByRole("button", { name: "Copy approval for Claude Code" })).toBeTruthy();
  });

  it("names the agent that made the change on the approval hand-off", async () => {
    mount({ overview: { ...overview, revision: { ...overview.revision, provenance_host: "codex" } } as ReviewOverview });
    fireEvent.click(await screen.findByRole("button", { name: /Looks fixed/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Approve revision 2" }));
    const panel = await screen.findByTestId("back-approved");
    expect(within(panel).getByRole("button", { name: "Copy approval for Codex" })).toBeTruthy();
    expect(panel.textContent).not.toContain("Claude Code");
  });

  it("with no recorded agent, the hand-off button does not say \"for The agent\"", async () => {
    mount({ overview: { ...overview, revision: { ...overview.revision, provenance_host: "" } } as ReviewOverview });
    fireEvent.click(await screen.findByRole("button", { name: /Looks fixed/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Approve revision 2" }));
    const panel = await screen.findByTestId("back-approved");
    expect(within(panel).getByRole("button", { name: "Copy approval" })).toBeTruthy();
  });

  it("will not approve when an earlier request cannot be matched to the new code", async () => {
    mount({ notes: [{ ...annotation, unit_key: "missing", symbol: "gone", path: "gone.py" }] });
    expect(await screen.findByText(/Earlier notes still need attention/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Looks good/ }));
    await waitFor(() => expect(api.postMark).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Approve revision 2" })).toBeNull();
  });

  it("saves a new revision note instead of approving if the reviewer says it still needs a change", async () => {
    mount();
    const change = await screen.findByRole("button", { name: /Still needs a change/ });
    fireEvent.click(change);
    fireEvent.change(screen.getByLabelText("What is still wrong?"), { target: { value: "Still missing the validation guard" } });
    fireEvent.click(screen.getByRole("button", { name: "Keep open" }));
    await waitFor(() => expect(api.postAnnotation).toHaveBeenCalledWith("r1",
      expect.objectContaining({ body: "Still missing the validation guard", target_unit_key: "auth", kind: "request_change" }), "rev2"));
    expect(await screen.findByRole("button", { name: /request changes/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Approve revision 2" })).toBeNull();
    expect(api.postReviewOutcome).not.toHaveBeenCalled();
  });

  it("does not allow judgments in a read-only view", async () => {
    mount({ readOnly: true });
    const button = await screen.findByRole("button", { name: /Looks fixed/ });
    expect(button.hasAttribute("disabled")).toBe(true);
    expect(screen.queryByRole("button", { name: "Approve revision 2" })).toBeNull();
  });

  it("BACK-1: says the new revision needs its own signed record, with the SPEC headline", async () => {
    mount();
    expect(await screen.findByText("Claude Code answered your note.")).toBeTruthy();
    expect(screen.getByTestId("back-banner")).toHaveProperty("textContent",
      expect.stringContaining("Your Revision 1 record stays on Revision 1. Revision 2 needs its own signed record."));
    expect(screen.getByText("The agent's commit message was not used as evidence")).toBeTruthy();
    // review-21.4: the change since Revision 1 is drawn by the diff viewer, not printed as raw patch text.
    const diff = screen.getByRole("region", { name: "Changes in a.py since Revision 1" });
    expect(within(diff).getByTestId("mock-pierre-diff")).toBeTruthy();
    expect(diff.querySelector('[data-line-type="change-addition"]')?.textContent).toBe("check_permission");
    // review-21.3: a note row has no "Your note" badge on top of the quote that says it.
    expect(within(screen.getAllByTestId("back-part")[0]).queryByText("Your note", { selector: "span" })).toBeNull();
    // Hosted has no Compare reader, so the link is there only when the reader says it exists.
    expect(screen.queryByTestId("back-compare-link")).toBeNull();
  });

  it("review-22.1: links the whole comparison, in the Compare reader for this review's two revisions", async () => {
    mount({ canCompare: true });
    await screen.findByText("Claude Code answered your note.");
    expect(screen.getByTestId("back-compare-link").textContent).toBe("Compare with Revision 1");
    fireEvent.click(screen.getByTestId("back-compare-link"));
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/r/example/compare/rev1..rev2"));
  });

  it("DONE-6: tiles that would read zero are not shown, and no lone Came back tile or Round eyebrow repeats the heading (review-21.3)", async () => {
    mount();
    await screen.findByText("Claude Code answered your note.");
    expect(screen.queryByLabelText("Revision summary")).toBeNull();
    expect(screen.queryByText("Came back")).toBeNull();
    expect(screen.queryByText("Not from your notes", { selector: "div" })).toBeNull();
    expect(screen.queryByText("Not touched")).toBeNull();
    expect(screen.queryByText(/^0$/)).toBeNull();
    expect(screen.queryByText(/Round \d/)).toBeNull();
    // The sub line still says how many notes came back.
    expect(screen.getByText(/1 note came back changed\./)).toBeTruthy();
  });

  it("BACK-5: a test that passes on both revisions does not prove the fix", async () => {
    api.fetchReviewVerification.mockImplementation(async (_id: string, snapshot: string) => ({
      snapshot_id: snapshot, checks: [{ name: "tests/test_auth.py::guest", stable_key: "k", state: "current", snapshot_id: snapshot, status: "PASS" }],
    }));
    mount();
    expect(await screen.findByTestId("back-no-proof")).toHaveProperty("textContent", expect.stringContaining("it doesn't prove the fix"));
    expect(screen.getByText("Is it fixed? Your answer is needed.")).toBeTruthy();
    expect(screen.queryByTestId("back-fail-pass")).toBeNull();
  });

  describe("BACK-2: Send N note & request changes goes through the real guarded path", () => {
    const needsChange = { ...target, state: "needs_changes", reviewed_revision_id: "rev2", answer: { category: "decision" } } as unknown as ReviewTarget;
    const stored = { id: "n2", review_id: "r1", revision_id: "rev2", state: "open", source: "human", kind: "request_change",
      body: "Still not right: admin check", path: "a.py", parent_id: "", start_line: 0, end_line: 0 } as unknown as Annotation;
    const handoff = { kind: "agent" as const, label: "Claude Code", available: true, reason: "" };
    const queued = { state: "pending", operation_id: `fs-${"b".repeat(32)}`, send_after: new Date(Date.now() + 10_000).toISOString(), created_at: new Date().toISOString() };

    const other = { ...stored, id: "n3", body: "An older open note on the same revision" } as unknown as Annotation;
    const exported = { revision_id: "rev2", open: 1, orphaned: 0, feedback_hash: "h", operation_id: "op", annotation_versions: { n2: 1 }, markdown: "MUST FIX: Still not right: admin check" };
    beforeEach(() => { window.localStorage.clear(); window.sessionStorage.clear(); api.exportFeedback.mockResolvedValue(exported); });

    async function ready(onQueue = vi.fn().mockResolvedValue(queued)) {
      mount({ targets: [needsChange], annotations: [stored, other], handoff, onQueue });
      const send = await screen.findByTestId("back-send-changes");
      expect((send as HTMLButtonElement).disabled).toBe(true); // no importance chosen: nothing is previewed or sendable
      expect(screen.queryByTestId("back-exact-preview")).toBeNull();
      fireEvent.click(await screen.findByRole("button", { name: "Must fix" }));
      await screen.findByTestId("back-exact-preview");
      await waitFor(() => expect((send as HTMLButtonElement).disabled).toBe(false));
      return { send, onQueue };
    }

    it("shows only the server's notes and the exact preview, sends exactly what was shown, and lands on Done", async () => {
      const { send, onQueue } = await ready();
      expect(screen.getAllByTestId("back-send-note")).toHaveLength(1);
      expect(screen.queryByText("An older open note on the same revision")).toBeNull();
      expect(send.textContent).toBe("Send 1 note & request changes");
      expect(screen.getByTestId("back-exact-preview").textContent).toContain("MUST FIX");
      fireEvent.click(send);
      await waitFor(() => expect(onQueue).toHaveBeenCalledWith("changes_requested", "", expect.objectContaining({ feedback_hash: "h", operation_id: "op" }), expect.stringMatching(/^fs-[0-9a-f]{32}$/)));
      expect(api.exportFeedback).toHaveBeenLastCalledWith("r1", expect.objectContaining({ presentation: expect.objectContaining({ verdict: "changes_requested", importance: { n2: "must_fix" } }) }));
      await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/r/example/done"));
    });

    it("refuses when the notes changed after the preview was shown", async () => {
      const { send, onQueue } = await ready();
      api.exportFeedback.mockResolvedValue({ ...exported, feedback_hash: "changed", annotation_versions: { n2: 1, n3: 1 } });
      fireEvent.click(send);
      expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("changed after the preview"));
      expect(onQueue).not.toHaveBeenCalled();
    });

    it("sends nothing when the review is no longer open", async () => {
      api.fetchFinishReview.mockResolvedValue({ status: "finished", target_count: 1 });
      const { send, onQueue } = await ready(vi.fn());
      fireEvent.click(send);
      expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("The review changed"));
      expect(onQueue).not.toHaveBeenCalled();
    });

    it("is disabled, with the reason, when the destination cannot take an undoable send", async () => {
      mount({ targets: [needsChange], annotations: [stored], handoff: { ...handoff, kind: "export", label: "the author" }, onQueue: vi.fn() });
      const send = await screen.findByTestId("back-send-changes");
      expect((send as HTMLButtonElement).disabled).toBe(true);
      expect(screen.getByRole("button", { name: "Open Wrap up" })).toBeTruthy();
    });
  });
});

describe("Welcome back: what happened to your earlier answers", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchRevisionComparison.mockResolvedValue(revision);
    api.fetchReviewOutcome.mockResolvedValue({ current: null });
    api.fetchReviewVerification.mockImplementation(async (_id: string, snapshot: string) => ({ snapshot_id: snapshot, checks: [] }));
    frontier.fetchFrontierNotes.mockResolvedValue([]);
  });
  afterEach(() => cleanup());
  const kept = (id: string) => ({ ...target, target_id: id, unit_key: id, symbol: id, state: "reviewed", reviewed_revision_id: "rev1" }) as ReviewTarget;
  const delta = (extra: Record<string, unknown> = {}) => ({ since_snapshot: 0, added: [], changed: [], removed: 0, stale_evidence: [], reopened_decisions: [], comments: { carried: 0, resolved: 0 }, ...extra });
  const discard = (index: number) => ({ unit_key: `u${index}`, kind: "symbol", state: index % 2 ? "needs_changes" : "reviewed", label: `pay.py · f${index}`, reason: "The function was deleted." });
  const working = (extra: Partial<ReviewOverview> = {}) => ({ ...overview, session: { ...overview.session, range_mode: "working_tree" }, ...extra }) as ReviewOverview;

  it("says what was kept and removed since the last revision, with the removed parts named", async () => {
    mount({ targets: [target, kept("a"), kept("b"), { ...kept("c"), reviewed_revision_id: "rev2" }], delta: delta({ removed: 2, removed_parts: ["old.py · gone", "old.py · other"] }) as never });
    const card = await screen.findByTestId("back-carried");
    expect(within(card).getByRole("heading", { name: "Since Revision 1" })).toBeTruthy();
    // "c" was answered on this revision, so it is not carried over.
    expect(within(card).getByTestId("back-carried-counts")).toHaveTextContent(/^Kept 2 · Removed 2$/);
    expect(within(card).getByText("old.py · gone")).toBeTruthy();
    expect(within(card).getByText("old.py · other")).toBeTruthy();
    expect(within(card).queryByTestId("back-rebased")).toBeNull();
  });

  it("hides a zero count, counts the rest past 25 removed names, and says when only the base moved", async () => {
    const names = Array.from({ length: 25 }, (_, index) => `old.py · f${index}`);
    mount({ overview: { ...overview, refreshed: { rebased: 3 } } as ReviewOverview, delta: delta({ removed: 30, removed_parts: names }) as never });
    const card = await screen.findByTestId("back-carried");
    expect(within(card).getByTestId("back-carried-counts")).toHaveTextContent(/^Removed 30$/);
    expect(within(card).getByTestId("back-rebased")).toHaveTextContent("3 kept because only the base moved.");
    expect(within(card).getByText("and 5 more")).toBeTruthy();
  });

  it("shows no card with nothing kept, removed or moved", async () => {
    mount({ delta: delta() as never });
    await screen.findByText("Keep the permission check");
    expect(screen.queryByTestId("back-carried")).toBeNull();
    expect(screen.queryByTestId("back-discarded")).toBeNull();
    // A committed range has no moving base, so the frontier is never asked.
    expect(frontier.fetchFrontierNotes).not.toHaveBeenCalled();
  });

  it("lists past answers that no longer apply, with the server's reason, bounded at 25", async () => {
    mount({ overview: { ...overview, discarded: Array.from({ length: 25 }, (_, index) => discard(index)) } as ReviewOverview });
    const section = await screen.findByTestId("back-discarded");
    expect(within(section).getByRole("heading", { name: "Past answers no longer apply · 25" })).toBeTruthy();
    const rows = within(section).getAllByTestId("back-discarded-row");
    expect(rows).toHaveLength(25);
    expect(rows[0]).toHaveTextContent("pay.py · f0 · Looks good · The function was deleted.");
    expect(rows[1]).toHaveTextContent("pay.py · f1 · Needs a change · The function was deleted.");
    expect(within(section).queryByText(/more$/)).toBeNull();
    cleanup();
    mount({ overview: { ...overview, discarded: Array.from({ length: 26 }, (_, index) => discard(index)) } as ReviewOverview });
    const longer = await screen.findByTestId("back-discarded");
    expect(within(longer).getByRole("heading", { name: "Past answers no longer apply · 26" })).toBeTruthy();
    expect(within(longer).getAllByTestId("back-discarded-row")).toHaveLength(25);
    expect(within(longer).getByText("and 1 more")).toBeTruthy();
  });

  it("names the base movement on a working-tree review only when the server has a note", async () => {
    frontier.fetchFrontierNotes.mockResolvedValue(["main moved 3 commits since this review started."]);
    mount({ overview: working() });
    const note = await screen.findByTestId("back-base-movement");
    expect(note).toHaveTextContent("Base movement · main moved 3 commits since this review started.");
    expect(frontier.fetchFrontierNotes).toHaveBeenCalledWith("r1");
    cleanup();
    frontier.fetchFrontierNotes.mockResolvedValue([]);
    mount({ overview: working() });
    await screen.findByText("Keep the permission check");
    await waitFor(() => expect(frontier.fetchFrontierNotes).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("back-base-movement")).toBeNull();
    expect(screen.queryByTestId("back-carried")).toBeNull();
  });
});
