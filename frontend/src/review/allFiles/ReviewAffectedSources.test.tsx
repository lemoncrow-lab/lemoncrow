import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ReviewAffectedSources } from "./ReviewAffectedSources";
import type { AffectedSite } from "./allFilesModel";

vi.mock("../reviewApi", () => ({ fetchRelatedSource: vi.fn() }));
import { fetchRelatedSource } from "../reviewApi";

const site: AffectedSite = {
  key: "src/views.py:22", path: "src/views.py", line: 22, symbol: "order_view",
  snippet: "can_view_order(order)", kind: "caller", exactness: "snapshot", uncertainty: "",
  inspectedByAgent: null, changedTargetId: "target-auth", changedTargetLabel: "can_view_order",
};

describe("All files affected code", () => {
  beforeEach(() => vi.mocked(fetchRelatedSource).mockReset());

  it("opens read-only source Peek, returns on Back, and navigates to its changed part", async () => {
    const jump = vi.fn();
    vi.mocked(fetchRelatedSource).mockResolvedValue({
      path: site.path, focus_line: site.line, start_line: 21, end_line: 23,
      language: "python", text: "def order_view():\n  return can_view_order(order)\n# end",
    });
    const user = userEvent.setup();
    render(<ReviewAffectedSources reviewId="review-01" sites={[site]} onJumpToChanged={jump}/>);
    await user.click(screen.getByRole("button", { name: /Inspect related code/ }));
    const section = screen.getByTestId("all-files-affected");
    expect(within(section).getByText("Checked in code")).toBeTruthy();
    await user.click(within(section).getByRole("button", { name: "Peek at code" }));
    await waitFor(() => expect(fetchRelatedSource).toHaveBeenCalledWith("review-01", "src/views.py", 22));
    expect(await screen.findByText(/return can_view_order\(order\)/)).toBeTruthy();
    expect(screen.getByText(/response does not carry a revision identifier/)).toBeTruthy();
    await user.click(within(screen.getByTestId("all-files-peek")).getByRole("button", { name: "Back" }));
    expect(screen.queryByTestId("all-files-peek")).toBeNull();
    await user.click(within(section).getByRole("button", { name: /Go to changed part/ }));
    expect(jump).toHaveBeenCalledWith("target-auth");
  });

  it("shows only Checked in code or Not verified · reason, and counts callers inside changed files", () => {
    render(<ReviewAffectedSources reviewId="review-01" sites={[{ ...site, exactness: "current_index" }]} inChangedFiles={2} onJumpToChanged={vi.fn()}/>);
    return userEvent.setup().click(screen.getByRole("button", { name: /Inspect related code/ })).then(() => {
      const chip = within(screen.getByTestId("all-files-affected")).getByText(/^Not verified · /);
      expect(chip.textContent).toBe("Not verified · found in the current code index, not in this revision");
      expect(screen.queryByText(/Recorded with this revision|Unverified location|Current index/)).toBeNull();
      expect(screen.getByText("2 more callers are in files this change touches and are not listed here.")).toBeTruthy();
    });
  });

  it("rejects a mismatched source location instead of treating it as evidence", async () => {
    vi.mocked(fetchRelatedSource).mockResolvedValue({
      path: "wrong/file.py", focus_line: 22, start_line: 21, end_line: 23,
      language: "python", text: "unrelated content",
    });
    const user = userEvent.setup();
    render(<ReviewAffectedSources reviewId="review-01" sites={[site]} onJumpToChanged={vi.fn()}/>);
    await user.click(screen.getByRole("button", { name: /Inspect related code/ }));
    await user.click(screen.getByRole("button", { name: "Peek at code" }));
    expect((await screen.findByRole("alert")).textContent).toContain("did not match");
    expect(screen.queryByText("unrelated content")).toBeNull();
  });
});
