import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { GitHubPublishPreview } from "./types";

const api = vi.hoisted(() => ({
  previewGitHubPublish: vi.fn(),
  publishGitHubReview: vi.fn(),
}));
vi.mock("./reviewApi", () => api);

import GitHubPublishSheet from "./GitHubPublishSheet";

const preview: GitHubPublishPreview = {
  pr: { owner: "acme", repo: "widgets", number: 7, url: "https://github.com/acme/widgets/pull/7" },
  commit_id: "a".repeat(40),
  event: "APPROVE",
  summary: "Ship it",
  body: "Ship it\n\nHuman review. Nothing here was decided by an AI.",
  inline: [{ annotation_id: "ann-1", path: "src/app.py", side: "RIGHT", start_line: 3, line: 3, body: "why three?" }],
  folded: [{ annotation_id: "ann-2", location: "src/app.py:L25", body: "cleanup", reason: "outside the lines GitHub shows in the diff" }],
  notices: [],
  plan_hash: "plan-1",
  review_id: "review-1",
  revision_id: "rrv-1",
  operation_id: "gop-1",
  head_sha: "a".repeat(40),
  stale: false,
  empty: false,
};

afterEach(() => vi.clearAllMocks());

describe("GitHubPublishSheet", () => {
  it("shows exactly what will be posted and publishes that preview", async () => {
    api.previewGitHubPublish.mockResolvedValue(preview);
    api.publishGitHubReview.mockResolvedValue({
      state: "sent",
      operation_id: "gop-1",
      remote_ref: "github-review:100",
      html_url: "https://github.com/acme/widgets/pull/7#pullrequestreview-100",
      adopted: false,
      event: "APPROVE",
      inline: 1,
      folded: 1,
    });
    render(<GitHubPublishSheet reviewId="review-1" onClose={() => {}} />);

    expect(await screen.findByText("src/app.py:L3")).toBeTruthy();
    expect(screen.getByText("Approve")).toBeTruthy();
    expect(screen.getByTestId("github-sheet-body").textContent).toBe("Ship it\n\nHuman review. Nothing here was decided by an AI.");
    expect(screen.getByText(/outside the lines GitHub shows/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Publish to GitHub" }));

    await waitFor(() => expect(api.publishGitHubReview).toHaveBeenCalledWith("review-1", preview));
    expect(await screen.findByText(/Published: Approve, 1 inline, 1 in the review body/)).toBeTruthy();
  });

  it("shows the human-review line GitHub will receive under the reviewer's name, and no hidden marker", async () => {
    api.previewGitHubPublish.mockResolvedValue({ ...preview, summary: "", body: "Human review. Nothing here was decided by an AI." });
    render(<GitHubPublishSheet reviewId="review-1" onClose={() => {}} />);

    expect((await screen.findByTestId("github-sheet-body")).textContent).toBe("Human review. Nothing here was decided by an AI.");
    expect(document.body.textContent).not.toContain("lemoncrow:op=");
  });

  it("will not publish a stale or empty preview", async () => {
    api.previewGitHubPublish.mockResolvedValue({ ...preview, stale: true });
    render(<GitHubPublishSheet reviewId="review-1" onClose={() => {}} />);

    expect(await screen.findByText(/has new commits/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Publish to GitHub" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows GitHub's refusal instead of a result", async () => {
    api.previewGitHubPublish.mockRejectedValue(new Error("`gh` is not signed in; run `gh auth login`"));
    render(<GitHubPublishSheet reviewId="review-1" onClose={() => {}} />);

    expect((await screen.findByRole("alert")).textContent).toContain("gh auth login");
  });
});
