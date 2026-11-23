import { describe, expect, it } from "vitest";

import type { Draft } from "./annotationModel";
import {
  applyStableOrder,
  createReviewStoreState,
  mergeOutline,
  reviewStoreReducer,
  shiftOutlineState,
  withPriorities,
  type ReviewStoreAction,
  type ReviewStoreState,
} from "./reviewStore";
import type {
  Annotation,
  BulkMarkResult,
  BulkUndoResult,
  MarkResult,
  MarkState,
  RefreshInfo,
  ReviewOutlineItem,
  ReviewOverview,
  ReviewTarget,
  ReviewTargetList,
  ReviewTargetPriority,
} from "./types";

const target = (id: string, state: MarkState = "unreviewed", path = `src/${id}.ts`): ReviewTarget =>
  ({
    target_id: id,
    unit_key: `unit-${id}`,
    path,
    state,
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_level: "normal",
  }) as ReviewTarget;

const row = (path: string, counts: Partial<ReviewOutlineItem> = {}): ReviewOutlineItem =>
  ({
    path,
    target_count: 2,
    reviewed: 0,
    changed_since_review: 0,
    needs_changes: 0,
    unreviewed: 2,
    unknown: 0,
    mechanical: 0,
    ...counts,
  }) as ReviewOutlineItem;

const list = (
  revision: string,
  targets: ReviewTarget[],
  extra: Partial<ReviewTargetList> = {},
): ReviewTargetList =>
  ({
    revision_id: revision,
    order: "recommended",
    targets,
    progress: { target_count: targets.length, reviewed: 0, changed_since_review: 0, needs_changes: 0, unreviewed: targets.length, unknown: 0, mechanical: 0 },
    outline: [row("src/a.ts")],
    complete: true,
    ...extra,
  }) as ReviewTargetList;

const note = (id: string, state: Annotation["state"] = "open", updatedAt = "2026-01-01T00:00:00Z"): Annotation =>
  ({ id, state, updated_at: updatedAt, body: id }) as Annotation;

const overview = (status: "open" | "finished" | "archived" = "open"): ReviewOverview =>
  ({ session: { status, id: "s" }, revision: { id: "rev-1" } }) as unknown as ReviewOverview;

const draft = (extra: Partial<Draft> = {}): Draft =>
  ({ path: "src/a.ts", startLine: 3, endLine: 5, side: "new", ...extra }) as Draft;

const withState = (patch: Partial<ReviewStoreState>): ReviewStoreState => ({ ...createReviewStoreState(), ...patch });
const run = (state: ReviewStoreState, action: ReviewStoreAction) => reviewStoreReducer(state, action);

function deepFreeze<T>(value: T): T {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const inner of Object.values(value as Record<string, unknown>)) deepFreeze(inner);
  }
  return value;
}

describe("initial state", () => {
  it("matches the Reader's useState defaults", () => {
    expect(createReviewStoreState()).toEqual({
      overview: null,
      revisionAdvanced: false,
      revisionInfo: null,
      targetList: null,
      committedMarks: 0,
      bulkUndo: null,
      annotations: [],
      commentsState: "loading",
      draft: null,
      activeTargetId: "",
    });
  });

  it("returns a fresh object each time", () => {
    expect(createReviewStoreState()).not.toBe(createReviewStoreState());
  });
});

describe("revision", () => {
  it("overview/set replaces the overview", () => {
    const next = overview();
    const state = run(createReviewStoreState(), { type: "overview/set", overview: next });
    expect(state.overview).toBe(next);
    expect(run(state, { type: "overview/set", overview: null }).overview).toBeNull();
  });

  it("overview/sessionStatusSet changes only the status; null stays null", () => {
    const base = withState({ overview: overview("open") });
    const state = run(base, { type: "overview/sessionStatusSet", status: "finished" });
    expect(state.overview?.session).toEqual({ status: "finished", id: "s" });
    expect(state.overview?.revision).toBe(base.overview?.revision);
    const empty = createReviewStoreState();
    expect(run(empty, { type: "overview/sessionStatusSet", status: "finished" })).toBe(empty);
  });

  it("revision/advancedSet sets and clears", () => {
    const on = run(createReviewStoreState(), { type: "revision/advancedSet", advanced: true });
    expect(on.revisionAdvanced).toBe(true);
    expect(run(on, { type: "revision/advancedSet", advanced: false }).revisionAdvanced).toBe(false);
  });

  it("revision/infoSet sets and clears", () => {
    const info = { revision_id: "rev-2" } as unknown as RefreshInfo;
    const set = run(createReviewStoreState(), { type: "revision/infoSet", info });
    expect(set.revisionInfo).toBe(info);
    expect(run(set, { type: "revision/infoSet", info: null }).revisionInfo).toBeNull();
  });
});

