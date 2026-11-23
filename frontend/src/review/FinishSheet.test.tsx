import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import FinishSheet, { type FinishRecordFacts } from "./FinishSheet";
import type { ReviewClosure } from "./types";

function summary(overrides: Partial<ReviewClosure> = {}): ReviewClosure {
  return {
    status: "open",
    target_count: 10,
    reviewed_targets: 6,
    unreviewed_targets: 1,
    changed_since_review: 1,
    needs_changes: 1,
    unknown_targets: 1,
    open_comments: 2,
    orphaned_comments: 1,
    failed_verification: 1,
    unresolved_verification: 2,
    previous_revision_evidence: 3,
    discarded_verdicts: 1,
    ...overrides,
  };
}

const CLEAN: Partial<ReviewClosure> = {
  target_count: 10,
  reviewed_targets: 10,
  unreviewed_targets: 0,
  changed_since_review: 0,
  needs_changes: 0,
  unknown_targets: 0,
  open_comments: 0,
  orphaned_comments: 0,
  failed_verification: 0,
  unresolved_verification: 0,
};

const record: FinishRecordFacts = {
  revisionNumber: 3,
  headSha: "a1b2c3d4e5f60718",
  targets: [
    { id: "t-1", label: "charge", path: "billing/charge.py", reviewed: true },
    { id: "t-2", label: "refund", path: "billing/refund.py", reviewed: false },
    { id: "t-3", label: "void", path: "billing/void.py", reviewed: false, needsChanges: true },
  ],
  passedChecks: [],
  openConditions: [],
  reviewer: "alice",
};

function group(term: string): HTMLElement {
  const dt = within(screen.getByRole("region", { name: "What you are signing" })).getByText(term);
  return dt.nextElementSibling as HTMLElement;
}

