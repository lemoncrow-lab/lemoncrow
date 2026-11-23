import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchGitHubBotApprovals } from "../reviewApi";
import type { GitHubBotApprovals } from "../types";
import { BotApprovalHint } from "./BotApprovalHint";

vi.mock("../reviewApi", () => ({ fetchGitHubBotApprovals: vi.fn() }));
const api = vi.mocked(fetchGitHubBotApprovals);
const approvals = (...rows: Partial<GitHubBotApprovals["approvals"][number]>[]): GitHubBotApprovals => ({
  review_id: "r", revision_id: "v", head_sha: "b52e7a9",
  approvals: rows.map((row) => ({ login: "copilot-pull-request-reviewer[bot]", name: "Copilot", commit_id: "b52e7a9", current: true, ...row })),
});

beforeEach(() => api.mockReset());
afterEach(cleanup);

describe("P6-3 bot approval hint (SPEC 4.17, GH-3)", () => {
  it("says a bot approved, labels it a hint, and says the files come back", async () => {
    api.mockResolvedValue(approvals({}));
    render(<BotApprovalHint reviewId="r" enabled />);
    const hint = await screen.findByTestId("bot-approval");
    expect(hint.textContent).toContain("Bot verdict · a hint, not a judgment");
    expect(screen.getByTestId("bot-approval-title").textContent).toBe("Copilot approved this pull request");
    expect(screen.getByTestId("bot-approval-detail").textContent).toBe(
      "Its files come back to you as quick checks or decisions. Nothing was accepted on its behalf.",
    );
  });

  it("asks nothing and shows nothing off a pull request", async () => {
    render(<BotApprovalHint reviewId="r" enabled={false} />);
    await Promise.resolve();
    expect(api).not.toHaveBeenCalled();
    expect(screen.queryByTestId("bot-approval")).toBeNull();
  });

  it("shows nothing for an approval of an earlier commit or for none", async () => {
    api.mockResolvedValue(approvals({ current: false }));
    const first = render(<BotApprovalHint reviewId="r" enabled />);
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("bot-approval")).toBeNull();
    first.unmount();
    api.mockResolvedValue(approvals());
    render(<BotApprovalHint reviewId="r" enabled />);
    await waitFor(() => expect(api).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("bot-approval")).toBeNull();
  });

  it("shows nothing when the lookup fails: a hint that is not there is not a fact", async () => {
    api.mockRejectedValueOnce(new Error("409"));
    render(<BotApprovalHint reviewId="r" enabled />);
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByTestId("bot-approval")).toBeNull();
  });
});
