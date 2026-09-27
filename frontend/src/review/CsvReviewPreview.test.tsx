import { render, screen } from "@testing-library/react";

import CsvReviewPreview, { parseCsv } from "./CsvReviewPreview";

const preview = {
  kind: "csv" as const,
  old_content: 'name,note\nalpha,"old, value"\n',
  new_content: 'name,note\nalpha,"new, value"\nbeta,"two\nlines"\n',
};

describe("CsvReviewPreview", () => {
  it("parses quoted commas and embedded newlines", () => {
    const parsed = parseCsv(preview.new_content);
    expect(parsed.rows).toEqual([
      ["name", "note"],
      ["alpha", "new, value"],
      ["beta", "two\nlines"],
    ]);
    expect(parsed.totalRows).toBe(3);
  });

  it("renders one exact Snapshot table in preview mode and both sides in compare mode", () => {
    const { rerender } = render(
      <CsvReviewPreview preview={preview} revisionId="rrv-1" status="modified" mode="preview" />,
    );

    expect(screen.getByTestId("csv-review-preview")).toBeTruthy();
    expect(screen.getByText("Snapshot rrv-1 · rendered CSV")).toBeTruthy();
    expect(screen.getByText("new, value")).toBeTruthy();
    expect(screen.queryByText("old, value")).toBeNull();

    rerender(<CsvReviewPreview preview={preview} revisionId="rrv-1" status="modified" mode="compare" />);
    expect(screen.getByText("Before")).toBeTruthy();
    expect(screen.getByText("After")).toBeTruthy();
    expect(screen.getByText("old, value")).toBeTruthy();
    expect(screen.getByText("new, value")).toBeTruthy();
  });
});