describe("parts", () => {
  it("parts/set replaces the list", () => {
    const next = list("r1", [target("a")]);
    const state = run(createReviewStoreState(), { type: "parts/set", targetList: next });
    expect(state.targetList).toBe(next);
    expect(run(state, { type: "parts/set", targetList: null }).targetList).toBeNull();
  });

  it("parts/startupRetained keeps a complete list by identity and takes a different revision's list", () => {
    const full = list("r1", [target("a"), target("b")], { complete: true });
    const startup = list("r1", [target("a", "reviewed")], { complete: false });
    const base = withState({ targetList: full });
    expect(run(base, { type: "parts/startupRetained", incoming: startup })).toBe(base);
    const other = list("r2", [target("a")], { complete: false });
    expect(run(base, { type: "parts/startupRetained", incoming: other }).targetList).toBe(other);
    expect(run(createReviewStoreState(), { type: "parts/startupRetained", incoming: startup }).targetList).toBe(startup);
  });

  it("parts/refreshedIfUnchanged applies when before matches, ignored otherwise", () => {
    const before = list("r1", [target("a")]);
    const next = list("r1", [target("a"), target("b")]);
    const base = withState({ targetList: before });
    expect(run(base, { type: "parts/refreshedIfUnchanged", before, next }).targetList).toBe(next);
    const moved = withState({ targetList: list("r1", [target("z")]) });
    expect(run(moved, { type: "parts/refreshedIfUnchanged", before, next })).toBe(moved);
    const empty = createReviewStoreState();
    expect(run(empty, { type: "parts/refreshedIfUnchanged", before: null, next }).targetList).toBe(next);
  });

  it("parts/pathSliceMerged appends unseen targets, keeps outline, progress, movement, complete", () => {
    const current = list("r1", [target("a"), target("b")], { complete: false, movement: null });
    const slice = list("r1", [target("b"), target("c")], { complete: true, outline: [row("x")] });
    const state = run(withState({ targetList: current }), { type: "parts/pathSliceMerged", slice, stableOrder: [] });
    expect(state.targetList?.targets.map((item) => item.target_id)).toEqual(["a", "b", "c"]);
    expect(state.targetList?.targets[1]).toBe(current.targets[1]);
    expect(state.targetList?.outline).toBe(current.outline);
    expect(state.targetList?.progress).toBe(current.progress);
    expect(state.targetList?.movement).toBe(current.movement);
    expect(state.targetList?.complete).toBe(false);
  });

  it("parts/pathSliceMerged ignores another revision or a missing list", () => {
    const current = list("r1", [target("a")], { complete: false });
    const base = withState({ targetList: current });
    const other = list("r2", [target("c")]);
    expect(run(base, { type: "parts/pathSliceMerged", slice: other, stableOrder: [] })).toBe(base);
    const empty = createReviewStoreState();
    expect(run(empty, { type: "parts/pathSliceMerged", slice: other, stableOrder: [] })).toBe(empty);
  });

  it("parts/pathSliceMerged applies the stable order on a complete list only", () => {
    const slice = list("r1", [target("c")]);
    const complete = list("r1", [target("a"), target("b")], { complete: true });
    const ordered = run(withState({ targetList: complete }), {
      type: "parts/pathSliceMerged", slice, stableOrder: ["b", "a"],
    });
    expect(ordered.targetList?.targets.map((item) => item.target_id)).toEqual(["b", "a", "c"]);
    const partial = list("r1", [target("a"), target("b")], { complete: false });
    const unordered = run(withState({ targetList: partial }), {
      type: "parts/pathSliceMerged", slice, stableOrder: ["b", "a"],
    });
    expect(unordered.targetList?.targets.map((item) => item.target_id)).toEqual(["a", "b", "c"]);
  });

  it("parts/pathSliceMerged treats an undefined complete as complete: it takes the stable order and keeps complete undefined", () => {
    const slice = list("r1", [target("c")]);
    const unknown = list("r1", [target("a"), target("b")], { complete: undefined });
    expect(unknown.complete).toBeUndefined();
    const state = run(withState({ targetList: unknown }), {
      type: "parts/pathSliceMerged", slice, stableOrder: ["b", "a"],
    });
    expect(state.targetList?.targets.map((item) => item.target_id)).toEqual(["b", "a", "c"]);
    expect(state.targetList?.complete).toBeUndefined();
  });

  it("parts/commentTargetApplied swaps by unit_key, applies priorities, no-op on a null list", () => {
    const current = list("r1", [target("a"), target("b")]);
    const updated = { ...target("b", "needs_changes") };
    const progress = { ...current.progress, needs_changes: 1, unreviewed: 1 };
    const priority = { rank: 1 } as unknown as ReviewTargetPriority;
    const state = run(withState({ targetList: current }), {
      type: "parts/commentTargetApplied", target: updated, progress, priorities: { a: priority },
    });
    expect(state.targetList?.targets[1]).toBe(updated);
    expect(state.targetList?.targets[0].priority).toBe(priority);
    expect(state.targetList?.progress).toBe(progress);
    expect(state.targetList?.outline).toBe(current.outline);
    const outline = [row("src/z.ts")];
    const withOutline = run(withState({ targetList: current }), {
      type: "parts/commentTargetApplied", target: updated, progress, outline,
    });
    expect(withOutline.targetList?.outline).toBe(outline);
    const empty = createReviewStoreState();
    expect(run(empty, { type: "parts/commentTargetApplied", target: updated, progress })).toBe(empty);
  });

  it("parts/pathSliceMerged returns a new list even when the slice adds nothing, and still applies the stable order", () => {
    const complete = list("r1", [target("a"), target("b")], { complete: true });
    const base = withState({ targetList: complete });
    const state = run(base, { type: "parts/pathSliceMerged", slice: list("r1", [target("a")]), stableOrder: ["b", "a"] });
    expect(state).not.toBe(base);
    expect(state.targetList).not.toBe(complete);
    expect(state.targetList?.targets.map((item) => item.target_id)).toEqual(["b", "a"]);
    const partial = list("r1", [target("a")], { complete: false });
    const again = run(withState({ targetList: partial }), { type: "parts/pathSliceMerged", slice: list("r1", [target("a")]), stableOrder: [] });
    expect(again.targetList).not.toBe(partial);
    expect(again.targetList?.targets.map((item) => item.target_id)).toEqual(["a"]);
  });

  it("parts/commentTargetApplied matches by unit_key, not target_id, and merges outlineUpdates", () => {
    const current = list("r1", [target("a"), target("b")]);
    // Same unit as `b`, but a different target_id: only a unit_key match swaps it.
    const swapped = { ...target("b", "needs_changes"), target_id: "b-renamed" } as ReviewTarget;
    const update = row("src/a.ts", { needs_changes: 1, unreviewed: 1 });
    const state = run(withState({ targetList: current }), {
      type: "parts/commentTargetApplied", target: swapped, progress: current.progress, outlineUpdates: [update],
    });
    expect(state.targetList?.targets[1]).toBe(swapped);
    expect(state.targetList?.targets[0]).toBe(current.targets[0]);
    expect(state.targetList?.outline).toEqual([update]);
  });
});

