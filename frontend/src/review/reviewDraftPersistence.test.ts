import { beforeEach, describe, expect, it } from "vitest";

import {
  askDraftKey,
  commentDraftKey,
  loadAskDraft,
  loadCommentDraft,
  saveAskDraft,
  saveCommentDraft,
} from "./reviewDraftPersistence";

describe("review draft persistence", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("keeps a comment draft scoped to one Review and exact revision", () => {
    saveCommentDraft("review-a", "rev-1", {
      path: "src/auth.ts",
      startLine: 10,
      endLine: 14,
      side: "additions",
      body: "Check this boundary",
      kind: "request_change",
      markTarget: true,
      targetUnitKey: "unit:auth",
      fileLevel: false,
    });

    expect(loadCommentDraft("review-a", "rev-1")).toMatchObject({
      path: "src/auth.ts",
      startLine: 10,
      endLine: 14,
      body: "Check this boundary",
      kind: "request_change",
      targetUnitKey: "unit:auth",
    });
    expect(loadCommentDraft("review-a", "rev-2")).toBeNull();
    expect(loadCommentDraft("review-b", "rev-1")).toBeNull();
    expect(commentDraftKey("review-a", "rev-1")).not.toBe(commentDraftKey("review-a", "rev-2"));
  });

  it("removes persisted comment text only when the draft is explicitly cleared", () => {
    saveCommentDraft("review-a", "rev-1", {
      path: "src/a.ts",
      startLine: 1,
      endLine: 1,
      side: "additions",
      body: "draft",
      kind: "comment",
      markTarget: false,
      fileLevel: false,
    });
    expect(loadCommentDraft("review-a", "rev-1")?.body).toBe("draft");
    saveCommentDraft("review-a", "rev-1", null);
    expect(loadCommentDraft("review-a", "rev-1")).toBeNull();
  });

  it("keeps Ask drafts scoped to Review, revision and anchor", () => {
    const scope = {
      target_unit_key: "unit:auth",
      path: "src/auth.ts",
      symbol: "authorize",
      side: "new",
      start_line: 20,
      end_line: 30,
    };
    const otherAnchor = { ...scope, start_line: 40, end_line: 50 };

    saveAskDraft("review-a", "rev-1", scope, "Why is this trust boundary safe?");

    expect(loadAskDraft("review-a", "rev-1", scope)).toBe("Why is this trust boundary safe?");
    expect(loadAskDraft("review-a", "rev-1", otherAnchor)).toBe("");
    expect(loadAskDraft("review-a", "rev-2", scope)).toBe("");
    expect(loadAskDraft("review-b", "rev-1", scope)).toBe("");
    expect(askDraftKey("review-a", "rev-1", scope)).not.toBe(askDraftKey("review-a", "rev-1", otherAnchor));
  });

  it("clears an Ask draft after submission without touching another anchor", () => {
    const first = { path: "src/a.ts", start_line: 1, end_line: 2 };
    const second = { path: "src/a.ts", start_line: 5, end_line: 6 };
    saveAskDraft("review-a", "rev-1", first, "first");
    saveAskDraft("review-a", "rev-1", second, "second");

    saveAskDraft("review-a", "rev-1", first, "");

    expect(loadAskDraft("review-a", "rev-1", first)).toBe("");
    expect(loadAskDraft("review-a", "rev-1", second)).toBe("second");
  });
});
