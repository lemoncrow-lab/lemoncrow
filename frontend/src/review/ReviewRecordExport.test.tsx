import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ReviewRecordExportResult, ReviewRecordStatus, ReviewRecordSummary } from "./types";

const api = vi.hoisted(() => ({
  fetchReviewRecordStatus: vi.fn(),
  exportReviewRecord: vi.fn(),
  fetchReviewRecordFile: vi.fn(),
}));
vi.mock("./reviewApi", () => api);

import ReviewRecordExport, { downloadDecisionRecord, UNSIGNED_RECORD_LABEL } from "./ReviewRecordExport";
import ReviewReadinessSheet, { deriveReviewReadiness } from "./ReviewReadinessSheet";

function summary(overrides: Partial<ReviewRecordSummary> = {}): ReviewRecordSummary {
  return {
    record_id: "rec_signed",
    review_id: "rev-1",
    revision_id: "rr-1",
    payload_digest: "sha256:" + "a".repeat(64),
    signing_state: "signed",
    signing_mode: "organization_key",
    key_id: "rk_abc",
    issued_at: "2026-10-04T12:00:00+00:00",
    decision_kind: "human_outcome",
    label: "Signed with organization key rk_abc",
    decision: "approved",
    decided_by: "user_rev",
    ...overrides,
  };
}

function status(overrides: Partial<ReviewRecordStatus> = {}): ReviewRecordStatus {
  return {
    review_id: "rev-1",
    snapshot_id: "rr-1",
    snapshot_number: 2,
    decision: {
      kind: "human_outcome",
      outcome: "approved",
      actor: { id: "user_rev", type: "human" },
      decided_at: "2026-10-04T11:59:00+00:00",
      reference: "out-1",
      detail: {},
    },
    signing: {
      mode: "organization_key",
      available: true,
      reason: "",
      code: "",
      key_id: "rk_abc",
      algorithm: "EdDSA",
      issuer_kind: "organization",
      issuer_id: "org_a",
    },
    records: [],
    ...overrides,
  };
}

function exported(record: ReviewRecordSummary, verdict: "verified" | "unsigned"): ReviewRecordExportResult {
  return {
    record,
    envelope: { payloadType: "application/vnd.in-toto+json", payload: "e30=", signatures: [] },
    verification: {
      status: verdict,
      verified: verdict === "verified",
      headline: verdict === "verified" ? "VERIFIED: signed by rk_abc" : "UNSIGNED: this record carries no signature",
      reasons: [],
      payload_digest: record.payload_digest,
      key_id: record.key_id,
      key_status: verdict === "verified" ? "active" : "",
      key_revoked_at: "",
      signing_state: record.signing_state,
      signing_mode: record.signing_mode,
      exit_code: verdict === "verified" ? 0 : 2,
    },
    store: { snapshot: "match" },
    matches_store: true,
    reproduced_digest: record.payload_digest,
    reproduced: true,
  };
}