describe("marks", () => {
  it("marks/swapped honours expected, shifts outline counts, returns the same state on no match", () => {
    const current = list("r1", [target("a"), target("b", "reviewed")], { outline: [row("src/a.ts", { unreviewed: 1, reviewed: 1 })] });
    const base = withState({ targetList: current });
    const next = { ...target("a", "reviewed") };
    const state = run(base, { type: "marks/swapped", swaps: [{ targetId: "a", expected: "unreviewed", next }] });
    expect(state.targetList?.targets[0]).toBe(next);
    expect(state.targetList?.progress.reviewed).toBe(2);
    expect(state.targetList?.outline[0]).toMatchObject({ unreviewed: 0, reviewed: 2 });
    // A newer local change wins: `expected` no longer matches.
    expect(run(base, { type: "marks/swapped", swaps: [{ targetId: "b", expected: "unreviewed", next }] })).toBe(base);
    expect(run(base, { type: "marks/swapped", swaps: [{ targetId: "nope", expected: "unreviewed", next }] })).toBe(base);
    const empty = createReviewStoreState();
    expect(run(empty, { type: "marks/swapped", swaps: [{ targetId: "a", expected: "unreviewed", next }] })).toBe(empty);
  });

  const markResult = (extra: Partial<MarkResult> = {}): MarkResult =>
    ({ unit_key: "unit-a", requested: "reviewed", recorded: "reviewed", downgraded: false, note: "", ...extra }) as MarkResult;

  it("marks/committed uses the server's totals without other marks in flight", () => {
    const current = list("r1", [target("a"), target("b")]);
    const serverProgress = { ...current.progress, reviewed: 9 };
    const serverOutline = [row("src/server.ts")];
    const state = run(withState({ targetList: current }), {
      type: "marks/committed",
      target: current.targets[0],
      result: markResult({ progress: serverProgress, outline: serverOutline }),
      othersInFlight: false,
    });
    expect(state.committedMarks).toBe(1);
    expect(state.targetList?.targets[0]).toMatchObject({ state: "reviewed", changed_since_mark: false });
    expect(state.targetList?.progress).toBe(serverProgress);
    expect(state.targetList?.outline).toBe(serverOutline);
  });

  it("marks/committed recomputes totals with othersInFlight", () => {
    const current = list("r1", [target("a"), target("b", "reviewed")], { outline: [row("src/a.ts", { unreviewed: 1, reviewed: 1 })] });
    const state = run(withState({ targetList: current }), {
      type: "marks/committed",
      target: current.targets[0],
      result: markResult({ progress: { ...current.progress, reviewed: 99 }, outline: [row("ignored")] }),
      othersInFlight: true,
    });
    expect(state.targetList?.progress.reviewed).toBe(2);
    expect(state.targetList?.outline[0]).toMatchObject({ path: "src/a.ts", unreviewed: 0, reviewed: 2 });
  });

  it("marks/committed shows a fallback target as recorded with the change flag cleared, on both branches", () => {
    const stale = { ...target("a"), changed_since_mark: true } as ReviewTarget;
    const outline = [row("src/a.ts", { unreviewed: 1, reviewed: 1 })];
    const current = list("r1", [stale, target("b", "reviewed")], { outline });
    // The list's stored totals are deliberately stale, so only a recompute from the targets matches.
    const recomputed = { target_count: 2, reviewed: 2, changed_since_review: 0, needs_changes: 0, unreviewed: 0, unknown: 0, mechanical: 0 };
    const settled = run(withState({ targetList: current }), {
      type: "marks/committed", target: stale, result: markResult(), othersInFlight: false,
    });
    expect(settled.targetList?.targets[0]).toMatchObject({ state: "reviewed", changed_since_mark: false });
    expect(settled.targetList?.progress).toEqual(recomputed);
    expect(settled.targetList?.outline).toBe(outline);
    const inFlight = run(withState({ targetList: current }), {
      type: "marks/committed", target: stale, result: markResult(), othersInFlight: true,
    });
    expect(inFlight.targetList?.targets[0]).toMatchObject({ state: "reviewed", changed_since_mark: false });
    expect(inFlight.targetList?.progress).toEqual(recomputed);
    expect(inFlight.targetList?.outline[0]).toMatchObject({ unreviewed: 0, reviewed: 2 });
  });

  it("marks/committed bumps the count even with a null list", () => {
    const state = run(createReviewStoreState(), {
      type: "marks/committed", target: target("a"), result: markResult(), othersInFlight: false,
    });
    expect(state.committedMarks).toBe(1);
    expect(state.targetList).toBeNull();
  });

  const bulkResult = (extra: Partial<BulkMarkResult> = {}): BulkMarkResult =>
    ({ marked: [], skipped: [], group_counts: {}, ...extra }) as unknown as BulkMarkResult;

  it("marks/bulkApplied marks targets and sets bulkUndo, or nulls it when nothing was marked", () => {
    const current = list("r1", [target("a"), target("b")]);
    const base = withState({ targetList: current });
    const state = run(base, {
      type: "marks/bulkApplied",
      path: "src/a.ts",
      result: bulkResult({ marked: [{ unit_key: "unit-a", path: "src/a.ts", event_id: "e1" }] }),
    });
    expect(state.targetList?.targets[0]).toMatchObject({ state: "reviewed", reviewed_revision_id: "r1" });
    expect(state.targetList?.targets[1]).toBe(current.targets[1]);
    expect(state.targetList?.progress.reviewed).toBe(1);
    expect(state.bulkUndo).toEqual({ path: "src/a.ts", marks: [{ unit_key: "unit-a", event_id: "e1" }] });
    const none = run(state, { type: "marks/bulkApplied", path: "src/a.ts", result: bulkResult() });
    expect(none.bulkUndo).toBeNull();
  });

  it("marks/bulkApplied sets bulkUndo even when the list is missing", () => {
    const state = run(createReviewStoreState(), {
      type: "marks/bulkApplied",
      path: "src/a.ts",
      result: bulkResult({ marked: [{ unit_key: "u", path: "src/a.ts", event_id: "e" }] }),
    });
    expect(state.bulkUndo).toEqual({ path: "src/a.ts", marks: [{ unit_key: "u", event_id: "e" }] });
    expect(state.targetList).toBeNull();
  });

  it("marks/bulkUndone reverts targets, clears the change flag and reviewed revision, and clears bulkUndo", () => {
    const reviewed = (id: string) =>
      ({ ...target(id, "reviewed"), changed_since_mark: true, reviewed_revision_id: "r0" }) as ReviewTarget;
    const outline = [row("src/a.ts", { reviewed: 2, unreviewed: 0 })];
    const current = list("r1", [reviewed("a"), reviewed("b")], { outline });
    const base = withState({ targetList: current, bulkUndo: { path: "src/a.ts", marks: [{ unit_key: "unit-a", event_id: "e1" }] } });
    const result = { undone: [{ unit_key: "unit-a", path: "src/a.ts" }], skipped: [], group_counts: {} } as unknown as BulkUndoResult;
    const state = run(base, { type: "marks/bulkUndone", result });
    expect(state.targetList?.targets[0]).toMatchObject({ state: "unreviewed", changed_since_mark: false, reviewed_revision_id: "" });
    expect(state.targetList?.targets[1]).toBe(current.targets[1]);
    // No server totals: progress is recomputed from the targets, the outline is kept.
    expect(state.targetList?.progress).toEqual({ target_count: 2, reviewed: 1, changed_since_review: 0, needs_changes: 0, unreviewed: 1, unknown: 0, mechanical: 0 });
    expect(state.targetList?.outline).toBe(outline);
    expect(state.bulkUndo).toBeNull();
    const noList = run(withState({ bulkUndo: base.bulkUndo }), { type: "marks/bulkUndone", result });
    expect(noList.bulkUndo).toBeNull();
  });

  it("marks/committed leaves the outline alone when the list already shows the mark (othersInFlight)", () => {
    // The real optimistic shape: the list already shows `reviewed`, the target passed in is the pre-mark copy.
    const outline = [row("src/a.ts", { unreviewed: 1, reviewed: 1 })];
    const current = list("r1", [target("a", "reviewed"), target("b", "reviewed")], { outline });
    const state = run(withState({ targetList: current }), {
      type: "marks/committed",
      target: target("a", "unreviewed"),
      result: markResult({ recorded: "reviewed" }),
      othersInFlight: true,
    });
    expect(state.committedMarks).toBe(1);
    expect(state.targetList?.outline).toBe(outline);
    expect(state.targetList?.progress.reviewed).toBe(2);
  });

  it("marks/committed takes the server's target and priorities, and merges outline_updates", () => {
    const current = list("r1", [target("a"), target("b")]);
    const fromServer = { ...target("a", "reviewed"), label: "from-server" } as ReviewTarget;
    const priority = { rank: 3 } as unknown as ReviewTargetPriority;
    const update = row("src/a.ts", { reviewed: 2, unreviewed: 0 });
    const state = run(withState({ targetList: current }), {
      type: "marks/committed",
      target: current.targets[0],
      result: markResult({ target: fromServer, priorities: { b: priority }, outline_updates: [update] }),
      othersInFlight: false,
    });
    expect(state.targetList?.targets[0]).toBe(fromServer);
    expect(state.targetList?.targets[1].priority).toBe(priority);
    expect(state.targetList?.outline).toEqual([update]);
    // Recomputed branch: the server's target still wins, its priorities and outline do not.
    const inFlight = run(withState({ targetList: current }), {
      type: "marks/committed",
      target: current.targets[0],
      result: markResult({ target: fromServer, priorities: { b: priority }, outline_updates: [update] }),
      othersInFlight: true,
    });
    expect(inFlight.targetList?.targets[0]).toBe(fromServer);
    expect(inFlight.targetList?.targets[1].priority).toBeUndefined();
    expect(inFlight.committedMarks).toBe(1);
  });

  it("marks/bulkApplied clears changed_since_mark, applies priorities and the server's progress", () => {
    const stale = { ...target("a"), changed_since_mark: true } as ReviewTarget;
    const current = list("r1", [stale, target("b")]);
    const priority = { rank: 2 } as unknown as ReviewTargetPriority;
    const progress = { ...current.progress, reviewed: 7 };
    const state = run(withState({ targetList: current }), {
      type: "marks/bulkApplied",
      path: "src/a.ts",
      result: bulkResult({
        marked: [{ unit_key: "unit-a", path: "src/a.ts", event_id: "e1" }],
        priorities: { b: priority },
        progress,
      }),
    });
    expect(state.targetList?.targets[0].changed_since_mark).toBe(false);
    expect(state.targetList?.targets[1].priority).toBe(priority);
    expect(state.targetList?.progress).toBe(progress);
    const update = row("src/a.ts", { reviewed: 2, unreviewed: 0 });
    const updated = run(withState({ targetList: current }), {
      type: "marks/bulkApplied",
      path: "src/a.ts",
      result: bulkResult({ marked: [{ unit_key: "unit-a", path: "src/a.ts", event_id: "e1" }], outline_updates: [update] }),
    });
    expect(updated.targetList?.outline).toEqual([update]);
  });

  it("marks/bulkApplied takes the server's outline over outline_updates", () => {
    const current = list("r1", [target("a"), target("b")]);
    const outline = [row("src/a.ts", { reviewed: 1, unreviewed: 1 })];
    const state = run(withState({ targetList: current }), {
      type: "marks/bulkApplied",
      path: "src/a.ts",
      result: bulkResult({
        marked: [{ unit_key: "unit-a", path: "src/a.ts", event_id: "e1" }],
        outline,
        outline_updates: [row("src/q.ts")],
      }),
    });
    expect(state.targetList?.outline).toBe(outline);
  });

  it("marks/bulkUndone applies priorities, the server's progress, outline_updates and the server's outline", () => {
    const current = list("r1", [target("a", "reviewed"), target("b", "reviewed")], {
      outline: [row("src/a.ts", { reviewed: 2, unreviewed: 0 })],
    });
    const priority = { rank: 2 } as unknown as ReviewTargetPriority;
    const progress = { ...current.progress, reviewed: 5 };
    // Differs from the current row, so an ignored update cannot pass by equality.
    const update = row("src/a.ts", { reviewed: 1, unreviewed: 1 });
    const result = {
      undone: [{ unit_key: "unit-a", path: "src/a.ts" }],
      skipped: [],
      group_counts: {},
      priorities: { b: priority },
      progress,
      outline_updates: [update],
    } as unknown as BulkUndoResult;
    const state = run(withState({ targetList: current }), { type: "marks/bulkUndone", result });
    expect(state.targetList?.targets[1].priority).toBe(priority);
    expect(state.targetList?.progress).toBe(progress);
    expect(state.targetList?.outline).toEqual([update]);
    const outline = [row("src/z.ts", { reviewed: 0, unreviewed: 2 })];
    const full = run(withState({ targetList: current }), {
      type: "marks/bulkUndone", result: { ...result, outline } as unknown as BulkUndoResult,
    });
    expect(full.targetList?.outline).toBe(outline);
  });
});

