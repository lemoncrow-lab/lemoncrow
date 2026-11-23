import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchReviewRecordStatus } from "../reviewApi";
import type { ReviewRecordSummary } from "../types";
import { SignedRecordLine } from "./SignedRecordLine";

vi.mock("../reviewApi", () => ({
  fetchReviewRecordStatus: vi.fn(),
  fetchReviewRecordVerify: vi.fn(),
  fetchReviewRecordFile: vi.fn(),
  fetchReviewRecordPdf: vi.fn(),
}));
const statusApi = vi.mocked(fetchReviewRecordStatus);
type Status = Awaited<ReturnType<typeof fetchReviewRecordStatus>>;

const RECORD = {
  record_id: "rec_a", review_id: "r1", revision_id: "rev1", payload_digest: "sha256:1", signing_state: "signed", signing_mode: "user_key",
  key_id: "user-1", issued_at: "2026-11-07T14:02:00+00:00", decision_kind: "human_outcome", label: "Signed with personal key user-1",
  snapshot_number: 0, head_sha: "4e1b9c2deadbeef", parts_reviewed_one_by_one: 3,
} as ReviewRecordSummary;
const due = { records: [], signing: { available: true }, signing_due: true } as unknown as Status;
const landed = { records: [RECORD], signing: { available: true }, signing_due: false } as unknown as Status;
const notDue = { records: [], signing: { available: true }, signing_due: false } as unknown as Status;

