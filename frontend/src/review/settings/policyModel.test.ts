import { describe, expect, it } from "vitest";

import type { ReviewDirectoryRow, ReviewPolicyProjection } from "../types";
import { alwaysADecision, commitLine, latestReview, pendingPolicyChanges, repoLabel } from "./policyModel";

const row = (id: string, updated_at: string, project = "shop") => ({ id, updated_at, project }) as ReviewDirectoryRow;
const policy = (focus: NonNullable<ReviewPolicyProjection["authority"]["focus"]>) =>
  ({ authority: { base_state: "present", rules: [], ai_content_access: [], rule_documents: [], errors: [], focus }, future_changes: [], suggestions: [] }) as ReviewPolicyProjection;

describe("which repository the policy is read from", () => {
  it("is the review changed most recently, and none when there is no review", () => {
    expect(latestReview([row("a", "2026-11-01T10:00:00Z"), row("b", "2026-11-03T10:00:00Z"), row("c", "2026-11-02T10:00:00Z")])?.id).toBe("b");
    expect(latestReview([])).toBeNull();
  });

  it("names the repository by its folder", () => {
    expect(repoLabel({ project: "/work/shop" })).toBe("shop");
    expect(repoLabel({})).toBe("");
  });
});

describe("Areas that always need a person (SET-4)", () => {
  it("lists critical paths with the review they require, and leaves out the rest", () => {
    const areas = alwaysADecision(policy([
      { paths: ["app/auth/**"], criticality: "critical", require: ["security"] },
      { paths: ["payments/**", "billing/**"], criticality: "critical" },
      { paths: ["docs/**"], criticality: "low" },
    ]));
    expect(areas).toEqual([
      { paths: "app/auth/**", why: "Critical · needs a security review" },
      { paths: "payments/**, billing/**", why: "Critical" },
    ]);
  });

  it("is empty without a policy", () => {
    expect(alwaysADecision(null)).toEqual([]);
  });
});

describe("History and pending changes (SPEC 4.19)", () => {
  it("reads a commit the way SPEC writes it", () => {
    expect(commitLine({ commit: "7c1d2e0", date: "2026-10-28", author: "Maya Chen", subject: "Add public API routes to always a decision" })).toBe(
      "7c1d2e0 · 2026-10-28 · Maya Chen · “Add public API routes to always a decision”",
    );
  });

  it("lists only the open reviews that edit a policy file", () => {
    const rows = [
      { id: "a", title: "Retry a failed charge and loosen the payments rules", status: "open", edits_policy: true, review_path: "/r/a" },
      { id: "b", title: "Plain change", status: "open" },
      { id: "c", title: "Merged policy edit", status: "finished", edits_policy: true },
      { id: "d", title: "Flag false", status: "open", edits_policy: false },
    ] as ReviewDirectoryRow[];
    expect(pendingPolicyChanges(rows)).toEqual([{ id: "a", title: "Retry a failed charge and loosen the payments rules", href: "/r/a" }]);
    expect(pendingPolicyChanges([])).toEqual([]);
  });
});