describe("notes", () => {
  it("notes/loadStarted sets loading", () => {
    const state = run(withState({ commentsState: "ready" }), { type: "notes/loadStarted" });
    expect(state.commentsState).toBe("loading");
  });

  it("notes/loaded sets notes and ready", () => {
    const notes = [note("n1")];
    const state = run(createReviewStoreState(), { type: "notes/loaded", annotations: notes });
    expect(state.annotations).toBe(notes);
    expect(state.commentsState).toBe("ready");
  });

  it("notes/loadFailed sets failed and keeps notes", () => {
    const notes = [note("n1")];
    const state = run(withState({ annotations: notes, commentsState: "ready" }), { type: "notes/loadFailed" });
    expect(state.commentsState).toBe("failed");
    expect(state.annotations).toBe(notes);
  });

  it("notes/set replaces", () => {
    const notes = [note("n2")];
    expect(run(withState({ annotations: [note("n1")] }), { type: "notes/set", annotations: notes }).annotations).toBe(notes);
  });

  it("notes/merged keeps newer updated_at and drops rows the server no longer lists", () => {
    const local = [note("keep", "open", "2026-02-01T00:00:00Z"), note("gone"), note("older", "open", "2026-01-01T00:00:00Z")];
    const server = [note("keep", "resolved", "2026-01-01T00:00:00Z"), note("older", "resolved", "2026-03-01T00:00:00Z"), note("new")];
    const state = run(withState({ annotations: local }), { type: "notes/merged", annotations: server });
    expect(state.annotations).toHaveLength(3);
    expect(state.annotations[0]).toBe(local[0]);
    expect(state.annotations[1]).toBe(server[1]);
    expect(state.annotations[2]).toBe(server[2]);
  });

  it("notes/merged takes the server's row when updated_at ties", () => {
    const local = [note("n1", "open", "2026-01-01T00:00:00Z")];
    const server = [note("n1", "resolved", "2026-01-01T00:00:00Z")];
    const state = run(withState({ annotations: local }), { type: "notes/merged", annotations: server });
    expect(state.annotations[0]).toBe(server[0]);
  });

  it("notes/stateShown changes only when from matches and returns a new array regardless", () => {
    const notes = [note("n1", "open"), note("n2", "open")];
    const base = withState({ annotations: notes });
    const shown = run(base, { type: "notes/stateShown", id: "n1", from: "open", to: "resolved" });
    expect(shown.annotations.map((item) => item.state)).toEqual(["resolved", "open"]);
    expect(shown.annotations[1]).toBe(notes[1]);
    const stale = run(base, { type: "notes/stateShown", id: "n1", from: "orphaned", to: "resolved" });
    expect(stale.annotations.map((item) => item.state)).toEqual(["open", "open"]);
    expect(stale.annotations).not.toBe(notes);
    const unknownId = run(base, { type: "notes/stateShown", id: "zz", from: "open", to: "resolved" });
    expect(unknownId.annotations).not.toBe(notes);
  });

  it("notes/replaced swaps by id", () => {
    const notes = [note("n1"), note("n2")];
    const replacement = note("n1", "resolved");
    const state = run(withState({ annotations: notes }), { type: "notes/replaced", id: "n1", annotation: replacement });
    expect(state.annotations[0]).toBe(replacement);
    expect(state.annotations[1]).toBe(notes[1]);
    expect(state.annotations).not.toBe(notes);
  });
});

