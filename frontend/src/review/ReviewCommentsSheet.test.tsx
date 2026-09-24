import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReviewCommentsSheet from "./ReviewCommentsSheet";
import type { Annotation } from "./types";

function annotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: "ann-1",
    review_id: "rev-1",
    revision_id: "rrv-1",
    parent_id: "",
    kind: "comment",
    state: "open",
    body: "Please explain this branch",
    created_by: "alice",
    created_by_actor: "human",
    created_at: "2026-09-15T20:00:00Z",
    updated_at: "2026-09-15T20:00:00Z",
    anchor_method: "identical_blob",
    anchor_method_label: "file unchanged since the comment",
    anchor_exact: true,
    anchor_detail: "",
    anchored: true,
    path: "src/app.py",
    side: "new",
    start_line: 12,
    end_line: 12,
    unit_key: "sym:app",
    symbol: "run",
    origin_symbol: "run",
    ...overrides,
  };
}

describe("ReviewCommentsSheet", () => {
  it("moves keyboard focus and the visible comments together across filters", async () => {
    const user = userEvent.setup();
    render(<ReviewCommentsSheet
      annotations={[annotation(), annotation({ id: "resolved", state: "resolved", body: "Verified fix" })]}
      onClose={vi.fn()}
      onJump={vi.fn()}
    />);
    screen.getByRole("tab", { name: "Open" }).focus();
    await user.keyboard("{ArrowRight}{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Resolved" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Resolved" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Verified fix")).toBeVisible();
    expect(screen.queryByText("Please explain this branch")).toBeNull();
    await user.keyboard("{End}{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Open" })).toHaveFocus();
    expect(screen.getByText("Please explain this branch")).toBeVisible();
  });

  it("shows the coding-agent handoff lifecycle and jumps only current anchored comments", async () => {
    const user = userEvent.setup();
    const onJump = vi.fn();
    render(
      <ReviewCommentsSheet
        annotations={[
          annotation(),
          annotation({ id: "ann-2", body: "old concern", state: "orphaned", anchored: false, path: "src/old.py" }),
          annotation({ id: "reply-1", parent_id: "ann-1", body: "author reply", created_by: "bob" }),
        ]}
        feedbackStatus={{ open_total: 2, unpublished: 1, published: 0, in_flight: 0, addressed: 1 }}
        feedbackDelivery={{
          supported: true,
          host: "claude",
          session_id: "session-1",
          target_ref: "claude:session-1",
          label: "Claude",
          reason: "",
        }}
        onClose={vi.fn()}
        onJump={onJump}
      />,
    );

    expect(screen.getByText("Please explain this branch")).toBeTruthy();
    expect(screen.getByText("1 not sent · 1 ready for re-review")).toBeTruthy();
    expect(screen.getByText(/send to Claude → agent fixes and verifies → marks addressed/)).toBeTruthy();
    expect(screen.getByText(/1 reply/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Show in review" }));
    expect(onJump).toHaveBeenCalledWith(expect.objectContaining({ id: "ann-1" }));

    await user.click(screen.getByRole("tab", { name: "Orphaned" }));
    expect(screen.getByText("old concern")).toBeTruthy();
    expect(screen.getByText("Not anchored in current revision")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Show in review" })).toBeNull();
  });
});
