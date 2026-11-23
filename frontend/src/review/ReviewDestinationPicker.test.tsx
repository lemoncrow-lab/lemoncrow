import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  fetchReviewDestinationOptions: vi.fn(),
  putReviewDestination: vi.fn(),
}));

vi.mock("./reviewApi", () => api);

import { ReviewHandoffControls } from "./ReviewDestinationPicker";
import type { FeedbackDeliveryCapability, ReviewDestinationState } from "./types";
import { fmtWhen } from "../lib/format";

const claude: FeedbackDeliveryCapability = {
  supported: true, host: "claude", session_id: "a1b2c3", target_ref: "claude:a1b2c3", label: "Claude", reason: "",
  kind: "agent", destination: "auto", fallback: false,
};

const state = (kind: ReviewDestinationState["destination"]["kind"], extra: Partial<ReviewDestinationState["destination"]> = {}): ReviewDestinationState => ({
  review_id: "review-1",
  destination: { kind, host: "", session_id: "", external_ref: "", updated_at: "", ...extra },
  effective: { kind: "agent", label: "Claude", host: "claude", session_id: "a1b2c3", available: true, fallback: false, reason: "" },
});

function renderControls(overrides: Partial<Parameters<typeof ReviewHandoffControls>[0]> = {}) {
  const props = {
    reviewId: "review-1",
    delivery: claude,
    unsent: 2,
    busy: false,
    onSendNow: vi.fn(),
    onDestinationChanged: vi.fn(),
    ...overrides,
  };
  render(<ReviewHandoffControls {...props} />);
  return props;
}

beforeEach(() => {
  api.fetchReviewDestinationOptions.mockReset();
  api.putReviewDestination.mockReset();
});

