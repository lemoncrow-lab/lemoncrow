import { describe, expect, it } from "vitest";

import { retainTargetsOnSameRevision } from "./reviewTargetHydration";
import type { ReviewTarget, ReviewTargetList } from "./types";

const target = (id: string, state: ReviewTarget["state"] = "unreviewed") =>
  ({
    target_id: id,
    path: `src/${id}.ts`,
    state,
  }) as ReviewTarget;
const list = (
  revision: string,
  targets: ReviewTarget[],
  complete: boolean
): ReviewTargetList =>
  ({
    revision_id: revision,
    targets,
    complete,
    outline: [],
    progress: { target_count: 1203 },
  }) as unknown as ReviewTargetList;

describe("same-Snapshot target hydration", () => {
  it("retains a full 401-file target model when a late compact startup load finishes", () => {
    const full = list("snapshot-0", [target("first"), target("last")], true);
    const startup = list("snapshot-0", [target("first", "reviewed")], false);
    const result = retainTargetsOnSameRevision(full, startup);
    expect(result.complete).toBe(true);
    expect(result.targets.map((item) => item.target_id)).toEqual([
      "first",
      "last",
    ]);
    expect(result).toBe(full);
    expect(result.targets[0].state).toBe("unreviewed");
    expect(result.targets[1]).toBe(full.targets[1]);
    expect(result.progress).toBe(full.progress);
  });

  it("retains an explicitly opened distant target across a compact reload", () => {
    const previous = list(
      "snapshot-0",
      [target("first"), target("distant")],
      false
    );
    const startup = list("snapshot-0", [target("first")], false);
    expect(
      retainTargetsOnSameRevision(previous, startup).targets.map(
        (row) => row.target_id
      )
    ).toEqual(["first", "distant"]);
  });

  it("never carries targets forward to a different Snapshot", () => {
    const previous = list(
      "snapshot-0",
      [target("first"), target("distant")],
      true
    );
    const next = list("snapshot-1", [target("first")], false);
    expect(retainTargetsOnSameRevision(previous, next)).toBe(next);
  });

  it("does not carry a differently ordered full model into a file-order refresh", () => {
    const previous = {
      ...list("snapshot-0", [target("first"), target("distant")], true),
      order: "recommended" as const,
    };
    const next = {
      ...list("snapshot-0", [target("first")], false),
      order: "file" as const,
    };
    expect(retainTargetsOnSameRevision(previous, next)).toBe(next);
  });

  it("lets an authoritative complete model replace the previous projection", () => {
    const previous = list(
      "snapshot-0",
      [target("first"), target("distant")],
      true
    );
    const updated = list("snapshot-0", [target("first")], true);
    expect(retainTargetsOnSameRevision(previous, updated)).toBe(updated);
  });
});
