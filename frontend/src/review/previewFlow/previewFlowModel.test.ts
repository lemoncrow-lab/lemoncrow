import { describe, expect, it } from "vitest";
import type { ReviewSurfaceInfo, ReviewTarget, ReviewSurfaceRunResult } from "../types";
import {
  canCompare, changeSummary, compareText, isExecutableCheck, notChecked, pathsOutsideChange, plainLocator, previewMeaning, providerName,
  provenanceText, reachedPaths, runToResult, sideSummary, surfaceGroup, surfaceKey, surfaceRows, type PreviewResult,
} from "./previewFlowModel";

const KIND: Record<string, string> = { web: "web.route", bruno: "api.request", service: "service" };
const surface = (id: string, provider: string, affected_paths: string[] = [], extra: Partial<ReviewSurfaceInfo> = {}): ReviewSurfaceInfo => ({
  id, provider, kind: KIND[provider] ?? provider, title: id, locator: id, runtime: "", capabilities: [], affected_paths, metadata: {}, ...extra,
});
const target = (path: string) => ({ path } as ReviewTarget);
describe("P4-1 truthful frozen surface-evidence contract", () => {
  it("distinguishes known reached paths from discovered-but-unverified surfaces", () => {
    const targets = [target("src/a.ts"), target("src/b.ts")];
    const rows = surfaceRows([surface("unproved", "web"), surface("page", "web", ["src/a.ts", "src/else.ts"])], targets);
    expect(rows.map((r) => [r.key, r.reached, r.verifiedPaths])).toEqual([
      ["web:page", true, ["src/a.ts"]], ["web:unproved", false, []],
    ]);
    expect(reachedPaths(surface("unused", "web", ["x", "y"]), targets)).toEqual([]);
    expect(rows[0].magnitude).toBeNull();
  });
  it("ranks only real supplied difference scores; absence never becomes a guessed magnitude", () => {
    const one = surface("one", "web", ["a"]);
    const two = surface("two", "web", ["a"]);
    const rows = surfaceRows([one, two], [target("a")], { "web:two": 12 });
    expect(rows.map((r) => [r.key, r.magnitude])).toEqual([["web:two", 12], ["web:one", null]]);
    expect(surfaceRows([one], [target("a")], { "web:one": Number.NaN })[0].magnitude).toBeNull();
  });
  it("files only a real check under Checks: a command service is one, a running http service is not", () => {
    const command = surface("t", "service", [], { capabilities: ["execute"], runtime: "r", metadata: { runner: "command" } });
    const http = surface("api", "service", [], { capabilities: ["execute"], runtime: "r", metadata: { runner: "http-service" } });
    expect(surfaceGroup(command)).toBe("Checks");
    expect(surfaceGroup(http)).toBe("Other");
    expect(isExecutableCheck(http)).toBe(false);
    expect(isExecutableCheck(command)).toBe(true);
    expect(surfaceGroup(surface("unit", "pytest", [], { kind: "test.suite" }))).toBe("Checks");
  });
  it("groups actual kinds without pretending a check was executed", () => {
    expect(surfaceGroup(surface("p", "web"))).toBe("Pages");
    expect(surfaceGroup(surface("req", "bruno"))).toBe("API requests");
    expect(previewMeaning(surface("t", "service", [], { capabilities: ["execute"], runtime: "r", metadata: { runner: "command" } }))).toContain("Not run");
  });
  it("limits comparable sides to a declared renderer or executable runner", () => {
    expect(canCompare(surface("p", "web"))).toBe(false);
    expect(canCompare(surface("c", "service", [], { capabilities: ["execute"], runtime: "fixture", metadata: { runner: "command" } }))).toBe(true);
    expect(canCompare(surface("p", "web", [], { preview: {
      version: 1, renderer: "html", display: "html", media_type: "text/html", label: "web",
      sides: ["old", "new"], scope: "snapshot", options: {}, status: "unavailable",
    } }))).toBe(false);
  });
  it("never calls a run without exact revision and refuses mismatched results", () => {
    const run = { surface_id: "r", provider: "service", revision_id: "rev1", side: "new", status: "failed",
      summary: "Server replied 500", runner: "command", runtime: "r", exit_code: 1, duration_ms: 200, output: "500",
      data: {} } as ReviewSurfaceRunResult;
    expect(() => runToResult(run, "rev2")).toThrow("another revision");
    expect(runToResult(run, "rev1")).toMatchObject({ source: "run", status: "failed", text: "500" });
  });
  it("shows text differences only when BOTH sides exist on the same revision", () => {
    const old = { source: "preview", display: "text", revisionId: "rev", text: "403 Forbidden\nunchanged", side: "old" } as const;
    const newer = { source: "preview", display: "text", revisionId: "rev", text: "200 OK\nunchanged", side: "new" } as const;
    const diff = compareText(old as never, newer as never);
    expect(diff).toMatchObject({ changed: true, magnitude: 2, removed: ["403 Forbidden"], added: ["200 OK"] });
    expect(compareText(old as never, null)).toBeNull();
    expect(compareText(old as never, { ...newer, revisionId: "rev2" } as never)).toBeNull();
    expect(surfaceKey(surface("a", "bruno"))).toBe("bruno:a");
  });
});
const output = (text: string, extra: Partial<PreviewResult> = {}): PreviewResult => ({ source: "preview", side: "old", revisionId: "rev", display: "json", mime: "application/json", text, filename: "r", ...extra });
describe("P4-1 evidence labels and provenance in plain words", () => {
  it("labels the evidence with the first line of each side, an HTTP status as its code", () => {
    expect(changeSummary(output("403 Forbidden\n{}"), output("200 OK\n{}"))).toBe("403 → 200");
    expect(changeSummary(output("same"), output("same"))).toBe("unchanged");
    expect(sideSummary(output("", { source: "run", status: "passed" }))).toBe("passed");
    expect(sideSummary(output("\n\n"))).toBe("empty");
  });
  it("words provenance without a raw token or an identifier", () => {
    const text = provenanceText([output("a", { source: "run", durationMs: 1800 }), null], { head_sha: "4e1b9c2f00aa", revision_number: 3 });
    expect(text).toBe("Checked in code · Ran revision 4e1b9c2 in a sandbox · 1.8 s");
    expect(provenanceText([output("a")], { head_sha: "", revision_number: 3 })).toBe("Checked in code · Rendered revision #4 in a sandbox");
    expect(text).not.toMatch(/reviewer|requested|`/);
  });
  it("lists what is not checked yet", () => {
    const s = surface("p", "web", ["a", "gone"]);
    expect(notChecked(s, null, null)).toEqual([
      "Before: not rendered for this revision", "After: not rendered for this revision",
    ]);
    expect(notChecked(surface("q", "web", ["a"]), output("x"), output("y"))).toEqual([]);
    // review-17.4: a listed file outside the change is not "not checked"; it is said beside Relates to.
    expect(notChecked(s, output("x"), output("y"))).toEqual([]);
    expect(pathsOutsideChange(s, [target("a")])).toEqual(["gone"]);
  });
  it("names the provider and the request without a collection's variables (review-17.2)", () => {
    expect(providerName("bruno")).toBe("Bruno");
    expect(providerName("service")).toBe("Service");
    expect(plainLocator("GET {{baseUrl}}/api/status")).toBe("GET /api/status");
    // Only the base address goes; a variable inside the path is part of what is requested.
    expect(plainLocator("GET {{ host }}/v1/{{version}}/orders")).toBe("GET /v1/{{version}}/orders");
    expect(plainLocator("/pricing")).toBe("/pricing");
  });
});
