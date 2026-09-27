import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewHistorySheet from "./ReviewHistorySheet";

const api = vi.hoisted(() => ({
  fetchRevisions: vi.fn(),
  fetchReviewActivity: vi.fn(),
}));
vi.mock("./reviewApi", () => ({
  fetchRevisions: api.fetchRevisions,
  fetchReviewActivity: api.fetchReviewActivity,
  historicalReviewHref: (reviewId: string, revisionNumber: number) => `/r/${reviewId}?base=${revisionNumber}&target=${revisionNumber}`,
  reviewCompareHref: (reviewId: string, base: number, target: number) => `/r/${reviewId}?base=${base}&target=${target}`,
}));

describe("ReviewHistorySheet", () => {
  beforeEach(() => {
    api.fetchRevisions.mockResolvedValue({
      revisions: [
        { id: "11111111111171118111111111111111", ref: "rr/11111111111171118111111111111111", revision_number: 0, range_mode: "working_tree", base_sha: "a", head_sha: "", dirty: true, degraded: [], provenance_host: "", provenance_model: "", provenance_session_id: "", provenance_certainty: "none", created_at: "2026-09-15T18:00:00Z", is_last_reviewed: true, is_last_commented: true, previous_revision_number: null, review_area_count: 2, review_area_delta: { added: 2, changed: 0, removed: 0 }, delta: { added: 3, changed: 0, removed: 0 } },
        { id: "22222222222272228222222222222222", ref: "rr/22222222222272228222222222222222", revision_number: 1, range_mode: "working_tree", base_sha: "b", head_sha: "abcdef1234567890", dirty: true, degraded: [], provenance_host: "", provenance_model: "", provenance_session_id: "", provenance_certainty: "none", created_at: "2026-09-15T19:00:00Z", is_latest: true, previous_revision_number: 0, semantic_changed: false, source_identity_changed: true, base_moved: true, review_area_count: 2, review_area_delta: { added: 0, changed: 0, removed: 0 }, delta: { added: 0, changed: 0, removed: 0 } },
      ],
    });
    api.fetchReviewActivity.mockResolvedValue({
      review_id: "rev-abc",
      events: [
        {
          id: "act-system",
          review_id: "rev-abc",
          revision_id: "rrv-2",
          kind: "mark.reconciled",
          actor_id: "lemoncrow",
          actor_type: "unknown",
          subject_type: "review_target",
          subject_id: "sym:checkout",
          summary: "local: reviewed → changed since review",
          detail: { reason: "content changed" },
          created_at: "2026-09-15T19:00:01Z",
        },
        {
          id: "act-human",
          review_id: "rev-abc",
          revision_id: "rrv-2",
          kind: "mark.judgment",
          actor_id: "pankaj",
          actor_type: "human",
          subject_type: "review_target",
          subject_id: "sym:checkout",
          summary: "pankaj: changed since review → needs changes",
          detail: {},
          created_at: "2026-09-15T19:01:00Z",
        },
        {
          id: "act-agent",
          review_id: "rev-abc",
          revision_id: "rrv-2",
          kind: "annotation.author_response",
          actor_id: "claude-session",
          actor_type: "agent",
          subject_type: "annotation",
          subject_id: "ann-1",
          summary: "Author says addressed",
          detail: {},
          created_at: "2026-09-15T19:02:00Z",
        },
      ],
    });
  });

  it("opens on revisions and keeps append-only activity one tab away", async () => {
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={1} historical={false} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText("Snapshot 1").length).toBeGreaterThan(0));
    expect(screen.getByRole("tab", { name: /snapshots/i }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByText("2 initial review areas")).toBeTruthy();
    expect(screen.getByText("Δ +0 ~0 −0 review areas")).toBeTruthy();
    expect(screen.queryByText("3 initial review areas")).toBeNull();
    expect(screen.getByText("Base moved · review areas unchanged")).toBeTruthy();
    expect(screen.getByText("SCM abcdef1234")).toBeTruthy();
    expect(screen.queryByRole("tab", { name: /compare/i })).toBeNull();
    await userEvent.click(screen.getByRole("tab", { name: /activity/i }));
    await waitFor(() => expect(screen.getByText("Author says addressed")).toBeTruthy());
    expect(screen.getByText("pankaj")).toBeTruthy();
    expect(screen.getByText("claude-session")).toBeTruthy();
    expect(screen.getByText("lemoncrow")).toBeTruthy();
    expect(screen.getByText("content changed")).toBeTruthy();
  });

  it("offers a retry when history cannot be loaded", async () => {
    api.fetchRevisions.mockRejectedValueOnce(new Error("history unavailable"));

    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={1} historical={false} onClose={vi.fn()} />);

    expect(await screen.findByText("Could not load history")).toBeTruthy();
    expect(screen.getByText("history unavailable")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(screen.getAllByText("Snapshot 1").length).toBeGreaterThan(0));
    expect(api.fetchRevisions.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("keeps quick and arbitrary Snapshot comparison inside history", async () => {
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={0} historical onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByText("Snapshot 0").length).toBeGreaterThan(0));

    const quickCompare = screen.getByRole("link", { name: /compare to latest/i });
    expect(quickCompare.getAttribute("href")).toBe("/r/rev-abc?base=0&target=1");

    await userEvent.click(screen.getByText("Advanced comparison"));
    expect(screen.getByLabelText("Compare from Snapshot")).toBeTruthy();
    expect(screen.getByLabelText("Compare to Snapshot")).toBeTruthy();
    const advancedCompare = screen.getByRole("link", { name: "Compare" });
    expect(advancedCompare.getAttribute("href")).toBe("/r/rev-abc?base=0&target=1");
  });

  it("links the latest revision and older revisions through human revision numbers", async () => {
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={0} historical onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("tab", { name: /snapshots/i })).toBeTruthy());
    const latest = screen.getAllByText("Snapshot 1").map((node) => node.closest("a")).find(Boolean);
    const old = screen.getAllByText("Snapshot 0").map((node) => node.closest("a")).find(Boolean);
    expect(latest?.getAttribute("href")).toBe("/r/rev-abc?base=1&target=1");
    expect(old?.getAttribute("href")).toBe("/r/rev-abc?base=0&target=0");
    expect(screen.getByText("Latest")).toBeTruthy();
    expect(screen.getByText("Viewing")).toBeTruthy();
    expect(screen.queryByText(/checkpoint rrv-/)).toBeNull();
  });
});
