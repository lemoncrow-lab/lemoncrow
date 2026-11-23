import { describe, expect, it } from "vitest";
import type { ReviewConsumerGroup, ReviewProjection, ReviewTarget } from "../types";
import { FOLDED_GROUP, affectedSites, allFilesGroups, callersInChangedFiles, foldedGroupLabel, generatedPaths, markEventSentence, materialLabel, noteStateWord, siteEvidenceLabel, verifiedSite } from "./allFilesModel";

function target(path: string, id: string, category: "decision" | "quick_check" | "accepted_by_rule" = "quick_check"): ReviewTarget {
  return { path, target_id: id, symbol: id, answer: { category } } as ReviewTarget;
}
function material(files: Array<{ path: string; role: "generated" | "implementation" | "tests"; collapsed_default: boolean }>): ReviewProjection {
  return { material: { files, roles: {}, implementation_changed_lines: 0, total_changed_lines: 0, explicit_metadata_state: "present" } } as ReviewProjection;
}
function group(): ReviewConsumerGroup {
  return {
    key: "a", path: "src/auth.py", symbol: "can_read", join: "exact", target_ids: ["d"],
    known_callers: 3, omitted: 1, unlisted_callers: 1,
    sites: [
      { path: "src/ui.py", line: 19, symbol: "view_order", snippet: "can_read(order)", kind: "caller", impact_kind: "untouched_caller", in_patch: false, exactness: "snapshot", uncertainty: "" },
      { path: "src/ui.py", line: 19, symbol: "view_order", snippet: "can_read(order)", kind: "caller", impact_kind: "untouched_caller", in_patch: false, exactness: "snapshot", uncertainty: "" },
      { path: "src/other.py", line: 7, symbol: "list_orders", snippet: "can_read()", kind: "caller", impact_kind: "untouched_caller", in_patch: false, exactness: "current_index", uncertainty: "not in stored snapshot" },
      { path: "src/auth.py", line: 18, symbol: "can_read", snippet: "def can_read():", kind: "caller", impact_kind: "signature_change", in_patch: true, exactness: "snapshot", uncertainty: "" },
    ],
  };
}
describe("All files recorded classifications", () => {
  it("groups generated files and filename lockfiles but never hides human decisions", () => {
    const rows = [
      target("generated/api.ts", "a"),
      target("package-lock.json", "b"),
      target("src/app.ts", "d", "decision"),
      target("src/app.ts", "e", "quick_check"),
      target("generated/security.ts", "f", "decision"),
      target("generated/security.ts", "g"),
    ];
    const projection = material([
      { path: "generated/api.ts", role: "generated", collapsed_default: true },
      { path: "src/app.ts", role: "implementation", collapsed_default: false },
      { path: "generated/security.ts", role: "generated", collapsed_default: true },
    ]);
    expect([...generatedPaths(rows, projection)].sort()).toEqual(["generated/api.ts", "package-lock.json"]);
    expect(materialLabel("generated/api.ts", projection)).toContain("classified by Review");
    expect(materialLabel("package-lock.json", projection)).toContain("filename");
  });
  it("does not invent generated classification for ordinary files", () => {
    expect([...generatedPaths([target("src/payments.ts", "pay")], null)]).toEqual([]);
    expect(materialLabel("src/payments.ts", null)).toBe("");
  });
  it("deduplicates unchanged callers, keeps exactness and never treats current-index candidates as Snapshot proof", () => {
    const projection = { context: { consumers: [group()], consumers_omitted: 0, readiness: {} } } as ReviewProjection;
    const rows = affectedSites([target("src/auth.py", "d", "decision")], projection);
    expect(rows.map((row) => `${row.path}:${row.line}`)).toEqual(["src/other.py:7", "src/ui.py:19"]);
    expect(rows[1].changedTargetId).toBe("d");
    expect(rows[1].changedTargetLabel).toBe("d");
    expect(verifiedSite(rows[1])).toBe(true);
    expect(verifiedSite(rows[0])).toBe(false);
    expect(rows[0].uncertainty).toContain("not in stored");
  });
  it("never treats missing impact evidence as proof there are zero callers", () => {
    expect(affectedSites([target("src/auth.py", "d")], null)).toEqual([]);
  });
});

