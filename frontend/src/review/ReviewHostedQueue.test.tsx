import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HostedQueuePanel } from "./ReviewHostedQueue";
import type { HostedAction, HostedAttachmentInfo, HostedAttachmentStatus } from "./types";

const api = vi.hoisted(() => ({
  fetchHostedAttachment: vi.fn(),
  resolveHostedAction: vi.fn(),
  detachHostedAttachment: vi.fn(),
}));
vi.mock("./reviewApi", () => ({
  fetchHostedAttachment: api.fetchHostedAttachment,
  resolveHostedAction: api.resolveHostedAction,
  detachHostedAttachment: api.detachHostedAttachment,
}));

const HOSTED: HostedAttachmentInfo = {
  reference: "r/9",
  origin: "https://hosted.example",
  url: "https://hosted.example/r/9",
  local_review_id: "local-1",
};

function action(overrides: Partial<HostedAction>): HostedAction {
  return {
    id: "rha-1",
    sequence: 1,
    kind: "mark",
    snapshot_id: "rrv-h1",
    supersede_key: "",
    payload: { state: "reviewed", unit_key: "sym:a" },
    preconditions: {},
    state: "pending",
    reason: "",
    result: {},
    created_at: "",
    resolved_at: "",
    ...overrides,
  };
}

function status(actions: HostedAction[]): HostedAttachmentStatus {
  return { attached: true, review_id: "hosted-9", reference: "r/9", actions };
}

const QUEUE = status([
  action({ id: "queued", sequence: 1, kind: "comment", payload: { path: "src/a.py", start_line: 3, body: "why?" } }),
  action({ id: "stale", sequence: 2, state: "conflict", reason: "snapshot_advanced" }),
  action({ id: "done", sequence: 3, state: "applied" }),
]);

function renderPanel(current: HostedAttachmentStatus | null, error = "") {
  const onStatus = vi.fn();
  const onDetached = vi.fn();
  render(
    <HostedQueuePanel
      reviewId="hosted-9"
      hosted={HOSTED}
      status={current}
      error={error}
      onStatus={onStatus}
      onDetached={onDetached}
    />,
  );
  return { onStatus, onDetached };
}

describe("HostedQueuePanel", () => {
  beforeEach(() => {
    api.fetchHostedAttachment.mockReset();
    api.resolveHostedAction.mockReset();
    api.detachHostedAttachment.mockReset();
  });

  it("lists the conflict first with why hosted refused it, and hides applied actions", () => {
    renderPanel(QUEUE);
    const items = screen.getAllByRole("listitem");
    expect(items.map((item) => item.getAttribute("data-state"))).toEqual(["conflict", "pending"]);
    expect(items[0].textContent).toContain("Hosted has a newer revision than the one you judged.");
    expect(items[0].textContent).toContain("Mark reviewed · sym:a");
    expect(items[1].textContent).toContain("Comment on src/a.py:L3: “why?”");
    // Only a conflict offers re-application; a pending action can only be discarded.
    expect(screen.getAllByRole("button", { name: "Re-apply" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Discard" })).toHaveLength(2);
    expect(screen.getByText("1 conflict need you")).toBeTruthy();
  });

  it("re-applies a conflict only when the reviewer asks, and reports the new status", async () => {
    const next = status([action({ id: "stale", sequence: 4, state: "pending" })]);
    api.resolveHostedAction.mockResolvedValue({ ...next, action: next.actions?.[0] });
    const { onStatus } = renderPanel(QUEUE);

    await userEvent.click(screen.getByRole("button", { name: "Re-apply" }));

    expect(api.resolveHostedAction).toHaveBeenCalledWith("hosted-9", "stale", "requeue");
    await waitFor(() => expect(onStatus).toHaveBeenCalledWith(expect.objectContaining({ review_id: "hosted-9" })));
  });

  it("discards one action and shows a refusal", async () => {
    api.resolveHostedAction.mockRejectedValue(new Error("hosted action rha-x is already applied"));
    const { onStatus } = renderPanel(QUEUE);

    await userEvent.click(screen.getAllByRole("button", { name: "Discard" })[1]);

    expect(api.resolveHostedAction).toHaveBeenCalledWith("hosted-9", "queued", "discard");
    expect((await screen.findByRole("alert")).textContent).toBe("hosted action rha-x is already applied");
    expect(onStatus).not.toHaveBeenCalled();
  });

  it("detaches at once when nothing is unresolved", async () => {
    api.detachHostedAttachment.mockResolvedValue({
      detached: true, review_id: "local-1", discarded_unresolved: 0, removed_resolved: 1, kept: "",
    });
    const { onDetached } = renderPanel(status([action({ state: "applied" })]));
    expect(screen.getByText("Nothing is waiting for hosted.")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Detach" }));

    expect(api.detachHostedAttachment).toHaveBeenCalledWith("hosted-9", false);
    await waitFor(() => expect(onDetached).toHaveBeenCalledWith("local-1"));
  });

  it("never discards unresolved actions without an explicit confirmation", async () => {
    api.detachHostedAttachment.mockResolvedValue({
      detached: true, review_id: "local-1", discarded_unresolved: 2, removed_resolved: 1, kept: "",
    });
    const { onDetached } = renderPanel(QUEUE);

    await userEvent.click(screen.getByRole("button", { name: "Detach" }));
    expect(api.detachHostedAttachment).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("2 unresolved actions have not reached hosted.");

    // Backing out keeps the attachment and its queue.
    await userEvent.click(screen.getByRole("button", { name: "Keep attached" }));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(api.detachHostedAttachment).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Detach" }));
    await userEvent.click(screen.getByRole("button", { name: "Detach and discard 2" }));

    expect(api.detachHostedAttachment).toHaveBeenCalledWith("hosted-9", true);
    await waitFor(() => expect(onDetached).toHaveBeenCalledWith("local-1"));
  });

  it("keeps the attachment and says why when detaching fails", async () => {
    api.detachHostedAttachment.mockRejectedValue(new Error("2 hosted actions are unresolved"));
    const { onDetached } = renderPanel(status([action({ state: "applied" })]), "The hosted queue is unavailable: offline");

    await userEvent.click(screen.getByRole("button", { name: "Detach" }));

    await waitFor(() => expect(screen.getAllByRole("alert").map((node) => node.textContent)).toEqual([
      "The hosted queue is unavailable: offline",
      "2 hosted actions are unresolved",
    ]));
    expect(onDetached).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Detach" })).toBeTruthy();
  });
});
