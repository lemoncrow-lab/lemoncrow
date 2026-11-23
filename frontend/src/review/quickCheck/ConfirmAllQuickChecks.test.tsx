import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchFile } from "../reviewApi";
import type { ReviewTarget } from "../types";
import { ConfirmAllQuickChecks } from "./ConfirmAllQuickChecks";
import { resetPatchEvidence } from "./patchEvidence";

vi.mock("../reviewApi", async (importOriginal) => ({ ...(await importOriginal<typeof import("../reviewApi")>()), fetchFile: vi.fn() }));
const file = vi.mocked(fetchFile);

const part = (id: string) => ({ target_id: id, unit_key: `sym:${id}`, path: `src/${id}.py` }) as ReviewTarget;
const PARTS = [part("a"), part("b"), part("c")];

beforeEach(() => {
  resetPatchEvidence();
  file.mockReset().mockImplementation(async (_id: string, path: string) => ({ path, revision_id: "rev1", renderable: true, patch: "@@" }) as never);
});
afterEach(cleanup);

function show(actions = { confirm: vi.fn(), undo: vi.fn() }) {
  render(<ConfirmAllQuickChecks parts={PARTS} ownAnswer={2} apiReviewId="review-1" sessionId="s1" revisionId="rev1" actions={actions} />);
  return actions;
}

describe("Confirm all quick checks", () => {
  it("asks first, naming the count and what is left out, then confirms every offered part in one action and can undo it", async () => {
    const marked = PARTS.map((p) => ({ unit_key: p.unit_key, event_id: `e-${p.target_id}` }));
    const result = { marked, single: [], skipped: [] };
    const actions = show({ confirm: vi.fn().mockResolvedValue(result), undo: vi.fn().mockResolvedValue(true) });
    fireEvent.click(screen.getByRole("button", { name: "Confirm all 3 quick checks" }));
    expect(actions.confirm).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Confirm 3 quick checks?" })).toBeTruthy();
    expect(screen.getByTestId("confirm-all-quick-count").textContent).toBe("3 quick checks will be recorded as Looks good, under your name.");
    expect(screen.getByText("2 quick checks need their own answer and are not included.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm 3" }));
    await waitFor(() => expect(actions.confirm).toHaveBeenCalledWith(PARTS));
    expect((await screen.findByTestId("confirm-all-quick-status")).textContent).toBe("3 quick checks confirmed as Looks good");
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(actions.undo).toHaveBeenCalledWith(result));
    expect((await screen.findByText("3 quick checks undone."))).toBeTruthy();
  });

  it("Cancel records nothing", () => {
    const actions = show();
    fireEvent.click(screen.getByRole("button", { name: "Confirm all 3 quick checks" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(actions.confirm).not.toHaveBeenCalled();
  });

  it("leaves out a part whose code could not be read here, as Quick check's batch does", async () => {
    file.mockImplementation(async (_id: string, path: string) => {
      if (path === "src/b.py") throw new Error("offline");
      return { path, revision_id: "rev1", renderable: true, patch: "@@" } as never;
    });
    const actions = show({ confirm: vi.fn().mockResolvedValue({ marked: [{ unit_key: "sym:a", event_id: "1" }, { unit_key: "sym:c", event_id: "2" }], single: [], skipped: [] }), undo: vi.fn() });
    fireEvent.click(screen.getByRole("button", { name: "Confirm all 3 quick checks" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm 3" }));
    await waitFor(() => expect(actions.confirm).toHaveBeenCalledWith([PARTS[0], PARTS[2]]));
    expect((await screen.findByTestId("confirm-all-quick-status")).textContent).toMatch(/the code of 1 could not be read here/);
  });

  it("says so when nothing was saved", async () => {
    show({ confirm: vi.fn().mockResolvedValue(null), undo: vi.fn() });
    fireEvent.click(screen.getByRole("button", { name: "Confirm all 3 quick checks" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm 3" }));
    expect((await screen.findByTestId("confirm-all-quick-status")).textContent).toBe("The quick checks could not be confirmed. Nothing was recorded.");
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  });
});
