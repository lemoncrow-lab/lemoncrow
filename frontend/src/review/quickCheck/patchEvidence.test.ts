/** readPatches: the part in hand is read alone first, the rest follow in rounds, and every outcome is recorded. */
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fetchFile } from "../reviewApi";
import { patchEvidence, readPatches, resetPatchEvidence } from "./patchEvidence";

vi.mock("../reviewApi", () => ({ fetchFile: vi.fn() }));

const apiFile = vi.mocked(fetchFile);
const REVIEW = "session-1";
const REVISION = "rrv-1";

const usable = (path: string) => ({ path, revision_id: REVISION, renderable: true, patch: "@@ -1 +1 @@" }) as never;
const paths = (count: number) => Array.from({ length: count }, (_, index) => `src/file_${index}.py`);

/** One promise per requested path, settled by the test, so the order the reads start in is the thing observed. */
function heldReads() {
  const held = new Map<string, { settle: (value: unknown) => void; fail: () => void }>();
  apiFile.mockImplementation((_id: string, path: string) => new Promise((resolve, reject) => {
    held.set(path, { settle: resolve, fail: () => reject(new Error("no")) });
  }) as never);
  return held;
}
const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  apiFile.mockReset();
  resetPatchEvidence();
});

describe("readPatches", () => {
  it("reads the part in hand alone, then the rest in rounds of twenty", async () => {
    const held = heldReads();
    const all = paths(45);
    const done = readPatches("rev", REVIEW, REVISION, all, { inHand: all[30] });
    await tick();
    expect([...held.keys()]).toEqual([all[30]]);
    held.get(all[30]!)!.settle(usable(all[30]!));
    await tick();
    // The part in hand is on record before any of its neighbours has been asked for.
    expect(patchEvidence(REVIEW, REVISION).loaded.has(all[30]!)).toBe(true);
    expect(held.size).toBe(1 + 20);
    for (const path of all) held.get(path)?.settle(usable(path));
    await tick();
    expect(held.size).toBe(1 + 20 + 20);
    for (const path of all) held.get(path)?.settle(usable(path));
    await tick();
    expect(held.size).toBe(45);
    for (const path of all) held.get(path)?.settle(usable(path));
    await done;
    expect(apiFile).toHaveBeenCalledTimes(45);
    expect(patchEvidence(REVIEW, REVISION).loaded.size).toBe(45);
  });

  it("does not wait on the part in hand to be usable: a failed read still lets the rest go, and is recorded as unusable", async () => {
    const held = heldReads();
    const all = paths(3);
    const done = readPatches("rev", REVIEW, REVISION, all, { inHand: all[0] });
    await tick();
    held.get(all[0]!)!.fail();
    await tick();
    expect(held.has(all[1]!)).toBe(true);
    expect(held.has(all[2]!)).toBe(true);
    held.get(all[1]!)!.settle(usable(all[1]!));
    held.get(all[2]!)!.settle(usable(all[2]!));
    await done;
    const evidence = patchEvidence(REVIEW, REVISION);
    expect([...evidence.failed]).toEqual([all[0]]);
    expect([...evidence.loaded]).toEqual([all[1], all[2]]);
  });

  it("reads in plain rounds when the part in hand is not among the paths, or none is named", async () => {
    const held = heldReads();
    const all = paths(25);
    void readPatches("rev", REVIEW, REVISION, all, { inHand: "src/elsewhere.py" });
    await tick();
    expect(held.size).toBe(20);
    held.clear();
    apiFile.mockClear();
    void readPatches("rev", REVIEW, REVISION, all);
    await tick();
    expect(held.size).toBe(20);
  });

  it("never records another revision's code, a patchless file, or an unrenderable one as usable", async () => {
    const held = heldReads();
    const all = paths(4);
    const done = readPatches("rev", REVIEW, REVISION, all, { inHand: all[0] });
    await tick();
    held.get(all[0]!)!.settle({ ...(usable(all[0]!) as object), revision_id: "rrv-other" });
    await tick();
    held.get(all[1]!)!.settle({ ...(usable(all[1]!) as object), patch: "" });
    held.get(all[2]!)!.settle({ ...(usable(all[2]!) as object), renderable: false });
    held.get(all[3]!)!.settle(usable(all[3]!));
    await done;
    const evidence = patchEvidence(REVIEW, REVISION);
    expect([...evidence.failed].sort()).toEqual([all[0], all[1], all[2]].sort());
    expect([...evidence.loaded]).toEqual([all[3]]);
  });
});