describe("draft", () => {
  it("draft/set sets and clears", () => {
    const next = draft();
    const set = run(createReviewStoreState(), { type: "draft/set", draft: next });
    expect(set.draft).toBe(next);
    expect(run(set, { type: "draft/set", draft: null }).draft).toBeNull();
  });

  it("draft/proposalTextFilled fills only a matching proposal range and keeps existing text", () => {
    const fill = { type: "draft/proposalTextFilled", path: "src/a.ts", startLine: 3, endLine: 5, text: "selected" } as const;
    const proposal = draft({ mode: "proposal" });
    expect(run(withState({ draft: proposal }), fill).draft?.replacementText).toBe("selected");
    const typed = draft({ mode: "proposal", replacementText: "mine" });
    expect(run(withState({ draft: typed }), fill).draft?.replacementText).toBe("mine");
    for (const other of [
      draft({ mode: "annotation" }),
      draft({ mode: "proposal", path: "src/b.ts" }),
      draft({ mode: "proposal", startLine: 4 }),
      draft({ mode: "proposal", endLine: 9 }),
    ]) {
      const base = withState({ draft: other });
      expect(run(base, fill)).toBe(base);
    }
    const empty = createReviewStoreState();
    expect(run(empty, fill)).toBe(empty);
  });

  it("draft/proposalLinked links the id, keeps the rest of the draft and leaves null alone", () => {
    const held = draft({ mode: "proposal", body: "why", replacementText: "new" });
    const linked = run(withState({ draft: held }), { type: "draft/proposalLinked", proposalId: "p1" });
    expect(linked.draft).toEqual({ ...held, proposalId: "p1" });
    const empty = createReviewStoreState();
    expect(run(empty, { type: "draft/proposalLinked", proposalId: "p1" })).toBe(empty);
  });
});

