import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewDevelopmentSheet, { groupTimeline, hostLabel } from "./ReviewDevelopmentSheet";
import { development, event, projection } from "./developmentTestFixtures";

const api = vi.hoisted(() => ({ fetchReviewDevelopmentEvents: vi.fn() }));
vi.mock("./reviewApi", () => ({ fetchReviewDevelopmentEvents: api.fetchReviewDevelopmentEvents }));

describe("ReviewDevelopmentSheet", () => {
  beforeEach(() => {
    api.fetchReviewDevelopmentEvents.mockReset();
    api.fetchReviewDevelopmentEvents.mockResolvedValue({ events: [event("e1")], next_before: null });
  });

  it("renders the server projection: summary, attention, contributors, verification and feedback", async () => {
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development()} historical={false} onClose={vi.fn()} />);

    const dialog = screen.getByRole("dialog", { name: "Development" });
    expect(within(dialog).getByText(/Snapshot 52 · frozen with this Snapshot/)).toBeTruthy();
    expect(within(dialog).getByText("Session expiry now rejects stale tokens; focused auth tests pass.")).toBeTruthy();
    expect(within(dialog).getByText("Harden session expiry")).toBeTruthy();
    expect(within(dialog).getByText(/2 contributors · 1 verifier · 3 files · 42 passed · 1 failed/)).toBeTruthy();

    const attention = within(dialog).getByRole("heading", { name: "Reviewer attention" }).closest("section")!;
    expect(within(attention).getByText("uv run mypy src")).toBeTruthy();
    expect(within(attention).getByText("Should mobile keep legacy cookies?")).toBeTruthy();
    expect(within(attention).getByText(/1 changed file with no recorded author/)).toBeTruthy();

    const contributors = within(dialog).getByRole("heading", { name: "Contributors" }).closest("section")!;
    const rows = within(contributors).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringMatching(/^Claude.*author.*src\/auth\.py.*src\/session\.py/),
      expect.stringMatching(/^Codex.*author.*src\/cache\.py/),
      expect.stringMatching(/^CI.*verifier.*1 check failed/),
    ]);

    expect(within(dialog).getByText("Claude addressed 2 comments")).toBeTruthy();
    expect(within(dialog).getByText(/1 file changed · 4 checks passed · included in Snapshot 53/)).toBeTruthy();
    await waitFor(() => expect(api.fetchReviewDevelopmentEvents).toHaveBeenCalledWith("review-1", expect.objectContaining({ revisionNumber: 52, scope: "snapshot", view: "all" })));
  });

  it("keeps newer work separate from the frozen Snapshot and switches the timeline scope", async () => {
    const since = projection({
      scope: "since",
      headline: "",
      contributors: [{ ...projection().contributors[1], active: true }],
      verification: { pass: 3, fail: 0, pending: 0, checks: [] },
      newer_paths: ["src/retry.py", "src/backoff.py"],
      active: true,
      event_count: 6,
      feedback: [],
    });
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development({ since })} historical={false} onClose={vi.fn()} />);

    const band = screen.getByText("Working now").closest("[role=status]")!;
    expect(band.textContent).toContain("Codex · 2 newer files · 3 passed");
    // The frozen summary never absorbs newer work.
    expect(screen.getByText(/2 contributors · 1 verifier · 3 files/)).toBeTruthy();

    await userEvent.click(within(band as HTMLElement).getByRole("button", { name: "Show newer activity" }));
    await waitFor(() => expect(api.fetchReviewDevelopmentEvents).toHaveBeenLastCalledWith("review-1", expect.objectContaining({ scope: "since" })));
  });

  it("filters, pages and collapses repetitive progress updates", async () => {
    api.fetchReviewDevelopmentEvents
      .mockResolvedValueOnce({
        events: [
          event("e9", { kind: "message", summary: "Tests pass now.", path: "", status: "" }),
          event("e8", { kind: "message", summary: "Editing auth.", path: "", status: "" }),
          event("e7", { kind: "message", summary: "Reading callers.", path: "", status: "" }),
          event("e6", { kind: "test", summary: "pytest -q", status: "pass", path: "" }),
        ],
        next_before: 6,
      })
      .mockResolvedValueOnce({ events: [event("e2", { summary: "Edited src/session.py", path: "src/session.py" })], next_before: null })
      .mockResolvedValue({ events: [event("e6", { kind: "test", summary: "pytest -q", status: "pass", path: "" })], next_before: null });

    render(<ReviewDevelopmentSheet reviewId="review-1" development={development()} historical={false} onClose={vi.fn()} />);

    expect(await screen.findByText("3 progress updates from Claude")).toBeTruthy();
    expect(screen.getByText("pytest -q", { selector: "span" })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Load earlier activity" }));
    expect(await screen.findByText("Edited src/session.py")).toBeTruthy();
    expect(api.fetchReviewDevelopmentEvents).toHaveBeenLastCalledWith("review-1", expect.objectContaining({ before: 6 }));

    await userEvent.click(screen.getByRole("tab", { name: "Verification" }));
    await waitFor(() => expect(api.fetchReviewDevelopmentEvents).toHaveBeenLastCalledWith("review-1", expect.objectContaining({ view: "verification", before: null })));
  });

  it("explains an empty Snapshot honestly and closes on Escape", async () => {
    const onClose = vi.fn();
    render(
      <ReviewDevelopmentSheet
        reviewId="review-1"
        development={development({
          snapshot: projection({ available: false, contributors: [], event_count: 0, headline: "", intent: "" }),
          reason: "No exact development evidence is attached to this Snapshot yet.",
        })}
        historical
        onClose={onClose}
      />,
    );
    expect(screen.getByText("No exact development evidence is attached to this Snapshot yet.")).toBeTruthy();
    expect(screen.getByText(/Snapshot 52 · frozen historical record/)).toBeTruthy();
    expect(api.fetchReviewDevelopmentEvents).not.toHaveBeenCalled();
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("opens a changed path and closes itself", async () => {
    const onClose = vi.fn();
    const onOpenPath = vi.fn();
    render(<ReviewDevelopmentSheet reviewId="review-1" development={development()} historical={false} onClose={onClose} onOpenPath={onOpenPath} />);
    const changed = screen.getByRole("heading", { name: "Changed during development" }).closest("section")!;
    await userEvent.click(within(changed).getByRole("button", { name: "src/cache.py" }));
    expect(onClose).toHaveBeenCalled();
    expect(onOpenPath).toHaveBeenCalledWith("src/cache.py");
    expect(within(changed).getByText("2 agents")).toBeTruthy();
  });

  it("reads the server narrative instead of recomputing a summary", () => {
    const narrative = [
      "Claude and Codex changed 3 files — Claude: auth.py and session.py; Codex: cache.py.",
      "1 of 43 checks failed (run by Claude and CI).",
    ];
    render(
      <ReviewDevelopmentSheet
        reviewId="review-1"
        development={development({ snapshot: projection({ narrative }) })}
        historical={false}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByTestId("development-narrative").textContent).toBe(narrative.join(" "));
    expect(screen.queryByText(/2 contributors · 1 verifier/)).toBeNull();
  });
});

describe("development helpers", () => {
  it("labels hosts and groups only consecutive updates from one session", () => {
    expect(hostLabel("opencode")).toBe("OpenCode");
    expect(hostLabel("lemoncrow:code")).toBe("LemonCrow");
    const rows = groupTimeline([
      event("e5", { kind: "message", session_id: "a" }),
      event("e4", { kind: "message", session_id: "a" }),
      event("e3", { kind: "message", session_id: "b" }),
      event("e2", { kind: "edit" }),
      event("e1", { kind: "message", session_id: "a" }),
    ]);
    expect(rows.map((row) => (row.type === "updates" ? `updates:${row.events.length}` : row.event.id))).toEqual([
      "updates:2",
      "e3",
      "e2",
      "e1",
    ]);
  });
});
