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
    expect(screen.getByText("· covers lines 10–20")).toBeTruthy();
  });

  it("matches scope border and subtle header wash to the code change type", () => {
    const { rerender } = render(
      <ReviewTargetHeader
        target={{ ...TARGET, spans: [{ side: "old", start_line: 10, end_line: 20, hunk_ordinal: 0 }], additions: 0, deletions: 11 }}
        active
        busy={false}
        scopeTop
        onFocus={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );

    let group = screen.getByRole("group", { name: "Checkout review target" });
    expect(group.className).toContain("border-rose-700/45");
    expect(group.className).toContain("bg-rose-950/10");
    expect(group.className).not.toContain("border-emerald");

    rerender(
      <ReviewTargetHeader
        target={{ ...TARGET, spans: [
          { side: "old", start_line: 10, end_line: 20, hunk_ordinal: 0 },
          { side: "new", start_line: 10, end_line: 20, hunk_ordinal: 0 },
        ] }}
        active
        busy={false}
        scopeTop
        onFocus={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );

    group = screen.getByRole("group", { name: "Checkout review target" });
    expect(group.className).toContain("border-sky-700/35");
    expect(group.className).toContain("bg-sky-950/10");
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

  it("makes a durable reviewed judgment visually unmistakable", () => {
    render(
      <ReviewTargetHeader
        target={{ ...TARGET, state: "reviewed", attention_level: "normal", reasons: [] }}
        active={false}
        busy={false}
        onFocus={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );

    const group = screen.getByRole("group", { name: "Checkout review target" });
    expect(group.className).toContain("bg-emerald-950/15");
    expect(group.className).toContain("border-emerald-700/60");
    expect(screen.getByText("Reviewed")).toBeTruthy();
    expect(screen.getByText("reviewed", { selector: ".sr-only" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Mark Checkout reviewed" }).textContent).toContain("✓ Reviewed");
    expect(screen.getByRole("button", { name: "Mark Checkout reviewed" }).hasAttribute("disabled")).toBe(true);
  });

  it("surfaces an addressed thread as re-review work without implying it is resolved", () => {
    const onContext = vi.fn();
    render(
      <ReviewTargetHeader
        target={{ ...TARGET, annotation_counts: { open: 1, orphaned: 0, addressed_needs_rereview: 1 } }}
        active
        busy={false}
        onFocus={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={onContext}
      />,
    );

    const rereview = screen.getByRole("button", { name: "marked addressed · re-review" });
    fireEvent.click(rereview);
    expect(onContext).toHaveBeenCalledWith("discussion");
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
    expect(screen.getByText("· covers lines 1–67, 664–877")).toBeTruthy();
    expect(screen.queryByText(/covers lines 1–877/)).toBeNull();
  });
});
