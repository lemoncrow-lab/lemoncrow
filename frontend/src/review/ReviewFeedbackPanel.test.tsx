import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReviewFeedbackPanel from "./ReviewFeedbackPanel";
import type { FeedbackExport } from "./types";

const feedback: FeedbackExport = {
  markdown: "## Review feedback\n\nPlease fix this.",
  open: 1,
  orphaned: 0,
  resolved: 0,
  revision_id: "rrv-1",
  feedback_hash: "feedback-hash-1",
  operation_id: "fop-1",
  annotation_versions: { "ann-1": 1 },
  delivery: {
    supported: false,
    host: "",
    session_id: "",
    target_ref: "",
    label: "",
    reason: "Direct delivery unavailable",
  },
  status: {
    open_total: 1,
    unpublished: 1,
    published: 0,
    in_flight: 0,
    addressed: 0,
  },
};

describe("ReviewFeedbackPanel", () => {
  it("confirms when the fallback bundle was copied", async () => {
    const onCopy = vi.fn(async () => {});

    const { rerender } = render(
      <ReviewFeedbackPanel
        feedback={feedback}
        delivery={null}
        busy={false}
        canDirectSend={false}
        onCopy={onCopy}
        onSend={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Copy review feedback bundle" }));
    await waitFor(() => expect(onCopy).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Copied")).toBeTruthy();

    rerender(
      <ReviewFeedbackPanel
        feedback={{ ...feedback, operation_id: "fop-2" }}
        delivery={null}
        busy={false}
        canDirectSend={false}
        onCopy={onCopy}
        onSend={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(await screen.findByText("Copy feedback")).toBeTruthy();
  });

  it("does not show stale unsent actions after delivery succeeds", () => {
    render(
      <ReviewFeedbackPanel
        feedback={{
          ...feedback,
          delivery: {
            supported: true,
            host: "claude",
            session_id: "session-1",
            target_ref: "claude:session-1",
            label: "Claude",
            reason: "",
          },
        }}
        delivery={{
          state: "sent",
          target_ref: "claude:session-1",
          remote_ref: "message-1",
          message: "sent",
          annotation_count: 1,
          operation_id: "fop-1",
        }}
        busy={false}
        canDirectSend
        onCopy={vi.fn()}
        onSend={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("Sent to Claude")).toBeTruthy();
    expect(screen.queryByText("1 to send")).toBeNull();
    expect(screen.queryByRole("button", { name: "Copy review feedback bundle" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Send 1 to Claude" })).toBeNull();
  });

  it("does not offer resend when delivery is uncertain", () => {
    render(
      <ReviewFeedbackPanel
        feedback={{
          ...feedback,
          delivery: {
            supported: true,
            host: "claude",
            session_id: "session-1",
            target_ref: "claude:session-1",
            label: "Claude",
            reason: "",
          },
          status: { open_total: 1, unpublished: 0, published: 0, in_flight: 1, addressed: 0 },
        }}
        delivery={{
          state: "uncertain",
          target_ref: "claude:session-1",
          remote_ref: "job-1",
          message: "Claude started but verification failed",
          annotation_count: 1,
          operation_id: "fop-1",
        }}
        busy={false}
        canDirectSend
        onCopy={vi.fn()}
        onSend={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("Delivery needs inspection")).toBeTruthy();
    expect(screen.getByText(/Do not resend this prepared operation/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send 1 to Claude" })).toBeNull();
  });

  it("keeps manual copy recovery visible when clipboard copy fails", async () => {
    const onCopy = vi.fn(async () => {
      throw new Error("clipboard denied");
    });

    render(
      <ReviewFeedbackPanel
        feedback={feedback}
        delivery={null}
        busy={false}
        canDirectSend={false}
        onCopy={onCopy}
        onSend={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Copy review feedback bundle" }));

    expect(await screen.findByText(/Clipboard copy failed/)).toBeTruthy();
    expect(screen.getByText(/Open the preview below and copy the feedback manually/)).toBeTruthy();
    expect(screen.getByText("Preview exact handoff")).toBeTruthy();
  });
});
