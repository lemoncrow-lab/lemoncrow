import { fireEvent, render, screen, within } from "@testing-library/react";
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

describe("ReviewTargetHeader queue reason", () => {
  it("shows the queue's typed reason, never a ranker descriptor", () => {
    const onContext = vi.fn();
    render(
      <ReviewTargetHeader
        target={{
          ...TARGET,
          reasons: ["4 known callers", "public contract changed"],
          priority: {
            version: 1,
            tier: 3,
            attention: "review_first",
            outstanding: true,
            position: 0,
            reasons: [{ code: "check_failed_linked", detail: "1 check failing on this file" }],
          },
        }}
        active
        busy={false}
        onFocus={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={onContext}
      />,
    );
    expect(screen.queryByText("4 known callers")).toBeNull();
    expect(screen.queryByText("public contract changed")).toBeNull();
    fireEvent.click(screen.getByText("1 check failing on this file"));
    expect(onContext).toHaveBeenCalledWith("checks");
  });

  it("shows no attention reason for Understand work", () => {
    render(
      <ReviewTargetHeader
        target={{
          ...TARGET,
          priority: {
            version: 1,
            tier: 4,
            attention: "understand",
            outstanding: true,
            position: 0,
            reasons: [{ code: "unreviewed", detail: "Not reviewed yet" }],
          },
        }}
        active
        busy={false}
        onFocus={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );
    expect(screen.queryByText("public contract changed")).toBeNull();
  });
});

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

    expect(screen.getByRole("group", { name: "Checkout review area" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Mark Checkout reviewed" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Mark Checkout needs changes" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Comment on Checkout" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open context for Checkout" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Checkout symbol/i }).getAttribute("aria-current")).toBe("true");
    expect(screen.getByText("· covers lines 10–20")).toBeTruthy();
  });

  it("shows nearby module declarations as one review action", () => {
    render(
      <ReviewTargetHeader
        target={{
          ...TARGET,
          kind: "document_section",
          symbol: "",
          label: "src/checkout.ts::Module declarations",
          start_line: 48,
          end_line: 62,
          spans: [{ side: "new", start_line: 48, end_line: 62, hunk_ordinal: 0 }],
        }}
        active
        busy={false}
        onFocus={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );

    expect(screen.getByRole("group", { name: "Module declarations review area" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Mark Module declarations reviewed" })).toBeTruthy();
    expect(screen.getByText("· covers lines 48–62")).toBeTruthy();
  });

  it("uses a neutral compact header with a scope-tone left accent", () => {
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

    let group = screen.getByRole("group", { name: "Checkout review area" });
    expect(group.className).toContain("border-border/80");
    expect(group.className).toContain("bg-rose-950/8");
    expect(group.className).toContain("inset_2px_0_0_rgba(251,113,133,0.42)");

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

    group = screen.getByRole("group", { name: "Checkout review area" });
    expect(group.className).toContain("border-border/80");
    expect(group.className).toContain("bg-sky-950/8");
    expect(group.className).toContain("inset_2px_0_0_rgba(56,189,248,0.38)");
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

    const group = screen.getByRole("group", { name: "Checkout review area" });
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

    const group = screen.getByRole("group", { name: "Checkout review area" });
    expect(group.className).toContain("bg-emerald-950/15");
    expect(group.className).toContain("border-emerald-700/60");
    expect(screen.getByText("Reviewed")).toBeTruthy();
    expect(screen.getByText("reviewed", { selector: ".sr-only" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Mark Checkout reviewed" }).textContent).toContain("✓ Reviewed");
    expect(screen.getByRole("button", { name: "Mark Checkout reviewed" })).toHaveAttribute("aria-disabled", "true");
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

    expect(screen.getByRole("group", { name: "Changes outside detected symbols · 2 regions review area" })).toBeTruthy();
    expect(screen.getByText("· covers lines 1–67, 664–877")).toBeTruthy();
    expect(screen.queryByText(/covers lines 1–877/)).toBeNull();
  });
});

describe("ReviewTargetHeader target states (plan §8.4)", () => {
  const STATES: Array<{
    state: ReviewTarget["state"];
    badge: string | null;
    spoken: string;
    tone: string | null;
    explanation: string | null;
    foldable: boolean;
    markedReviewed: boolean;
  }> = [
    { state: "reviewed", badge: "Reviewed", spoken: "reviewed", tone: "bg-emerald-950/15", explanation: null, foldable: true, markedReviewed: true },
    { state: "needs_changes", badge: "Needs changes", spoken: "needs changes", tone: "bg-rose-950/15", explanation: null, foldable: true, markedReviewed: false },
    {
      state: "changed_since_review",
      badge: "Changed · re-review",
      spoken: "changed since review",
      tone: "bg-sky-950/15",
      explanation: "Changed since you reviewed · your earlier judgment no longer covers this review area.",
      foldable: false,
      markedReviewed: false,
    },
    {
      state: "unknown",
      badge: "State uncertain",
      spoken: "unknown",
      tone: "bg-amber-950/15",
      explanation: "Review state uncertain · LemonCrow could not safely carry your earlier judgment forward.",
      foldable: false,
      markedReviewed: false,
    },
    { state: "unreviewed", badge: null, spoken: "unreviewed", tone: null, explanation: null, foldable: false, markedReviewed: false },
  ];

  it.each(STATES)("renders $state with its badge, tone, explanation and actions", (row) => {
    const onMark = vi.fn();
    const onToggleFold = vi.fn();
    render(
      <ReviewTargetHeader
        target={{ ...TARGET, state: row.state, changed_since_mark: row.state === "changed_since_review" }}
        active
        busy={false}
        onToggleFold={onToggleFold}
        onFocus={vi.fn()}
        onMark={onMark}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );

    const group = screen.getByRole("group", { name: "Checkout review area" });
    expect(within(group).getByText(row.spoken, { selector: ".sr-only" })).toBeTruthy();
    for (const other of ["Reviewed", "Needs changes", "Changed · re-review", "State uncertain"]) {
      if (other === row.badge) expect(within(group).getByText(other)).toBeTruthy();
      else expect(within(group).queryByText(other)).toBeNull();
    }
    if (row.tone) expect(group.className).toContain(row.tone);
    else expect(group.className).toContain("border-sky-900/60");
    if (row.explanation) expect(within(group).getByText(row.explanation)).toBeTruthy();
    else expect(within(group).queryByText(/earlier judgment/)).toBeNull();

    const fold = within(group).queryByRole("button", { name: "Fold Checkout" });
    expect(Boolean(fold)).toBe(row.foldable);
    const markReviewed = within(group).getByRole("button", { name: "Mark Checkout reviewed" });
    if (row.markedReviewed) {
      expect(markReviewed).toHaveAttribute("aria-disabled", "true");
    } else {
      fireEvent.click(markReviewed);
      expect(onMark).toHaveBeenCalledWith("reviewed", true);
    }
    fireEvent.click(within(group).getByRole("button", { name: "Mark Checkout needs changes" }));
    expect(onMark).toHaveBeenLastCalledWith("needs_changes");
  });

  it("keeps an explanation for the active area only", () => {
    render(
      <ReviewTargetHeader
        target={{ ...TARGET, state: "changed_since_review", changed_since_mark: true }}
        active={false}
        busy={false}
        onFocus={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );
    expect(screen.getByText("Changed · re-review")).toBeTruthy();
    expect(screen.queryByText(/earlier judgment no longer covers/)).toBeNull();
  });

  it.each([
    { state: "reviewed" as const, spoken: "reviewed", text: "· Reviewed", tone: "bg-background/40" },
    { state: "needs_changes" as const, spoken: "needs changes", text: "· Needs changes", tone: "bg-rose-950/8" },
  ])("folds $state into a compact strip that expands on request", ({ state, spoken, text, tone }) => {
    const onFocus = vi.fn();
    const onToggleFold = vi.fn();
    const onMark = vi.fn();
    render(
      <ReviewTargetHeader
        target={{ ...TARGET, state, annotation_counts: { open: 1, orphaned: 0, addressed_needs_rereview: 1 } }}
        active={false}
        busy={false}
        folded
        onToggleFold={onToggleFold}
        onFocus={onFocus}
        onMark={onMark}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );

    const strip = screen.getByRole("group", { name: `Checkout review area · ${spoken} · folded` });
    expect(strip.dataset.reviewTargetFolded).toBe("true");
    expect(strip.className).toContain(tone);
    expect(within(strip).getByText(text)).toBeTruthy();
    expect(within(strip).getByText("· 2 comments")).toBeTruthy();
    // A folded strip offers no judgment actions until it is expanded.
    expect(within(strip).queryByRole("button", { name: /^Mark Checkout/ })).toBeNull();
    fireEvent.click(within(strip).getByRole("button", { name: "Expand Checkout" }));
    expect(onFocus).toHaveBeenCalledTimes(1);
    expect(onToggleFold).toHaveBeenCalledTimes(1);
    expect(onMark).not.toHaveBeenCalled();
  });

  it("never folds a comparison row", () => {
    render(
      <ReviewTargetHeader
        target={{ ...TARGET, state: "reviewed" }}
        active={false}
        busy={false}
        folded
        comparisonMode
        onFocus={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );
    expect(screen.getByRole("group", { name: "Checkout review area" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Expand Checkout" })).toBeNull();
    expect(screen.queryByText("Reviewed")).toBeNull();
  });

  it("names the reason that placed the area in Review first", () => {
    render(
      <ReviewTargetHeader
        target={{
          ...TARGET,
          priority: {
            version: 1,
            tier: 3,
            attention: "review_first",
            outstanding: true,
            position: 2,
            reasons: [
              { code: "author_guidance_conflict", detail: "Authors disagree about retention" },
              { code: "unreviewed", detail: "Not reviewed yet" },
            ],
          },
        }}
        active
        busy={false}
        onFocus={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Authors disagree about retention" })).toBeTruthy();
    expect(screen.queryByText("Not reviewed yet")).toBeNull();
  });
});
