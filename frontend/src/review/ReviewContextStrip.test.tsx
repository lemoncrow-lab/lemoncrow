import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Clock3, GitCompareArrows, TriangleAlert } from "lucide-react";
import { describe, expect, it, vi } from "vitest";

import ReviewContextStrip, { type ContextSignal } from "./ReviewContextStrip";

describe("ReviewContextStrip", () => {
  it("renders nothing when there is no exceptional context", () => {
    const { container } = render(<ReviewContextStrip signals={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("keeps one row: the first signal leads, the rest become chips", async () => {
    const openHistory = vi.fn();
    const signals: ContextSignal[] = [
      {
        id: "historical",
        tone: "amber",
        icon: Clock3,
        label: "Historical",
        chipLabel: "Historical",
        detail: "Snapshot 3 · read-only",
        testId: "historical-revision-banner",
        actions: <button type="button">Back to latest · Snapshot 5</button>,
      },
      { id: "source", tone: "sky", icon: GitCompareArrows, label: "New Snapshot available", chipLabel: "New Snapshot available · 2 files", onActivate: openHistory },
      {
        id: "failures",
        tone: "amber",
        icon: TriangleAlert,
        label: "Some review context is unavailable",
        chipLabel: "Some context unavailable",
        chip: <details><summary>Some context unavailable</summary><div>Review evidence unavailable: offline</div></details>,
      },
    ];
    render(<ReviewContextStrip signals={signals} />);

    const strip = screen.getByTestId("historical-revision-banner");
    expect(strip.getAttribute("role")).toBe("status");
    expect(within(strip).getByText("Historical")).toBeTruthy();
    expect(within(strip).getByRole("button", { name: "Back to latest · Snapshot 5" })).toBeTruthy();
    expect(within(strip).queryByText("Capture current source")).toBeNull();
    await userEvent.click(within(strip).getByRole("button", { name: "New Snapshot available · 2 files" }));
    expect(openHistory).toHaveBeenCalledTimes(1);
    expect(within(strip).getByText("Review evidence unavailable: offline")).toBeTruthy();
    expect(document.querySelectorAll(".review-context-strip")).toHaveLength(1);
  });

  it("announces blocking signals assertively", () => {
    render(
      <ReviewContextStrip
        signals={[{ id: "advanced", tone: "rose", icon: Clock3, role: "alert", label: "Snapshot updated elsewhere", chipLabel: "Snapshot updated elsewhere" }]}
      />,
    );
    const strip = screen.getByRole("alert");
    expect(strip.getAttribute("aria-live")).toBe("assertive");
    expect(strip.textContent).toContain("Snapshot updated elsewhere");
  });
});
