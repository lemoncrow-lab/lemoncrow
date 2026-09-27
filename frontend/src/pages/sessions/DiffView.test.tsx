import { render, screen } from "@testing-library/react";
import { InlineFileDiff } from "./DiffView";

describe("InlineFileDiff — unified diff parsing", () => {
  it("treats a removed line whose content starts with -- as content, not a file header, and keeps line numbers stable", () => {
    // Old file:            New file:
    // 1 line1               1 line1
    // 2 -- disable trigger  2 line3-changed
    // 3 line3                3 line4
    // 4 line4
    const diff = [
      "--- a/file.sql",
      "+++ b/file.sql",
      "@@ -1,4 +1,3 @@",
      " line1",
      "--- disable trigger",
      "-line3",
      "+line3-changed",
      " line4",
    ].join("\n");

    render(<InlineFileDiff path="file.sql" diff={diff} forceExpand />);

    // The removed "-- disable trigger" line must render as content — the
    // old parser matched its diff-line prefix "---" as a file header and
    // silently dropped the deletion.
    const removedContent = screen.getByText("-- disable trigger");
    expect(removedContent).toBeInTheDocument();
    expect(removedContent.previousElementSibling?.textContent).toBe("2");

    // Old-side line numbers after the removal must not shift: "line4" is
    // physically the 4th line of the old file, not the 3rd (which is what
    // the old parser produced by failing to increment past the dropped
    // line).
    const [leftLine4] = screen.getAllByText("line4");
    expect(leftLine4.previousElementSibling?.textContent).toBe("4");
  });

  it("renders left/right cells of the same row as siblings in a shared grid, not two independent columns", () => {
    const diff = [
      "--- a/f.txt",
      "+++ b/f.txt",
      "@@ -1,2 +1,2 @@",
      "-old line",
      "+new line",
      " context line",
    ].join("\n");

    render(<InlineFileDiff path="f.txt" diff={diff} forceExpand />);

    const oldCell = screen.getByText("old line");
    const newCell = screen.getByText("new line");
    // Same row → same parent grid container. Two independent w-1/2 flow
    // columns (the pre-fix structure) would put them under different
    // parents, which is exactly what lets a wrapped line desync every row
    // below it.
    expect(oldCell.closest("div")?.parentElement).toBe(
      newCell.closest("div")?.parentElement
    );
  });
});
