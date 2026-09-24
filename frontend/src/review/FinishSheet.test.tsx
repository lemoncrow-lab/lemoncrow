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
  it("shows only current blockers by default and keeps previous-revision history collapsed", () => {
    render(<FinishSheet summary={summary()} busy={false} onClose={vi.fn()} onFinish={vi.fn()} />);

    expect(screen.getByText("6/10")).toBeTruthy();
    expect(screen.getByText("Before you finish")).toBeTruthy();
    expect(screen.getByText("Unreviewed")).toBeTruthy();
    expect(screen.getByText("Changed since review")).toBeTruthy();
    expect(screen.getByText("Needs changes")).toBeTruthy();
    expect(screen.getByText("Unknown identity")).toBeTruthy();
    expect(screen.getByText("Open comments")).toBeTruthy();
    const history = screen.getByText("Previous review history").closest("details") as HTMLDetailsElement;
    expect(history.open).toBe(false);
    expect(screen.getByText("Previous-revision evidence")).toBeTruthy();
    expect(screen.getByText(/4 targets and 6 risk signals still need attention/)).toBeTruthy();
    expect(screen.getByText(/6 risk signals still need attention/)).toBeTruthy();
  });

  it("shows the same named evidence facts at finish", () => {
    render(
      <FinishSheet
        summary={summary()}
        evidenceFacts={[
          { id: "unit", label: "Unit tests", detail: "42 passed", source: "pytest", state: "verified" },
          { id: "api", label: "API contract", detail: "response changed", source: "integration", state: "failed" },
          { id: "stale", label: "1 stale artifact", detail: "Older screenshot", source: "artifact", state: "stale" },
        ]}
        busy={false}
        onClose={vi.fn()}
        onFinish={vi.fn()}
      />,
    );

    expect(screen.getByText("Evidence at finish")).toBeTruthy();
    expect(screen.getByText("Unit tests")).toBeTruthy();
    expect(screen.getByText("API contract")).toBeTruthy();
    expect(screen.getByText("1 stale artifact")).toBeTruthy();
    expect(screen.getByText("PASS")).toBeTruthy();
    expect(screen.getByText("FAIL")).toBeTruthy();
    expect(screen.getByText("STALE")).toBeTruthy();
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
    expect(screen.getByText(/No current blockers/)).toBeTruthy();
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
    expect(screen.getByText("· 99%")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Finish anyway" })).toBeTruthy();
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
    expect(screen.getByText(/0 targets and 1 risk signal still need attention./)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Finish anyway" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Finish review" })).toBeNull();
    expect(screen.queryByText(/No current blockers/)).toBeNull();
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
    const last = screen.getByRole("button", { name: "Finish anyway" });
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

  it("records the selected reviewer verdict only with the explicit finish action", async () => {
    const onFinish = vi.fn();
    render(<FinishSheet summary={summary()} busy={false} onClose={vi.fn()} onFinish={onFinish} />);

    await userEvent.click(screen.getByRole("radio", { name: "LGTM" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Review outcome summary" }), "Ship it.");
    expect(onFinish).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Finish anyway" }));
    expect(onFinish).toHaveBeenCalledWith("lgtm", "Ship it.");
  });

  it("does not silently carry an older-revision verdict into the current finish choice", () => {
    render(
      <FinishSheet
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
    expect(screen.getByRole("radio", { name: "Comment" }).getAttribute("aria-checked")).toBe("true");
  });
});
