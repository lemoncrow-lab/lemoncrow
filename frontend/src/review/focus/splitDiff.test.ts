import { describe, expect, it } from "vitest";
import { parsePatchRows, partItems } from "./partDiff";
import { alignSplitRows, patchOldLineNumbers } from "./splitDiff";

describe("split code presentation of frozen patch rows", () => {
  const patch = "@@ -10,3 +10,4 @@\n def run():\n-    return old\n+    new = True\n+    return new\n # end\n";
  it("aligns delete/add blocks without conflating unchanged code with changed code", () => {
    const items = partItems(parsePatchRows(patch), 10, 13);
    const lines = alignSplitRows(items);
    const pairs = lines.filter((row) => row.kind === "pair");
    expect(pairs.map((pair) => [pair.before?.row.kind ?? null, pair.after?.row.kind ?? null]))
      .toEqual([["ctx", "ctx"], ["del", "add"], [null, "add"], ["ctx", "ctx"]]);
    expect(pairs[1].kind === "pair" && pairs[1].before?.row.num).toBe(11);
    expect(pairs[1].kind === "pair" && pairs[1].after?.row.num).toBe(11);
  });
  it("uses the exact old hunk coordinates for context, never the new number after insertion", () => {
    const numbers = patchOldLineNumbers(patch);
    expect(numbers.get(10)).toBe(10);
    expect(numbers.get(13)).toBe(12);
    expect(numbers.has(11)).toBe(false);
    expect(numbers.has(12)).toBe(false);
  });
  it("preserves gaps and removed-only lines", () => {
    const patch = "@@ -1,1 +1,0 @@\n-old\n";
    const pairs = alignSplitRows(partItems(parsePatchRows(patch), 0, 0));
    expect(pairs).toHaveLength(1);
    expect(pairs[0]).toMatchObject({ kind: "pair", before: { row: { kind: "del", num: 1 } }, after: null });
  });
});
