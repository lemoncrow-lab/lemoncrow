import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const request = vi.fn();
const directory = vi.fn();
vi.mock("../reviewApi", () => ({
  requestBrowserJson: (...args: unknown[]) => request(...args),
  fetchReviewDirectory: (...args: unknown[]) => directory(...args),
  fetchOverview: () => Promise.resolve({ policy: { authority: { base_state: "present", rules: [], ai_content_access: [], rule_documents: [], errors: [], focus: [] }, future_changes: [], suggestions: [] } }),
}));

import { SettingsPolicy } from "./SettingsPolicy";

const ROWS = [
  { id: "a", title: "Loosen review policy for payments", project: "shop", updated_at: "2026-11-08T10:00:00Z", status: "open", edits_policy: true, review_path: "/r/a" },
  { id: "b", title: "Plain change", project: "shop", updated_at: "2026-11-07T10:00:00Z", status: "open" },
];
function show() {
  return render(<MemoryRouter><SettingsPolicy /></MemoryRouter>);
}
beforeEach(() => { request.mockReset(); directory.mockReset().mockResolvedValue({ reviews: ROWS }); });
afterEach(cleanup);

describe("Review policy history and pending changes (SPEC 4.19)", () => {
  it("lists the commits that changed the rules file, and the open reviews that would change it", async () => {
    request.mockResolvedValue({ path: ".lemoncrow/review.yaml", available: true, entries: [
      { commit: "7c1d2e0", date: "2026-10-28", author: "Maya Chen", subject: "Add public API routes to always a decision" },
      { commit: "b218f6e", date: "2026-09-30", author: "Maya Chen", subject: "Create review policy" },
    ] });
    show();
    const history = await screen.findByTestId("settings-policy-history");
    expect(history.textContent).toContain("7c1d2e0 · 2026-10-28 · Maya Chen · “Add public API routes to always a decision”");
    expect(history.textContent).toContain("b218f6e");
    const pending = screen.getByTestId("settings-policy-pending");
    expect(pending.textContent).toContain("Loosen review policy for payments");
    expect(pending.textContent).toContain("pending review");
    expect(pending.textContent).not.toContain("Plain change");
    expect(request).toHaveBeenCalledWith("/api/reviews/a/policy-history");
  });

  it("says plainly when the history can't be read, and draws no pending block when nothing is pending", async () => {
    directory.mockResolvedValue({ reviews: [ROWS[1]] });
    request.mockRejectedValue(Object.assign(new Error("Not Found"), { status: 404 }));
    show();
    await waitFor(() => expect(screen.getByTestId("settings-policy-history").textContent).toContain("can't be read from here"));
    expect(screen.queryByTestId("settings-policy-pending")).toBeNull();
  });

  it("an empty history is said as empty, not as unreadable", async () => {
    request.mockResolvedValue({ path: ".lemoncrow/review.yaml", available: true, entries: [] });
    show();
    await waitFor(() => expect(screen.getByTestId("settings-policy-history").textContent).toContain("No commit has changed"));
  });
});
