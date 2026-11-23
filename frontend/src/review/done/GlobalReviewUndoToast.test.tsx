import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchReviewFlowSend, undoReviewFlowSend } from "../reviewApi";
import type { ReviewFlowSend } from "../types";
import { GlobalReviewUndoToast } from "./GlobalReviewUndoToast";
import { clearPendingSendToken, savePendingSendToken } from "./pendingSendToken";

vi.mock("../reviewApi", () => ({ fetchReviewFlowSend: vi.fn(), undoReviewFlowSend: vi.fn() }));
const status = vi.mocked(fetchReviewFlowSend);
const undoApi = vi.mocked(undoReviewFlowSend);
const PENDING: ReviewFlowSend = {
  state: "pending", operation_id: `fs-${"c".repeat(32)}`, outcome: "changes_requested", has_feedback: true, note_count: 1,
  send_after: new Date(Date.now() + 60_000).toISOString(), created_at: new Date().toISOString(),
};

function show() {
  return render(<MemoryRouter initialEntries={["/r/other"]}><GlobalReviewUndoToast /></MemoryRouter>);
}
beforeEach(() => {
  status.mockReset().mockResolvedValue(PENDING);
  undoApi.mockReset().mockResolvedValue({ ...PENDING, state: "undone" });
  savePendingSendToken("shop", "review", PENDING, "Claude Code");
});
afterEach(() => { cleanup(); clearPendingSendToken(); document.body.innerHTML = ""; });

describe("P2-5 undo toast", () => {
  it("names the destination and Undo Z, and Z undoes through the shared keymap", async () => {
    show();
    expect(await screen.findByTestId("global-review-undo")).toHaveTextContent("Sending to Claude Code");
    expect(screen.getByRole("button", { name: "Undo review send" })).toHaveTextContent("Undo");
    await userEvent.keyboard("z");
    await waitFor(() => expect(undoApi).toHaveBeenCalledWith("review", PENDING.operation_id));
  });

  it("Z does nothing while a dialog is open", async () => {
    show();
    await screen.findByTestId("global-review-undo");
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    document.body.appendChild(dialog);
    await userEvent.keyboard("z");
    expect(undoApi).not.toHaveBeenCalled();
  });

  it("Z does nothing while a menu is open or while typing", async () => {
    show();
    await screen.findByTestId("global-review-undo");
    const menu = document.createElement("div");
    menu.setAttribute("role", "menu");
    document.body.appendChild(menu);
    await userEvent.keyboard("z");
    expect(undoApi).not.toHaveBeenCalled();
    menu.remove();
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    await userEvent.keyboard("z");
    expect(undoApi).not.toHaveBeenCalled();
  });
});
