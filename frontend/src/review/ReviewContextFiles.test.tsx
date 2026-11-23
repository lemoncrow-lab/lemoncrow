import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const { reviewApi } = vi.hoisted(() => ({
  reviewApi: {
    fetchReviewMap: vi.fn(),
    fetchHistoricalReviewMap: vi.fn(),
  },
}));

vi.mock("./reviewApi", () => reviewApi);

import ReviewContextFiles from "./ReviewContextFiles";

function node(id: string, path: string, exactness: "snapshot" | "current_index") {
  return {
    id,
    label: path,
    kind: "file",
    path,
    line: 1,
    end_line: 10,
    status: "context",
    exactness,
    target_ids: [],
    important_public_contract: false,
    boundaries: [],
    boundary_basis: "",
    selection: { kind: "file", path, line: 1, end_line: 10 },
  };
}

describe("ReviewContextFiles", () => {
  it("loads context separately from parts and never exposes a review mutation", async () => {
    reviewApi.fetchReviewMap.mockResolvedValue({
      level: "architecture",
      graph: {
        nodes: [
          node("docs", "docs/architecture.md", "current_index"),
          node("schema", "schemas/order.json", "snapshot"),
          { ...node("changed", "src/changed.py", "snapshot"), status: "changed" },
        ],
        edges: [],
      },
      stats: {},
      focus: null,
      truncated: false,
      omitted_node_count: 0,
    });
    const onOpenInCode = vi.fn();

    render(
      <ReviewContextFiles
        reviewId="review-1"
        revisionNumber={3}
        historical={false}
        onOpenInCode={onOpenInCode}
      />,
    );

    expect(screen.getByText(/opening or loading it never marks a part reviewed/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Load" }));
    expect(await screen.findByText("docs/architecture.md")).toBeInTheDocument();
    expect(screen.getByText("schemas/order.json")).toBeInTheDocument();
    expect(screen.queryByText("src/changed.py")).toBeNull();
    expect(screen.getByText("current index")).toBeInTheDocument();
    expect(screen.getByText("Revision context")).toBeInTheDocument();

    await userEvent.click(screen.getByText("docs/architecture.md"));
    expect(onOpenInCode).toHaveBeenCalledWith({ path: "docs/architecture.md", line: 1, endLine: 10 });
  });
});
