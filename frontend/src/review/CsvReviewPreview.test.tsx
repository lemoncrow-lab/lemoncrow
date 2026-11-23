import { render, screen } from "@testing-library/react";

import CsvReviewPreview, { alignCsvRows, parseCsv } from "./CsvReviewPreview";

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

  it("renders one exact revision table in preview mode and both sides in compare mode", () => {
    const { rerender } = render(
      <CsvReviewPreview preview={preview} revisionId="rrv-1" status="modified" mode="preview" />,
    );

    expect(screen.getByTestId("csv-review-preview")).toBeTruthy();
    expect(screen.getByText("Revision · rendered CSV")).toBeTruthy();
    expect(screen.getByText("new, value")).toBeTruthy();
    expect(screen.queryByText("old, value")).toBeNull();

    rerender(<CsvReviewPreview preview={preview} revisionId="rrv-1" status="modified" mode="compare" />);
    expect(screen.getByText("Before")).toBeTruthy();
    expect(screen.getByText("After")).toBeTruthy();
    expect(screen.getByText("old, value")).toBeTruthy();
    expect(screen.getByText("new, value")).toBeTruthy();
  });
});

describe("CSV row alignment", () => {
  const csv = (rows: string[]) => parseCsv(`id,v\n${rows.join("\n")}\n`);
  const kinds = (a: ReturnType<typeof alignCsvRows>) => ({
    old: a.old.map((m) => m.kind),
    new: a.new.map((m) => m.kind),
  });

  it("a single inserted row does not flag following rows", () => {
    const a = alignCsvRows(csv(["1,a", "2,b", "3,c"]), csv(["1,a", "9,z", "2,b", "3,c"]));
    expect(kinds(a)).toEqual({ old: ["same", "same", "same"], new: ["same", "unpaired", "same", "same"] });
  });

  it("a single deleted row does not flag following rows", () => {
    const a = alignCsvRows(csv(["1,a", "2,b", "3,c"]), csv(["1,a", "3,c"]));
    expect(kinds(a)).toEqual({ old: ["same", "unpaired", "same"], new: ["same", "same"] });
  });

  it("pairs a changed row so only the changed cell differs", () => {
    const a = alignCsvRows(csv(["1,a", "2,b", "3,c"]), csv(["1,a", "2,B", "3,c"]));
    expect(kinds(a)).toEqual({ old: ["same", "changed", "same"], new: ["same", "changed", "same"] });
    expect(a.new[1]).toEqual({ kind: "changed", partner: ["2", "b"] });
  });

  it("handles duplicate rows deterministically", () => {
    const a = alignCsvRows(csv(["1,a", "1,a", "2,b"]), csv(["1,a", "1,a", "1,a", "2,b"]));
    expect(kinds(a)).toEqual({ old: ["same", "same", "same"], new: ["same", "same", "unpaired", "same"] });
    expect(alignCsvRows(csv(["1,a", "1,a", "2,b"]), csv(["1,a", "1,a", "1,a", "2,b"]))).toEqual(a);
  });

  it("a reordered row flags only the moved rows, not the whole tail", () => {
    const a = alignCsvRows(csv(["1,a", "2,b", "3,c", "4,d", "5,e"]), csv(["2,b", "3,c", "4,d", "5,e", "1,a"]));
    expect(a.old.filter((m) => m.kind === "same")).toHaveLength(4);
    expect(a.new.filter((m) => m.kind === "same")).toHaveLength(4);
  });

  it("does not flag rows pushed past the render limit of a truncated large CSV", () => {
    const big = Array.from({ length: 400 }, (_, i) => `${i},v${i}`);
    const inserted = ["0,v0", "x,new", ...big.slice(1)];
    const oldCsv = csv(big);
    const newCsv = csv(inserted);
    expect(oldCsv.truncatedRows).toBeGreaterThan(0);
    const a = alignCsvRows(oldCsv, newCsv);
    expect(a.new.filter((m) => m.kind === "unpaired")).toHaveLength(1);
    expect(a.old.filter((m) => m.kind === "unpaired")).toHaveLength(0);
    expect(a.old.at(-1)?.kind).toBe("unknown");
    render(<CsvReviewPreview preview={{ kind: "csv", old_content: big.join("\n"), new_content: inserted.join("\n") }} mode="compare" />);
    expect(screen.getAllByText(/more row\(s\) not rendered/).length).toBe(2);
  });

  it("highlights only the inserted row's cells in compare mode", () => {
    const { container } = render(
      <CsvReviewPreview
        preview={{ kind: "csv", old_content: "id,v\n1,a\n2,b\n3,c\n", new_content: "id,v\n1,a\n9,z\n2,b\n3,c\n" }}
        mode="compare"
      />,
    );
    expect(container.querySelectorAll("td.bg-amber-950\\/15")).toHaveLength(2);
  });
});
