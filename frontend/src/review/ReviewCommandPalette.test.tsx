import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReviewCommandPalette, { type ReviewPaletteAction } from "./ReviewCommandPalette";
import ReviewOverviewSheet from "./ReviewOverviewSheet";
import type { ReviewOverview } from "./types";

const OVERVIEW = {
  title: "Review",
  brief: {
    summary: "2 files across Review",
    themes: ["Review"],
    major_changes: [],
    review_first: [],
    verification: { pass: 1, fail: 0, not_run: 0, unknown: 0 },
    annotations: { human: 0, author: 0, lemoncrow: 0, ai_review: 0 },
    artifacts: { current: 1, stale: 0 },
  },
  provenance: { host: "claude", certainty: "exact", model: "sonnet" },
  revision: { provenance_host: "claude", provenance_certainty: "exact", provenance_model: "sonnet" },
} as unknown as ReviewOverview;

function actions(): ReviewPaletteAction[] {
  return [
    { id: "refresh", label: "Refresh local revision", detail: "Accept latest source", run: vi.fn() },
    { id: "feedback", label: "Prepare feedback", detail: "Preview human feedback", run: vi.fn() },
    { id: "finish", label: "Finish review", run: vi.fn() },
  ];
}

describe("ReviewCommandPalette", () => {
  it("filters actions and executes the selected match with Enter", async () => {
    const rows = actions();
    const onClose = vi.fn();
    render(<ReviewCommandPalette mode="actions" actions={rows} onClose={onClose} />);

    const input = screen.getByRole("textbox", { name: "Filter review actions" });
    await waitFor(() => expect(document.activeElement).toBe(input));
    await userEvent.type(input, "feedback");

    expect(screen.queryByText("Refresh local revision")).toBeNull();
    expect(screen.getByText("Prepare feedback")).toBeTruthy();
    await userEvent.keyboard("{Enter}");

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(rows[1].run).toHaveBeenCalledTimes(1);
  });

  it("uses arrow keys to move the action selection", async () => {
    const rows = actions();
    render(<ReviewCommandPalette mode="actions" actions={rows} onClose={vi.fn()} />);

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Filter review actions" })));
    await userEvent.keyboard("{ArrowDown}{Enter}");

    expect(rows[0].run).not.toHaveBeenCalled();
    expect(rows[1].run).toHaveBeenCalledTimes(1);
  });

  it("traps Tab inside the action dialog", async () => {
    const rows = actions();
    render(<ReviewCommandPalette mode="actions" actions={rows} onClose={vi.fn()} />);

    const close = screen.getByRole("button", { name: "Close review commands" });
    const last = screen.getByRole("button", { name: "Finish review" });
    last.focus();
    await userEvent.tab();
    expect(document.activeElement).toBe(close);
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(last);
  });

  it("hands a dialog-opening action a usable dialog: focus inside it, Escape closes it", async () => {
    // The palette is a hand-off surface: an action can trade it for another dialog. The
    // user-visible contract is that the new dialog owns the screen — it is the modal the
    // opener's focus-restore guard can see, it holds focus, and Escape closes it.
    function Host() {
      const [paletteOpen, setPaletteOpen] = useState(true);
      const [overviewOpen, setOverviewOpen] = useState(false);
      const rows: ReviewPaletteAction[] = [
        { id: "overview", label: "Open change overview", run: () => setOverviewOpen(true) },
        { id: "refresh", label: "Refresh local revision", run: vi.fn() },
      ];
      return (
        <>
          <button type="button">Review actions</button>
          {paletteOpen && (
            <ReviewCommandPalette mode="actions" actions={rows} onClose={() => setPaletteOpen(false)} />
          )}
          {overviewOpen && (
            <ReviewOverviewSheet overview={OVERVIEW} onClose={() => setOverviewOpen(false)} onSelectPath={vi.fn()} />
          )}
        </>
      );
    }

    render(<Host />);
    const opener = screen.getByRole("button", { name: "Review actions" });
    opener.focus();

    await userEvent.click(screen.getByRole("button", { name: /Open change overview/ }));

    const overview = await screen.findByRole("dialog", { name: "Change overview" });
    expect(screen.queryByRole("dialog", { name: "Review actions" })).toBeNull();
    // Exactly one modal is on screen, and it is the new dialog: this is the marker the
    // palette owner's focus-restore guard keys on before it re-focuses the opener.
    const modals = Array.from(document.querySelectorAll('[aria-modal="true"]'));
    expect(modals).toHaveLength(1);
    expect(modals[0]).toBe(overview);
    await waitFor(() => expect(overview.contains(document.activeElement)).toBe(true));
    expect(document.activeElement).not.toBe(opener);

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Change overview" })).toBeNull());
  });

  it("shows the complete keyboard model and closes with Escape", async () => {
    const onClose = vi.fn();
    render(<ReviewCommandPalette mode="shortcuts" actions={actions()} onClose={onClose} />);

    expect(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeTruthy();
    for (const key of ["j / k", "J / K", "] / [", "r", "u", "x", "c", "s", "/", "f", "e", "p", "?"]) {
      expect(screen.getByText(key)).toBeTruthy();
    }
    expect(screen.getByText("Next / previous attention target")).toBeTruthy();
    expect(screen.getAllByText("Esc")).toHaveLength(2);

    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
