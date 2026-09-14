import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import ReviewTargetHeader from "./ReviewTargetHeader";
import type { ReviewTarget } from "./types";

const TARGET: ReviewTarget = {
  target_id: "target:checkout",
  unit_key: "sym:checkout",
  kind: "symbol",
  path: "src/checkout.ts",
  label: "src/checkout.ts::Checkout",
  symbol: "Checkout",
  start_line: 10,
  end_line: 20,
  hunk_ordinals: [0],
  spans: [{ side: "new", start_line: 12, end_line: 12, hunk_ordinal: 0 }],
  state: "unreviewed",
  changed_since_mark: false,
  reviewed_revision_id: "",
  attention_rank: 1,
  attention_level: "high",
  reasons: ["public contract changed"],
  additions: 2,
  deletions: 1,
  fingerprint_method: "symbol_body_sha256",
  verification: { pass: 0, fail: 0, unknown: 0 },
  annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
};

describe("ReviewTargetHeader accessibility", () => {
  it("names the target group and every primary review action", () => {
    render(
      <ReviewTargetHeader
        target={TARGET}
        active
        busy={false}
        onFocus={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );

    expect(screen.getByRole("group", { name: "Checkout review target" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Mark Checkout reviewed" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Mark Checkout needs changes" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Comment on Checkout" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open context for Checkout" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Checkout symbol/i }).getAttribute("aria-current")).toBe("true");
  });

  it("does not change the active review target merely because the pointer crossed it", () => {
    const onFocus = vi.fn();
    render(
      <ReviewTargetHeader
        target={TARGET}
        active={false}
        busy={false}
        onFocus={onFocus}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );

    const group = screen.getByRole("group", { name: "Checkout review target" });
    fireEvent.mouseEnter(group);
    expect(onFocus).not.toHaveBeenCalled();

    fireEvent.focus(screen.getByRole("button", { name: /Checkout symbol/i }));
    expect(onFocus).toHaveBeenCalledTimes(1);
  });

  it("does not present a disjoint fallback as one contiguous line range", () => {
    const fallback: ReviewTarget = {
      ...TARGET,
      target_id: "target:fallback",
      unit_key: "hun:fallback",
      kind: "hunk",
      symbol: "",
      start_line: 1,
      end_line: 877,
      spans: [
        { side: "new", start_line: 1, end_line: 67, hunk_ordinal: 0 },
        { side: "new", start_line: 664, end_line: 877, hunk_ordinal: 0 },
      ],
    };
    render(
      <ReviewTargetHeader
        target={fallback}
        active
        busy={false}
        onFocus={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );

    expect(screen.getByRole("group", { name: "Uncovered changes · 2 regions review target" })).toBeTruthy();
    expect(screen.queryByText(/Lines 1–877/)).toBeNull();
  });
});
