import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FlowReviewTools } from "../flowReviewTools";
import { note, revision } from "../notes/notesFixtures";
import type { ReviewRevisionHistory } from "../types";
import { SnapshotMenu } from "./SnapshotMenu";

const api = vi.hoisted(() => ({ fetchRevisions: vi.fn(), fetchAnnotations: vi.fn() }));
vi.mock("../reviewApi", async (original) => ({ ...(await original<typeof import("../reviewApi")>()), ...api }));

function history(revisions: ReturnType<typeof revision>[]): ReviewRevisionHistory {
  return { revisions, latest_revision_number: null, last_reviewed_revision_number: null, last_commented_revision_number: null, default_base_revision_number: null, default_target_revision_number: null };
}

const REVISIONS = [
  revision(0, { review_area_count: 4, previous_revision_number: null, has_review_feedback: true }),
  revision(1, { review_area_count: 5, previous_revision_number: 0, review_area_delta: { added: 1, changed: 2, removed: 0 }, is_last_reviewed: true }),
  revision(2, { review_area_count: 5, previous_revision_number: 1, base_moved: true, semantic_changed: false }),
];

function tools(extra: Partial<FlowReviewTools> = {}): FlowReviewTools {
  return {
    reviewId: "api-1", reviewRef: "shop-7", revisionId: "s2", revisionNumber: 2, historical: false, readOnly: false, busy: false,
    annotations: [note("a", { revision_id: "s0" }), note("b", { revision_id: "s0", state: "resolved" }), note("ai", { revision_id: "s1", source: "ai_review" })],
    targets: [], revisionHistory: history(REVISIONS.slice(0, 2)),
    setState: vi.fn(), setDisposition: vi.fn(), createNote: vi.fn(), hosted: null,
    ...extra,
  };
}

function Where() {
  const location = useLocation();
  return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
}

function mount(value: FlowReviewTools, number = value.revisionNumber) {
  render(<MemoryRouter initialEntries={["/r/shop-7"]}><SnapshotMenu tools={value} revisionNumber={number} /><Where /></MemoryRouter>);
}

const row = (n: number) => screen.getAllByTestId("revision-row").find((item) => item.getAttribute("data-revision") === String(n))!;

beforeEach(() => {
  api.fetchRevisions.mockResolvedValue(history(REVISIONS));
  api.fetchAnnotations.mockResolvedValue({ revision_id: "s2", annotations: [note("late", { revision_id: "s2" })], counts: {} });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("the revision menu", () => {
  it("keeps the chip's look and opens a fresh list, newest first, with tags, parts and comment counts", async () => {
    const user = userEvent.setup();
    mount(tools());
    expect(screen.getByText("Revision 3").className).toContain("text-p2");
    await user.click(screen.getByRole("button", { name: "Revision 3, revision history" }));
    await waitFor(() => expect(screen.getAllByTestId("revision-row")).toHaveLength(3));
    expect(api.fetchRevisions).toHaveBeenCalledWith("api-1");
    expect(screen.getAllByTestId("revision-row").map((item) => item.getAttribute("data-revision"))).toEqual(["2", "1", "0"]);
    expect(row(2)).toHaveTextContent("Viewing");
    expect(row(2)).toHaveTextContent("Newest saved");
    expect(row(2)).toHaveTextContent("Base moved · parts unchanged");
    expect(row(1)).toHaveTextContent("Last reviewed");
    expect(row(1)).toHaveTextContent("Δ +1 ~2 −0 parts");
    expect(row(0)).toHaveTextContent("Has feedback");
    expect(row(0)).toHaveTextContent("4 initial parts");
    // Reviewer comments only: the AI finding on Revision 2 is not counted.
    expect(row(0)).toHaveTextContent("1 open · 1 resolved");
    expect(row(1)).toHaveTextContent("No comments");
  });

  it("opens a past revision at its own address and compares any row to the latest", async () => {
    const user = userEvent.setup();
    mount(tools());
    await user.click(screen.getByRole("button", { name: /revision history/ }));
    await waitFor(() => expect(screen.getAllByTestId("revision-row")).toHaveLength(3));
    expect(within(row(0)).getByRole("link", { name: "Open Revision 1" })).toHaveAttribute("href", "/r/shop-7?base=0&target=0");
    expect(within(row(2)).queryByRole("link")).toBeNull();
    expect(within(row(2)).queryByRole("button", { name: /Compare to latest/ })).toBeNull();
    await user.click(within(row(0)).getByRole("button", { name: /Compare to latest/ }));
    expect(screen.getByTestId("where").textContent).toBe("/r/shop-7/compare/s0..s2");
    expect(screen.queryByTestId("revision-menu")).toBeNull();
  });

  it("compares a picked pair, and the menu stays open while picking", async () => {
    const user = userEvent.setup();
    mount(tools());
    await user.click(screen.getByRole("button", { name: /revision history/ }));
    await waitFor(() => expect(screen.getAllByTestId("revision-row")).toHaveLength(3));
    // The default pair: the newest earlier revision with feedback, to the latest.
    expect(screen.getByRole("combobox", { name: "Compare from" })).toHaveTextContent("Revision 1");
    expect(screen.getByRole("combobox", { name: "Compare to" })).toHaveTextContent("Revision 3");
    await user.click(screen.getByRole("combobox", { name: "Compare to" }));
    await user.click(screen.getByRole("option", { name: "Revision 1" }));
    expect(screen.getByTestId("revision-menu")).toBeInTheDocument();
    expect(screen.getByText("Choose two revisions")).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Compare to" }));
    await user.click(screen.getByRole("option", { name: "Revision 2" }));
    expect(screen.getByTestId("revision-menu")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Compare" }));
    expect(screen.getByTestId("where").textContent).toBe("/r/shop-7/compare/s0..s1");
  });

  it("on a past revision counts from today's comment list, not the one as of that revision", async () => {
    const user = userEvent.setup();
    mount(tools({ historical: true, revisionNumber: 0, annotations: [] }));
    await user.click(screen.getByRole("button", { name: "Revision 1, revision history" }));
    await waitFor(() => expect(row(2)).toHaveTextContent("1 open · 0 resolved"));
    expect(api.fetchAnnotations).toHaveBeenCalledWith("api-1");
    expect(within(row(2)).getByRole("link", { name: "Open Revision 3" })).toHaveAttribute("href", "/r/shop-7?base=2&target=2");
  });

  it("shows the list it has and offers Retry when the fresh read fails", async () => {
    api.fetchRevisions.mockRejectedValueOnce(new Error("offline"));
    const user = userEvent.setup();
    mount(tools());
    await user.click(screen.getByRole("button", { name: /revision history/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the revisions: offline");
    expect(screen.getAllByTestId("revision-row")).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getAllByTestId("revision-row")).toHaveLength(3));
  });
});