describe("current part", () => {
  it("part/selected sets the id", () => {
    expect(run(createReviewStoreState(), { type: "part/selected", targetId: "t9" }).activeTargetId).toBe("t9");
  });
});

describe("invariants", () => {
  it("setting the same value returns the same state object for each set-style action", () => {
    const next = overview();
    const info = { revision_id: "r" } as unknown as RefreshInfo;
    const targets = list("r1", [target("a")]);
    const notes = [note("n1")];
    const current = draft();
    const base = withState({
      overview: next,
      revisionAdvanced: true,
      revisionInfo: info,
      targetList: targets,
      annotations: notes,
      draft: current,
      activeTargetId: "t1",
      commentsState: "ready",
    });
    const same: ReviewStoreAction[] = [
      { type: "overview/set", overview: next },
      { type: "revision/advancedSet", advanced: true },
      { type: "revision/infoSet", info },
      { type: "parts/set", targetList: targets },
      { type: "notes/set", annotations: notes },
      { type: "notes/loaded", annotations: notes },
      { type: "draft/set", draft: current },
      { type: "part/selected", targetId: "t1" },
    ];
    for (const action of same) expect(run(base, action), action.type).toBe(base);
  });

  // One representative action per type. The mapped type makes a new action a compile error until it is listed here.
  type ActionCases = { [K in ReviewStoreAction["type"]]: [Extract<ReviewStoreAction, { type: K }>, Array<keyof ReviewStoreState>] };
  const actionCases = (baseList: ReviewTargetList): ActionCases => {
    const t1 = target("a", "reviewed");
    return {
      "overview/set": [{ type: "overview/set", overview: overview("finished") }, ["overview"]],
      "overview/sessionStatusSet": [{ type: "overview/sessionStatusSet", status: "archived" }, ["overview"]],
      "revision/advancedSet": [{ type: "revision/advancedSet", advanced: true }, ["revisionAdvanced"]],
      "revision/infoSet": [{ type: "revision/infoSet", info: { revision_id: "r9" } as unknown as RefreshInfo }, ["revisionInfo"]],
      "parts/set": [{ type: "parts/set", targetList: list("r2", [target("z")]) }, ["targetList"]],
      "parts/startupRetained": [{ type: "parts/startupRetained", incoming: list("r2", [target("z")], { complete: false }) }, ["targetList"]],
      "parts/refreshedIfUnchanged": [{ type: "parts/refreshedIfUnchanged", before: baseList, next: list("r1", [target("z")]) }, ["targetList"]],
      "parts/pathSliceMerged": [{ type: "parts/pathSliceMerged", slice: list("r1", [target("c")]), stableOrder: ["b", "a"] }, ["targetList"]],
      "parts/commentTargetApplied": [{
        type: "parts/commentTargetApplied",
        target: target("b", "needs_changes"),
        progress: baseList.progress,
        priorities: { a: { rank: 1 } as unknown as ReviewTargetPriority },
        outlineUpdates: [row("src/b.ts")],
      }, ["targetList"]],
      "marks/swapped": [{ type: "marks/swapped", swaps: [{ targetId: "a", expected: "unreviewed", next: t1 }] }, ["targetList"]],
      "marks/committed": [{
        type: "marks/committed",
        target: target("a"),
        result: { recorded: "reviewed", priorities: { b: { rank: 1 } as unknown as ReviewTargetPriority }, outline_updates: [row("src/a.ts")] } as unknown as MarkResult,
        othersInFlight: false,
      }, ["targetList", "committedMarks"]],
      "marks/bulkApplied": [{
        type: "marks/bulkApplied",
        path: "src/a.ts",
        result: { marked: [{ unit_key: "unit-a", path: "src/a.ts", event_id: "e1" }], skipped: [], group_counts: {}, priorities: { b: { rank: 1 } as unknown as ReviewTargetPriority } } as unknown as BulkMarkResult,
      }, ["targetList", "bulkUndo"]],
      "marks/bulkUndone": [{
        type: "marks/bulkUndone",
        result: { undone: [{ unit_key: "unit-b", path: "src/b.ts" }], skipped: [], group_counts: {}, priorities: { a: { rank: 1 } as unknown as ReviewTargetPriority } } as unknown as BulkUndoResult,
      }, ["targetList", "bulkUndo"]],
      "notes/loadStarted": [{ type: "notes/loadStarted" }, ["commentsState"]],
      "notes/loaded": [{ type: "notes/loaded", annotations: [note("n9")] }, ["annotations", "commentsState"]],
      "notes/loadFailed": [{ type: "notes/loadFailed" }, ["commentsState"]],
      "notes/set": [{ type: "notes/set", annotations: [note("n9")] }, ["annotations"]],
      "notes/merged": [{ type: "notes/merged", annotations: [note("n1", "resolved", "2026-02-01T00:00:00Z"), note("n9")] }, ["annotations"]],
      "notes/stateShown": [{ type: "notes/stateShown", id: "n1", from: "open", to: "resolved" }, ["annotations"]],
      "notes/replaced": [{ type: "notes/replaced", id: "n1", annotation: note("n1", "resolved") }, ["annotations"]],
      "draft/set": [{ type: "draft/set", draft: draft({ body: "other" }) }, ["draft"]],
      "draft/proposalTextFilled": [{ type: "draft/proposalTextFilled", path: "src/a.ts", startLine: 3, endLine: 5, text: "x" }, ["draft"]],
      "draft/proposalLinked": [{ type: "draft/proposalLinked", proposalId: "p" }, ["draft"]],
      "part/selected": [{ type: "part/selected", targetId: "t" }, ["activeTargetId"]],
    };
  };
  // Built per test so no test can see another's mutation of shared fixtures.
  const baseState = () => withState({
    overview: overview(),
    targetList: list("r1", [target("a"), target("b", "reviewed")], { complete: true, outline: [row("src/a.ts"), row("src/b.ts")] }),
    annotations: [note("n1")],
    commentsState: "ready",
    draft: draft({ mode: "proposal" }),
    bulkUndo: { path: "src/a.ts", marks: [{ unit_key: "unit-a", event_id: "e0" }] },
  });
  const actionTypes = Object.keys(actionCases(list("r1", []))) as Array<ReviewStoreAction["type"]>;
  const caseFor = (type: ReviewStoreAction["type"]) => {
    const base = baseState();
    const [action, changes] = actionCases(base.targetList as ReviewTargetList)[type];
    return { base, action: action as ReviewStoreAction, changes };
  };

  it("covers every action type and changes only its own slice", () => {
    expect(actionTypes).toHaveLength(24);
    for (const type of actionTypes) {
      const { base, action, changes } = caseFor(type);
      expect(action.type).toBe(type);
      const result = run(base, action);
      expect(result, `${type} must change something`).not.toBe(base);
      for (const key of Object.keys(base) as Array<keyof ReviewStoreState>) {
        if (!changes.includes(key)) expect(result[key], `${type} must not touch ${key}`).toBe(base[key]);
      }
    }
  });

  it("never mutates its state or its action, for any of the 24 actions", () => {
    expect(actionTypes).toHaveLength(24);
    for (const type of actionTypes) {
      const { base, action } = caseFor(type);
      // Frozen state and action: any write throws in strict mode. `refreshedIfUnchanged` holds the frozen list as `before`.
      deepFreeze(base);
      deepFreeze(action);
      expect(() => run(base, action), type).not.toThrow();
    }
  });
});

