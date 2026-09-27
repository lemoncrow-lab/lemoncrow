import { reviewPlace } from "./Home";
import type { ReviewDirectoryRow } from "../review/types";

const row = (overrides: Partial<ReviewDirectoryRow>): ReviewDirectoryRow => ({
  id: "rev-1",
  ref: "r/abc",
  review_path: "/r/abc",
  repo_id: "repo_6bab3bd1e08710426daa8d09afdef3c9",
  title: "working tree",
  source_ref: "working_tree",
  status: "open",
  revision_number: 0,
  revision_id: "rrv-1",
  updated_at: "",
  created_at: "",
  ...overrides,
});

describe("Home review place", () => {
  it("names the project, a distinct worktree and the branch instead of the opaque repository id", () => {
    expect(
      reviewPlace(
        row({
          project: "lemoncrow-dev",
          worktree: "agentic-review",
          branch: "main",
        })
      )
    ).toBe("lemoncrow-dev · agentic-review · main");
    expect(
      reviewPlace(
        row({
          project: "lemoncrow-dev",
          worktree: "lemoncrow-dev",
          branch: "main",
        })
      )
    ).toBe("lemoncrow-dev · main");
    expect(reviewPlace(row({ directory: "demo" }))).toBe("demo");
  });

  it("falls back to the repository id only when nothing human is known", () => {
    expect(reviewPlace(row({}))).toBe("repo_6bab3bd1e08710426daa8d09afdef3c9");
  });
});