const line = () => screen.getByTestId("record-line").textContent;
async function advance(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
async function draw() {
  render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
  await advance(0);
}

beforeEach(() => { vi.useFakeTimers(); statusApi.mockReset(); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("SignedRecordLine while a record is slow to arrive (OWNER-4)", () => {
  it("a verdict still being stored (the server says signing is due) is Signing, never a failure, and a record that then lands replaces it", async () => {
    // The server reports due from before the verdict is stored until its signer has started (P6-1 O).
    statusApi.mockResolvedValueOnce(due).mockResolvedValue(landed);
    await draw();
    expect(line()).toBe("Signing the record…");
    expect(screen.queryByText(/could not be signed/)).toBeNull();
    await advance(600);
    expect(line()).toBe("Signed record · Revision 1 · 3 decisions answered · Details");
  });

  it("a send after a retraction is Signing while the server signs it, not the retracted record, and the new record replaces it", async () => {
    const retracted = { ...RECORD, record_id: "rec_old", issued_at: "2026-11-07T13:00:00+00:00", retracted: { by: "me", at: "2026-11-07T13:30:00+00:00" } } as ReviewRecordSummary;
    statusApi.mockResolvedValueOnce({ ...due, records: [retracted] } as Status).mockResolvedValue({ ...landed, records: [retracted, RECORD] } as Status);
    await draw();
    expect(line()).toBe("Signing the record…");
    await advance(600);
    expect(line()).toBe("Signed record · Revision 1 · 3 decisions answered · Details");
  });

  it("a stored verdict the server no longer says is signing, with no record, is a failure at once", async () => {
    statusApi.mockResolvedValue(notDue);
    await draw();
    expect(line()).toBe("Not signed · the record could not be signed");
    await advance(60_000);
    expect(statusApi).toHaveBeenCalledTimes(1);
  });

  it("keeps asking past 10 s while signing is due", async () => {
    statusApi.mockResolvedValue(due);
    await draw();
    await advance(10_000);
    const before = statusApi.mock.calls.length;
    await advance(10_000);
    expect(statusApi.mock.calls.length).toBeGreaterThan(before);
    expect(line()).toBe("Signing the record…");
  });

  it("shows a record that lands after 30 s", async () => {
    let at = 0;
    statusApi.mockImplementation(async () => (Date.now() - at >= 30_000 ? landed : due));
    at = Date.now();
    await draw();
    await advance(35_000);
    expect(line()).toBe("Signed record · Revision 1 · 3 decisions answered · Details");
  });

  it("spaces the asks with growing gaps", async () => {
    statusApi.mockResolvedValue(due);
    await draw();
    expect(statusApi).toHaveBeenCalledTimes(1);
    await advance(500); expect(statusApi).toHaveBeenCalledTimes(2);
    await advance(999); expect(statusApi).toHaveBeenCalledTimes(2);
    await advance(1); expect(statusApi).toHaveBeenCalledTimes(3);
    await advance(2_000); expect(statusApi).toHaveBeenCalledTimes(4);
  });

  it("at the cap says Still signing with a check again button, and stops asking", async () => {
    statusApi.mockResolvedValue(due);
    await draw();
    await advance(120_000);
    expect(line()).toBe("Still signing · check again");
    expect(screen.getByRole("button", { name: "check again" })).toBeTruthy();
    const calls = statusApi.mock.calls.length;
    await advance(60_000);
    expect(statusApi.mock.calls.length).toBe(calls);
    expect(screen.queryByText(/could not be signed/)).toBeNull();
  });

  it("check again asks once at once and restarts the gaps", async () => {
    statusApi.mockResolvedValue(due);
    await draw();
    await advance(120_000);
    const calls = statusApi.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "check again" }));
    await advance(0);
    expect(statusApi.mock.calls.length).toBe(calls + 1);
    expect(line()).toBe("Signing the record…");
    await advance(500);
    expect(statusApi.mock.calls.length).toBe(calls + 2);
    await advance(999);
    expect(statusApi.mock.calls.length).toBe(calls + 2);
    await advance(1);
    expect(statusApi.mock.calls.length).toBe(calls + 3);
  });

  it("shows could not be signed only once the server stops reporting signing as due with no record", async () => {
    statusApi.mockResolvedValueOnce(due).mockResolvedValueOnce(due).mockResolvedValue(notDue);
    await draw();
    expect(line()).toBe("Signing the record…");
    expect(screen.queryByText(/could not be signed/)).toBeNull();
    await advance(1_500);
    expect(line()).toBe("Not signed · the record could not be signed");
    expect(screen.queryByRole("button", { name: "check again" })).toBeNull();
  });

  it("one failed ask while signing is due does not end the wait, and the signed line lands", async () => {
    statusApi.mockResolvedValueOnce(due).mockResolvedValueOnce(due).mockRejectedValueOnce(new Error("net")).mockResolvedValueOnce(due).mockResolvedValue(landed);
    await draw();
    await advance(60_000);
    expect(line()).toBe("Signed record · Revision 1 · 3 decisions answered · Details");
  });

  it("asks that keep failing while signing is due end in Still signing · check again", async () => {
    statusApi.mockResolvedValueOnce(due).mockRejectedValue(new Error("net"));
    await draw();
    await advance(120_000);
    expect(line()).toBe("Still signing · check again");
  });

  it("a failed check again leaves the line asking, and it recovers", async () => {
    statusApi.mockResolvedValue(due);
    await draw();
    await advance(120_000);
    statusApi.mockReset().mockRejectedValueOnce(new Error("net")).mockResolvedValue(landed);
    fireEvent.click(screen.getByRole("button", { name: "check again" }));
    await advance(10_000);
    expect(line()).toBe("Signed record · Revision 1 · 3 decisions answered · Details");
  });

  it("a first load that fails offers check again, and asking again loads the line", async () => {
    statusApi.mockRejectedValueOnce(new Error("net")).mockResolvedValue(landed);
    await draw();
    expect(line()).toBe("The signed record could not be loaded · check again");
    fireEvent.click(screen.getByRole("button", { name: "check again" }));
    await advance(0);
    expect(line()).toBe("Signed record · Revision 1 · 3 decisions answered · Details");
  });

  it("leaves no pending timer when it unmounts mid-wait", async () => {
    statusApi.mockResolvedValue(due);
    const view = render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    await advance(0);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("a revision change mid-wait leaves one chain of asks", async () => {
    statusApi.mockResolvedValue(due);
    const view = render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    await advance(700);
    view.rerender(<SignedRecordLine reviewId="r1" revisionId="rev2" revisionNumber={1} />);
    await advance(0);
    statusApi.mockClear();
    await advance(500);
    expect(statusApi).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(1);
  });

  it("check again keeps focus on the record line", async () => {
    statusApi.mockResolvedValue(due);
    await draw();
    await advance(120_000);
    const button = screen.getByRole("button", { name: "check again" });
    button.focus();
    fireEvent.click(button);
    await advance(0);
    expect(document.activeElement).toBe(screen.getByTestId("record-line"));
  });

  it("names the newer revision when the decision moved on and nothing was signed", async () => {
    statusApi.mockResolvedValue({ ...notDue, snapshot_id: "rev2", snapshot_number: 1 } as unknown as Status);
    await draw();
    expect(line()).toBe("Not signed · the review has moved on to Revision 2");
  });
});
