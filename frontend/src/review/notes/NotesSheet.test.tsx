import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FlowReviewTools } from "../flowReviewTools";
import type { Annotation } from "../types";
import { note, target } from "./notesFixtures";
import { NotesSheet } from "./NotesSheet";

function Where() {
  const location = useLocation();
  return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
}

function tools(annotations: Annotation[], extra: Partial<FlowReviewTools> = {}): FlowReviewTools {
  return {
    reviewId: "api-1", reviewRef: "shop-7", revisionId: "s1", revisionNumber: 1, historical: false, readOnly: false, busy: false,
    annotations, targets: [target("a", { unit_key: "u-a", path: "src/a.py", symbol: "apply" })], revisionHistory: null,
    setState: vi.fn(), setDisposition: vi.fn(), createNote: vi.fn(async () => true), hosted: null,
    ...extra,
  };
}

function sheet(value: FlowReviewTools, onOpenChange = vi.fn()) {
  const view = render(<MemoryRouter initialEntries={["/r/shop-7"]}><NotesSheet tools={value} open onOpenChange={onOpenChange} /><Where /></MemoryRouter>);
  return { ...view, rerenderWith: (next: FlowReviewTools) => view.rerender(<MemoryRouter initialEntries={["/r/shop-7"]}><NotesSheet tools={next} open onOpenChange={onOpenChange} /><Where /></MemoryRouter>) };
}

const rows = () => screen.queryAllByTestId("notes-row").map((row) => row.getAttribute("data-note"));
const tab = (name: RegExp) => screen.getByRole("tab", { name });

afterEach(cleanup);

describe("the Notes sheet", () => {
  const NOTES = [
    note("c1", { title: "Rounding" }),
    note("c2", { state: "resolved" }),
    note("agent", { author_response: "addressed", author_response_source_id: "sess-1", author_addressed_revision_number: 1 }),
    note("moved", { state: "orphaned", anchored: false, anchor_method_label: "No match", anchor_detail: "The lines were rewritten." }),
  ];

  it("counts each filter and opens on Open", () => {
    sheet(tools(NOTES));
    expect(tab(/^Open/)).toHaveTextContent("Open3");
    expect(tab(/^Needs re-review/)).toHaveTextContent("1");
    expect(tab(/^Resolved/)).toHaveTextContent("1");
    expect(tab(/^Lines moved/)).toHaveTextContent("1");
    expect(rows()).toEqual(["c1", "agent", "moved"]);
    expect(screen.getByText("3 open · 1 need re-review")).toBeInTheDocument();
  });

  it("resolves from the list and keeps the row in place until the tab changes", () => {
    const value = tools(NOTES);
    const { rerenderWith } = sheet(value);
    const row = screen.getAllByTestId("notes-row")[0];
    fireEvent.click(within(row).getByRole("button", { name: "Resolve" }));
    expect(value.setState).toHaveBeenCalledWith("c1", "resolved");
    rerenderWith({ ...value, annotations: NOTES.map((item) => (item.id === "c1" ? { ...item, state: "resolved" as const } : item)) });
    expect(rows()).toEqual(["c1", "agent", "moved"]);
    fireEvent.mouseDown(tab(/^Resolved/));
    fireEvent.mouseDown(tab(/^Open/));
    expect(rows()).toEqual(["agent", "moved"]);
  });

  it("lets a comment whose lines moved be resolved, and says why it lost its place", () => {
    const value = tools(NOTES);
    sheet(value);
    fireEvent.mouseDown(tab(/^Lines moved/));
    expect(rows()).toEqual(["moved"]);
    expect(screen.getByTestId("notes-moved-reason")).toHaveTextContent("No match: The lines were rewritten.");
    expect(screen.queryByRole("button", { name: "Show part" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Resolve" }));
    expect(value.setState).toHaveBeenCalledWith("moved", "resolved");
  });

  it("says the coding agent marked it addressed on Needs re-review", () => {
    sheet(tools(NOTES));
    fireEvent.mouseDown(tab(/^Needs re-review/));
    expect(rows()).toEqual(["agent"]);
    expect(screen.getByText("The coding agent marked this addressed; only you can resolve it.")).toBeInTheDocument();
  });

  it("jumps to the note's part and closes", () => {
    const onOpenChange = vi.fn();
    sheet(tools(NOTES), onOpenChange);
    fireEvent.click(within(screen.getAllByTestId("notes-row")[0]).getByRole("button", { name: "Show part" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(screen.getByTestId("where").textContent).toBe("/r/shop-7/one/apply");
  });

  it("shows an AI finding's disposition and runs Dismiss and Request change through the finding actions", async () => {
    const finding = note("f1", { source: "ai_review", created_by_actor: "agent", title: "Null check", body: "x may be None", finding_disposition: "open" });
    const value = tools([finding]);
    sheet(value);
    const row = screen.getByTestId("notes-row");
    expect(within(row).getByLabelText("AI finding actions")).toHaveTextContent("Open");
    fireEvent.click(within(row).getByRole("button", { name: "Dismiss" }));
    fireEvent.click(within(row).getByRole("button", { name: "Incorrect" }));
    expect(value.setDisposition).toHaveBeenCalledWith("f1", "dismissed", "incorrect");

    fireEvent.click(within(row).getByRole("button", { name: "Request change" }));
    expect(value.setDisposition).toHaveBeenCalledWith("f1", "confirmed_by_reviewer");
    const box = within(row).getByRole("textbox") as HTMLTextAreaElement;
    expect(box.value).toBe("Null check\n\nx may be None");
    fireEvent.keyDown(box, { key: "Enter", metaKey: true });
    await waitFor(() => expect(value.createNote).toHaveBeenCalled());
    expect(value.createNote).toHaveBeenCalledWith(expect.objectContaining({
      path: "src/a.py", start_line: 10, end_line: 12, side: "additions", kind: "request_change", body: "Null check\n\nx may be None", target_unit_key: "u-a",
    }));
    // Saved: the composer closes.
    await waitFor(() => expect(within(row).queryByRole("textbox")).toBeNull());
  });

  it("draws nothing writable on a past revision", () => {
    sheet(tools(NOTES, { historical: true, readOnly: true }));
    expect(screen.getByText("3 open · 1 need re-review · as of Revision 2, read-only")).toBeInTheDocument();
    for (const button of screen.getAllByRole("button", { name: "Resolve" })) expect(button).toBeDisabled();
  });

  it("says so when the review has no notes", () => {
    sheet(tools([]));
    expect(screen.getByText("No comments yet")).toBeInTheDocument();
  });
});
