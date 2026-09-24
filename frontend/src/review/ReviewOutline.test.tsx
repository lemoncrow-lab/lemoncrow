import { createRef } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReviewOutline from "./ReviewOutline";
import type { ReviewOutlineItem } from "./types";

const ROWS: ReviewOutlineItem[] = [
  {
    path: "src/a.py",
    target_count: 2,
    reviewed: 0,
    changed_since_review: 1,
    needs_changes: 0,
    unreviewed: 1,
    unknown: 0,
    mechanical: 0,
    attention_rank: 1,
    reasons: ["public contract changed"],
  },
  {
    path: "src/b.py",
    target_count: 1,
    reviewed: 0,
    changed_since_review: 0,
    needs_changes: 0,
    unreviewed: 1,
    unknown: 0,
    mechanical: 0,
    attention_rank: 2,
    reasons: ["+1 -0"],
  },
];

describe("ReviewOutline accessibility", () => {
  it("exposes section expansion and the active file semantically", async () => {
    render(
      <ReviewOutline
        rows={ROWS}
        activePath="src/a.py"
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    expect(screen.getByRole("complementary", { name: "Review outline" })).toBeTruthy();
    const changed = screen.getByRole("button", { name: /Changed since review/i });
    expect(changed.getAttribute("aria-expanded")).toBe("true");
    const activeFile = screen.getByRole("button", { name: /a.py/i });
    expect(activeFile.getAttribute("aria-current")).toBe("location");
    expect(activeFile.querySelector('[data-file-icon="a.py"]')).toBeTruthy();

    await userEvent.click(changed);
    expect(changed.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: /a.py/i })).toBeNull();
  });

  it("shows a compact change story and live review-first jump", async () => {
    const onSelectPath = vi.fn();
    const onSelectTarget = vi.fn();
    const onNavigateAttention = vi.fn();
    const onShowOverview = vi.fn();
    render(
      <ReviewOutline
        rows={ROWS}
        activePath="src/a.py"
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        storySteps={[
          { label: "Identity", path: "src/a.py" },
          { label: "API", path: "src/b.py" },
        ]}
        storyMore={2}
        nextAttention={{
          targetId: "target-b",
          path: "src/b.py",
          label: "authorize",
          reason: "Changed since your last review",
        }}
        attentionCount={4}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={onSelectPath}
        onSelectTarget={onSelectTarget}
        onNavigateAttention={onNavigateAttention}
        onShowOverview={onShowOverview}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    expect(screen.getByTestId("review-guide")).toBeTruthy();
    expect(screen.getByText("Change story")).toBeTruthy();
    expect(screen.getByText("Review first")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "API" }));
    expect(onSelectPath).toHaveBeenCalledWith("src/b.py");

    await userEvent.click(screen.getByRole("button", { name: "Review first: authorize" }));
    expect(onSelectTarget).toHaveBeenCalledWith("target-b");

    await userEvent.click(screen.getByRole("button", { name: "Next attention target" }));
    await userEvent.click(screen.getByRole("button", { name: "Previous attention target" }));
    expect(onNavigateAttention).toHaveBeenNthCalledWith(1, 1);
    expect(onNavigateAttention).toHaveBeenNthCalledWith(2, -1);

    await userEvent.click(screen.getByRole("button", { name: "Show 2 more change areas" }));
    expect(onShowOverview).toHaveBeenCalledTimes(1);
  });

  it("omits orientation when a small review would only repeat the file list", () => {
    render(
      <ReviewOutline
        rows={ROWS.slice(0, 1)}
        activePath="src/a.py"
        query=""
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={0}
        matchFileCount={0}
        matchCountsByPath={new Map()}
        nextAttention={{
          targetId: "target-a",
          path: "src/a.py",
          label: "authorize",
          reason: "public contract changed",
        }}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        onSelectTarget={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    expect(screen.queryByTestId("review-guide")).toBeNull();
  });

  it("hides orientation while an explicit search is active", () => {
    render(
      <ReviewOutline
        rows={ROWS}
        activePath="src/a.py"
        query="contract"
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={1}
        matchFileCount={1}
        matchCountsByPath={new Map([["src/a.py", 1]])}
        storySteps={[
          { label: "Identity", path: "src/a.py" },
          { label: "API", path: "src/b.py" },
        ]}
        nextAttention={{
          targetId: "target-a",
          path: "src/a.py",
          label: "authorize",
          reason: "public contract changed",
        }}
        onQuery={vi.fn()}
        onNavigateMatch={vi.fn()}
        onDismissSearch={vi.fn()}
        onSelectPath={vi.fn()}
        onSelectTarget={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    expect(screen.queryByTestId("review-guide")).toBeNull();
  });

  it("shows target/file match counts and uses Enter, Shift+Enter, and Escape for search flow", async () => {
    const onNavigateMatch = vi.fn();
    const onDismissSearch = vi.fn();
    render(
      <ReviewOutline
        rows={ROWS.slice(0, 1)}
        activePath="src/a.py"
        query="contract"
        searchInputRef={createRef<HTMLInputElement>()}
        matchTargetCount={2}
        matchFileCount={1}
        matchCountsByPath={new Map([["src/a.py", 2]])}
        onQuery={vi.fn()}
        onNavigateMatch={onNavigateMatch}
        onDismissSearch={onDismissSearch}
        onSelectPath={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
      />,
    );

    expect(screen.getByText("2 targets · 1 file")).toBeTruthy();
    expect(screen.getByText("2 matches")).toBeTruthy();
    const input = screen.getByRole("textbox", { name: "Search review" });
    expect(input.classList.contains("review-outline-search-field")).toBe(true);
    await userEvent.click(input);
    await userEvent.keyboard("{Enter}");
    await userEvent.keyboard("{Shift>}{Enter}{/Shift}");
    await userEvent.keyboard("{Escape}");

    expect(onNavigateMatch).toHaveBeenNthCalledWith(1, 1);
    expect(onNavigateMatch).toHaveBeenNthCalledWith(2, -1);
    expect(onDismissSearch).toHaveBeenCalledTimes(1);
  });
});
