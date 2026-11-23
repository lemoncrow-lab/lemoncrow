import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchReviewRecordFile, fetchReviewRecordPdf, fetchReviewRecordStatus, fetchReviewRecordVerify } from "../reviewApi";
import type { ReviewRecordOutcomeName, ReviewRecordSummary, ReviewRecordVerifyResult } from "../types";
import { currentFlowToast, dismissFlowToast } from "../flowToast";
import { SignedRecordLine } from "./SignedRecordLine";
import { VerifyRecordDialog } from "./VerifyRecordDialog";

vi.mock("../reviewApi", () => ({
  fetchReviewRecordStatus: vi.fn(),
  fetchReviewRecordVerify: vi.fn(),
  fetchReviewRecordFile: vi.fn(),
  fetchReviewRecordPdf: vi.fn(),
}));
const statusApi = vi.mocked(fetchReviewRecordStatus);
const verifyApi = vi.mocked(fetchReviewRecordVerify);
const fileApi = vi.mocked(fetchReviewRecordFile);
const pdfApi = vi.mocked(fetchReviewRecordPdf);

const RECORD: ReviewRecordSummary = {
  record_id: "rec_a", review_id: "r1", revision_id: "rev1", payload_digest: "sha256:1", signing_state: "signed", signing_mode: "user_key",
  key_id: "user-1", issued_at: "2026-11-07T14:02:00+00:00", decision_kind: "human_outcome", label: "Signed with personal key user-1",
  snapshot_number: 0, head_sha: "4e1b9c2deadbeef", parts_reviewed_one_by_one: 3,
};
const HEADLINES: Record<ReviewRecordOutcomeName, string> = {
  valid: "Valid · signed by you at 14:02 UTC · matches 4e1b9c2",
  code_changed: "Not valid · the code changed after signing (now 8c3d0a1)",
  key_not_allowed: "Can't verify · key user-1 is not in your trusted signing keys",
  retracted: "Retracted · by @srivera at 15:10 UTC",
  not_valid: "Not valid · the record was altered after signing",
  unsigned: "Can't verify · this record is not signed",
};
function verified(outcome: ReviewRecordOutcomeName): ReviewRecordVerifyResult {
  return {
    record: RECORD, repository: "shop",
    details: [{ label: "Version", value: "Revision 1 · 4e1b9c2" }, { label: "Your verdict", value: "Approve · by you" }],
    outcome: { outcome, exit_code: 0, code: "matches", key_problem: "", key_id: "user-1", signed_by: "local", signed_at: RECORD.issued_at,
      signed_head_sha: "4e1b9c2deadbeef", current_head_sha: "", retraction: null, reasons: [], headline: HEADLINES[outcome] },
    verification: {} as ReviewRecordVerifyResult["verification"], current: { revision_number: 0, head_sha: "", tree_fingerprint: "" },
    store: {}, matches_store: true,
  };
}
beforeEach(() => {
  verifyApi.mockReset().mockResolvedValue(verified("valid"));
  statusApi.mockReset().mockResolvedValue({ review_id: "r1", snapshot_id: "rev1", snapshot_number: 0, decision: null, signing: { available: true } , records: [RECORD] } as unknown as Awaited<ReturnType<typeof fetchReviewRecordStatus>>);
  fileApi.mockReset().mockResolvedValue(new Blob(["{}"]));
  pdfApi.mockReset().mockResolvedValue(new Blob(["%PDF-"]));
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  URL.createObjectURL = vi.fn(() => "blob:record");
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => { cleanup(); dismissFlowToast(); vi.restoreAllMocks(); });

function openDialog(handlers: { onDownloadJson?: () => void; onDownloadPdf?: () => void } = {}) {
  return render(<VerifyRecordDialog open onOpenChange={() => {}} reviewId="r1" record={RECORD}
    onDownloadJson={handlers.onDownloadJson ?? (() => {})} onDownloadPdf={handlers.onDownloadPdf ?? (() => {})} />);
}

describe("REC-3 / REC-4: the Verify dialog", () => {
  it.each(Object.keys(HEADLINES) as ReviewRecordOutcomeName[])("%s shows its one plain line in the status row", async (outcome) => {
    verifyApi.mockResolvedValue(verified(outcome));
    openDialog();
    const dialog = await screen.findByRole("dialog", { name: "Verify signed record" });
    await waitFor(() => expect(within(dialog).getByTestId("verify-result").getAttribute("data-outcome")).toBe(outcome));
    const row = within(dialog).getByTestId("verify-result");
    expect(row.getAttribute("role")).toBe("status");
    expect(row.textContent).toBe(HEADLINES[outcome]);
  });

  it("says what it checked against, with SPEC's footer and legal sentence", async () => {
    openDialog();
    const dialog = await screen.findByRole("dialog", { name: "Verify signed record" });
    expect((await within(dialog).findByTestId("verify-footer")).textContent).toBe(
      "Checked just now against the latest captured revision, not against this page. Revision 1 · shop @ 4e1b9c2");
    expect(within(dialog).getByText("This is a technical signature that proves who reviewed what. It is not a legal e-signature.")).toBeTruthy();
  });

  it("copies lc review verify with this record's own address", async () => {
    const user = userEvent.setup();
    openDialog();
    const dialog = await screen.findByRole("dialog", { name: "Verify signed record" });
    const command = `lc review verify ${window.location.origin}/api/reviews/r1/records/rec_a`;
    expect(within(dialog).getByTestId("verify-command").textContent).toBe(command);
    await user.click(within(dialog).getByRole("button", { name: /Copy command/ }));
    expect(await within(dialog).findByText("Copied the command.")).toBeTruthy();
    expect(await navigator.clipboard.readText()).toBe(command);
  });

  it("traps focus: tabbing never leaves the dialog", async () => {
    const user = userEvent.setup();
    render(<><button>outside</button><VerifyRecordDialog open onOpenChange={() => {}} reviewId="r1" record={RECORD} onDownloadJson={() => {}} onDownloadPdf={() => {}} /></>);
    const dialog = await screen.findByRole("dialog", { name: "Verify signed record" });
    for (let i = 0; i < 12; i += 1) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });

  it("offers Download JSON and Download PDF", async () => {
    const json = vi.fn();
    const pdf = vi.fn();
    const user = userEvent.setup();
    openDialog({ onDownloadJson: json, onDownloadPdf: pdf });
    await user.click(await screen.findByRole("button", { name: /Download JSON/ }));
    await user.click(screen.getByRole("button", { name: /Download PDF/ }));
    expect(json).toHaveBeenCalledTimes(1);
    expect(pdf).toHaveBeenCalledTimes(1);
  });

  it("says so when the record cannot be checked, and claims no outcome", async () => {
    verifyApi.mockRejectedValue(new Error("offline"));
    openDialog();
    const dialog = await screen.findByRole("dialog", { name: "Verify signed record" });
    await waitFor(() => expect(within(dialog).getByTestId("verify-result").getAttribute("data-outcome")).toBe("error"));
    expect(within(dialog).getByTestId("verify-result").textContent).toContain("could not be checked");
  });
});

describe("REC-1 / REC-2: the collapsed line, its menu and Details", () => {
  it("is one plain line with technical names only inside Details", async () => {
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    const line = await screen.findByTestId("record-line");
    expect(line.textContent).toBe("Signed record · Revision 1 · 3 decisions answered · Details");
    expect(screen.queryByTestId("record-details")).toBeNull();
    await userEvent.setup().click(within(line).getByRole("button", { name: "Details" }));
    const details = await screen.findByTestId("record-details");
    expect(await within(details).findByTestId("record-row-Version")).toBeTruthy();
    expect(details.textContent).toContain("Proof of what you reviewed, tied to this exact version. It stops being valid if the code changes.");
  });

  it("puts Details, Download JSON, Download PDF and Verify in the … menu, and Verify opens the dialog", async () => {
    const user = userEvent.setup();
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    await screen.findByTestId("record-line");
    await user.click(screen.getByRole("button", { name: "Record options" }));
    const names = (await screen.findAllByRole("menuitem")).map((item) => item.textContent);
    expect(names).toEqual(["Details", "Download JSON", "Download PDF", "Verify"]);
    await user.click(screen.getByRole("menuitem", { name: "Verify" }));
    expect(await screen.findByRole("dialog", { name: "Verify signed record" })).toBeTruthy();
  });

  it("downloads the stored file and the PDF copy of this record", async () => {
    const user = userEvent.setup();
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    await screen.findByTestId("record-line");
    await user.click(screen.getByRole("button", { name: "Record options" }));
    await user.click(await screen.findByRole("menuitem", { name: "Download JSON" }));
    await waitFor(() => expect(fileApi).toHaveBeenCalledWith("r1", "rec_a"));
    const notice = await screen.findByTestId("record-notice");
    expect(notice.textContent).toBe("Downloaded review-record-rec_a.dsse.json.");
    // The shared toast announces; the line's own text is not a live region.
    expect(currentFlowToast()).toMatchObject({ text: "Downloaded review-record-rec_a.dsse.json.", tone: "status" });
    expect(notice.getAttribute("role")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Record options" }));
    await user.click(await screen.findByRole("menuitem", { name: "Download PDF" }));
    await waitFor(() => expect(pdfApi).toHaveBeenCalledWith("r1", "rec_a"));
  });

  it("with no signing key, says so and offers no verification of a record that does not exist", async () => {
    statusApi.mockResolvedValue({ records: [], signing: { available: false } } as unknown as Awaited<ReturnType<typeof fetchReviewRecordStatus>>);
    const user = userEvent.setup();
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    expect((await screen.findByText("Not signed · no signing key is set up on this machine"))).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Record options" }));
    expect(((await screen.findByRole("menuitem", { name: "Verify" })).getAttribute("aria-disabled"))).toBe("true");
    expect(screen.getByRole("menuitem", { name: "Download PDF" }).getAttribute("aria-disabled")).toBe("true");
  });

  it("shows the record when the signature lands after the line first drew, and never claims a failure meanwhile", async () => {
    const empty = { records: [], signing: { available: true }, signing_due: true } as unknown as Awaited<ReturnType<typeof fetchReviewRecordStatus>>;
    const landed = { records: [RECORD], signing: { available: true } } as unknown as Awaited<ReturnType<typeof fetchReviewRecordStatus>>;
    statusApi.mockReset().mockResolvedValueOnce(empty).mockResolvedValueOnce(empty).mockResolvedValue(landed);
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    expect((await screen.findByTestId("record-line")).textContent).toBe("Signing the record…");
    expect(screen.queryByText(/could not be signed/)).toBeNull();
    await waitFor(() => expect(screen.getByTestId("record-line").textContent).toBe("Signed record · Revision 1 · 3 decisions answered · Details"), { timeout: 4000 });
  });

  it("says the record could not be signed at once when the server is not signing this verdict", async () => {
    statusApi.mockReset().mockResolvedValue({ records: [], signing: { available: true }, signing_due: false } as unknown as Awaited<ReturnType<typeof fetchReviewRecordStatus>>);
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    expect((await screen.findByTestId("record-line")).textContent).toBe("Not signed · the record could not be signed");
    expect(statusApi).toHaveBeenCalledTimes(1);
  });

  it("a record of another revision is not shown as this one's", async () => {
    statusApi.mockResolvedValue({ records: [{ ...RECORD, revision_id: "rev0" }], signing: { available: true } } as unknown as Awaited<ReturnType<typeof fetchReviewRecordStatus>>);
    render(<SignedRecordLine reviewId="r1" revisionId="rev1" revisionNumber={0} />);
    expect(await screen.findByText("Not signed · the record could not be signed")).toBeTruthy();
  });
});
