import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchReviewRecordStatus, fetchReviewRecordVerify, retractReviewRecord } from "../reviewApi";
import { currentFlowToast, dismissFlowToast } from "../flowToast";
import type { ReviewRecordSummary } from "../types";
import { RetractedRecordCard } from "./RetractedRecordCard";
import { SignedRecordLine } from "./SignedRecordLine";

vi.mock("../reviewApi", () => ({
  fetchReviewRecordStatus: vi.fn(),
  fetchReviewRecordVerify: vi.fn(),
  fetchReviewRecordFile: vi.fn(),
  fetchReviewRecordPdf: vi.fn(),
  retractReviewRecord: vi.fn(),
}));
const statusApi = vi.mocked(fetchReviewRecordStatus);
const verifyApi = vi.mocked(fetchReviewRecordVerify);
const retractApi = vi.mocked(retractReviewRecord);
type Status = Awaited<ReturnType<typeof fetchReviewRecordStatus>>;

const soon = () => new Date(Date.now() + 23 * 3_600_000).toISOString();
const base = (over: Partial<ReviewRecordSummary> = {}): ReviewRecordSummary => ({
  record_id: "rec_a", review_id: "r1", revision_id: "rev1", payload_digest: "sha256:1", signing_state: "signed", signing_mode: "user_key",
  key_id: "user-1", issued_at: new Date(Date.now() - 3_600_000).toISOString(), decision_kind: "human_outcome", label: "Signed with personal key user-1",
  snapshot_number: 0, head_sha: "4e1b9c2deadbeef", parts_reviewed_one_by_one: 3, retract_until: soon(), retractable: true, retracted: null, ...over,
});
const status = (...records: ReviewRecordSummary[]) => ({ review_id: "r1", snapshot_id: "rev1", snapshot_number: 0, decision: null, signing: { available: true }, signing_due: false, records }) as unknown as Status;

beforeEach(() => { statusApi.mockReset(); verifyApi.mockReset(); retractApi.mockReset(); });
afterEach(() => { cleanup(); dismissFlowToast(); });

describe("the Retract button on the record", () => {
  it("is offered with SPEC's words while the server says the window is open", async () => {
    statusApi.mockResolvedValue(status(base()));
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    const button = await screen.findByTestId("record-retract");
    expect(button.textContent).toMatch(/^Retract this verdict · until (today|tomorrow) \d\d:\d\d UTC$/);
    // Outlined like the other actions, so it does not read as a line of body text.
    expect(button.className).toContain("border-input");
  });

  it("is not offered when the server says it cannot be used", async () => {
    statusApi.mockResolvedValue(status(base({ retractable: false })));
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    await screen.findByTestId("record-line");
    expect(screen.queryByTestId("record-retract")).toBeNull();
  });

  it("is not offered once the end of the window has passed on this clock, even if the page is stale", async () => {
    statusApi.mockResolvedValue(status(base({ retract_until: new Date(Date.now() - 1000).toISOString() })));
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    await screen.findByTestId("record-line");
    expect(screen.queryByTestId("record-retract")).toBeNull();
  });

  it("asks the server, then shows the retracted record and goes back to Wrap up", async () => {
    const retracted = base({ retractable: false, retracted: { by: "srivera", at: new Date().toISOString() } });
    statusApi.mockResolvedValueOnce(status(base())).mockResolvedValue(status(retracted));
    retractApi.mockResolvedValue({ record: retracted, retraction: { by: "srivera", at: retracted.retracted!.at, created: true } });
    const onOpenWrap = vi.fn();
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} onOpenWrap={onOpenWrap} />);
    await userEvent.click(await screen.findByTestId("record-retract"));
    expect(retractApi).toHaveBeenCalledWith("r1", "rec_a");
    await waitFor(() => expect(onOpenWrap).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("record-line").textContent).toBe("Retracted record · Revision 1 · 3 decisions answered · Details");
    expect(screen.queryByTestId("record-retract")).toBeNull();
  });

  it("says what the server refused and changes nothing when the window ended", async () => {
    statusApi.mockResolvedValue(status(base()));
    retractApi.mockRejectedValue(Object.assign(new Error("closed"), { status: 409, detail: { code: "window_closed" } }));
    const onOpenWrap = vi.fn();
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} onOpenWrap={onOpenWrap} />);
    await userEvent.click(await screen.findByTestId("record-retract"));
    const text = "Can't retract · the 24 hours from signing have passed. Nothing was changed.";
    expect((await screen.findByTestId("record-retract-error")).textContent).toBe(text);
    expect(onOpenWrap).not.toHaveBeenCalled();
  });

  it("announces a refusal through the one toast, and its own visible text is not a live region", async () => {
    statusApi.mockResolvedValue(status(base()));
    retractApi.mockRejectedValue(Object.assign(new Error("closed"), { status: 409, detail: { code: "window_closed" } }));
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    await userEvent.click(await screen.findByTestId("record-retract"));
    const shown = await screen.findByTestId("record-retract-error");
    expect(currentFlowToast()).toEqual({ text: shown.textContent, tone: "alert", serial: expect.any(Number) });
    expect(shown.getAttribute("role")).toBeNull();
    expect(shown.closest("[role=alert], [role=status]")).toBeNull();
  });

  it("puts focus on the record line when a refusal takes the button away", async () => {
    // The server now says the window ended: the focused button unmounts, so focus must not fall to the page.
    statusApi.mockResolvedValueOnce(status(base())).mockResolvedValue(status(base({ retractable: false })));
    retractApi.mockRejectedValue(Object.assign(new Error("closed"), { status: 409, detail: { code: "window_closed" } }));
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    const button = await screen.findByTestId("record-retract");
    button.focus();
    await userEvent.click(button);
    await waitFor(() => expect(screen.queryByTestId("record-retract")).toBeNull());
    expect(document.activeElement).toBe(screen.getByTestId("record-line"));
  });

  it("keeps focus on the button while it works and when the server still allows Retract", async () => {
    statusApi.mockResolvedValue(status(base()));
    retractApi.mockRejectedValue(new Error("network"));
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    const button = await screen.findByTestId("record-retract");
    await userEvent.click(button);
    await screen.findByTestId("record-retract-error");
    expect(document.activeElement).toBe(screen.getByTestId("record-retract"));
    expect(screen.getByTestId("record-retract").hasAttribute("disabled")).toBe(false);
  });

  it("offers Return to Wrap up on a record that was already retracted", async () => {
    statusApi.mockResolvedValue(status(base({ retractable: false, retracted: { by: "srivera", at: new Date().toISOString() } })));
    const onOpenWrap = vi.fn();
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} onOpenWrap={onOpenWrap} />);
    await userEvent.click(await screen.findByTestId("record-return-wrap"));
    expect(onOpenWrap).toHaveBeenCalledTimes(1);
  });
});