describe("All files groups", () => {
  it("keeps an unanswered quick check visible even when its file name looks generated", () => {
    const lock = target("package-lock.json", "lock", "quick_check");
    const generatedFile = target("generated/api.ts", "api", "accepted_by_rule");
    const decision = target("src/app.ts", "d", "decision");
    const groups = allFilesGroups([decision], [lock], [generatedFile]);
    expect(groups.map(([label, rows]) => [label, rows.map((row) => row.target_id)])).toEqual([
      ["Decisions", ["d"]], ["Quick check", ["lock"]], ["Accepted by rule", ["api"]],
    ]);
    expect(FOLDED_GROUP).toBe("Accepted by rule");
  });

  it("folds every part accepted by rule into one group counted in files, whatever the file is", () => {
    const accepted = [
      target("assets/fonts/Inter.woff2", "font", "accepted_by_rule"),
      target("uv.lock", "uv", "accepted_by_rule"),
      target("vendor/lib.js", "v1", "accepted_by_rule"),
      target("vendor/lib.js", "v2", "accepted_by_rule"),
    ];
    const groups = allFilesGroups([], [], accepted);
    expect(groups.filter(([label]) => label === FOLDED_GROUP).flatMap(([, rows]) => rows.map((row) => row.target_id))).toEqual(["font", "uv", "v1", "v2"]);
    expect(foldedGroupLabel(accepted)).toBe("Accepted by rule · 3 files");
    expect(foldedGroupLabel(accepted.slice(0, 1))).toBe("Accepted by rule · 1 file");
  });
});

describe("All files plain words", () => {
  it("allows only Checked in code or Not verified · <reason> as a site's evidence label", () => {
    expect(siteEvidenceLabel({ exactness: "snapshot", uncertainty: "" })).toBe("Checked in code");
    expect(siteEvidenceLabel({ exactness: "current_index", uncertainty: "" })).toBe("Not verified · found in the current code index, not in this revision");
    expect(siteEvidenceLabel({ exactness: "snapshot", uncertainty: "matched by name only" })).toBe("Not verified · matched by name only");
    expect(siteEvidenceLabel({ exactness: "unknown", uncertainty: "" })).toBe("Not verified · the location was not confirmed");
  });
  it("counts callers that sit in changed files instead of dropping them", () => {
    const changed = target("src/auth.py", "d", "decision");
    expect(callersInChangedFiles([changed], { context: { consumers: [group()] } } as ReviewProjection)).toBe(1);
    expect(callersInChangedFiles([target("src/ui.py", "e")], { context: { consumers: [group()] } } as ReviewProjection)).toBe(2);
  });
  it("never shows a stored event kind or state name in History", () => {
    const text = [
      markEventSentence({ event_kind: "judgment", from_state: "", to_state: "reviewed" }),
      markEventSentence({ event_kind: "judgment", from_state: "reviewed", to_state: "needs_changes" }),
      markEventSentence({ event_kind: "reconciled", from_state: "reviewed", to_state: "reviewed" }),
      markEventSentence({ event_kind: "discarded", from_state: "reviewed", to_state: "" }),
      markEventSentence({ event_kind: "migrated", from_state: "later", to_state: "later" }),
      noteStateWord("orphaned"),
    ].join(" | ");
    expect(text).toBe("Answered Looks good | Answer changed from Looks good to Needs a change | Answer Looks good carried over to a newer revision | Answer dropped because the code changed | Answer moved with the part | its lines moved or went away");
    expect(text).not.toMatch(/needs_changes|judgment|reconciled|discarded|migrated|unreviewed/);
  });
});
