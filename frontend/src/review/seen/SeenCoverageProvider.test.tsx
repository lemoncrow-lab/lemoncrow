import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ReviewTarget } from "../types";
import { useReviewFlowNavigation } from "../useReviewFlowNavigation";
import { SeenCoverageProvider, useSeenCoverage } from "./SeenCoverageProvider";

vi.mock("../reviewApi", () => ({
  fetchReviewSeenCoverage: vi.fn(),
  postReviewSeenCoverage: vi.fn(),
}));
import { fetchReviewSeenCoverage, postReviewSeenCoverage } from "../reviewApi";

const part = (id: string): ReviewTarget => ({
  target_id: id, unit_key: `unit/${id}`, path: `src/${id}.py`, label: id, symbol: id, content_fingerprint: "sha256:x",
  spans: [{ side: "new", start_line: 9, end_line: 11, hunk_ordinal: 1 }],
  answer: { category: "decision" }, attention_level: "normal",
} as unknown as ReviewTarget);

function Harness({ targets }: { targets: ReviewTarget[] }) {
  const { goTo } = useReviewFlowNavigation("shop");
  const { focusAnswered, coverage } = useSeenCoverage();
  const where = useLocation();
  return <div>
    <span data-testid="where">{`${where.pathname}${where.hash}`}</span>
    <span data-testid="seen">{targets[0] ? coverage(targets[0]).seen : 0}</span>
    <button onClick={() => goTo({ kind: "wrap" }, { wrapUp: true })}>wrap-up</button>
    <button onClick={() => goTo({ kind: "wrap" })}>back-to-wrap</button>
    <button onClick={() => targets[0] && focusAnswered(targets[0])}>answer-first</button>
  </div>;
}

function mount(targets: ReviewTarget[], recordable = true) {
  return render(<MemoryRouter initialEntries={["/r/shop/one"]}>
    <SeenCoverageProvider reviewRef="shop" apiReviewId="rev-1" reviewerId="viewer" revisionId="revision-1" targets={targets} recordable={recordable}>
      <Harness targets={targets} />
    </SeenCoverageProvider>
  </MemoryRouter>);
}

describe("P3-2 coverage dialog", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.mocked(fetchReviewSeenCoverage).mockReset().mockResolvedValue({ basis: "reviewer_reported_not_independent_evidence", revision_id: "revision-1", seen: [], skipped: [] } as never);
    vi.mocked(postReviewSeenCoverage).mockReset().mockImplementation(async (_id, body) => ({ revision_id: "revision-1", action: (body as { action: string }).action }) as never);
  });
  afterEach(cleanup);

  it("opens only for the Wrap up action, never for a plain move to Wrap", async () => {
    const user = userEvent.setup();
    mount([part("apply_discount")]);
    await user.click(screen.getByText("back-to-wrap"));
    expect(screen.queryByTestId("seen-coverage-dialog")).toBeNull();
    expect(screen.getByTestId("where").textContent).toBe("/r/shop/wrap");
    await user.click(screen.getByText("wrap-up"));
    expect(await screen.findByTestId("seen-coverage-dialog")).toBeTruthy();
  });

  it("starts on Open it, lists the part in SPEC's words, and Enter opens the part instead of skipping", async () => {
    const user = userEvent.setup();
    mount([part("apply_discount")]);
    await user.click(screen.getByText("wrap-up"));
    const dialog = await screen.findByTestId("seen-coverage-dialog");
    expect(dialog.textContent).toContain("You haven't seen 1 risky part");
    expect(dialog.textContent).toContain("apply_discount · lines 9–11 · seen 0 of 3 lines");
    expect(dialog.textContent).toContain("You can still wrap up. Anything you skip is written in the signed record.");
    await waitFor(() => expect(document.activeElement?.textContent).toMatch(/^Open (it|the first one)/));
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.queryByTestId("seen-coverage-dialog")).toBeNull());
    expect(screen.getByTestId("where").textContent).toBe("/r/shop/all#apply_discount");
    expect(postReviewSeenCoverage).not.toHaveBeenCalled();
  });

  it("Enter means Open it even when Wrap up anyway has focus (SPEC 3.5)", async () => {
    const user = userEvent.setup();
    mount([part("apply_discount")]);
    await user.click(screen.getByText("wrap-up"));
    await screen.findByTestId("seen-coverage-dialog");
    screen.getByRole("button", { name: /Wrap up anyway/ }).focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/r/shop/all#apply_discount"));
    expect(postReviewSeenCoverage).not.toHaveBeenCalled();
  });

  it("Shift+Enter wraps up anyway and records exactly the unseen keys", async () => {
    const user = userEvent.setup();
    mount([part("apply_discount")]);
    await user.click(screen.getByText("wrap-up"));
    await screen.findByTestId("seen-coverage-dialog");
    await user.keyboard("{Shift>}{Enter}{/Shift}");
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/r/shop/wrap"));
    expect(postReviewSeenCoverage).toHaveBeenCalledWith("rev-1", {
      revision_id: "revision-1", action: "skipped", block_keys: ["unit/apply_discount:src/apply_discount.py:sha256:x:new:1:9:11"],
    });
  });

  it("says exactly what the server kept when a later batch fails", async () => {
    const user = userEvent.setup();
    const many = Array.from({ length: 501 }, (_, i) => part(`part_${String(i).padStart(3, "0")}`));
    let calls = 0;
    vi.mocked(postReviewSeenCoverage).mockImplementation(async () => { calls += 1; if (calls > 1) throw new Error("offline"); return { revision_id: "revision-1", action: "skipped" } as never; });
    mount(many);
    await user.click(screen.getByText("wrap-up"));
    await screen.findByTestId("seen-coverage-dialog");
    await user.click(screen.getByRole("button", { name: /Wrap up anyway/ }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("The server recorded 500 of 501 skipped blocks and kept them for the signed record. The other 1 are not included.");
    expect(alert.textContent).not.toContain("not included in a signed record");
    await act(async () => undefined);
    const saved = JSON.parse(window.localStorage.getItem("lemoncrow.review.seen.v1.rev-1.viewer.revision-1") ?? "{}");
    expect(saved.omission).toMatchObject({ serverRecorded: false, recordedCount: 500 });
  });

  it("reports seen code for the latest revision, and keeps it local once a newer revision has arrived", async () => {
    const user = userEvent.setup();
    mount([part("apply_discount")]);
    await user.click(screen.getByText("answer-first"));
    await waitFor(() => expect(postReviewSeenCoverage).toHaveBeenCalledTimes(1));
    expect(vi.mocked(postReviewSeenCoverage).mock.calls[0]?.[1]).toMatchObject({ revision_id: "revision-1", action: "seen", source: "focus_answer" });
    cleanup();
    window.localStorage.clear();
    vi.mocked(postReviewSeenCoverage).mockClear();

    mount([part("apply_discount")], false);
    await user.click(screen.getByText("answer-first"));
    await waitFor(() => expect(screen.getByTestId("seen").textContent).not.toBe("0"));
    expect(postReviewSeenCoverage).not.toHaveBeenCalled();
  });

  it("goes straight to Wrap when no risky part is unseen", async () => {
    const user = userEvent.setup();
    mount([{ ...part("fine"), answer: { category: "quick_check" } } as unknown as ReviewTarget]);
    await user.click(screen.getByText("wrap-up"));
    expect(screen.queryByTestId("seen-coverage-dialog")).toBeNull();
    expect(screen.getByTestId("where").textContent).toBe("/r/shop/wrap");
  });
});