describe("ReviewHandoffControls", () => {
  it("sends the counted comments and keeps the destination dropdown", async () => {
    const props = renderControls();
    await userEvent.click(screen.getByRole("button", { name: "Send 2 comments" }));
    expect(props.onSendNow).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Change where fixes go" })).toHaveAttribute("title", "Fixes go to: Claude");
  });

  it("uses the same label for copying to a person and publishing to a pull request", () => {
    renderControls({ unsent: 1, delivery: { ...claude, kind: "export", label: "author", host: "", session_id: "" } });
    expect(screen.getByRole("button", { name: "Send 1 comment" })).toBeTruthy();
    renderControls({ unsent: 3, delivery: { ...claude, kind: "github", label: "PR #12", host: "", session_id: "" } });
    expect(screen.getByRole("button", { name: "Send 3 comments" })).toBeTruthy();
  });

  it("disables the action and dropdown during a mutation", () => {
    renderControls({ busy: true, busyLabel: "Preparing feedback…" });
    expect(screen.getByRole("button", { name: "Preparing feedback…" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("button", { name: "Change where fixes go" })).toBeDisabled();
  });

  it("explains an unreachable destination without changing the feedback label", async () => {
    renderControls({ delivery: { ...claude, supported: false, reason: "direct feedback delivery is not configured for gemini" } });
    await userEvent.hover(screen.getByRole("button", { name: "Send 2 comments" }));
    expect(await screen.findByRole("tooltip")).toHaveTextContent("direct feedback delivery is not configured for gemini");
  });

  it("lists destinations with the reason an unavailable one cannot be picked, and pins the chosen one", async () => {
    api.fetchReviewDestinationOptions.mockResolvedValue({
      ...state("auto"),
      options: [
        { kind: "auto", label: "Author sessions", detail: "Claude · wrote this revision", available: true, reason: "" },
        { kind: "new_session", host: "codex", label: "New Codex session", detail: "starts in this worktree", available: false, reason: "codex CLI is not installed" },
        { kind: "export", label: "Copy for the author", detail: "Markdown for a human", available: true, reason: "" },
      ],
    });
    api.putReviewDestination.mockResolvedValue(state("export"));
    const props = renderControls();

    await userEvent.click(screen.getByRole("button", { name: "Change where fixes go" }));
    expect(await screen.findByText("codex CLI is not installed")).toBeTruthy();
    expect(screen.getByRole("menuitem", { name: /New Codex session/ })).toHaveAttribute("data-disabled");
    await userEvent.click(screen.getByRole("menuitem", { name: /Copy for the author/ }));

    await waitFor(() => expect(api.putReviewDestination).toHaveBeenCalledWith("review-1", { kind: "export" }));
    expect(props.onDestinationChanged).toHaveBeenCalledWith(state("export"));
  });

  it("orders the picker: to a person, then a new agent, then sessions with the default first", async () => {
    api.fetchReviewDestinationOptions.mockResolvedValue({
      ...state("auto"),
      // The server's order; the picker regroups it.
      options: [
        { kind: "auto", label: "Author sessions", detail: "Claude · wrote this revision", available: true, reason: "" },
        { kind: "session", host: "claude", session_id: "bbbb2222", label: "Claude bbbb", detail: "running here", available: true, reason: "" },
        { kind: "new_session", host: "claude", label: "New Claude session", detail: "starts in this worktree", available: true, reason: "" },
        { kind: "github_pr", external_ref: "github:acme/widgets#7", label: "PR #7", detail: "acme/widgets", available: true, reason: "" },
        { kind: "export", label: "Copy for the author", detail: "Markdown for a human", available: true, reason: "" },
      ],
    });
    renderControls({ unsent: 1 });

    await userEvent.click(screen.getByRole("button", { name: "Change where fixes go" }));
    await screen.findByRole("menuitem", { name: /Copy for the author/ });
    // (Each item: its label, then its detail.)
    expect(screen.getAllByRole("menuitem").map((item) => item.querySelector("span.block")?.textContent)).toEqual([
      "Copy for the author",
      "PR #7",
      "New Claude session",
      "Author sessions · default",
      "Claude bbbb",
    ]);
    expect(screen.getAllByRole("group").map((group) => group.getAttribute("aria-label"))).toEqual(["To a person", "New agent", "Sessions"]);

  });

  it("shows today and yesterday locally, full dates for older sessions, and hides unknown times", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 8, 29, 15, 0));
    const today = new Date(2026, 8, 29, 9, 5);
    const yesterday = new Date(2026, 8, 28, 23, 45);
    const older = new Date(2025, 8, 28, 18, 30);
    api.fetchReviewDestinationOptions.mockResolvedValue({
      ...state("auto"),
      options: [
        { kind: "session", host: "claude", session_id: "today", label: "Claude today", detail: "running here", available: true, activity_at: today.toISOString() },
        { kind: "session", host: "claude", session_id: "yesterday", label: "Claude yesterday", detail: "last active here", available: true, activity_at: yesterday.toISOString() },
        { kind: "session", host: "codex", session_id: "older", label: "Codex older", detail: "wrote Revision 2", available: true, activity_at: older.toISOString() },
        { kind: "session", host: "claude", session_id: "unknown", label: "Claude unknown", detail: "running here", available: true },
        { kind: "session", host: "claude", session_id: "invalid", label: "Claude invalid", detail: "running here", available: true, activity_at: "invalid" },
      ],
    });
    try {
      renderControls();
      await userEvent.click(screen.getByRole("button", { name: "Change where fixes go" }));
      const todayRow = await screen.findByRole("menuitem", { name: /Claude today/ });
      expect(todayRow.querySelector("time")).toHaveTextContent(
        fmtWhen(today),
      );
      const yesterdayTime = screen.getByRole("menuitem", { name: /Claude yesterday/ }).querySelector("time");
      expect(yesterdayTime).toHaveTextContent(
        fmtWhen(yesterday),
      );
      expect(todayRow.querySelector("time")?.parentElement).toBe(screen.getByText("Claude today").parentElement);
      const olderTime = screen.getByRole("menuitem", { name: /Codex older/ }).querySelector("time");
      expect(olderTime).toHaveTextContent(
        fmtWhen(older),
      );
      expect(olderTime).toHaveAttribute("datetime", older.toISOString());
      expect(screen.getByRole("menuitem", { name: /Claude unknown/ }).querySelector("time")).toBeNull();
      expect(screen.getByRole("menuitem", { name: /Claude invalid/ }).querySelector("time")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
