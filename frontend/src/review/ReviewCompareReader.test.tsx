import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewCompareReader, { compareErrorText } from "./ReviewCompareReader";

const api = vi.hoisted(() => ({
  fetchSourceComparison: vi.fn(),
  fetchRevisions: vi.fn(),
}));
const stream = vi.hoisted(() => ({ lastProps: null as any }));

vi.mock("./reviewApi", () => ({
  fetchSourceComparison: api.fetchSourceComparison,
  fetchRevisions: api.fetchRevisions,
  reviewDirectoryHref: () => "/reviews",
  reviewHref: (id: string) => `/r/${id}`,
  sourceCompareHref: (from: string, to: string, scope = "") => {
    const params = new URLSearchParams({ from, to });
    if (scope) params.set("scope", scope);
    return `/r/x?${params.toString()}`;
  },
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
    api.fetchSourceComparison.mockReset();
    api.fetchRevisions.mockReset();
    api.fetchRevisions.mockRejectedValue(new Error("404"));
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

  it("keeps arbitrary source specs containing '..' distinct in query URLs", async () => {
    render(
      <MemoryRouter initialEntries={["/r/x?from=git%2Fmain..feature&to=git%2FHEAD&scope=r%2Faaaaaaaa"]}>
        <Routes>
          <Route path="/r/x/*" element={<ReviewCompareReader />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(api.fetchSourceComparison).toHaveBeenCalledWith(
      "git/main..feature",
      "git/HEAD",
      "r/aaaaaaaa",
    ));
    expect(screen.getByDisplayValue("git/main..feature")).toBeTruthy();
    expect(screen.getByDisplayValue("git/HEAD")).toBeTruthy();
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

  it.each([
    ["no '..' separator", "/r/x/rr%2Fonly-one-spec"],
    ["an empty from spec", "/r/x/..rr%2F2"],
    ["an empty to spec", "/r/x/rr%2F1.."],
  ])("explains a comparison URL with %s without fetching or offering a retry", async (_name, url) => {
    render(
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/r/x/*" element={<ReviewCompareReader />} />
        </Routes>
      </MemoryRouter>,
    );

    expect((await screen.findByRole("alert")).textContent).toContain("Comparison URL must contain two source specs separated by '..'.");
    expect(api.fetchSourceComparison).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByTestId("compare-stream")).toBeNull();
  });

  it("review-22.2: says a missing side in plain words instead of the server's raw reason", () => {
    expect(compareErrorText("the comparison Review source is not available")).toBe(
      "One of these two versions could not be found, so there is nothing to compare. Check both versions above, or go back to the review.",
    );
    expect(compareErrorText("unknown source spec 'rr/3'")).toBe("unknown source spec 'rr/3'");
  });

  it("names a bare HTTP failure and retries the same source pair", async () => {
    api.fetchSourceComparison.mockRejectedValueOnce(new Error("502"));
    render(
      <MemoryRouter initialEntries={["/r/x/git%2FHEAD~1..worktree"]}>
        <Routes>
          <Route path="/r/x/*" element={<ReviewCompareReader />} />
        </Routes>
      </MemoryRouter>,
    );

    expect((await screen.findByRole("alert")).textContent).toContain("The server could not compare these sources (HTTP 502).");
    expect(screen.queryByTestId("compare-stream")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("compare-stream")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(api.fetchSourceComparison).toHaveBeenCalledTimes(2);
    expect(api.fetchSourceComparison.mock.calls.every(([from, to]) => from === "git/HEAD~1" && to === "worktree")).toBe(true);
  });

  it("keeps the server's refusal text and offers a way back to the scoped review", async () => {
    api.fetchSourceComparison.mockRejectedValueOnce(new Error("unknown source spec 'rr/33333333'"));
    render(
      <MemoryRouter initialEntries={["/r/aaaaaaaa/compare/11111111111171118111111111111111..33333333"]}>
        <Routes>
          <Route path="/r/:reviewRef/compare/*" element={<ReviewCompareReader />} />
          <Route path="/r/:reviewRef" element={<div data-testid="scoped-review" />} />
        </Routes>
      </MemoryRouter>,
    );

    expect((await screen.findByRole("alert")).textContent).toContain("unknown source spec 'rr/33333333'");
    await userEvent.click(screen.getByRole("button", { name: "Back to review" }));
    expect(await screen.findByTestId("scoped-review")).toBeTruthy();
  });

  it("review-22.2: a review's comparison picks its revisions by name, never by raw id", async () => {
    const revision = (id: string, number: number) => ({ id, ref: `rr/${id.slice(0, 4)}`, revision_number: number, capture_kind: "revision" });
    api.fetchRevisions.mockResolvedValue({ revisions: [
      revision("33333333333373338333333333333333", 2), revision("11111111111171118111111111111111", 0), revision("22222222222272228222222222222222", 1),
    ] });
    function Where() {
      const location = useLocation();
      return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
    }
    render(
      <MemoryRouter initialEntries={["/r/aaaaaaaa/compare/1111..22222222222272228222222222222222"]}>
        <Routes>
          <Route path="/r/:reviewRef/compare/*" element={<><ReviewCompareReader /><Where /></>} />
          <Route path="/r/x" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );

    const from = await screen.findByRole("combobox", { name: "Compare from revision" });
    await waitFor(() => expect(from.textContent).toBe("Revision 1"));
    expect(screen.getByRole("combobox", { name: "Compare to revision" }).textContent).toBe("Revision 2");
    expect(api.fetchRevisions).toHaveBeenCalledWith("aaaaaaaa");
    // No source field, and no raw revision id anywhere on the page.
    expect(screen.queryByRole("textbox")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("compare-stream")).toBeTruthy());
    expect(document.querySelector("header")?.textContent).not.toMatch(/rr\/|1111|2222/);
    expect(document.title).toBe("Compare: Revision 1 → Revision 2");

    await userEvent.click(screen.getByRole("combobox", { name: "Compare to revision" }));
    const options = screen.getAllByRole("option").map((option) => option.textContent);
    expect(options).toEqual(["Revision 1", "Revision 2", "Revision 3"]);
    await userEvent.click(screen.getByRole("option", { name: "Revision 3" }));
    const where = new URLSearchParams((await screen.findByTestId("where")).textContent?.split("?")[1] ?? "");
    expect(where.get("from")).toBe("rr/1111");
    expect(where.get("to")).toBe("rr/3333");
  });

  it("review-22.2: a revision the review does not have reads Choose a revision, not its id", async () => {
    api.fetchRevisions.mockResolvedValue({ revisions: [{ id: "11111111111171118111111111111111", ref: "rr/1111", revision_number: 0 }] });
    api.fetchSourceComparison.mockRejectedValueOnce(new Error("the comparison Review source is not available"));
    render(
      <MemoryRouter initialEntries={["/r/aaaaaaaa/compare/1111..9999"]}>
        <Routes>
          <Route path="/r/:reviewRef/compare/*" element={<ReviewCompareReader />} />
        </Routes>
      </MemoryRouter>,
    );
    expect((await screen.findByRole("alert")).textContent).toContain("One of these two versions could not be found");
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Compare to revision" }).textContent).toBe("Choose a revision"));
    expect(screen.getByRole("combobox", { name: "Compare from revision" }).textContent).toBe("Revision 1");
    expect(document.body.textContent).not.toContain("rr/");
  });

  it("says when two sources have no differences instead of rendering an empty stream", async () => {
    api.fetchSourceComparison.mockResolvedValueOnce({
      from: { spec: "git/HEAD", label: "git/HEAD", kind: "git" },
      to: { spec: "worktree", label: "worktree", kind: "worktree" },
      summary: { files: 0, additions: 0, deletions: 0 },
      files: [],
    });
    render(
      <MemoryRouter initialEntries={["/r/x/git%2FHEAD..worktree"]}>
        <Routes>
          <Route path="/r/x/*" element={<ReviewCompareReader />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("No source differences between these revisions.")).toBeTruthy();
    expect(screen.queryByTestId("compare-stream")).toBeNull();
  });

  it("stops showing progress when navigation lands on a malformed URL mid-comparison", async () => {
    api.fetchSourceComparison.mockImplementationOnce(() => new Promise(() => {}));
    function Jump() {
      const navigate = useNavigate();
      return <button type="button" onClick={() => navigate("/r/x/not-a-pair")}>Jump</button>;
    }
    render(
      <MemoryRouter initialEntries={["/r/x/git%2FHEAD..worktree"]}>
        <Routes>
          <Route path="/r/x/*" element={<><Jump /><ReviewCompareReader /></>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText("Comparing sources…")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Jump" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Comparison URL must contain two source specs");
    expect(screen.queryByText("Comparing sources…")).toBeNull();
  });
});
