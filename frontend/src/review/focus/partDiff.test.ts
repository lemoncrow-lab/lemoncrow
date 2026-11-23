import { describe, expect, it } from "vitest";

import { changedWordPieces, fullFunctionItems, gapLabel, itemsToPatch, parsePatchRows, partItems, signOf, wordChangeForItem } from "./partDiff";

// A git unified patch as the server sends it for one file: header, then hunks.
const PATCH = [
  "diff --git a/app/orders.py b/app/orders.py",
  "index 1111111..2222222 100644",
  "--- a/app/orders.py",
  "+++ b/app/orders.py",
  "@@ -8,6 +8,5 @@ def apply_discount(total, percent):",
  "     if total <= 0:",
  "         return 0",
  "-    percent = min(percent, 100)",
  "     return round(total * (100 - percent) / 100, 2)",
  " ",
  " def order_total(items):",
  "@@ -30,3 +29,4 @@ def order_total(items):",
  "     total = 0",
  "+    total += 1",
  "     return total",
  "",
].join("\n");

describe("parsePatchRows", () => {
  it("numbers added and unchanged lines on the new side and removed lines on the old side", () => {
    const rows = parsePatchRows(PATCH);
    expect(rows.map((r) => [r.kind, r.num, r.text])).toEqual([
      ["ctx", 8, "    if total <= 0:"],
      ["ctx", 9, "        return 0"],
      ["del", 10, "    percent = min(percent, 100)"],
      ["ctx", 10, "    return round(total * (100 - percent) / 100, 2)"],
      ["ctx", 11, ""],
      ["ctx", 12, "def order_total(items):"],
      ["ctx", 29, "    total = 0"],
      ["add", 30, "    total += 1"],
      ["ctx", 31, "    return total"],
    ]);
    expect(rows[2].at).toBe(10);
  });

  it("skips file headers, 'no newline' markers and anything before the first hunk", () => {
    const rows = parsePatchRows("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n");
    expect(rows.map((r) => r.kind)).toEqual(["del", "add"]);
  });

  it("keeps a removed line that looks like a header inside a hunk", () => {
    const rows = parsePatchRows("@@ -1,2 +1,1 @@\n--- disable trigger\n x\n");
    expect(rows[0]).toMatchObject({ kind: "del", text: "-- disable trigger" });
  });
});

describe("lexical word highlights", () => {
  it("highlights the changed word rather than the whole replacement line", () => {
    expect(changedWordPieces("    return False", "    return True")).toEqual({
      old: { before: "    return ", changed: "False", after: "" },
      new: { before: "    return ", changed: "True", after: "" },
    });
  });

  it("keeps shared suffix tokens outside the highlight", () => {
    const result = changedWordPieces("percent = old_value + 1", "percent = new_value + 1");
    expect(result?.old.before).toBe("percent = ");
    expect(result?.old.changed).toBe("old_value");
    expect(result?.new.changed).toBe("new_value");
    expect(result?.new.after).toBe(" + 1");
  });

  it("never pairs ambiguous groups with more than one deletion or addition", () => {
    const several = partItems(parsePatchRows("@@ -1,3 +1,3 @@\n-old one\n-old two\n+new one\n+new two\n context\n"), 1, 3);
    expect(several.map((_, index) => wordChangeForItem(several, index))).toEqual(several.map(() => null));
    expect(changedWordPieces("unchanged", "unchanged")).toBeNull();
  });
});

describe("fullFunctionItems from frozen Snapshot", () => {
  const patch = parsePatchRows("@@ -2,4 +2,5 @@\n def f():\n-    old_guard()\n+    new_guard()\n     return 3\n");
  const complete = "# module\ndef f():\n    new_guard()\n    return 3\n\n# unrelated\n";

  it("fills unchanged function lines only from the supplied snapshot, retaining deleted old lines", () => {
    const items = fullFunctionItems(patch, 2, 5, complete);
    expect(items).not.toBeNull();
    expect(items?.map((item) => item.type === "row" ? `${signOf(item.row.kind)}${item.row.num}: ${item.row.text}` : "gap")).toEqual([
      "2: def f():", "−3:     old_guard()", "+3:     new_guard()", "4:     return 3", "5: ",
    ]);
  });

  it("rejects a revision mismatch between the patch and source instead of drawing fake unchanged lines", () => {
    expect(fullFunctionItems(patch, 2, 5, complete.replace("new_guard", "different_guard"))).toBeNull();
  });

  it("refuses incomplete, missing or huge function sources", () => {
    expect(fullFunctionItems(patch, 2, 5, null)).toBeNull();
    expect(fullFunctionItems(patch, 2, 20, complete)).toBeNull();
    expect(fullFunctionItems(patch, 2, 5, complete, 3)).toBeNull();
  });
});

describe("partItems", () => {
  const rows = parsePatchRows(PATCH);

  it("shows the part's range with removed lines in place and collapses what the patch did not carry", () => {
    const items = partItems(rows, 8, 20);
    expect(items.map((i) => (i.type === "gap" ? `gap ${i.lines}` : `${signOf(i.row.kind)}${i.row.num}`))).toEqual([
      "8", "9", "−10", "10", "11", "12", "gap 8",
    ]);
  });

  it("leaves out lines of other parts", () => {
    const items = partItems(rows, 29, 31);
    expect(items.every((i) => i.type === "row")).toBe(true);
    expect(items).toHaveLength(3);
  });

  it("a leading gap when the patch starts inside the range", () => {
    const items = partItems(rows, 4, 9);
    expect(items[0]).toEqual({ type: "gap", lines: 4 });
  });

  it("a part with no range shows the whole patch", () => {
    expect(partItems(rows, 0, 0)).toHaveLength(rows.length + 1);
  });

  it("labels", () => {
    expect(gapLabel(1)).toBe("1 unchanged line");
    expect(gapLabel(5)).toBe("5 unchanged lines");
  });
});

describe("itemsToPatch", () => {
  it("round-trips the part's rows as one hunk per run, with old numbers from the patch", () => {
    const patch = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -10,3 +10,3 @@\n a\n-b\n+B\n c\n@@ -40,2 +40,3 @@\n d\n+e\n f\n";
    const rows = parsePatchRows(patch);
    const items = partItems(rows, 0, 0);
    const out = itemsToPatch("x.py", items, new Map([[10, 10], [12, 12], [40, 40], [42, 41]]));
    expect(out).toContain("@@ -10,3 +10,3 @@\n a\n-b\n+B\n c\n");
    expect(out).toContain("@@ -40,2 +40,3 @@\n d\n+e\n f\n");
    expect(parsePatchRows(out)).toEqual(rows);
  });
});
