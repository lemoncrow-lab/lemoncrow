import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewCompareReader from "./ReviewCompareReader";

const api = vi.hoisted(() => ({
  fetchSourceComparison: vi.fn(),
}));
const stream = vi.hoisted(() => ({ lastProps: null as any }));

vi.mock("./reviewApi", () => ({
  fetchSourceComparison: api.fetchSourceComparison,
  reviewDirectoryHref: () => "/reviews",
  reviewHref: (id: string) => `/r/${id}`,
  sourceCompareHref: (from: string, to: string, scope = "") => `/r/x/${from}..${to}${scope ? `?scope=${encodeURIComponent(scope)}` : ""}`,
}));
vi.mock("./ReviewStream", () => ({
  default: (props: any) => {
    stream.lastProps = props;
    return <div data-testid="compare-stream" />;
  },
}));

describe("ReviewCompareReader", () => {
  beforeEach(() => {
    stream.lastProps = null;
    api.fetchSourceComparison.mockResolvedValue({
      from: { spec: "rr/11111111111171118111111111111111", label: "rev 1 · rr/11111111111171118111111111111111", kind: "review_revision" },
      to: { spec: "rr/22222222222272228222222222222222", label: "rev 2 · rr/22222222222272228222222222222222", kind: "review_revision" },
      summary: { files: 1, additions: 1, deletions: 1 },
      files: [{
        path: "src/app.py",
        old_path: "src/app.py",
        status: "modified",
        additions: 1,
        deletions: 1,
        patch: "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n",
        renderable: true,
        refusal: "",
        detail: "",
      }],
    });
  });

  it("uses the normal diff stream in read-only comparison mode for URL source pairs", async () => {
    render(
      <MemoryRouter initialEntries={["/r/x/rr/11111111111171118111111111111111..rr/22222222222272228222222222222222?scope=r%2Faaaaaaaa"]}>
        <Routes>
          <Route path="/r/x/*" element={<ReviewCompareReader />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(api.fetchSourceComparison).toHaveBeenCalledWith("rr/11111111111171118111111111111111", "rr/22222222222272228222222222222222", "r/aaaaaaaa"));
    expect(screen.getByDisplayValue("rr/11111111111171118111111111111111")).toBeTruthy();
    expect(screen.getByDisplayValue("rr/22222222222272228222222222222222")).toBeTruthy();
    expect(screen.getByText("1 files ·")).toBeTruthy();
    expect(screen.getByTestId("compare-stream")).toBeTruthy();
    expect(stream.lastProps.comparisonMode).toBe(true);
    expect(stream.lastProps.annotations).toEqual([]);
    expect(stream.lastProps.details["src/app.py"].patch).toContain("+new");
    expect(stream.lastProps.targets).toHaveLength(1);
  });

  it("decodes the clean review-scoped route and Back returns to the prior review location", async () => {
    render(
      <MemoryRouter
        initialEntries={[
          "/rr/11111111111171118111111111111111?review-path=src%2Fapp.py",
          "/r/aaaaaaaa/compare/11111111111171118111111111111111..22222222222272228222222222222222",
        ]}
        initialIndex={1}
      >
        <Routes>
          <Route path="/r/:reviewRef/compare/*" element={<ReviewCompareReader />} />
          <Route path="/rr/:revisionRef" element={<div data-testid="prior-review-location" />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(api.fetchSourceComparison).toHaveBeenCalledWith(
      "rr/11111111111171118111111111111111",
      "rr/22222222222272228222222222222222",
      "r/aaaaaaaa",
    ));
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByTestId("prior-review-location")).toBeTruthy();
  });
});
