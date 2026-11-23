import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FlowHostedQueue } from "./flowReviewTools";
import { HostedQueueChip, hostedCounts, hostedQueueText } from "./HostedQueueChip";
import type { HostedAction, HostedAttachmentStatus } from "./types";

function action(id: string, state: HostedAction["state"], sequence: number): HostedAction {
  return { id, sequence, kind: "comment", snapshot_id: "h1", supersede_key: id, state, reason: state === "conflict" ? "thread_deleted" : "", payload: { path: "src/a.py", body: `note ${id}` } } as unknown as HostedAction;
}

function queue(status: HostedAttachmentStatus | null, error = ""): FlowHostedQueue {
  return {
    info: { reference: "acme/shop#12", origin: "https://hosted.example", url: "https://hosted.example/r/12", local_review_id: "local-1" },
    status, error, onStatus: vi.fn(), onDetached: vi.fn(),
  };
}

const STATUS: HostedAttachmentStatus = {
  attached: true, review_id: "api-1",
  actions: [action("q1", "pending", 1), action("q2", "pending", 2), action("c1", "conflict", 3)],
  counts: { pending: 2, conflict: 1, applied: 0, discarded: 0 },
};

afterEach(cleanup);

describe("the hosted queue chip", () => {
  it("counts queued and conflicting actions, from the counts or else from the actions", () => {
    expect(hostedCounts(STATUS)).toEqual({ queued: 2, conflicts: 1 });
    expect(hostedCounts({ ...STATUS, counts: undefined })).toEqual({ queued: 2, conflicts: 1 });
    expect(hostedCounts(null)).toEqual({ queued: 0, conflicts: 0 });
    expect(hostedQueueText({ queued: 2, conflicts: 1 })).toBe("2 queued · 1 conflict");
    expect(hostedQueueText({ queued: 0, conflicts: 3 })).toBe("3 conflicts");
    expect(hostedQueueText({ queued: 0, conflicts: 0 })).toBe("");
  });

  it("shows the counts, conflicts as a warning, and opens the hosted queue", async () => {
    const user = userEvent.setup();
    render(<HostedQueueChip reviewId="api-1" hosted={queue(STATUS)} />);
    const chip = screen.getByRole("button", { name: "Hosted acme/shop#12 · 2 queued · 1 conflict" });
    expect(screen.getByText("2 queued")).toBeInTheDocument();
    expect(screen.getByText("1 conflict").className).toContain("text-p1");
    await user.click(chip);
    const panel = await screen.findByTestId("hosted-queue-panel");
    expect(panel).toHaveTextContent("Conflict");
    expect(panel).toHaveTextContent("The thread was deleted on hosted.");
    expect(screen.getByRole("button", { name: "Re-apply" })).toBeInTheDocument();
  });

  it("warns when the queue cannot be read", () => {
    render(<HostedQueueChip reviewId="api-1" hosted={queue(null, "The hosted queue is unavailable: offline")} />);
    expect(screen.getByRole("button", { name: "Hosted acme/shop#12 · queue unavailable" })).toBeInTheDocument();
    expect(screen.getByText("Queue unavailable").className).toContain("text-p1");
  });
});