beforeEach(() => {
  api.fetchReviewRecordFile.mockResolvedValue(new Blob(["{}"], { type: "application/json" }));
  Object.assign(URL, { createObjectURL: vi.fn(() => "blob:record"), revokeObjectURL: vi.fn() });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("downloadDecisionRecord (Finish)", () => {
  it("signs when it can, binds the export to the finished revision and downloads the stored envelope", async () => {
    const record = summary();
    api.fetchReviewRecordStatus.mockResolvedValue(status());
    api.exportReviewRecord.mockResolvedValue(exported(record, "verified"));
    await expect(downloadDecisionRecord("rev-1", "rr-1")).resolves.toBe("Signed with organization key rk_abc");
    expect(api.exportReviewRecord).toHaveBeenCalledWith("rev-1", { signing: "auto", expected_revision_id: "rr-1" });
    expect(api.fetchReviewRecordFile).toHaveBeenCalledWith("rev-1", "rec_signed");
  });

  it("falls back to an explicitly UNSIGNED record when no key can sign, and says so", async () => {
    const record = summary({ record_id: "rec_unsigned", signing_state: "unsigned", signing_mode: "unsigned", key_id: "" });
    api.fetchReviewRecordStatus.mockResolvedValue(status({ signing: { ...status().signing, available: false, code: "not_configured" } }));
    api.exportReviewRecord.mockResolvedValue(exported(record, "unsigned"));
    await expect(downloadDecisionRecord("rev-1", "rr-1")).resolves.toBe(UNSIGNED_RECORD_LABEL);
    expect(api.exportReviewRecord).toHaveBeenCalledWith("rev-1", { signing: "unsigned", expected_revision_id: "rr-1" });
  });
});

describe("ReviewRecordExport", () => {
  it("exports a signed record of the shown revision, downloads it and labels it signed", async () => {
    const record = summary();
    api.fetchReviewRecordStatus.mockResolvedValueOnce(status()).mockResolvedValueOnce(status({ records: [record] }));
    api.exportReviewRecord.mockResolvedValue(exported(record, "verified"));
    render(<ReviewRecordExport reviewId="rev-1" snapshotId="rr-1" />);

    expect(await screen.findByTestId("record-signing-status")).toHaveTextContent("Signs with the organization key rk_abc");
    await userEvent.click(screen.getByRole("button", { name: "Export signed record" }));

    await waitFor(() => expect(api.exportReviewRecord).toHaveBeenCalledWith("rev-1", { signing: "auto", expected_revision_id: "rr-1" }));
    expect(await screen.findByTestId("record-export-result")).toHaveTextContent("VERIFIED: signed by rk_abc · matches the stored Review");
    expect(api.fetchReviewRecordFile).toHaveBeenCalledWith("rev-1", "rec_signed");
    const list = screen.getByLabelText("Exported review records");
    expect(within(list).getByText("signed")).toBeInTheDocument();
    expect(within(list).getByText("Signed with organization key rk_abc")).toBeInTheDocument();
    expect(within(list).queryByText("UNSIGNED")).not.toBeInTheDocument();
  });

  it("labels an unsigned record UNSIGNED and never as signed, even if the server label disagrees", async () => {
    const record = summary({ record_id: "rec_unsigned", signing_state: "unsigned", signing_mode: "unsigned", key_id: "", label: "Signed with organization key rk_abc" });
    api.fetchReviewRecordStatus.mockResolvedValueOnce(status()).mockResolvedValueOnce(status({ records: [record] }));
    api.exportReviewRecord.mockResolvedValue(exported(record, "unsigned"));
    render(<ReviewRecordExport reviewId="rev-1" snapshotId="rr-1" />);

    await userEvent.click(await screen.findByRole("button", { name: "Export UNSIGNED record" }));
    await waitFor(() => expect(api.exportReviewRecord).toHaveBeenCalledWith("rev-1", { signing: "unsigned", expected_revision_id: "rr-1" }));
    expect(await screen.findByTestId("record-export-result")).toHaveTextContent("UNSIGNED: this record carries no signature");
    const list = await screen.findByLabelText("Exported review records");
    expect(within(list).getByText("UNSIGNED")).toBeInTheDocument();
    expect(within(list).getByText(UNSIGNED_RECORD_LABEL)).toBeInTheDocument();
    expect(within(list).queryByText(/Signed with/)).not.toBeInTheDocument();
    expect(within(list).getByRole("button", { name: `Download ${UNSIGNED_RECORD_LABEL}` })).toBeInTheDocument();
  });

  it("keeps signed export disabled with the reason when no key is configured, and surfaces refusals", async () => {
    const unavailable = status({
      signing: { ...status().signing, available: false, code: "not_configured", key_id: "", reason: "No organization signing key exists yet." },
    });
    api.fetchReviewRecordStatus.mockResolvedValue(unavailable);
    api.exportReviewRecord.mockRejectedValue(Object.assign(new Error("a newer revision exists"), { status: 409 }));
    render(<ReviewRecordExport reviewId="rev-1" snapshotId="rr-1" />);

    expect(await screen.findByTestId("record-signing-status")).toHaveTextContent("Signing unavailable: No organization signing key exists yet.");
    const signed = screen.getByRole("button", { name: "Export signed record" });
    expect(signed).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(signed);
    expect(api.exportReviewRecord).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Export UNSIGNED record" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("a newer revision exists");
  });

  it("offers no export before a decision or when read-only", async () => {
    api.fetchReviewRecordStatus.mockResolvedValue(status({ decision: null }));
    const { unmount } = render(<ReviewRecordExport reviewId="rev-1" />);
    expect(await screen.findByText(/No decision on the latest revision yet/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Export/ })).not.toBeInTheDocument();
    unmount();

    api.fetchReviewRecordStatus.mockResolvedValue(status({ records: [summary()] }));
    render(<ReviewRecordExport reviewId="rev-1" readOnly />);
    expect(await screen.findByLabelText("Exported review records")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Export/ })).not.toBeInTheDocument();
  });

  it("is the Review record section of the existing Review Status sheet", async () => {
    api.fetchReviewRecordStatus.mockResolvedValue(status());
    const model = deriveReviewReadiness({
      progress: { target_count: 1, reviewed: 1, changed_since_review: 0, needs_changes: 0, unreviewed: 0, unknown: 0, mechanical: 0 },
      openComments: 0,
      verification: { pass: 1, fail: 0, not_run: 0, unknown: 0 },
      outcome: null,
    });
    render(
      <ReviewReadinessSheet
        model={model}
        recordSection={<ReviewRecordExport reviewId="rev-1" snapshotId="rr-1" />}
        onClose={() => undefined}
      />,
    );
    await userEvent.click(screen.getByText("Review record"));
    const section = screen.getByRole("region", { name: "Review record" });
    expect(await within(section).findByRole("button", { name: "Export signed record" })).not.toHaveAttribute("aria-disabled", "true");
  });
});