describe("moved helpers", () => {
  it("applyStableOrder keeps known ids first and appends unseen ones", () => {
    const input = list("r1", [target("a"), target("b"), target("c"), target("d")]);
    const result = applyStableOrder(input, ["c", "gone", "a"]);
    expect(result.targets.map((item) => item.target_id)).toEqual(["c", "a", "b", "d"]);
    expect(result.revision_id).toBe("r1");
    expect(result).not.toBe(input);
    expect(input.targets.map((item) => item.target_id)).toEqual(["a", "b", "c", "d"]);
  });

  it("withPriorities returns the same array without priorities and only overrides listed targets", () => {
    const targets = [target("a"), target("b")];
    expect(withPriorities(targets, null)).toBe(targets);
    const priority = { rank: 2 } as unknown as ReviewTargetPriority;
    const result = withPriorities(targets, { b: priority });
    expect(result[0]).toBe(targets[0]);
    expect(result[1].priority).toBe(priority);
  });

  it("mergeOutline replaces by path and appends new rows", () => {
    const current = [row("a"), row("b")];
    expect(mergeOutline(current, undefined)).toBe(current);
    expect(mergeOutline(current, [])).toBe(current);
    const replacement = row("b", { reviewed: 2 });
    const added = row("c");
    expect(mergeOutline(current, [replacement, added])).toEqual([current[0], replacement, added]);
  });

  it("shiftOutlineState moves one target between buckets and never goes below zero", () => {
    const outline = [row("a", { unreviewed: 1, reviewed: 0 }), row("b", { unreviewed: 0 })];
    expect(shiftOutlineState(outline, "a", "reviewed", "reviewed")).toBe(outline);
    const moved = shiftOutlineState(outline, "a", "unreviewed", "reviewed");
    expect(moved[0]).toMatchObject({ unreviewed: 0, reviewed: 1 });
    expect(moved[1]).toBe(outline[1]);
    expect(shiftOutlineState(outline, "b", "unreviewed", "reviewed")[1]).toMatchObject({ unreviewed: 0, reviewed: 1 });
  });
});
