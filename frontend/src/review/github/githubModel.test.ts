import { describe, expect, it } from "vitest";

import type { GitHubBotApproval, GitHubPublishPreview } from "../types";
import { botHint, githubSentText, githubVerdictLabel, inlineLocation, nameList, pullRequestTarget } from "./githubModel";

const inline = (over: Partial<GitHubPublishPreview["inline"][number]> = {}): GitHubPublishPreview["inline"][number] => ({
  annotation_id: "a1", path: "app/payments/webhooks.py", side: "RIGHT", start_line: 42, line: 42, body: "Cap retries by the spend budget.", ...over,
});
const preview = (over: Partial<GitHubPublishPreview> = {}): GitHubPublishPreview => ({
  pr: { owner: "acme", repo: "payments", number: 412, url: "https://github.com/acme/payments/pull/412" },
  commit_id: "b52e7a9", event: "REQUEST_CHANGES", summary: "Fix first", body: "Fix first\n\nHuman review. Nothing here was decided by an AI.",
  inline: [inline()], folded: [], notices: [], plan_hash: "h", review_id: "r", revision_id: "v", operation_id: "o", head_sha: "b52e7a9",
  stale: false, empty: false, ...over,
});
const bot = (over: Partial<GitHubBotApproval> = {}): GitHubBotApproval => ({ login: "copilot-pull-request-reviewer[bot]", name: "Copilot", commit_id: "b52e7a9", current: true, ...over });

describe("githubVerdictLabel", () => {
  it("names the verdict GitHub will receive, in SPEC's words", () => {
    expect(githubVerdictLabel("REQUEST_CHANGES", "changes_requested")).toBe("Requested changes");
    expect(githubVerdictLabel("APPROVE", "lgtm")).toBe("Approved");
    expect(githubVerdictLabel("APPROVE", "lgtm_with_suggestions")).toBe("Approved with suggestions");
    expect(githubVerdictLabel("COMMENT", "comment")).toBe("Commented");
  });
  it("reads Commented when the server downgraded an approval, whatever the reviewer chose", () => {
    expect(githubVerdictLabel("COMMENT", "lgtm")).toBe("Commented");
    expect(githubVerdictLabel("COMMENT", "lgtm_with_suggestions")).toBe("Commented");
  });
});

describe("pullRequestTarget", () => {
  it("keeps only the number", () => {
    expect(pullRequestTarget("PR #412")).toBe("#412");
    expect(pullRequestTarget("pull request")).toBe("the pull request");
    expect(pullRequestTarget("")).toBe("the pull request");
  });
});

describe("inlineLocation", () => {
  it("is path:line, a range, and marks the old side", () => {
    expect(inlineLocation(inline())).toBe("app/payments/webhooks.py:42");
    expect(inlineLocation(inline({ start_line: 40, line: 45 }))).toBe("app/payments/webhooks.py:40-45");
    expect(inlineLocation(inline({ side: "LEFT" }))).toBe("app/payments/webhooks.py:42 (old)");
  });
});

describe("githubSentText", () => {
  it("is the verdict, the body as posted and each comment on its line, and nothing else", () => {
    expect(githubSentText(preview(), "changes_requested")).toBe(
      "Requested changes\n\nFix first\n\nHuman review. Nothing here was decided by an AI.\n\napp/payments/webhooks.py:42\nCap retries by the spend budget.",
    );
  });
  it("falls back to the summary when an older server sends no body, and skips empty parts", () => {
    expect(githubSentText(preview({ body: undefined, inline: [] }), "changes_requested")).toBe("Requested changes\n\nFix first");
    expect(githubSentText(preview({ body: "", summary: "", inline: [] }), "comment")).toBe("Requested changes");
  });
});

describe("botHint (SPEC 4.17: a hint, not a judgment)", () => {
  it("says who approved and that the files come back as quick checks or decisions", () => {
    expect(botHint([bot()])).toEqual({
      title: "Copilot approved this pull request",
      detail: "Its files come back to you as quick checks or decisions. Nothing was accepted on its behalf.",
    });
  });
  it("shows nothing for an approval left on an earlier commit, and nothing for no approval", () => {
    expect(botHint([bot({ current: false })])).toBeNull();
    expect(botHint([])).toBeNull();
  });
  it("lists each bot once", () => {
    expect(botHint([bot(), bot(), bot({ login: "x[bot]", name: "dependabot" })])?.title).toBe("Copilot and dependabot approved this pull request");
    expect(nameList(["a", "b", "c"])).toBe("a, b and c");
  });
});
