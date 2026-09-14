import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import FinishSheet from "./FinishSheet";
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

describe("FinishSheet", () => {
  it("explains the exact target partition and keeps history separate from current risk", () => {
    render(<FinishSheet summary={summary()} busy={false} onClose={vi.fn()} onFinish={vi.fn()} />);

    expect(screen.getByText("6/10")).toBeTruthy();
    expect(screen.getByText("Reviewed")).toBeTruthy();
    expect(screen.getByText("Unreviewed")).toBeTruthy();
    expect(screen.getByText("Changed since review")).toBeTruthy();
    expect(screen.getByText("Needs changes")).toBeTruthy();
    expect(screen.getByText("Unknown identity")).toBeTruthy();
    expect(screen.getByText("Outstanding risk")).toBeTruthy();
    expect(screen.getByText("Review history")).toBeTruthy();
    expect(screen.getByText(/4 targets still need judgment/)).toBeTruthy();
    expect(screen.getByText(/6 unresolved risk signals/)).toBeTruthy();
  });

  it("uses a plain finish action when nothing is outstanding", () => {
    render(
      <FinishSheet
        summary={summary({
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
          previous_revision_evidence: 0,
          discarded_verdicts: 0,
        })}
        busy={false}
        onClose={vi.fn()}
        onFinish={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Finish review" })).toBeTruthy();
    expect(screen.getByText(/Nothing outstanding/)).toBeTruthy();
  });

  it("floors completion so only a fully reviewed revision reads as 100%", () => {
    render(
      <FinishSheet
        summary={summary({
          target_count: 200,
          reviewed_targets: 199,
          unreviewed_targets: 1,
          changed_since_review: 0,
          needs_changes: 0,
          unknown_targets: 0,
          open_comments: 0,
          orphaned_comments: 0,
          failed_verification: 0,
          unresolved_verification: 0,
          previous_revision_evidence: 0,
          discarded_verdicts: 0,
        })}
        busy={false}
        onClose={vi.fn()}
        onFinish={vi.fn()}
      />,
    );
    expect(screen.getByText(/targets reviewed · 99%/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Finish with outstanding work" })).toBeTruthy();
  });

  it("withholds the plain finish action when a risk signal outlives the last unreviewed target", () => {
    // Boundary the other cases miss: every target is judged, so only the risk half of the
    // completeness test can keep the review outstanding. This case says nothing about the
    // percentage rounding — 10/10 reads 100% under both floor and round.
    render(
      <FinishSheet
        summary={summary({
          target_count: 10,
          reviewed_targets: 10,
          unreviewed_targets: 0,
          changed_since_review: 0,
          needs_changes: 0,
          unknown_targets: 0,
          open_comments: 0,
          orphaned_comments: 0,
          failed_verification: 1,
          unresolved_verification: 0,
          previous_revision_evidence: 0,
          discarded_verdicts: 0,
        })}
        busy={false}
        onClose={vi.fn()}
        onFinish={vi.fn()}
      />,
    );
    expect(screen.getByText(/0 targets still need judgment · 1 unresolved risk signal\./)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Finish with outstanding work" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Finish review" })).toBeNull();
    expect(screen.queryByText(/Nothing outstanding/)).toBeNull();
  });

  it("focuses its close control and supports Escape without finishing", async () => {
    const onFinish = vi.fn();
    const onClose = vi.fn();
    render(<FinishSheet summary={summary()} busy={false} onClose={onClose} onFinish={onFinish} />);
    const close = screen.getByRole("button", { name: "Close finish review" });
    await waitFor(() => expect(document.activeElement).toBe(close));
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onFinish).not.toHaveBeenCalled();
  });

  it("traps Tab inside the finish dialog", async () => {
    render(<FinishSheet summary={summary()} busy={false} onClose={vi.fn()} onFinish={vi.fn()} />);
    const close = screen.getByRole("button", { name: "Close finish review" });
    const last = screen.getByRole("button", { name: "Finish with outstanding work" });
    await waitFor(() => expect(document.activeElement).toBe(close));
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(last);
    await userEvent.tab();
    expect(document.activeElement).toBe(close);
  });

  it("does not mutate until the explicit finish action is pressed", async () => {
    const onFinish = vi.fn();
    const onClose = vi.fn();
    render(<FinishSheet summary={summary()} busy={false} onClose={onClose} onFinish={onFinish} />);
    expect(onFinish).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Continue reviewing" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onFinish).not.toHaveBeenCalled();
  });
});
