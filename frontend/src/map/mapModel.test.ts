import { describe, expect, it } from "vitest";

import { assignLanes, focusSymbolNote, subjectIds } from "./mapModel";
import type { MapEdge, MapMemberSymbol, MapNode, MapPayload } from "./types";

function file(path: string, status: MapNode["status"], lane: MapNode["lane"] = ""): MapNode {
  return { id: `file:${path}`, label: path.split("/").pop() ?? path, kind: "file", path, line: 0, end_line: 0, status, lane, exactness: "current_index" };
}

function edge(source: string, target: string): MapEdge {
  return { id: `${source}->${target}`, source: `file:${source}`, target: `file:${target}`, kind: "imports", exactness: "current_index", weight: 1 } as MapEdge;
}

function architecture(nodes: MapNode[], edges: MapEdge[] = []): MapPayload {
  return { level: "architecture", graph: { level: "architecture", focus: null, nodes, edges, truncated: false }, context: { source: "current_index" } };
}

function symbol(change: string): MapMemberSymbol {
  return { id: `s${Math.random()}`, label: "s", qualified_name: "s", path: "a.ts", line: 1, end_line: 2, change, caller_count: -1, usage_count: -1, centrality_rank: null, centrality_percentile: null };
}

describe("module focus subject", () => {
  it("treats every member of the focused module as the subject, changed or not", () => {
    // Code's module focus: src/billing's members (one uncommitted) and a page that imports one of them.
    const payload = architecture(
      [file("src/billing/charge.ts", "subject"), file("src/billing/refund.ts", "changed"), file("src/pages/pay.tsx", "context", "used_by")],
      [edge("src/pages/pay.tsx", "src/billing/charge.ts"), edge("src/billing/refund.ts", "src/billing/charge.ts")],
    );
    expect([...subjectIds(payload)].sort()).toEqual(["file:src/billing/charge.ts", "file:src/billing/refund.ts"]);
    const lanes = assignLanes(payload);
    expect(lanes.get("file:src/billing/charge.ts")).toBe("subject");
    expect(lanes.get("file:src/billing/refund.ts")).toBe("subject");
    expect(lanes.get("file:src/pages/pay.tsx")).toBe("used_by");
  });

  it("keeps a Review's subject to its changed files", () => {
    const payload = architecture([file("a.ts", "changed"), file("b.ts", "new"), file("c.ts", "context", "uses"), file("d.ts", "impacted")]);
    expect([...subjectIds(payload)].sort()).toEqual(["file:a.ts", "file:b.ts"]);
  });
});

describe("focusSymbolNote", () => {
  it("does not call a changed file's symbols changed when the server marks none of them", () => {
    // Code's file focus lists every symbol of the file with change "".
    expect(focusSymbolNote({ status: "changed", symbol_count: 9 }, Array.from({ length: 9 }, () => symbol("")))).toBe("9 symbols");
    expect(focusSymbolNote({ status: "subject", symbol_count: 1 }, [symbol("")])).toBe("1 symbol");
  });

  it("counts the changed symbols a Review sends", () => {
    expect(focusSymbolNote({ status: "changed", symbol_count: 14 }, [symbol("modified"), symbol("added")])).toBe("14 changed symbols");
    expect(focusSymbolNote({ status: "changed" }, [symbol("modified"), symbol(""), symbol("unchanged")])).toBe("1 changed symbol");
  });

  it("says why a focused file has no symbol rows", () => {
    expect(focusSymbolNote({ status: "subject" }, [])).toBe("No indexed symbols");
    expect(focusSymbolNote({ status: "changed" }, [])).toBe("No symbol boundary captured");
    expect(focusSymbolNote({ status: "impacted" }, [])).toBe("Affected by the change");
    expect(focusSymbolNote({ status: "context" }, [])).toBe("Surrounding code");
  });
});
