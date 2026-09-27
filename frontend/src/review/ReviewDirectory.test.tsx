import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewDirectory from "./ReviewDirectory";

const api = vi.hoisted(() => ({ fetchReviewDirectory: vi.fn() }));
vi.mock("./reviewApi", () => ({
  adoptBootstrapFragment: () => ({ token: "secret", reviewId: "" }),
  fetchReviewDirectory: api.fetchReviewDirectory,
  reviewHref: (id: string) => `/reviews/${id}#token`,
}));

describe("ReviewDirectory", () => {
  beforeEach(() => {
    api.fetchReviewDirectory.mockReset();
    api.fetchReviewDirectory.mockResolvedValue({
      reviews: [
        {
          id: "rev-legacy",
          review_path: "/reviews/rev-legacy",
          repo_id: "repo_opaque_hash",
          title: "Legacy change",
          source_ref: "client:working_tree",
          range_mode: "working_tree",
          status: "open",
          revision_number: 0,
          revision_id: "rr-legacy",
          progress: {
            target_count: 1, reviewed: 0, changed_since_review: 0, needs_changes: 0, unreviewed: 1, unknown: 0, mechanical: 0,
          },
          updated_at: "2026-09-15T20:05:00Z",
          created_at: "2026-09-15T19:05:00Z",
        },
        {
          id: "rev-open",
          review_path: "/reviews/rev-open",
          repo_id: "repo-1",
          title: "Open checkout change",
          source_ref: "client:working_tree",
          range_mode: "working_tree",
          directory: "/home/me/Projects/leanchain",
          project: "lemoncrow-dev",
          worktree: "agentic-review",
          worktree_root: "/home/me/Projects/leanchain/lemoncrow-dev/.lc-worktrees/agentic-review",
          branch: "feat/agentic-review",
          status: "open",
          revision_number: 3,
          revision_id: "rr-3",
          progress: {
            target_count: 5,
            reviewed: 3,
            changed_since_review: 0,
            needs_changes: 0,
            unreviewed: 2,
            unknown: 0,
            mechanical: 0,
          },
          updated_at: "2026-09-15T20:00:00Z",
          created_at: "2026-09-15T19:00:00Z",
        },
      ],
      status: "open",
      query: "",
      next_cursor: "",
    });
  });

  it("loads the shared review directory and preserves the local fragment on review links", async () => {
    render(<ReviewDirectory />);

    await waitFor(() => expect(screen.getAllByText("Open checkout change").length).toBeGreaterThan(0));
    expect(api.fetchReviewDirectory).toHaveBeenCalledTimes(1);
    expect(api.fetchReviewDirectory).toHaveBeenCalledWith({
      status: "open",
      query: "",
      cursor: "",
      limit: 50,
    });
    expect(screen.getAllByText("Working tree").length).toBe(2);
    expect(screen.queryByText("client:working_tree")).toBeNull();
    expect(screen.getByText("Workspace unavailable")).toBeTruthy();
    expect(screen.queryByText("repo_opaque_hash")).toBeNull();
    expect(screen.getByText("lemoncrow-dev")).toBeTruthy();
    expect(screen.getByText("agentic-review")).toBeTruthy();
    expect(screen.getByText("feat/agentic-review")).toBeTruthy();
    expect(screen.getByText("/home/me/Projects/leanchain")).toBeTruthy();
    expect(screen.getByText("2 left")).toBeTruthy();
    expect(screen.getByText(/3\/5 reviewed/)).toBeTruthy();
    expect(screen.getAllByText("Open checkout change")[0].closest("a")?.getAttribute("href")).toBe(
      "/reviews/rev-open#token",
    );
    expect(screen.getByTestId("review-directory")).toBeTruthy();
  });

  it("omits workspace facets that are unavailable instead of rendering placeholder dashes", async () => {
    api.fetchReviewDirectory.mockResolvedValueOnce({
      reviews: [{
        id: "rev-hinted",
        review_path: "/reviews/rev-hinted",
        repo_id: "repo-hidden",
        title: "Hinted workspace",
        source_ref: "client:working_tree",
        range_mode: "working_tree",
        directory: "/tmp",
        project: "demo",
        worktree: "demo",
        worktree_root: "/tmp/demo",
        branch: "",
        status: "open",
        revision_number: 0,
        revision_id: "rr-hinted",
        progress: {
          target_count: 1, reviewed: 0, changed_since_review: 0, needs_changes: 0, unreviewed: 1, unknown: 0, mechanical: 0,
        },
        updated_at: "2026-09-15T20:05:00Z",
        created_at: "2026-09-15T19:05:00Z",
      }],
      status: "open",
      query: "",
      next_cursor: "",
    });

    render(<ReviewDirectory />);
    await waitFor(() => expect(screen.getByText("Hinted workspace")).toBeTruthy());

    expect(screen.getAllByText("demo").length).toBe(2);
    expect(screen.queryByText("branch")).toBeNull();
    expect(screen.queryByText("—")).toBeNull();
  });
});
