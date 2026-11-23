import { describe, expect, it } from "vitest";

import { revisionLabel, revisionShortLabel } from "./revisionLabel";

describe("revision labels", () => {
  it("names commit checkpoints with Review-local commit number and sha", () => {
    const revision = { revision_number: 8, capture_kind: "commit" as const, commit_number: 3, commit_sha: "d01c872abcdef" };
    expect(revisionLabel(revision)).toBe("Revision 9 · Commit 3 · d01c872a");
    expect(revisionShortLabel(revision)).toBe("Revision 9");
  });

  it("names captures from uncommitted source as frozen revisions", () => {
    const revision = { revision_number: 9, capture_kind: "draft" as const, draft_number: 2 };
    expect(revisionLabel(revision)).toBe("Revision 10");
    expect(revisionShortLabel(revision)).toBe("Revision 10");
    expect(revisionShortLabel({ revision_number: 10, capture_kind: "draft" })).toBe("Revision 11");
  });

  it("keeps legacy rows understandable", () => {
    expect(revisionLabel({ revision_number: 4, capture_kind: "legacy" as const })).toBe("Revision 5");
  });
  it("marks rebased-away commit checkpoints as rewritten", () => {
    const revision = {
      revision_number: 7,
      capture_kind: "commit" as const,
      commit_number: 2,
      commit_sha: "abcdef0123456789",
      commit_state: "superseded" as const,
    };
    expect(revisionLabel(revision)).toBe("Revision 8 · Commit 2 · abcdef01 · rewritten");
    expect(revisionShortLabel(revision)).toBe("Revision 8");
  });

  it("distinguishes an unpublished draft from a saved revision", () => {
    const draft = { revision_number: 9, capture_kind: "draft" as const, is_published_snapshot: false };
    expect(revisionLabel(draft)).toBe("Draft revision 10");
    expect(revisionShortLabel(draft)).toBe("Running draft");
    expect(revisionShortLabel({ ...draft, is_published_snapshot: true })).toBe("Revision 10");
  });
});
