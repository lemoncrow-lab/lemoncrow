import { render, screen, waitFor, cleanup, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchAnnotations, fetchReviewFlowSend, fetchReviewRecordStatus, fetchRevisions, fetchTargets, listReviews, undoReviewFlowSend } from "../reviewApi";
import type { ReviewFlowSend, ReviewOverview, ReviewTarget } from "../types";
import { openTally, canUndoSend, doneStatus, doneSub, doneTitle, holdSeconds, remainingUndoMs, ReviewDoneScreen, sentAgo } from "./ReviewDoneScreen";
import { clearOfflineSend, saveOfflineSend, type OfflineReviewSend } from "./offlineReviewSend";

vi.mock("../reviewApi", () => ({
  fetchReviewFlowSend: vi.fn(),
  fetchRevisions: vi.fn(),
  listReviews: vi.fn(),
  fetchTargets: vi.fn(),
  fetchAnnotations: vi.fn(),
  undoReviewFlowSend: vi.fn(),
  fetchReviewRecordStatus: vi.fn(),
  fetchReviewRecordFile: vi.fn(),
  fetchReviewRecordPdf: vi.fn(),
  fetchReviewRecordVerify: vi.fn(),
  reviewHref: (ref: string) => `/r/${ref}`,
}));
const status = vi.mocked(fetchReviewFlowSend);
const revisions = vi.mocked(fetchRevisions);
const directory = vi.mocked(listReviews);
const undoApi = vi.mocked(undoReviewFlowSend);
const targetsApi = vi.mocked(fetchTargets);
const annotationsApi = vi.mocked(fetchAnnotations);
let position = 0;
const DECISION = (state: string) => ({
  target_id: `t${position}`, unit_key: `t${position}`, path: "a.py", state, reasons: [], answer: { category: "decision" },
  priority: { tier: 1, attention: "review_first", outstanding: state === "unreviewed", position: position++, reasons: [{ code: "", detail: "Needs your judgment" }] },
}) as unknown as ReviewTarget;
const OVERVIEW = {
  title: "Review checkout", session: { id: "review", ref: "shop", status: "open" },
  revision: { id: "rev1", revision_number: 0 },
} as ReviewOverview;
const BASE: ReviewFlowSend = {
  state: "pending", operation_id: "fs-id", revision_id: "rev1", outcome: "changes_requested",
  has_feedback: true, send_after: new Date(Date.now() + 20_000).toISOString(), created_at: new Date().toISOString(),
};
function Location() { const location = useLocation(); return <output data-testid="location">{location.pathname}</output>; }
function show() {
  return render(<MemoryRouter initialEntries={["/r/shop/done"]}>
    <ReviewDoneScreen reviewRef="shop" apiReviewId="review" overview={OVERVIEW} targets={[] as ReviewTarget[]} destinationLabel="Claude Code"/>
    <Location />
  </MemoryRouter>);
}
beforeEach(() => {
  status.mockReset().mockResolvedValue(BASE);
  vi.mocked(fetchReviewRecordStatus).mockReset().mockResolvedValue({ records: [], signing: { available: false } } as unknown as Awaited<ReturnType<typeof fetchReviewRecordStatus>>);
  revisions.mockReset().mockResolvedValue({ latest_revision_number: 1 } as Awaited<ReturnType<typeof fetchRevisions>>);
  directory.mockReset().mockResolvedValue({ reviews: [
    { id: "next", ref: "next", title: "Payments correction", status: "open" },
  ] } as Awaited<ReturnType<typeof listReviews>>);
  undoApi.mockReset().mockResolvedValue({ ...BASE, state: "undone" });
  targetsApi.mockReset().mockResolvedValue({ targets: [] } as unknown as Awaited<ReturnType<typeof fetchTargets>>);
  annotationsApi.mockReset().mockResolvedValue({ feedback: { unpublished: 0, published: 0, in_flight: 0 } } as unknown as Awaited<ReturnType<typeof fetchAnnotations>>);
});
afterEach(() => { cleanup(); clearOfflineSend(`fs-${"a".repeat(32)}`); vi.restoreAllMocks(); });

