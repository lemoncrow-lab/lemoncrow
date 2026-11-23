import { describe, expect, it } from "vitest";
import type { Annotation, ReviewTarget } from "../types";
import { type QuickGuard, groupedQuick, latchGuards, latchSession, reasonAfterRun, resetGuardLatch, quickConfirmation, quickGroup, quickGuard, quickParts, quickPatchPaths, quickRemaining } from "./quickCheckModel";

function target(id: string, rest: Partial<ReviewTarget> = {}): ReviewTarget {
  return {
    target_id: id, unit_key: `sym:${id}`, kind: "symbol", path: "app/orders.py", label: id, symbol: id,
    start_line: 1, end_line: 3, hunk_ordinals: [0], spans: [], state: "unreviewed",
    changed_since_mark: false, reviewed_revision_id: "", attention_rank: 1, attention_level: "normal",
    reasons: [], additions: 1, deletions: 1, fingerprint_method: "ast",
    verification: { pass: 0, fail: 0, unknown: 1 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    answer: { category: "quick_check", rule: "", rule_label: "", reason: "" },
    priority: { version: 1, tier: 3, attention: "routine", outstanding: true, position: 1, reasons: [] },
    ...rest,
  };
}

const patch = (content: string, path = "app/orders.py", revision_id = "rev1") => ({
  path, revision_id, patch: `@@ -1,3 +1,3 @@\n def order_total():\n-    ${content.split("|")[0]}\n+    ${content.split("|")[1]}\n     pass\n`,
  renderable: true,
});

const SERVER_LOOP = { guarded: true, code: "loop_bound", reason: "Test didn't run and the loop bound changed." } as const;
const SERVER_CLEAR = { guarded: false, code: "", reason: "" } as const;

describe("Quick check guard (P4-2, QC-1/QC-6)", () => {
  it("never batch-confirms a part whose read code is not this revision's exact, renderable patch", () => {
    const row = target("order_total", { batch_guard: SERVER_CLEAR });
    expect(quickGuard(row, "rev1", patch("x = 1|x = 2")).guarded).toBe(false);
    // A patch not read yet does not decide membership (it is read when the batch is confirmed); one that failed or is another revision's does.
    expect(quickGuard(row, "rev1").guarded).toBe(false);
    expect(quickGuard(row, "rev1", undefined).guarded).toBe(false);
    expect(quickGuard(row, "rev1", null).reason).toContain("unavailable or belongs to another revision");
    expect(quickGuard(row, "rev1", patch("x = 1|x = 2", "app/orders.py", "old")).guarded).toBe(true);
    expect(quickGuard(row, "rev1", patch("x = 1|x = 2", "wrong.py")).guarded).toBe(true);
    expect(quickGuard(row, "rev1", { ...patch("x = 1|x = 2"), renderable: false }).guarded).toBe(true);
    expect(quickGuard(row, "rev1", { ...patch("x = 1|x = 2"), patch: "" }).guarded).toBe(true);
    expect(quickGuard({ ...row, changed_since_mark: true }, "rev1", patch("label = old|label = new")).guarded).toBe(true);
  });

  it("keeps the latch for the page session: leaving Quick check and coming back does not reset it", () => {
    resetGuardLatch();
    const guarded = target("order_total", { batch_guard: SERVER_LOOP });
    latchSession("review-1", [guarded], "rev1");
    const back = latchSession("review-1", [{ ...guarded, batch_guard: SERVER_CLEAR }], "rev1");
    expect(back.guards.get(guarded.target_id)).toEqual(SERVER_LOOP);
    expect(latchSession("review-2", [{ ...guarded, batch_guard: SERVER_CLEAR }], "rev1").guards.get(guarded.target_id)).toEqual(SERVER_CLEAR);
  });

  it("says something true after the reviewer's own run: never 'Test didn't run' next to a finished run", () => {
    const reason = SERVER_LOOP.reason;
    expect(reasonAfterRun(reason, undefined)).toBe(reason);
    expect(reasonAfterRun(reason, { state: "running", verdict: "NOT_RUN" })).toBe(reason);
    expect(reasonAfterRun(reason, { state: "done", verdict: "PASS" })).toBe("Test passed, but nothing shows it checks the changed lines. Answer for this part.");
    expect(reasonAfterRun(reason, { state: "done", verdict: "FAIL" })).toMatch(/^Test failed\./);
    expect(reasonAfterRun(reason, { state: "done", verdict: "UNKNOWN" })).toMatch(/gave no result/);
    const unreadable = "Could not be checked: this file type is not read. This part stays out of the batch.";
    expect(reasonAfterRun(unreadable, { state: "done", verdict: "PASS" })).toBe(unreadable);
  });

  it("takes the server's guard and its reason, and does not read the patch text for arithmetic", () => {
    const row = target("order_total", { batch_guard: SERVER_LOOP });
    expect(quickGuard(row, "rev1", patch("label = old|label = new"))).toEqual({ guarded: true, reason: "Test didn't run and the loop bound changed." });
    // The same arithmetic-looking patch is not guarded when the server says it is clear: the client does not second-guess it.
    const clear = target("order_total", { batch_guard: SERVER_CLEAR });
    expect(quickGuard(clear, "rev1", patch("for i in range(10):|for i in range(9):"))).toEqual({ guarded: false, reason: "" });
  });

  it("fails closed when the server sent no guard for a quick-check part", () => {
    const guard = quickGuard(target("order_total"), "rev1", patch("label = old|label = new"));
    expect(guard.guarded).toBe(true);
    expect(guard.reason).toMatch(/Could not be checked/);
  });

  it("fails closed with a stated reason when the server says it could not check", () => {
    const row = target("go_part", { batch_guard: { guarded: true, code: "unverifiable", reason: "Could not be checked: this file type is not read. This part stays out of the batch." } });
    expect(quickGuard(row, "rev1", patch("a = 1|a = 1")).reason).toMatch(/^Could not be checked: this file type is not read/);
  });

  it("guards an anchored AI result hint even when the server cleared the code", () => {
    const row = target("explain", { path: "app/orders.py", batch_guard: SERVER_CLEAR });
    const finding = { body: "This changes the output of another route" } as Annotation;
    expect(quickGuard(row, "rev1", patch("status = old|status = new"), [finding]).guarded).toBe(true);
    expect(quickGuard(row, "rev1", patch("status = old|status = new"), [{ body: "Move text to a new helper" } as Annotation]).guarded).toBe(false);
  });

  it("latches the first server guard per revision so a mark or a Run test never moves a guarded card into a batch", () => {
    const guarded = target("order_total", { batch_guard: SERVER_LOOP });
    const first = latchGuards({ revision: "", guards: new Map() }, [guarded], "rev1");
    // A mark response replaces the target and drops the field; a later response may even say "ran".
    const replaced = { ...guarded, batch_guard: undefined, state: "reviewed" as const };
    expect(latchGuards(first, [replaced], "rev1").guards.get(guarded.target_id)).toEqual(SERVER_LOOP);
    const ran = { ...guarded, batch_guard: { ...SERVER_CLEAR } };
    expect(latchGuards(first, [ran], "rev1").guards.get(guarded.target_id)).toEqual(SERVER_LOOP);
    // A new revision starts clean.
    expect(latchGuards(first, [replaced], "rev2").guards.size).toBe(0);
    expect(latchGuards(first, [], "rev1")).toBe(first);
  });

  it("never puts lockfile, dependency, or decision parts into a batch regardless of patch", () => {
    const code = patch("source = old|source = new");
    expect(quickGuard(target("lock", { path: "poetry.lock" }), "rev1", { ...code, path: "poetry.lock" }).guarded).toBe(true);
    expect(quickGuard(target("dep", { kind: "dependency" }), "rev1", code).guarded).toBe(true);
    expect(quickGuard(target("decision", { answer: { category: "decision", rule: "", rule_label: "", reason: "" } }), "rev1", code).guarded).toBe(true);
  });

  it("groups only eligible parts, preserves server order, and never counts accepted rules", () => {
    const logic = target("logic", { priority: { ...target("x").priority!, position: 1 } });
    const test = target("test", { path: "tests/test_order.py", priority: { ...target("x").priority!, position: 2 } });
    const config = target("config", { path: "docs/guide.md", priority: { ...target("x").priority!, position: 3 } });
    const guarded = target("guarded", { priority: { ...target("x").priority!, position: 4 } });
    const accepted = target("generated", { answer: { category: "accepted_by_rule", rule: "generated_files", rule_label: "generated files", reason: "" } });
    const rows = quickParts([config, accepted, guarded, test, logic]);
    expect(rows.map((row) => row.target_id)).toEqual(["logic", "test", "config", "guarded"]);
    expect(quickGroup(test)).toBe("Tests");
    expect(quickGroup(config)).toBe("Config and docs");
    const guards = new Map(rows.map((row) => [row.target_id, { guarded: row === guarded, reason: "" } as const]));
    expect(groupedQuick(rows, guards).map((group) => [group.name, group.parts.map((row) => row.target_id)])).toEqual([
      ["Small logic changes", ["logic"]], ["Tests", ["test"]], ["Config and docs", ["config"]],
    ]);
    expect(quickRemaining(rows)).toBe(4);
    expect(quickRemaining([{ ...logic, state: "reviewed" }, { ...guarded, state: "needs_changes" }, { ...test, state: "later" }])).toBe(1);
    expect(quickConfirmation([logic, test])).toBe("2 parts saved as reviewed");
    expect(quickConfirmation([logic])).toBe("1 part saved as reviewed");
  });
});

describe("groups by directory in a large change (SCALE-2)", () => {
  const a = target("a", { path: "src/core/a.py" }), b = target("b", { path: "src/core/b.py" });
  const c = target("c", { path: "src/api/c.py" }), root = target("r", { path: "setup.py" });
  const clear = (...rows: ReviewTarget[]) => new Map<string, QuickGuard>(rows.map((row) => [row.target_id, { guarded: false, reason: "" } as const]));

  it("keeps what is proved routine since the review in one group, ahead of the directories", () => {
    const moved = target("moved", { path: "src/api/moved.py", kind: "hunk", state: "unreviewed", since_review: { routine: true, rule: "rehunked_identical", detail: "" } } as Partial<ReviewTarget>);
    expect(groupedQuick([a, moved, c], clear(a, moved, c), true).map((g) => [g.name, g.parts.map((p) => p.target_id)])).toEqual([
      ["Routine changes since your review", ["moved"]], ["src/api", ["c"]], ["src/core", ["a"]],
    ]);
  });

  it("groups parts under their directory in name order", () => {
    expect(groupedQuick([a, b, c, root], clear(a, b, c, root), true).map((g) => [g.name, g.parts.map((p) => p.target_id)])).toEqual([
      ["Repository root", ["r"]], ["src/api", ["c"]], ["src/core", ["a", "b"]],
    ]);
  });

  it("pins Repository root first even when a directory sorts before it", () => {
    const app = target("p", { path: "app/p.py" });
    expect(groupedQuick([app, root], clear(app, root), true).map((g) => g.name)).toEqual(["Repository root", "app"]);
  });

  it("never puts a guarded part in a group: it has its own card, the rest of its directory still groups", () => {
    const guards = new Map<string, QuickGuard>([...clear(a, c), ["b", { guarded: true, reason: "x" }]]);
    const groups = groupedQuick([a, b, c], guards, true);
    expect(groups.flatMap((g) => g.parts.map((p) => p.target_id))).toEqual(["c", "a"]);
  });

  it("a directory whose every part is guarded has no group", () => {
    expect(groupedQuick([a, b], new Map<string, QuickGuard>([["a", { guarded: true, reason: "x" }], ["b", { guarded: true, reason: "x" }]]), true)).toEqual([]);
  });
});


describe("quickPatchPaths: which patches Quick check reads", () => {
  const files = (count: number) => Array.from({ length: count }, (_, index) => ({ path: `app/f${String(index).padStart(5, "0")}.py` }));
  it("reads every file of a small change, the one in hand first", () => {
    const parts = files(200);
    const paths = quickPatchPaths(parts, "app/f00120.py");
    expect(paths[0]).toBe("app/f00120.py");
    expect([...paths].sort()).toEqual(parts.map((part) => part.path));
  });
  it("counts files, not parts, for the small-change case", () => {
    const parts = files(100).flatMap((part) => [part, part]);
    expect(quickPatchPaths(parts, "app/f00000.py")).toHaveLength(100);
  });
  it("reads a window around the part in hand and a look-ahead after it in a large change", () => {
    const parts = files(10_000);
    const paths = quickPatchPaths(parts, "app/f05000.py");
    expect(paths[0]).toBe("app/f05000.py");
    expect(paths).toHaveLength(30);
    expect([...paths].sort()[0]).toBe("app/f04990.py");
    expect([...paths].sort().at(-1)).toBe("app/f05019.py");
  });
  it("stays bounded at the start and the end of the list, and the bound does not grow with the change", () => {
    const parts = files(10_000);
    expect(quickPatchPaths(parts, "app/f00000.py")).toHaveLength(30);
    const last = quickPatchPaths(parts, "app/f09999.py");
    expect(last).toHaveLength(20);
    expect(last[0]).toBe("app/f09999.py");
    expect(quickPatchPaths(files(201), "app/f00100.py")).toHaveLength(30);
  });
  it("falls back to the start when no part is in hand", () => {
    expect(quickPatchPaths(files(1000), "")[0]).toBe("app/f00000.py");
    expect(quickPatchPaths(files(1000), "")).toHaveLength(30);
  });
});
