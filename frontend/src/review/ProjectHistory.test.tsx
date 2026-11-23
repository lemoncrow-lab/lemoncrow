import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ProjectHistory from "./ProjectHistory";
import type { ReviewProjectContext, ReviewProjectContextEntry } from "./types";

const fetchContext = vi.hoisted(() => vi.fn());
// A rejected promise stays out of the spy: Vitest fails a test on a spy's rejected result.
const reads = vi.hoisted(() => ({ fail: false }));
vi.mock("./reviewApi", () => ({
  fetchReviewProjectContext: (...args: unknown[]) =>
    reads.fail ? Promise.reject(new Error("offline")) : fetchContext(...args),
  reviewHref: (id: string) => `/r/${id}`,
}));

function entry(
  overrides: Partial<ReviewProjectContextEntry> = {}
): ReviewProjectContextEntry {
  return {
    entry_id: "e1",
    context_class: "project_knowledge",
    subject: { kind: "path", key: "docs/guide.md" },
    knowledge_kind: "documentation",
    summary: "Charges go through the gateway.",
    summary_truncated: false,
    state: "current",
    state_label: "Landed on main",
    freshness_label:
      "Landed on main · predates this Change, which edits docs/guide.md",
    stale_for_snapshot: true,
    support: "source_document",
    canonical_ref: "refs/heads/main",
    relevance: "changed_path",
    source_paths: ["docs/guide.md"],
    observed_at: "2026-11-01T00:00:00Z",
    history: { origin: null, superseded_by: null },
    citations: [
      {
        ref: "generation:1",
        kind: "generation",
        label: "Baseline observed",
        resolved: true,
      },
    ],
    ...overrides,
  };
}

function context(
  overrides: Partial<ReviewProjectContext> = {}
): ReviewProjectContext {
  return {
    purpose: "brief",
    available: true,
    reason: "",
    context_class: "project_knowledge",
    authority: "advisory",
    authority_note:
      "Landed project history. It cannot prove a fact about this revision.",
    current_generation: "g",
    entries: [entry()],
    narration: [],
    omitted: 0,
    truncated: false,
    filtered_by_content_access: 0,
    ...overrides,
  };
}

function answer(value: ReviewProjectContext) {
  fetchContext.mockResolvedValue({
    review_id: "r1",
    revision_id: "rev",
    revision_number: 2,
    project_context: value,
  });
}

beforeEach(() => {
  fetchContext.mockReset();
  reads.fail = false;
});
afterEach(cleanup);

describe("ProjectHistory", () => {
  it("labels landed history as background and links to its sources", async () => {
    answer(
      context({
        omitted: 2,
        entries: [
          entry({
            citations: [
              {
                ref: "review:r9",
                kind: "review",
                label: "Review r9",
                resolved: true,
                review_id: "r9",
              },
            ],
          }),
        ],
      })
    );
    render(<ProjectHistory apiReviewId="r1" revisionNumber={2} />);
    const section = await screen.findByTestId("project-history");
    expect(fetchContext).toHaveBeenCalledWith("r1", 2, "brief", {});
    expect(
      within(section).getByText("Project history · 1 entry")
    ).toBeInTheDocument();
    expect(screen.getByTestId("project-history-note")).toHaveTextContent(
      "cannot prove a fact about this revision"
    );
    expect(screen.getByTestId("project-history-freshness")).toHaveTextContent(
      "predates this Change"
    );
    expect(screen.getByTestId("project-history-sources")).toHaveTextContent(
      "Review r9"
    );
    expect(screen.getByTestId("project-history-review")).toHaveAttribute(
      "href",
      "/r/r9"
    );
    expect(screen.getByTestId("project-history-more")).toHaveTextContent(
      "2 more not shown"
    );
  });

  it("marks AI narration as not evidence", async () => {
    answer(
      context({
        entries: [],
        narration: [
          entry({ context_class: "ai_narration", support: "ai_narrated" }),
        ],
      })
    );
    render(<ProjectHistory apiReviewId="r1" revisionNumber={2} />);
    expect(
      await screen.findByTestId("project-history-freshness")
    ).toHaveTextContent("AI narration, not evidence");
  });

  it.each([
    [
      "no store is bound",
      context({ available: false, reason: "not_configured", entries: [] }),
    ],
    ["nothing matches", context({ entries: [] })],
    [
      "only path inventory matches",
      context({ entries: [entry({ knowledge_kind: "inventory" })] }),
    ],
  ])("renders nothing when %s", async (_name, value) => {
    answer(value);
    const { container } = render(
      <ProjectHistory apiReviewId="r1" revisionNumber={2} />
    );
    await waitFor(() => {
      expect(fetchContext).toHaveBeenCalled();
    });
    await new Promise((done) => {
      setTimeout(done, 0);
    });
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the read fails", async () => {
    reads.fail = true;
    const { container } = render(
      <ProjectHistory apiReviewId="r1" revisionNumber={2} />
    );
    await new Promise((done) => {
      setTimeout(done, 0);
    });
    expect(container).toBeEmptyDOMElement();
  });

  it("asks the Map's question with at most the paths the server accepts and keeps replaced-file rows", async () => {
    answer(
      context({
        purpose: "map",
        entries: [
          entry({ knowledge_kind: "inventory", state: "superseded" }),
          entry({ entry_id: "e2", knowledge_kind: "inventory" }),
        ],
      })
    );
    const paths = Array.from({ length: 20 }, (_, index) => `src/f${index}.py`);
    render(
      <ProjectHistory
        apiReviewId="r1"
        revisionNumber={2}
        purpose="map"
        paths={paths}
      />
    );
    expect(await screen.findAllByTestId("project-history-row")).toHaveLength(1);
    expect(fetchContext).toHaveBeenCalledWith("r1", 2, "map", {
      paths: paths.slice(0, 16),
    });
    expect(screen.getByTestId("project-history-more")).toHaveTextContent(
      "1 more not shown"
    );
  });

  it("does not show one revision's history on another", async () => {
    answer(context());
    const { rerender } = render(
      <ProjectHistory apiReviewId="r1" revisionNumber={2} />
    );
    await screen.findByTestId("project-history");
    let finish: (value: unknown) => void = () => undefined;
    fetchContext.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      })
    );
    rerender(<ProjectHistory apiReviewId="r1" revisionNumber={3} />);
    expect(screen.queryByTestId("project-history")).toBeNull();
    finish({ project_context: context({ entries: [] }) });
  });
});
