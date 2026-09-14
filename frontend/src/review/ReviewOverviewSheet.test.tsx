import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReviewOverviewSheet from "./ReviewOverviewSheet";
import type { ReviewOverview } from "./types";

const OVERVIEW = {
  title: "Review",
  brief: {
    summary: "2 files across Review",
    themes: ["Review"],
    major_changes: [{
      key: "commit:abc",
      label: "feat(review): focused reader",
      file_count: 2,
      target_count: 5,
      attention_count: 1,
      first_path: "src/review.ts",
    }],
    review_first: [],
    verification: { pass: 1, fail: 0, not_run: 0, unknown: 0 },
    annotations: { human: 0, author: 0, lemoncrow: 0, ai_review: 0 },
    artifacts: { current: 1, stale: 0 },
  },
  provenance: { host: "claude", certainty: "exact", model: "sonnet" },
  revision: { provenance_host: "claude", provenance_certainty: "exact", provenance_model: "sonnet" },
} as unknown as ReviewOverview;

describe("ReviewOverviewSheet", () => {
  it("traps Tab inside the overview dialog", async () => {
    render(<ReviewOverviewSheet overview={OVERVIEW} onClose={vi.fn()} onSelectPath={vi.fn()} />);
    const close = screen.getByRole("button", { name: "Close change overview" });
    const change = screen.getByRole("button", { name: /focused reader/i });
    await waitFor(() => expect(document.activeElement).toBe(close));
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(change);
    await userEvent.tab();
    expect(document.activeElement).toBe(close);
  });

  it("closes and selects the major change start path", async () => {
    const onClose = vi.fn();
    const onSelectPath = vi.fn();
    render(<ReviewOverviewSheet overview={OVERVIEW} onClose={onClose} onSelectPath={onSelectPath} />);
    await userEvent.click(screen.getByRole("button", { name: /focused reader/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSelectPath).toHaveBeenCalledWith("src/review.ts");
  });
});
