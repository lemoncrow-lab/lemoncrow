import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ requestBrowserJson: vi.fn() }));
vi.mock("./reviewApi", () => api);
const panel = vi.hoisted(() => ({ fetchReviewerSuggestions: vi.fn() }));
vi.mock("./reviewerSuggestionLogic", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./reviewerSuggestionLogic")>()),
  fetchReviewerSuggestions: panel.fetchReviewerSuggestions,
}));

import ReviewerSuggestionsPanel, { ReviewerSuggestionList } from "./ReviewerSuggestions";
import { degradedSignals, exclusionSummary, type ReviewerSuggestionSet } from "./reviewerSuggestionLogic";

const set: ReviewerSuggestionSet = {
  advisory: true,
  revision_id: "rev-2",
  suggestions: [
    {
      kind: "user",
      key: "handle:@sec-lead",
      label: "@sec-lead",
      principal_id: "",
      handle: "sec-lead",
      provider: "",
      durable_id: "",
      score: 400,
      explanation: "Reviewer rule auth-rule · src/auth.py",
      reasons: [
        { signal: "rule", paths: ["src/auth.py"], path_count: 1, sources: ["trusted base"], detail: "Reviewer rule auth-rule · src/auth.py", rule_ids: ["auth-rule"], count: 1 },
      ],
      paths: ["src/auth.py"],
      path_count: 1,
      critical_paths: ["src/auth.py"],
      access: "not_applicable",
      status: "suggested",
      automatic_rules: [],
    },
    {
      kind: "team",
      key: "github:team:77",
      label: "@acme/core",
      principal_id: "",
      handle: "acme/core",
      provider: "github",
      durable_id: "77",
      score: 200,
      explanation: "CODEOWNERS (.github/CODEOWNERS src/) · src/auth.py",
      reasons: [
        { signal: "codeowners", paths: ["src/auth.py"], path_count: 1, sources: ["base"], detail: "CODEOWNERS (.github/CODEOWNERS src/) · src/auth.py", rule_ids: [], count: 1 },
      ],
      paths: ["src/auth.py"],
      path_count: 1,
      critical_paths: [],
      access: "unknown",
      status: "requested",
      automatic_rules: [],
    },
  ],
  excluded: { author: 1, no_access: 2 },
  truncated: 0,
  signals: [
    { signal: "rule", state: "available", source: "", detail: "1 reviewer rule(s)" },
    { signal: "codeowners", state: "available", source: "", detail: "" },
    { signal: "history", state: "unavailable", source: "", detail: "git history is not mined for this Review" },
  ],
  head_changes: [
    { path: ".github/CODEOWNERS", change: "modified", note: ".github/CODEOWNERS changes in this Change. Suggestions use the trusted base's CODEOWNERS." },
  ],
  automatic_routing: false,
  errors: [],
};

describe("ReviewerSuggestionList", () => {
  it("explains every suggestion, marks requested ones and states what was excluded or degraded", () => {
    render(<ReviewerSuggestionList data={set} />);
    expect(screen.getByText("Suggested reviewers")).toBeTruthy();
    expect(screen.getByText(/nothing is requested until someone asks/)).toBeTruthy();
    expect(screen.getAllByTestId("reviewer-suggestion")).toHaveLength(2);
    expect(screen.getByText("Reviewer rule auth-rule · src/auth.py")).toBeTruthy();
    expect(screen.getByText(/Critical paths · src\/auth\.py/)).toBeTruthy();
    expect(screen.getByText("team")).toBeTruthy();
    expect(screen.getByText("Requested")).toBeTruthy();
    expect(screen.getByText(/changes in this Change/)).toBeTruthy();
    expect(screen.getByText("Excluded: 1 author · 2 without access")).toBeTruthy();
    expect(screen.getByText(/Git history: unavailable/)).toBeTruthy();
    // Advisory: no request control unless the host grants one.
    expect(screen.queryByRole("button", { name: /Request review/ })).toBeNull();
  });

  it("offers a request action only for suggested candidates when the host passes one", async () => {
    const onRequest = vi.fn();
    render(<ReviewerSuggestionList data={set} onRequest={onRequest} />);
    const buttons = screen.getAllByRole("button", { name: /Request review from/ });
    expect(buttons).toHaveLength(1);
    await userEvent.click(buttons[0]!);
    expect(onRequest).toHaveBeenCalledWith(set.suggestions[0]);
  });

  it("formats exclusions and degraded signals deterministically", () => {
    expect(exclusionSummary({ no_access: 1, author: 0, authoring_agent: 2 })).toBe("Excluded: 2 authoring agent · 1 without access");
    expect(exclusionSummary({})).toBe("");
    expect(degradedSignals([{ signal: "codeowners", state: "provisional", source: "", detail: "not the trusted base" }])).toEqual([
      "CODEOWNERS: provisional · not the trusted base",
    ]);
  });
});

describe("fetchReviewerSuggestions", () => {
  it("asks the Reader API for the exact Snapshot", async () => {
    const actual = await vi.importActual<typeof import("./reviewerSuggestionLogic")>("./reviewerSuggestionLogic");
    api.requestBrowserJson.mockResolvedValue(set);
    await expect(actual.fetchReviewerSuggestions("rv 1", "rev-2")).resolves.toBe(set);
    expect(api.requestBrowserJson).toHaveBeenCalledWith("/api/reviews/rv%201/reviewer-suggestions?revision_id=rev-2");
  });
});

describe("ReviewerSuggestionsPanel", () => {
  // A function returned from beforeEach is a cleanup hook; return nothing.
  beforeEach(() => {
    panel.fetchReviewerSuggestions.mockReset();
  });

  it("loads the current Snapshot's suggestions when Review Status opens", async () => {
    panel.fetchReviewerSuggestions.mockResolvedValue(set);
    render(<ReviewerSuggestionsPanel reviewId="rv 1" revisionId="rev-2" />);
    await waitFor(() => expect(screen.getAllByTestId("reviewer-suggestion")).toHaveLength(2));
    expect(panel.fetchReviewerSuggestions).toHaveBeenCalledWith("rv 1", "rev-2");
  });

  it("shows a load failure instead of an empty list", async () => {
    panel.fetchReviewerSuggestions.mockRejectedValue(new Error("503"));
    render(<ReviewerSuggestionsPanel reviewId="rv-1" revisionId="rev-2" />);
    expect(await screen.findByText("Could not load reviewer suggestions: 503")).toBeTruthy();
    expect(screen.queryByText(/No reviewer suggestions/)).toBeNull();
  });
});
