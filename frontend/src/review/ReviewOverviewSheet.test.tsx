import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReviewOverviewSheet from "./ReviewOverviewSheet";
import type { ReviewOverview, ReviewSurfaceInfo } from "./types";

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
  chapters: {
    intent: [
      {
        key: "intent:frontend",
        label: "Frontend",
        reason: "repository structure",
        rows: [{ path: "frontend/src/App.tsx" }],
        file_count: 3,
        attention_count: 1,
        reviewed_count: 1,
        changed_count: 0,
        min_attention_rank: 1,
        depends_on: [],
      },
      {
        key: "intent:tests",
        label: "Tests",
        reason: "repository structure",
        rows: [{ path: "tests/review.test.ts" }],
        file_count: 2,
        attention_count: 0,
        reviewed_count: 0,
        changed_count: 0,
        min_attention_rank: 0,
        depends_on: [],
      },
    ],
    dependency: [],
    commits: [],
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


  it("restores intent change areas as direct review navigation", async () => {
    const onClose = vi.fn();
    const onSelectPath = vi.fn();
    render(<ReviewOverviewSheet overview={OVERVIEW} onClose={onClose} onSelectPath={onSelectPath} />);

    expect(screen.getByText("Change areas")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open Frontend change area" }).textContent).toContain("3 files");
    expect(screen.getByRole("button", { name: "Open Frontend change area" }).textContent).toContain("1 attention");
    expect(screen.getByRole("button", { name: "Open Tests change area" }).textContent).toContain("2 files");

    await userEvent.click(screen.getByRole("button", { name: "Open Tests change area" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSelectPath).toHaveBeenCalledWith("tests/review.test.ts");
  });

  it("closes and selects the major change start path", async () => {
    const onClose = vi.fn();
    const onSelectPath = vi.fn();
    render(<ReviewOverviewSheet overview={OVERVIEW} onClose={onClose} onSelectPath={onSelectPath} />);
    await userEvent.click(screen.getByRole("button", { name: /focused reader/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSelectPath).toHaveBeenCalledWith("src/review.ts");
  });

  it("keeps surfaces as compact navigation instead of duplicating execution UI", async () => {
    const onClose = vi.fn();
    const onSelectPath = vi.fn();
    const onSelectSurface = vi.fn();
    const surfaces: ReviewSurfaceInfo[] = [{
      id: "/",
      provider: "web",
      kind: "web.route",
      title: "Storefront",
      locator: "/",
      runtime: "frontend",
      affected_paths: ["src/Hero.tsx", "src/style.css"],
      capabilities: ["preview", "compare", "execute"],
      metadata: {},
    }];

    render(
      <ReviewOverviewSheet
        overview={OVERVIEW}
        surfaces={surfaces}
        onClose={onClose}
        onSelectPath={onSelectPath}
        onSelectSurface={onSelectSurface}
      />,
    );

    expect(screen.getByText("Product surfaces")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Run" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Compare" })).toBeNull();

    const surfaceButton = screen.getByRole("button", { name: "Open / surface" });
    expect(surfaceButton.textContent).toContain("2 files");
    const majorChangeButton = screen.getByRole("button", { name: /focused reader/i });
    expect(majorChangeButton.compareDocumentPosition(surfaceButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await userEvent.click(surfaceButton);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSelectSurface).toHaveBeenCalledWith("web:/");
    expect(onSelectPath).not.toHaveBeenCalled();
  });
});