describe("Wrap up's retracted record", () => {
  const retracted = (over: Partial<ReviewRecordSummary> = {}) => base({ retractable: false, retracted: { by: "srivera", at: "2026-11-04T15:10:33+00:00" }, ...over });

  it("says SPEC's sentence with Verify and Download JSON", async () => {
    statusApi.mockResolvedValue(status(retracted()));
    render(<RetractedRecordCard reviewId="r1" revisionId="rev1" revisionNumber={0} repo="shop" />);
    expect((await screen.findByTestId("wrap-retracted-text")).textContent).toBe("You retracted the earlier record · Revision 1 · shop @ 4e1b9c2 · 15:10 UTC. Sending again signs a new one.");
    expect(screen.getByRole("button", { name: "Verify the retracted record" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Download JSON/ })).toBeTruthy();
  });

  it("announces a download through the one toast and keeps its visible line static", async () => {
    statusApi.mockResolvedValue(status(retracted()));
    const { fetchReviewRecordFile } = await import("../reviewApi");
    vi.mocked(fetchReviewRecordFile).mockRejectedValue(new Error("offline"));
    render(<RetractedRecordCard reviewId="r1" revisionId="rev1" revisionNumber={0} repo="shop" />);
    await userEvent.click(await screen.findByRole("button", { name: /Download JSON/ }));
    const line = await screen.findByTestId("wrap-retracted-notice");
    expect(currentFlowToast()).toMatchObject({ text: "The record could not be downloaded.", tone: "alert" });
    expect(line.getAttribute("role")).toBeNull();
  });

  it("shows nothing when the record stands", async () => {
    statusApi.mockResolvedValue(status(base()));
    render(<RetractedRecordCard reviewId="r1" revisionId="rev1" revisionNumber={0} repo="shop" />);
    await waitFor(() => expect(statusApi).toHaveBeenCalled());
    expect(screen.queryByTestId("wrap-retracted")).toBeNull();
  });

  it("shows nothing once a newer send has been signed", async () => {
    statusApi.mockResolvedValue(status(retracted(), base({ record_id: "rec_b", issued_at: new Date().toISOString() })));
    render(<RetractedRecordCard reviewId="r1" revisionId="rev1" revisionNumber={0} repo="shop" />);
    await waitFor(() => expect(statusApi).toHaveBeenCalled());
    expect(screen.queryByTestId("wrap-retracted")).toBeNull();
  });

  it("claims nothing when the status cannot be read", async () => {
    statusApi.mockRejectedValue(new Error("offline"));
    render(<RetractedRecordCard reviewId="r1" revisionId="rev1" revisionNumber={0} repo="shop" />);
    await waitFor(() => expect(statusApi).toHaveBeenCalled());
    expect(screen.queryByTestId("wrap-retracted")).toBeNull();
  });
});

describe("the toast outlives the component that raised it", () => {
  it("a refusal's toast is still there after the record line unmounts (no hook clears it)", async () => {
    statusApi.mockResolvedValue(status(base()));
    retractApi.mockRejectedValue(Object.assign(new Error("closed"), { status: 409, detail: { code: "window_closed" } }));
    const view = render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    await userEvent.click(await screen.findByTestId("record-retract"));
    await screen.findByTestId("record-retract-error");
    view.unmount();
    expect(currentFlowToast()?.text).toBe("Can't retract · the 24 hours from signing have passed. Nothing was changed.");
  });
});
