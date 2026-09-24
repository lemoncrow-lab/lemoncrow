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
          id: "rev-open",
          review_path: "/reviews/rev-open",
          repo_id: "repo-1",
          title: "Open checkout change",
          source_ref: "client:working_tree",
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
    expect(screen.getAllByText("repo-1").length).toBeGreaterThan(0);
    expect(screen.getByText("2 left")).toBeTruthy();
    expect(screen.getByText(/3\/5 reviewed/)).toBeTruthy();
    expect(screen.getAllByText("Open checkout change")[0].closest("a")?.getAttribute("href")).toBe(
      "/reviews/rev-open#token",
    );
    expect(screen.getByTestId("review-directory")).toBeTruthy();
  });
});
