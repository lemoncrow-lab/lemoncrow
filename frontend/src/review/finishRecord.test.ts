import { describe, expect, it } from "vitest";

import { finishRecordFacts } from "./finishRecord";
import type { ReviewTarget } from "./types";

function target(id: string, overrides: Partial<ReviewTarget> = {}): ReviewTarget {
  return {
    target_id: id,
    unit_key: id,
    kind: "symbol",
    path: "billing/charge.py",
    label: id,
    symbol: id,
    start_line: 1,
    end_line: 2,
    hunk_ordinals: [],
    spans: [],
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: 0,
    attention_level: "normal",
    reasons: [],
    additions: 1,
    deletions: 0,
    fingerprint_method: "",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    ...overrides,
  };
}

describe("finishRecordFacts", () => {
  it("names the revision and commit, the marks, the passed checks, and only the conditions not counted elsewhere", () => {
    const facts = finishRecordFacts({
      revision: { revision_number: 2, head_sha: "base1234", commit_sha: "abc1234def", dirty: false },
      targets: [
        target("charge", { state: "reviewed" }),
        target("refund", { state: "reviewed", changed_since_mark: true }),
        target("void"),
      ],
      readiness: {
        evidenceFacts: [
          { id: "evidence:pytest", label: "pytest", detail: "", source: "ci", state: "verified" },
          { id: "evidence:ruff", label: "ruff", detail: "", source: "agent", state: "not_run" },
        ],
        blockers: [
          { id: "unjudged", label: "1 part needs judgment", detail: "", tone: "amber" },
          { id: "comments", label: "2 open discussions", detail: "", tone: "amber" },
          { id: "latest-snapshot-reviewed", label: "Latest revision not reviewed yet", detail: "", tone: "amber" },
          { id: "verification", label: "Verification requirements", detail: "", tone: "amber" },
        ],
      },
      reviewer: "alice",
    });
    expect(facts.revisionNumber).toBe(2);
    expect(facts.headSha).toBe("abc1234def");
    expect(facts.targets.map((item) => [item.id, item.reviewed])).toEqual([["charge", true], ["refund", false], ["void", false]]);
    expect(facts.passedChecks).toEqual([{ id: "evidence:pytest", label: "pytest", source: "ci" }]);
    expect(facts.openConditions).toEqual([{ id: "verification", label: "Verification requirements" }]);
    expect(facts.reviewer).toBe("alice");
  });

  it("lists the open parts in the engine's queue order with the risky ones marked", () => {
    const placed = (id: string, position: number, attention: "review_first" | "understand", state: ReviewTarget["state"] = "unreviewed") =>
      target(id, {
        state,
        priority: {
          version: 1, tier: attention === "review_first" ? 1 : 4, attention, outstanding: state !== "reviewed", position,
          reasons: [{ code: attention === "review_first" ? "changed_since_judgment" : "unreviewed", detail: `why ${id}` }],
        },
      } as Partial<ReviewTarget>);
    const facts = finishRecordFacts({
      revision: { revision_number: 0, head_sha: "", dirty: false },
      targets: [placed("calm", 2, "understand"), placed("done", 0, "understand", "reviewed"), placed("risky", 1, "review_first")],
      readiness: { evidenceFacts: [], blockers: [{ id: "unresolved-checks", label: "3 unresolved checks", detail: "", tone: "amber" }] },
      reviewer: "alice",
      notMeasured: 4,
    });
    expect(facts.openParts?.map((part) => [part.id, part.risky, part.reason])).toEqual([
      ["risky", true, "why risky"],
      ["calm", false, "why calm"],
    ]);
    expect(facts.notMeasured).toBe(4);
    expect(facts.openConditions).toEqual([{ id: "unresolved-checks", label: "Tests or checks have not run" }]);
  });

  it("says checks have not run even when the server's landing conditions replaced the local blockers", () => {
    const facts = finishRecordFacts({
      revision: { revision_number: 0, head_sha: "", dirty: false },
      targets: [],
      readiness: { evidenceFacts: [], blockers: [], unresolvedChecks: 4, failedChecks: 0 },
      reviewer: "alice",
    });
    expect(facts.openConditions).toEqual([{ id: "unresolved-checks", label: "Tests or checks have not run" }]);
    const failed = finishRecordFacts({
      revision: { revision_number: 0, head_sha: "", dirty: false },
      targets: [],
      readiness: { evidenceFacts: [], blockers: [{ id: "failed-checks", label: "1 failed check", detail: "", tone: "rose" }], unresolvedChecks: 4, failedChecks: 1 },
      reviewer: "alice",
    });
    expect(failed.openConditions.map((item) => item.label)).toEqual(["1 failed check"]);
  });

  it("falls back to the head commit when the capture is not a commit", () => {
    const facts = finishRecordFacts({
      revision: { revision_number: 0, head_sha: "base1234", dirty: true },
      targets: [],
      readiness: { evidenceFacts: [], blockers: [] },
      reviewer: "",
    });
    expect(facts.headSha).toBe("base1234");
    expect(facts.dirty).toBe(true);
  });
});