describe("FinishSheet record", () => {
  it("shows persisted structure reconsideration in the record preview without inferring it from note wording", () => {
    render(<FinishSheet record={{ ...record, reconsideredNotes: [{ id: "ann-1", path: "src/auth.py", body: "Please restore the guard" }] }} summary={summary()} busy={false} onClose={vi.fn()} onFinish={vi.fn()} />);
    const notes = group("Notes");
    expect(notes).toHaveTextContent("Answer changed after seeing structure");
    expect(notes).toHaveTextContent("src/auth.py");
    expect(notes).toHaveTextContent("Please restore the guard");
  });

  it("shows what the verdict signs: revision, inspected, checked, still open, decided", async () => {
    render(
      <FinishSheet
        record={{
          ...record,
          passedChecks: [{ id: "evidence:pytest", label: "pytest", source: "ci" }],
          openConditions: [{ id: "verification", label: "Verification requirements" }],
        }}
        summary={summary()}
        busy={false}
        onClose={vi.fn()}
        onFinish={vi.fn()}
      />,
    );

    expect(screen.getByTestId("finish-revision")).toHaveTextContent("Revision 4 · a1b2c3d");
    // Reviewed or Needs changes counts as reviewed: 6 + 1 of 10.
    expect(group("Inspected")).toHaveTextContent("7 of 10 parts reviewed");
    await userEvent.click(within(group("Inspected")).getByText(/parts reviewed/));
    const listed = within(group("Inspected")).getByRole("list", { name: "Parts" });
    expect(listed).toHaveTextContent("reviewedcharge · billing/charge.py");
    expect(listed).toHaveTextContent("not reviewedrefund · billing/refund.py");
    expect(listed).toHaveTextContent("needs changesvoid · billing/void.py");
    expect(group("Checked")).toHaveTextContent("pytest · ci");
    const open = within(group("Still open")).getAllByRole("listitem").map((item) => item.textContent);
    expect(open).toEqual([
      "3 parts not reviewed (1 changed since review)",
      "1 needs-changes mark",
      "3 unresolved comments",
      "Verification requirements",
    ]);
    // Nothing reads as decided until a verdict is picked.
    expect(screen.queryByText("Decided")).toBeNull();
    expect(screen.getByRole("radio", { name: "Comment" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText(/No verdict chosen/)).toBeTruthy();
    await userEvent.click(screen.getByRole("radio", { name: "Approve" }));
    expect(group("Decided")).toHaveTextContent("Approve · by alice");
    expect(screen.queryByText(/No verdict chosen/)).toBeNull();

    expect(screen.queryByText(/items? need attention/)).toBeNull();
    expect(screen.queryByText(/Review Status/)).toBeNull();
    expect(screen.queryByText(/This is what your verdict signs/)).toBeNull();
    expect(screen.getByText(/Check what is still open/)).toBeTruthy();
  });

  it("lists the still-open parts, risky ones first, and opens one in the code", async () => {
    const onOpenPart = vi.fn();
    const openParts = [
      { id: "t-auth", label: "authorize", path: "billing/auth.py", reason: "Changed since your review", risky: true },
      { id: "t-refund", label: "refund", path: "billing/refund.py", reason: "Not reviewed yet", risky: false },
    ];
    render(
      <FinishSheet record={{ ...record, openParts }} summary={summary()} busy={false} onOpenPart={onOpenPart} onClose={vi.fn()} onFinish={vi.fn()} />,
    );
    const listed = within(group("Still open")).getByRole("list", { name: "Parts still open" });
    expect(within(listed).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "Look closelyauthorize · billing/auth.py",
      "refund · billing/refund.py",
    ]);
    await userEvent.click(within(listed).getByRole("button", { name: /authorize/ }));
    expect(onOpenPart).toHaveBeenCalledWith("t-auth");
  });

  it("names only the first five open parts and counts the rest", () => {
    const openParts = Array.from({ length: 8 }, (_, n) => ({ id: `t-${n}`, label: `part${n}`, path: "a.py", reason: "", risky: false }));
    render(<FinishSheet record={{ ...record, openParts }} summary={summary()} busy={false} onClose={vi.fn()} onFinish={vi.fn()} />);
    const listed = within(group("Still open")).getByRole("list", { name: "Parts still open" });
    expect(within(listed).getAllByRole("listitem")).toHaveLength(6);
    expect(listed).toHaveTextContent("3 more parts in the Code view");
  });

  it("says plainly when analyses could not run, and links to which", async () => {
    const onOpenStatus = vi.fn();
    render(
      <FinishSheet record={{ ...record, notMeasured: 4 }} summary={summary(CLEAN)} busy={false} onOpenStatus={onOpenStatus} onClose={vi.fn()} onFinish={vi.fn()} />,
    );
    expect(group("Still open")).toHaveTextContent("4 analyses could not run.");
    await userEvent.click(screen.getByRole("button", { name: "See which" }));
    expect(onOpenStatus).toHaveBeenCalledTimes(1);
  });

  it("finishing without a pick records a plain Comment", async () => {
    const onFinish = vi.fn();
    render(<FinishSheet record={record} summary={summary(CLEAN)} busy={false} onClose={vi.fn()} onFinish={onFinish} />);
    await userEvent.click(screen.getByRole("button", { name: "Finish" }));
    expect(onFinish).toHaveBeenCalledWith("comment", "", { send: false });
  });

  it("names an uncommitted capture instead of inventing a commit", () => {
    render(<FinishSheet record={{ ...record, headSha: "", dirty: true }} summary={summary(CLEAN)} busy={false} onClose={vi.fn()} onFinish={vi.fn()} />);
    expect(screen.getByTestId("finish-revision")).toHaveTextContent("Revision 4 · uncommitted changes");
  });

  it("says plainly when no check passed and nothing is open", () => {
    render(<FinishSheet record={record} summary={summary(CLEAN)} busy={false} onClose={vi.fn()} onFinish={vi.fn()} />);
    expect(group("Checked")).toHaveTextContent("No check passed on this revision");
    expect(group("Still open")).toHaveTextContent("Nothing open");
    expect(screen.getByRole("button", { name: "Finish" })).toBeTruthy();
  });

  it("never finishes cleanly while a part is left for later", () => {
    const { rerender } = render(
      <FinishSheet record={record} summary={summary({ ...CLEAN, later_targets: 1, reviewed_targets: 9 })} busy={false} onClose={vi.fn()} onFinish={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: "Finish anyway" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Finish" })).toBeNull();
    rerender(<FinishSheet record={record} summary={summary(CLEAN)} busy={false} onClose={vi.fn()} onFinish={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Finish" })).toBeTruthy();
  });

  it("counts an unmet readiness condition as a blocker", () => {
    render(
      <FinishSheet
        record={{ ...record, openConditions: [{ id: "landing-readiness", label: "Checking merge readiness" }] }}
        summary={summary(CLEAN)}
        busy={false}
        onClose={vi.fn()}
        onFinish={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Finish anyway" })).toBeTruthy();
  });

  it("after an approval confirms the verdict and downloads the Verified Change record", async () => {
    const onDownloadRecord = vi.fn().mockResolvedValue("UNSIGNED record (no signature)");
    const onClose = vi.fn();
    render(
      <FinishSheet
        record={record}
        summary={summary({ ...CLEAN, status: "finished" })}
        busy={false}
        recorded="lgtm"
        onDownloadRecord={onDownloadRecord}
        onClose={onClose}
        onFinish={vi.fn()}
      />,
    );
    expect(screen.getByRole("heading", { name: "Review finished" })).toBeTruthy();
    expect(screen.queryByRole("radio", { name: "Approve" })).toBeNull();
    expect(group("Decided")).toHaveTextContent("Approve · by alice");
    await userEvent.click(screen.getByRole("button", { name: "Download Verified Change record" }));
    expect(onDownloadRecord).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("status")).toHaveTextContent("Downloaded: UNSIGNED record (no signature)");
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("offers no record for a Comment verdict, which is not a decision", () => {
    render(
      <FinishSheet
        record={record}
        summary={summary({ ...CLEAN, status: "finished" })}
        busy={false}
        recorded="comment"
        onDownloadRecord={vi.fn()}
        onClose={vi.fn()}
        onFinish={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Download Verified Change record" })).toBeNull();
    expect(screen.getByRole("button", { name: "Done" })).toBeTruthy();
  });

  it("reports a failed record download in the sheet", async () => {
    render(
      <FinishSheet
        record={record}
        summary={summary({ ...CLEAN, status: "finished" })}
        busy={false}
        recorded="changes_requested"
        onDownloadRecord={vi.fn().mockRejectedValue(new Error("signing refused"))}
        onClose={vi.fn()}
        onFinish={vi.fn()}
      />,
    );
    expect(screen.getByRole("heading", { name: "Changes requested" })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Download Verified Change record" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Record download failed: signing refused");
  });
});

describe("FinishSheet", () => {

  it("does not mutate until the explicit finish action is pressed", async () => {
    const onFinish = vi.fn();
    const onClose = vi.fn();
    render(<FinishSheet record={record} summary={summary()} busy={false} onClose={onClose} onFinish={onFinish} />);
    expect(onFinish).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Continue reviewing" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onFinish).not.toHaveBeenCalled();
  });

  it("records the selected verdict and optional summary only on Finish", async () => {
    const onFinish = vi.fn();
    render(<FinishSheet record={record} summary={summary()} busy={false} onClose={vi.fn()} onFinish={onFinish} />);

    await userEvent.click(screen.getByRole("radio", { name: "Approve" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Review outcome summary" }), "Ship it.");
    expect(onFinish).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Approve & finish anyway" }));
    expect(onFinish).toHaveBeenCalledWith("lgtm", "Ship it.", { send: false });
  });

  it("does not silently carry an older-revision verdict into the current choice", () => {
    render(
      <FinishSheet
        record={record}
        summary={summary()}
        currentOutcome={{
          id: "rot-old",
          review_id: "rev-1",
          revision_id: "rrv-old",
          reviewer_id: "alice",
          outcome: "lgtm",
          summary: "Old LGTM",
          created_at: "2026-09-17T00:00:00Z",
          stale: true,
        }}
        busy={false}
        onClose={vi.fn()}
        onFinish={vi.fn()}
      />,
    );
    expect(screen.getByText(/applies to an older revision/i)).toBeTruthy();
    expect(screen.getByRole("radio", { name: "Comment" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("radio", { name: "Approve" })).toHaveAttribute("aria-checked", "false");
  });

  it("focuses its close control, traps focus and supports Escape", async () => {
    const onFinish = vi.fn();
    const onClose = vi.fn();
    render(<FinishSheet record={record} summary={summary()} busy={false} onClose={onClose} onFinish={onFinish} />);
    const close = screen.getByRole("button", { name: "Close review outcome" });
    const last = screen.getByRole("button", { name: "Finish anyway" });
    await waitFor(() => expect(document.activeElement).toBe(close));
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(last);
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onFinish).not.toHaveBeenCalled();
  });

  it("holds actions while Finish is in flight", () => {
    render(<FinishSheet record={record} summary={summary()} busy onClose={vi.fn()} onFinish={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Recording…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Continue reviewing" })).toBeDisabled();
  });
});

describe("FinishSheet pending feedback", () => {
  const claude = { unsent: 2, label: "Claude a1b2", kind: "agent" as const, available: true, reason: "" };

  it("keeps iterative Send feedback separate while allowing an explicit Finish delivery choice", async () => {
    const onFinish = vi.fn();
    render(<FinishSheet record={record} summary={summary()} busy={false} handoff={claude} onClose={vi.fn()} onFinish={onFinish} />);

    await userEvent.click(screen.getByRole("radio", { name: "Request changes" }));
    expect(screen.getByRole("region", { name: "Where fixes go" })).toHaveTextContent("Pending feedback");
    expect(screen.getByRole("region", { name: "Where fixes go" })).toHaveTextContent("2 comments");
    await userEvent.click(screen.getByRole("button", { name: "Request changes & send 2" }));
    expect(onFinish).toHaveBeenCalledWith("changes_requested", "", { send: true });
  });

  it("requires something actionable before recording Request changes with no pending feedback", async () => {
    const onFinish = vi.fn();
    render(
      <FinishSheet
        record={record}
        summary={summary({ open_comments: 0 })}
        busy={false}
        handoff={{ ...claude, unsent: 0 }}
        onClose={vi.fn()}
        onFinish={onFinish}
      />,
    );
    await userEvent.click(screen.getByRole("radio", { name: "Request changes" }));
    expect(screen.getByRole("button", { name: "Request changes" })).toBeDisabled();
    expect(screen.getByText("Add a comment or a summary for the fixer.")).toBeTruthy();
    await userEvent.type(screen.getByRole("textbox", { name: "Review outcome summary" }), "Handle the empty list.");
    await userEvent.click(screen.getByRole("button", { name: "Request changes" }));
    expect(onFinish).toHaveBeenCalledWith("changes_requested", "Handle the empty list.", { send: false });
  });

  it("refuses an unavailable delivery and says why", async () => {
    render(
      <FinishSheet
        record={record}
        summary={summary()}
        busy={false}
        handoff={{ ...claude, available: false, reason: "direct feedback delivery is not configured for gemini" }}
        onClose={vi.fn()}
        onFinish={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByRole("radio", { name: "Request changes" }));
    expect(screen.getByRole("button", { name: "Request changes & send 2" })).toBeDisabled();
    expect(screen.getByText(/not configured for gemini/)).toBeTruthy();
  });

  it("can finish without sending pending feedback for non-request-change outcomes", async () => {
    const onFinish = vi.fn();
    render(<FinishSheet record={record} summary={summary()} busy={false} handoff={claude} onClose={vi.fn()} onFinish={onFinish} />);
    await userEvent.click(screen.getByRole("radio", { name: "Approve" }));
    const also = screen.getByRole("checkbox", { name: "Also send 2 to Claude a1b2" });
    expect(also).toBeChecked();
    await userEvent.click(also);
    await userEvent.click(screen.getByRole("button", { name: "Approve & finish anyway" }));
    expect(onFinish).toHaveBeenCalledWith("lgtm", "", { send: false });
  });

  it("after failed delivery offers Retry and Finish without sending", async () => {
    const onFinish = vi.fn();
    render(
      <FinishSheet record={record} summary={summary()} busy={false} handoff={claude} sendError="session refused" onClose={vi.fn()} onFinish={onFinish} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Nothing was finished; the comments were not sent: session refused");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onFinish).toHaveBeenLastCalledWith("comment", "", { send: true });
    await userEvent.click(screen.getByRole("button", { name: "Finish without sending" }));
    expect(onFinish).toHaveBeenLastCalledWith("comment", "", { send: false });
  });
});