describe("P2-5 Done and undo", () => {
  it("DONE-7: offline local intent is visible and cancelling before a network attempt is safe", async () => {
    status.mockRejectedValue(new TypeError("network offline"));
    const draft: OfflineReviewSend = {
      reviewRef: "shop", reviewId: "review", revisionId: "rev1",
      operationId: `fs-${"a".repeat(32)}`, outcome: "lgtm", summary: "", feedback: null,
      createdAt: new Date().toISOString(), attempted: false, state: "waiting", error: "",
    };
    expect(saveOfflineSend(draft)).toBe(true);
    show();
    expect(await screen.findByTestId("done-offline")).toBeVisible();
    expect(screen.getByTestId("done-title")).toHaveTextContent("Waiting to send");
    expect(screen.getByTestId("done-offline-receipt")).toHaveTextContent("They have not been sent");
    expect(screen.queryByRole("button", { name: "Undo send" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Cancel and edit" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/r/shop/wrap");
  });

  it("DONE-7: interrupted confirmation does not offer a false local cancellation", async () => {
    status.mockRejectedValue(new TypeError("network offline"));
    expect(saveOfflineSend({ reviewRef: "shop", reviewId: "review", revisionId: "rev1",
      operationId: `fs-${"a".repeat(32)}`, outcome: "lgtm", summary: "", feedback: null,
      createdAt: new Date().toISOString(), attempted: true, state: "waiting", error: "" })).toBe(true);
    show();
    expect(await screen.findByTestId("done-offline")).toBeVisible();
    expect(screen.getByTestId("done-title")).toHaveTextContent("Checking send status");
    expect(screen.queryByRole("button", { name: "Cancel and edit" })).toBeNull();
  });

  it("DONE-1: a real server pending timestamp owns the countdown, not a browser-made delay", () => {
    const now = Date.UTC(2026, 10, 6, 12, 0, 0);
    const send = { ...BASE, send_after: new Date(now + 10_000).toISOString() };
    expect(remainingUndoMs(send, now)).toBe(10_000);
    expect(canUndoSend(send, now + 9_999)).toBe(true);
    expect(canUndoSend(send, now + 10_000)).toBe(false);
    expect(canUndoSend({ ...send, state: "dispatching" }, now + 100)).toBe(false);
  });

  it("DONE-2: only pending is sending; an accepted send earns the SPEC headline", () => {
    expect(doneTitle(BASE, "Claude Code")).toBe("Sending to Claude Code…");
    expect(doneTitle({ ...BASE, state: "dispatching" }, "Claude Code")).toContain("Handing off");
    expect(doneTitle({ ...BASE, state: "sent" }, "Claude Code")).toBe("Sent. Claude Code is on it.");
    expect(doneTitle({ ...BASE, state: "sent", outcome: "lgtm" }, "Claude Code")).toBe("Approved. Claude Code has your notes.");
    expect(doneTitle({ ...BASE, state: "failed" }, "Claude Code")).toBe("Could not send.");
    expect(doneTitle({ ...BASE, state: "uncertain" }, "Claude Code")).toBe("Delivery needs checking.");
    expect(doneTitle({ ...BASE, state: "sent", has_feedback: false, outcome: "lgtm" }, "Claude Code")).toBe("Approved. Nothing was merged.");
    expect(doneTitle({ ...BASE, state: "sent", has_feedback: false, outcome: "comment" }, "Claude Code")).toBe("Recorded.");
  });

  it("DONE-2: the sub line names the human revision, the server's hold and the note count", () => {
    const pending = { ...BASE, note_count: 1, created_at: "2026-11-07T10:00:00.000Z", send_after: "2026-11-07T10:00:10.000Z" };
    expect(holdSeconds(pending)).toBe(10);
    expect(doneSub(pending, 1, "Claude Code")).toBe("Your verdict on Revision 1 (Request changes) will be recorded and 1 note sent in 10 seconds.");
    expect(doneSub({ ...pending, note_count: 3 }, 2, "Claude Code")).toContain("and 3 notes sent in 10 seconds");
    expect(doneSub({ ...pending, has_feedback: false, outcome: "lgtm" }, 1, "Claude Code"))
      .toBe("Your verdict on Revision 1 (Approve) will be recorded in 10 seconds. LemonCrow never commits or merges.");
    expect(doneSub({ ...pending, has_feedback: false, outcome: "comment" }, 1, "Claude Code")).toContain("No notes will be sent.");
    expect(doneSub({ ...pending, state: "sent" }, 1, "Claude Code")).toBe("Your verdict on Revision 1 was recorded and 1 note went to Claude Code. Nothing was committed or merged.");
  });

  it("DONE-1: failed and uncertain copy never shows the raw server error", () => {
    const raw = { ...BASE, state: "uncertain" as const, error: "delivery: Traceback boom" };
    expect(doneSub(raw, 1, "Claude Code")).not.toContain("Traceback");
    expect(doneSub({ ...raw, state: "failed" }, 1, "Claude Code")).not.toContain("Traceback");
  });

  it("DONE-1: sent time reads just now, then minutes", () => {
    const at = Date.UTC(2026, 10, 7, 10, 0, 0);
    const sent = { ...BASE, state: "sent" as const, updated_at: new Date(at).toISOString() };
    // After the hold, "undo" is not said again: Retract on the record is the way back (review-20.5).
    expect(sentAgo(sent, at + 5_000)).toBe("Sent just now");
    expect(sentAgo(sent, at + 130_000)).toBe("Sent 2 min ago");
  });

  it("DONE-1: cold reload fetches the real server state with undo and Z available", async () => {
    show();
    const bar = await screen.findByTestId("done-undo-bar");
    expect(bar).toBeVisible();
    // The heading names the send once; the bar holds only the undo window (review-20.1).
    expect(screen.getByTestId("done-title")).toHaveTextContent("Sending to Claude Code");
    expect(bar).toHaveTextContent("Still time to undo");
    expect(bar).not.toHaveTextContent("Claude Code");
    expect(screen.getAllByText(/Sending to Claude Code/)).toHaveLength(1);
    // The undo window is not named a third time in the progress card, and its seconds are not repeated there.
    expect(screen.getByRole("region", { name: "Author progress" })).toHaveTextContent("Claude Code · not started yet");
    expect(screen.getByRole("region", { name: "Author progress" })).toHaveTextContent("Starts when the undo window ends");
    expect(screen.getByRole("region", { name: "Author progress" })).not.toHaveTextContent(/waiting for the undo window|\d+ s so you can undo/);
    expect(screen.getByRole("button", { name: "Undo send" })).toBeEnabled();
    await userEvent.keyboard("z");
    await waitFor(() => expect(undoApi).toHaveBeenCalledWith("review", "fs-id"));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/r/shop/wrap"));
  });

  it("DONE-3/4: Next up is navigable while Undo is available, with a reason that was checked", async () => {
    directory.mockResolvedValue({ reviews: [
      { id: "slow", ref: "slow", title: "Large refactor", status: "open" },
      { id: "next", ref: "next", title: "Payments correction", status: "open" },
    ] } as Awaited<ReturnType<typeof listReviews>>);
    targetsApi.mockImplementation(async (id: string) => ({
      targets: id === "slow" ? [DECISION("unreviewed"), DECISION("unreviewed")] : [DECISION("unreviewed")],
    }) as unknown as Awaited<ReturnType<typeof fetchTargets>>);
    show();
    expect(await screen.findByText("Payments correction")).toBeVisible();
    expect(screen.getByTestId("done-next-up")).toHaveTextContent("Next up · your turn · quickest to finish (1 decision)");
    expect(screen.getByRole("button", { name: "Undo send" })).toBeVisible();
    await userEvent.click(within(screen.getByTestId("done-next-up")).getByRole("button", { name: /Start next review/ }));
    expect(screen.getByTestId("location")).toHaveTextContent("/r/next");
  });

  it("Next up shows a placeholder, not All caught up, until the review list is read", async () => {
    let answer: (value: Awaited<ReturnType<typeof listReviews>>) => void = () => {};
    directory.mockReturnValue(new Promise((resolve) => { answer = resolve; }));
    show();
    expect(await screen.findByTestId("done-next-up-loading")).toBeInTheDocument();
    expect(screen.getByTestId("done-next-up")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByTestId("done-next-up")).not.toHaveTextContent("All caught up");
    expect(screen.getByTestId("done-next-up")).not.toHaveTextContent("No other open reviews");
    answer({ reviews: [] } as unknown as Awaited<ReturnType<typeof listReviews>>);
    expect(await screen.findByText("All caught up here")).toBeVisible();
    expect(screen.queryByTestId("done-next-up-loading")).toBeNull();
    expect(screen.getByTestId("done-next-up")).not.toHaveAttribute("aria-busy");
  });

  it("the heading Done focuses draws no focus box", async () => {
    show();
    expect((await screen.findByTestId("done-title")).className).toContain("focus:outline-none");
  });

  it("DONE-4: a review the agent still holds notes for is never called your turn", async () => {
    targetsApi.mockResolvedValue({ targets: [DECISION("unreviewed")] } as unknown as Awaited<ReturnType<typeof fetchTargets>>);
    annotationsApi.mockResolvedValue({ feedback: { unpublished: 0, published: 1, in_flight: 0 } } as unknown as Awaited<ReturnType<typeof fetchAnnotations>>);
    show();
    expect(await screen.findByText("Payments correction")).toBeVisible();
    expect(screen.getByTestId("done-next-up")).not.toHaveTextContent("your turn");
  });

  it("DONE-3: Revision 2 ready makes Review Revision 2 the only primary button", async () => {
    status.mockResolvedValue({ ...BASE, state: "sent" });
    revisions.mockResolvedValue({ latest_revision_number: 1 } as Awaited<ReturnType<typeof fetchRevisions>>);
    show();
    const lemon = await screen.findByRole("button", { name: /Review Revision 2/ });
    expect(lemon.className).toContain("bg-brand");
    const next = within(screen.getByTestId("done-next-up")).getByRole("button", { name: /Start next review/ });
    expect(next.className).not.toContain("bg-brand");
    expect(document.querySelectorAll("button.bg-brand")).toHaveLength(1);
  });

  it("DONE-1: expired server state never offers Undo, even with a stale browser timestamp", async () => {
    status.mockResolvedValue({ ...BASE, state: "sent", send_after: new Date(Date.now() + 10_000).toISOString() });
    show();
    expect(await screen.findByTestId("done-dispatched-status")).toHaveTextContent(/^Sent just now$/);
    expect(screen.queryByRole("button", { name: "Undo send" })).toBeNull();
    expect(screen.getByTestId("done-title")).toHaveTextContent("Sent. Claude Code is on it.");
  });

  it("DONE-6: zero-value claims and unverified progress are omitted", async () => {
    status.mockResolvedValue({ ...BASE, state: "sent", has_feedback: false, outcome: "lgtm" });
    directory.mockResolvedValue({ reviews: [], repo_root: "", status_filter: "open" });
    show();
    expect(await screen.findByTestId("done-dispatched-status")).toBeVisible();
    expect(screen.queryByText(/0 parts judged|0 notes sent|Fixed 2 of 2|Tests passed/)).toBeNull();
    expect(screen.queryByRole("region", { name: "Author progress" })).toBeNull();
    expect(screen.getByTestId("done-title")).toHaveTextContent("Approved. Nothing was merged.");
  });

  it("DONE-5: sent from wire revision 0 (Revision 1), the captured wire revision 1 opens human Revision 2", async () => {
    status.mockResolvedValue({ ...BASE, state: "sent" });
    revisions.mockResolvedValue({ latest_revision_number: 1 } as Awaited<ReturnType<typeof fetchRevisions>>);
    show();
    await waitFor(() => expect(revisions).toHaveBeenCalled());
    expect(screen.getByText("Revision 1", { selector: "span" })).toBeVisible();
    const review = await screen.findByRole("button", { name: /Review Revision 2/ });
    await userEvent.click(review);
    expect(screen.getByTestId("location")).toHaveTextContent("/r/shop/rev/2");
  });

  it("DONE-1: uncertain delivery is not auto retried or described as sent", async () => {
    status.mockResolvedValue({ ...BASE, state: "uncertain", error: "agent transport timed out" });
    show();
    expect(screen.queryByText(/transport timed out/)).toBeNull();
    expect(await screen.findByTestId("done-title")).toHaveTextContent("Delivery needs checking");
    expect(screen.getByRole("alert")).toHaveTextContent("won't send this again on its own");
    expect(screen.queryByRole("button", { name: "Undo send" })).toBeNull();
    expect(screen.queryByText("Sent just now")).toBeNull();
  });

  it("3.3: the bar's centre text names the send, one SPEC string per state", () => {
    const rev = "Revision 1";
    expect(doneStatus({ ...BASE, state: "pending" }, rev, null)).toEqual({ text: "Sending · Revision 1", tone: "neutral" });
    expect(doneStatus({ ...BASE, state: "sent" }, rev, null)?.text).toBe("Sent · Revision 1");
    expect(doneStatus({ ...BASE, state: "sent", has_feedback: false, outcome: "lgtm" }, rev, null)).toEqual({ text: "Approved · Revision 1", tone: "success" });
    expect(doneStatus({ ...BASE, state: "sent", has_feedback: false, outcome: "comment" }, rev, null)?.text).toBe("Recorded · Revision 1");
    expect(doneStatus(null, rev, "waiting")?.text).toBe("Waiting to send · Revision 1");
    expect(doneStatus(null, rev, "checking")?.text).toBe("Checking send status · Revision 1");
    expect(doneStatus(null, rev, "needs_review")?.text).toBe("Send not confirmed · Revision 1");
    expect(doneStatus({ ...BASE, state: "undone" }, rev, null)?.text).toBe("Cancelled · Revision 1");
    expect(doneStatus({ ...BASE, state: "failed" }, rev, null)?.text).toBe("Not sent · Revision 1");
    expect(doneStatus({ ...BASE, state: "uncertain" }, rev, null)?.text).toBe("Not confirmed · Revision 1");
    expect(doneStatus(null, rev, null)).toBeUndefined();
  });

  it("3.3: Done renders the shared top bar with Reviews as its back control", async () => {
    status.mockResolvedValue({ ...BASE, state: "sent", send_after: new Date(Date.now() - 5_000).toISOString() });
    show();
    expect(await screen.findByTestId("flow-top-bar")).toBeVisible();
    expect(screen.getByRole("button", { name: "Reviews, go back" })).toBeVisible();
    await waitFor(() => expect(screen.getByTestId("flow-top-bar")).toHaveTextContent("Sent · Revision 1"));
  });
});

describe("Next up tally", () => {
  it("counts only the parts still to answer, as Home does", () => {
    const answered = DECISION("reviewed");
    expect(openTally([answered, DECISION("unreviewed"), DECISION("needs_changes")])).toEqual({ decisions: 1, quickChecks: 0 });
  });
});
