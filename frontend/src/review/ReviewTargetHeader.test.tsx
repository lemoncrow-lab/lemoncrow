import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import ReviewTargetHeader, { partSummary } from "./ReviewTargetHeader";
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
  it("names the part group and every primary review action", () => {
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

    expect(screen.getByRole("group", { name: "Checkout part" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Mark Checkout reviewed" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Mark Checkout needs changes" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Comment on Checkout" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open context for Checkout" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Checkout" }).getAttribute("aria-current")).toBe("true");
    expect(screen.getByText("· lines 10–20 · modified")).toBeTruthy();
  });

  it("shows nearby module declarations as one review action", () => {
    render(
      <ReviewTargetHeader
        target={{
          ...TARGET,
          kind: "document_section",
          symbol: "",
          label: "src/checkout.ts::Imports and top-level code",
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

    expect(screen.getByRole("group", { name: "Imports and top-level code part" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Mark Imports and top-level code reviewed" })).toBeTruthy();
    expect(screen.getByText("· lines 48–62 · modified")).toBeTruthy();
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

    let group = screen.getByRole("group", { name: "Checkout part" });
    expect(group.className).toContain("border-border");
    expect(group.className).toContain("bg-muted");
    expect(group.className).toContain("border-l-p0-solid");

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

    group = screen.getByRole("group", { name: "Checkout part" });
    expect(group.className).toContain("border-border");
    expect(group.className).toContain("bg-muted");
    expect(group.className).toContain("border-l-p2-solid");
  });

  it("does not change the active part merely because the pointer crossed it", () => {
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

    const group = screen.getByRole("group", { name: "Checkout part" });
    fireEvent.mouseEnter(group);
    expect(onFocus).not.toHaveBeenCalled();

    fireEvent.focus(screen.getByRole("button", { name: "Checkout" }));
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

    const group = screen.getByRole("group", { name: "Checkout part" });
    expect(group.className).toContain("bg-muted");
    expect(group.className).toContain("border-l-ok-solid");
    expect(screen.getByTestId("target-state-badge").textContent).toBe("Reviewed");
    expect(screen.getByText("reviewed", { selector: ".sr-only" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Mark Checkout reviewed" }).textContent).toContain("Reviewed");
    expect(screen.getByRole("button", { name: "Mark Checkout reviewed" })).toHaveAttribute("aria-pressed", "true");
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

    expect(screen.getByRole("group", { name: "Other changed lines · 2 places part" })).toBeTruthy();
    expect(screen.getByText("· lines 1–67, 664–877 · modified")).toBeTruthy();
    expect(screen.queryByText(/lines 1–877/)).toBeNull();
  });
});

describe("ReviewTargetHeader compact summary", () => {
  it("uses one format for every kind of part", () => {
    expect(partSummary(TARGET)).toBe("lines 10–20 · modified");
    expect(partSummary({ ...TARGET, additions: 11, deletions: 0, spans: [{ side: "new", start_line: 10, end_line: 20, hunk_ordinal: 0 }] }))
      .toBe("lines 10–20 · new");
    expect(partSummary({ ...TARGET, additions: 2, deletions: 0 })).toBe("lines 10–20 · lines added");
    expect(partSummary({
      ...TARGET,
      spans: [
        { side: "new", start_line: 12, end_line: 12, hunk_ordinal: 0 },
        { side: "new", start_line: 18, end_line: 19, hunk_ordinal: 0 },
      ],
    })).toBe("lines 10–20 · modified in 2 places");
    // A hunk whose label already names its lines does not repeat them.
    expect(partSummary({ ...TARGET, kind: "hunk", symbol: "", spans: [{ side: "new", start_line: 3, end_line: 5, hunk_ordinal: 0 }] }))
      .toBe("modified");
  });

  it("wraps a long name and reason instead of truncating them", () => {
    render(
      <ReviewTargetHeader
        target={{
          ...TARGET,
          symbol: "can_view_order_for_a_very_long_tenant_scoped_permission_check",
          priority: {
            version: 1,
            tier: 3,
            attention: "review_first",
            outstanding: true,
            position: 0,
            reasons: [{ code: "sensitive_path", detail: "Review first: authentication path changed in a shared helper" }],
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
    const name = screen.getByRole("button", { name: "can_view_order_for_a_very_long_tenant_scoped_permission_check" });
    const reason = screen.getByRole("button", { name: "Review first: authentication path changed in a shared helper" });
    for (const element of [name, name.firstElementChild!, reason, reason.parentElement!]) {
      expect(element.className).not.toMatch(/\btruncate\b/);
    }
    expect(screen.getByText("· lines 10–20 · modified").className).not.toMatch(/\btruncate\b/);
  });

  it("keeps a visual copy's controls out of the tab order", () => {
    render(
      <ReviewTargetHeader
        target={TARGET}
        active
        busy={false}
        visualCopy
        onFocus={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );
    for (const button of screen.getAllByRole("button")) expect(button).toHaveAttribute("tabindex", "-1");
  });

  it("shows a pinned signature line with its number", () => {
    render(
      <ReviewTargetHeader
        target={TARGET}
        active
        busy={false}
        signature={{ line: 10, text: "  class Checkout:" }}
        onFocus={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );
    const signature = screen.getByTestId("target-signature");
    expect(signature.textContent).toBe("10class Checkout:");
    expect(within(signature).getByLabelText("line 10")).toBeTruthy();
  });
});

describe("ReviewTargetHeader mark actions", () => {
  function renderState(state: ReviewTarget["state"], onMark = vi.fn()) {
    render(
      <ReviewTargetHeader
        target={{ ...TARGET, state }}
        active={false}
        busy={false}
        onFocus={vi.fn()}
        onMark={onMark}
        onComment={vi.fn()}
        onContext={vi.fn()}
      />,
    );
    return {
      reviewed: screen.getByRole("button", { name: "Mark Checkout reviewed" }),
      needs: screen.getByRole("button", { name: "Mark Checkout needs changes" }),
    };
  }

  it("shows visible text on both actions without hover, keeping their accessible names", () => {
    const onMark = vi.fn();
    const { reviewed, needs } = renderState("unreviewed", onMark);
    expect(within(reviewed).getByText("Reviewed")).toBeVisible();
    expect(within(needs).getByText("Needs changes")).toBeVisible();
    // Both words at every width: a lone "Changes" reads like "show changes".
    expect(needs.textContent).toBe("Needs changes");
    expect(within(needs).queryByText("Changes")).toBeNull();
    expect(reviewed).toHaveAttribute("aria-pressed", "false");
    expect(needs).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(reviewed);
    expect(onMark).toHaveBeenCalledWith("reviewed", true);
    fireEvent.click(needs);
    expect(onMark).toHaveBeenLastCalledWith("needs_changes");
  });

  it("marks the current judgment as pressed", () => {
    const { reviewed, needs } = renderState("needs_changes");
    expect(needs).toHaveAttribute("aria-pressed", "true");
    expect(reviewed).toHaveAttribute("aria-pressed", "false");
  });
});

describe("ReviewTargetHeader part states (plan §8.4)", () => {
  const STATES: Array<{
    state: ReviewTarget["state"];
    badge: string | null;
    spoken: string;
    tone: string | null;
    explanation: string | null;
    foldable: boolean;
    markedReviewed: boolean;
  }> = [
    { state: "reviewed", badge: "Reviewed", spoken: "reviewed", tone: "border-l-ok-solid", explanation: null, foldable: true, markedReviewed: true },
    { state: "needs_changes", badge: "Needs changes", spoken: "needs changes", tone: "border-l-p0-solid", explanation: null, foldable: true, markedReviewed: false },
    {
      state: "changed_since_review",
      badge: "Changed · re-review",
      spoken: "changed since review",
      tone: "border-l-p2-solid",
      explanation: "Changed since you reviewed · your earlier judgment no longer covers this part.",
      foldable: false,
      markedReviewed: false,
    },
    {
      state: "unknown",
      badge: "State uncertain",
      spoken: "unknown",
      tone: "border-l-p1-solid",
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

    const group = screen.getByRole("group", { name: "Checkout part" });
    expect(within(group).getByText(row.spoken, { selector: ".sr-only" })).toBeTruthy();
    const badge = within(group).queryByTestId("target-state-badge");
    expect(badge?.textContent ?? null).toBe(row.badge);
    if (row.tone) expect(group.className).toContain(row.tone);
    else expect(group.className).toContain("border-input");
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
    { state: "reviewed" as const, spoken: "reviewed", text: "· Reviewed", tone: "bg-muted" },
    { state: "needs_changes" as const, spoken: "needs changes", text: "· Needs changes", tone: "bg-p0-bg" },
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

    const strip = screen.getByRole("group", { name: `Checkout part · ${spoken} · folded` });
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
    expect(screen.getByRole("group", { name: "Checkout part" })).toBeTruthy();
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
