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
  historicalReviewHref: (revisionIdOrRef: string) => `/rr/${revisionIdOrRef.startsWith("rr/") ? revisionIdOrRef.slice(3) : revisionIdOrRef}`,
  sourceCompareHref: (from: string, to: string, scope = "") => `/r/x/${from}..${to}${scope ? `?scope=${encodeURIComponent(scope)}` : ""}`,
}));

describe("ReviewHistorySheet", () => {
  beforeEach(() => {
    api.fetchRevisions.mockResolvedValue({
      revisions: [
        { id: "11111111111171118111111111111111", ref: "rr/11111111111171118111111111111111", revision_number: 1, range_mode: "working_tree", base_sha: "a", head_sha: "", dirty: true, degraded: [], provenance_host: "", provenance_model: "", provenance_session_id: "", provenance_certainty: "none", created_at: "2026-09-15T18:00:00Z" },
        { id: "22222222222272228222222222222222", ref: "rr/22222222222272228222222222222222", revision_number: 2, range_mode: "working_tree", base_sha: "a", head_sha: "", dirty: true, degraded: [], provenance_host: "", provenance_model: "", provenance_session_id: "", provenance_certainty: "none", created_at: "2026-09-15T19:00:00Z" },
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
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={2} historical={false} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("rev 2")).toBeTruthy());
    expect(screen.getByRole("tab", { name: /revisions/i }).getAttribute("aria-selected")).toBe("true");
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

    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={2} historical={false} onClose={vi.fn()} />);

    expect(await screen.findByText("Could not load history")).toBeTruthy();
    expect(screen.getByText("history unavailable")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("rev 2")).toBeTruthy();
    expect(api.fetchRevisions.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("links revision comparison into the dedicated full-screen compare route", async () => {
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={1} historical onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("rev 1")).toBeTruthy());

    const compare = screen.getByRole("link", { name: /compare to latest/i });
    expect(compare.getAttribute("href")).toBe("/r/x/rr/11111111111171118111111111111111..rr/22222222222272228222222222222222?scope=r%2Frev-abc");
  });

  it("links the latest revision and older revisions to immutable history", async () => {
    render(<ReviewHistorySheet reviewId="rev-abc" selectedRevisionNumber={1} historical onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("tab", { name: /revisions/i })).toBeTruthy());
    const latest = screen.getByText("rev 2").closest("a");
    const old = screen.getByText("rev 1").closest("a");
    expect(latest?.getAttribute("href")).toBe("/rr/22222222222272228222222222222222");
    expect(old?.getAttribute("href")).toBe("/rr/11111111111171118111111111111111");
    expect(screen.getByText("Latest")).toBeTruthy();
    expect(screen.getByText("Viewing")).toBeTruthy();
    expect(screen.queryByText(/checkpoint rrv-/)).toBeNull();
  });
});
