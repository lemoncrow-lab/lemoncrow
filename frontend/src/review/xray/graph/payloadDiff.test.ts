import { describe, expect, it } from "vitest";

import { diffCounts, diffJson, diffRows, inlineText, sameJson } from "./payloadDiff";

const before = { currency: "USD", plans: [{ name: "Starter", monthly: 0 }, { name: "Team", monthly: 49 }] };
const after = { currency: "USD", plans: [{ name: "Starter", monthly: 0 }, { name: "Team", monthly: 59 }, { name: "Enterprise", monthly: 899 }] };

describe("diffJson", () => {
  it("marks a changed value with its old text, and an added item as added", () => {
    const diff = diffJson(before, after);
    expect(diff.kind).toBe("changed");
    expect(diffCounts(diff)).toEqual({ added: 1, removed: 0, changed: 1 });
    const rows = diffRows(diff);
    const monthly = rows.find((row) => row.key === '"monthly": ' && row.kind === "changed")!;
    expect([monthly.was, monthly.text]).toEqual(["49", "59"]);
    expect(rows.find((row) => row.key === "[2] ")?.kind).toBe("added");
    // Unchanged siblings fold to one line.
    expect(rows.find((row) => row.key === "[0] ")).toMatchObject({ kind: "same", text: '{"name": "Starter", "monthly": 0}' });
    expect(rows.find((row) => row.key === '"currency": ')).toMatchObject({ kind: "same", text: '"USD"' });
  });

  it("lists a removed key after the keys After kept", () => {
    const rows = diffRows(diffJson({ a: 1, gone: true }, { a: 1 }));
    expect(rows.map((row) => [row.kind, row.key.trim()])).toEqual([["changed", ""], ["same", '"a":'], ["removed", '"gone":'], ["changed", ""]]);
    expect(rows[rows.length - 1].close).toBe(true);
  });

  it("reads a side without a sample as absent", () => {
    expect(diffJson(null, { a: 1 }).kind).toBe("added");
    expect(diffJson({ a: 1 }, undefined).kind).toBe("removed");
    expect(diffJson(null, undefined).kind).toBe("same");
    expect(diffCounts(diffJson(undefined, { a: 1, b: 2 }))).toEqual({ added: 2, removed: 0, changed: 0 });
  });

  it("compares by value, not by key order", () => {
    expect(sameJson({ a: 1, b: [1, 2] }, { b: [1, 2], a: 1 })).toBe(true);
    expect(sameJson([1, 2], [2, 1])).toBe(false);
    expect(diffJson({ a: 1, b: 2 }, { b: 2, a: 1 }).kind).toBe("same");
  });

  it("treats a type change as one changed value", () => {
    const rows = diffRows(diffJson({ v: [1] }, { v: "one" }));
    expect(rows.find((row) => row.key === '"v": ')).toMatchObject({ kind: "changed", was: "[1]", text: '"one"' });
  });

  it("folds a long value to a count", () => {
    expect(inlineText(Array.from({ length: 40 }, (_, i) => i))).toBe("[… 40 items]");
    expect(inlineText({ a: "x".repeat(80) })).toBe("{… 1 field}");
  });
});
