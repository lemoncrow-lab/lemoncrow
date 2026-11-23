import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchReviewFlowSend, fetchReviewOutcome } from "../reviewApi";
import { fetchSecondReviewer, type SecondReviewerStatus } from "../second/secondApi";
import type { ReviewOverview, ReviewTarget } from "../types";
import { hostedReceipt, hostedTold, ReviewDoneScreen } from "./ReviewDoneScreen";

vi.mock("../reviewApi", () => ({
  fetchReviewFlowSend: vi.fn(),
  fetchReviewOutcome: vi.fn(),
  fetchRevisions: vi.fn().mockResolvedValue({ latest_revision_number: 0 }),
  listReviews: vi.fn().mockResolvedValue({ reviews: [] }),
  fetchTargets: vi.fn().mockResolvedValue({ targets: [] }),
  fetchAnnotations: vi.fn().mockResolvedValue({ feedback: {} }),
  undoReviewFlowSend: vi.fn(),
  fetchReviewRecordStatus: vi.fn().mockResolvedValue({ records: [], signing: { available: false } }),
  fetchReviewRecordFile: vi.fn(),
  fetchReviewRecordPdf: vi.fn(),
  fetchReviewRecordVerify: vi.fn(),
  reviewHref: (ref: string) => `/r/${ref}`,
}));
vi.mock("../second/secondApi", async (original) => ({ ...(await original<typeof import("../second/secondApi")>()), fetchSecondReviewer: vi.fn() }));

const OVERVIEW = { title: "Review checkout", session: { id: "review", ref: "shop", status: "open" }, revision: { id: "rev1", revision_number: 0 } } as ReviewOverview;
const NOT_REQUIRED = { state: "not_required", role: "first", required: false, unavailable: "", area: "", decision_count: 0, revision_id: "rev1", reviewer: null, first_reviewer: null, first: null, second: null, signatures: { have: 0, needed: 1 }, verdict: "waiting" } as SecondReviewerStatus;

function show() {
  return render(
    <MemoryRouter initialEntries={["/r/shop/done"]}>
      <ReviewDoneScreen reviewRef="shop" apiReviewId="review" overview={OVERVIEW} targets={[] as ReviewTarget[]} destinationLabel="Claude Code" />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(fetchSecondReviewer).mockReset();
  vi.mocked(fetchReviewFlowSend).mockReset().mockRejectedValue(new Error("delayed send is only available for a local review"));
  vi.mocked(fetchReviewOutcome).mockReset().mockResolvedValue({ review_id: "review", revision_id: "rev1", reviewer_id: "me", current: { outcome: "changes_requested" }, history: [] } as never);
});
afterEach(() => cleanup());

describe("hosted Done (SPEC 7.1 P9-4 D): recorded at once, nothing held, nothing to undo", () => {
  it("says what was recorded and who is told, and never asks for a send", async () => {
    vi.mocked(fetchSecondReviewer).mockResolvedValue(NOT_REQUIRED);
    show();
    expect(await screen.findByRole("heading", { name: "Recorded." })).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("done-receipt").textContent).toBe("Your verdict on Revision 1 (Request changes) is recorded. Nothing was merged."));
    expect(screen.getByTestId("done-told").textContent).toBe("Your request for changes is on the review for the author to read.");
    // no hold, no Undo, and the local send route is never asked
    expect(screen.queryByTestId("done-undo-bar")).toBeNull();
    expect(screen.queryByRole("button", { name: /Undo/ })).toBeNull();
    expect(screen.queryByText(/server-side pending/)).toBeNull();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(fetchReviewFlowSend).not.toHaveBeenCalled();
  });

  it("a local install still reads the held send, once", async () => {
    vi.mocked(fetchSecondReviewer).mockResolvedValue(null);
    vi.mocked(fetchReviewFlowSend).mockResolvedValue({ state: "none" } as never);
    show();
    await waitFor(() => expect(fetchReviewFlowSend).toHaveBeenCalled());
    expect(screen.queryByTestId("done-told")).toBeNull();
  });

  it("each verdict says what the hosted server does with it", () => {
    expect(hostedReceipt("lgtm", 1)).toBe("Your verdict on Revision 1 (Approve) is recorded. Nothing was merged.");
    expect(hostedTold("lgtm")).toBe("An approval is sent to nobody. The author sees it on the review.");
    expect(hostedTold("comment")).toBe("Your comment is on the review for the author to read.");
    expect(hostedTold(null)).toBe("");
    // no verdict read back: no receipt that claims one
    expect(hostedReceipt(null, 1)).toBe("");
  });

  it("shows no receipt when the recorded verdict cannot be read back", async () => {
    vi.mocked(fetchSecondReviewer).mockResolvedValue(NOT_REQUIRED);
    vi.mocked(fetchReviewOutcome).mockReset().mockRejectedValue(new Error("offline"));
    show();
    expect(await screen.findByRole("heading", { name: "Recorded." })).toBeTruthy();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByTestId("done-receipt")).toBeNull();
    expect(screen.queryByTestId("done-told")).toBeNull();
  });
});
