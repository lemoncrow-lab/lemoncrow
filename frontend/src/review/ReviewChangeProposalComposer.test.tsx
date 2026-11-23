import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { EditorView } from "@codemirror/view";
import { undo } from "@codemirror/commands";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import ReviewChangeProposalComposer from "./ReviewChangeProposalComposer";
import type { ReviewChangeProposal, ReviewProposalSelection } from "./types";

const selection: ReviewProposalSelection = {
  revision_id: "rrv-1",
  path: "src/app.py",
  start_line: 4,
  end_line: 5,
  side: "additions",
  text: "if user:\n    allow()\n",
};

function proposal(overrides: Partial<ReviewChangeProposal> = {}): ReviewChangeProposal {
  return {
    id: "rcp-1",
    review_id: "review-1",
    base_revision_id: "rrv-1",
    base_revision_number: 1,
    path: "src/app.py",
    start_line: 4,
    end_line: 5,
    original_text: selection.text,
    replacement_text: "if user and user.active:\n    allow()\n",
    patch: "--- a/src/app.py\n+++ b/src/app.py\n@@ -4,2 +4,2 @@\n-if user:\n+if user and user.active:\n     allow()\n",
    state: "proposed",
    target_unit_key: "sym:user",
    annotation_id: "",
    intent: "Only active users may continue.",
    conflict_reason: "",
    created_by: "local",
    created_at: "2026-09-22T17:00:00Z",
    updated_at: "2026-09-22T17:00:00Z",
    applied_at: "",
    result_revision_id: "",
    result_target_ids: [],
    can_apply: true,
    ...overrides,
  };
}

describe("ReviewChangeProposalComposer", () => {
  it("edits exact reviewed source before creating a durable proposal", async () => {
    const onReplacementChange = vi.fn();
    const onCreate = vi.fn();
    render(
      <ReviewChangeProposalComposer
        selection={selection}
        proposal={null}
        loading={false}
        busy={false}
        replacementText={selection.text}
        intent=""
        onReplacementChange={onReplacementChange}
        onIntentChange={vi.fn()}
        onCreate={onCreate}
        onCreateAndApply={vi.fn()}
        onApply={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    const reviewed = screen.getByText("Current source").parentElement?.querySelector("pre");
    expect(reviewed?.textContent).toBe(selection.text);
    const replacement = await screen.findByRole("textbox", { name: "Replacement code" });
    await userEvent.clear(replacement);
    await userEvent.type(replacement, "if user and user.active:\n    allow()");
    expect(onReplacementChange).toHaveBeenCalled();
    expect(screen.getByText("Patch only · source unchanged")).toBeTruthy();
    expect(screen.getByText(/Saving creates a review patch only/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save suggestion" })).toBeTruthy();
  });

  it("offers Suggest and Apply in the compact inline quick editor", async () => {
    const onCreate = vi.fn();
    const onCreateAndApply = vi.fn();
    const onClose = vi.fn();
    render(
      <ReviewChangeProposalComposer
        selection={selection}
        proposal={null}
        loading={false}
        busy={false}
        quickEdit
        allowApply
        replacementText="if user and user.active:\n    allow()\n"
        intent=""
        onReplacementChange={vi.fn()}
        onIntentChange={vi.fn()}
        onCreate={onCreate}
        onCreateAndApply={onCreateAndApply}
        onApply={vi.fn()}
        onClose={onClose}
      />,
    );

    expect(screen.getByRole("region", { name: "Quick source edit" })).toBeTruthy();
    expect(screen.queryByText("Current source")).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Proposal intent" })).toBeNull();
    expect(screen.getByRole("button", { name: "Suggest" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Apply" })).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Suggest" }));
    expect(onCreate).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(onCreateAndApply).toHaveBeenCalledTimes(1);

    const editor = await screen.findByRole("textbox", { name: "Quick edit replacement code" });
    await userEvent.type(editor, "{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("uses Ctrl+Enter for Suggest and Ctrl+Shift+Enter for Apply in quick edit", async () => {
    const onCreate = vi.fn();
    const onCreateAndApply = vi.fn();
    render(
      <ReviewChangeProposalComposer
        selection={selection}
        proposal={null}
        loading={false}
        busy={false}
        quickEdit
        allowApply
        replacementText="changed()"
        intent=""
        onReplacementChange={vi.fn()}
        onIntentChange={vi.fn()}
        onCreate={onCreate}
        onCreateAndApply={onCreateAndApply}
        onApply={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    const editor = await screen.findByRole("textbox", { name: "Quick edit replacement code" });
    fireEvent.keyDown(editor, { key: "Enter", ctrlKey: true });
    expect(onCreate).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(editor, { key: "Enter", ctrlKey: true, shiftKey: true });
    expect(onCreateAndApply).toHaveBeenCalledTimes(1);
  });

  it("expands without replacing the editor or losing source lines, cursor, or undo", async () => {
    function Editing() {
      const [text, setText] = useState(selection.text);
      return <ReviewChangeProposalComposer
        selection={selection} proposal={null} loading={false} busy={false} quickEdit allowApply theme="light"
        replacementText={text} intent="" onReplacementChange={setText} onIntentChange={vi.fn()}
        onCreate={vi.fn()} onCreateAndApply={vi.fn()} onApply={vi.fn()} onClose={vi.fn()}
      />;
    }
    render(<Editing />);
    const textbox = await screen.findByRole("textbox", { name: "Quick edit replacement code" });
    const view = EditorView.findFromDOM(textbox)!;
    expect(view.dom.querySelector(".cm-lineNumbers")?.textContent).toContain("4");
    act(() => view.dispatch({ changes: { from: 0, to: 0, insert: "# note\n" }, selection: { anchor: 7 } }));
    expect(view.state.doc.toString()).toBe("# note\n" + selection.text);

    await userEvent.click(screen.getByRole("button", { name: "Expand editor" }));
    expect(screen.getByRole("button", { name: "Compact editor" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("textbox", { name: "Quick edit replacement code" })).toBe(textbox);
    expect(view.state.selection.main.head).toBe(7);
    act(() => { undo(view); });
    expect(view.state.doc.toString()).toBe(selection.text);
  });

  it("blocks quick edit submission by button and shortcut while busy or unchanged", async () => {
    const onCreate = vi.fn(), onCreateAndApply = vi.fn();
    const props = {
      selection, proposal: null, loading: false, busy: false, quickEdit: true, allowApply: true,
      replacementText: selection.text, intent: "", onReplacementChange: vi.fn(), onIntentChange: vi.fn(),
      onCreate, onCreateAndApply, onApply: vi.fn(), onClose: vi.fn(),
    };
    const rendered = render(<ReviewChangeProposalComposer {...props} />);
    const textbox = await screen.findByRole("textbox", { name: "Quick edit replacement code" });
    fireEvent.keyDown(textbox, { key: "Enter", ctrlKey: true });
    fireEvent.keyDown(textbox, { key: "Enter", ctrlKey: true, shiftKey: true });
    expect(screen.getByRole("button", { name: "Suggest" })).toBeDisabled();
    expect(onCreate).not.toHaveBeenCalled();
    expect(onCreateAndApply).not.toHaveBeenCalled();

    rendered.rerender(<ReviewChangeProposalComposer {...props} busy replacementText="changed()" />);
    expect(textbox).toHaveAttribute("contenteditable", "false");
    fireEvent.keyDown(textbox, { key: "Enter", ctrlKey: true, shiftKey: true });
    expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
    expect(onCreateAndApply).not.toHaveBeenCalled();
  });

  it("uses Tab to indent replacement code without leaving the editor", async () => {
    render(<ReviewChangeProposalComposer
      selection={selection} proposal={null} loading={false} busy={false} quickEdit
      replacementText={selection.text} intent="" onReplacementChange={vi.fn()} onIntentChange={vi.fn()}
      onCreate={vi.fn()} onCreateAndApply={vi.fn()} onApply={vi.fn()} onClose={vi.fn()}
    />);
    const textbox = await screen.findByRole("textbox", { name: "Quick edit replacement code" });
    const view = EditorView.findFromDOM(textbox)!;
    act(() => view.dispatch({ selection: { anchor: 0 } }));
    fireEvent.keyDown(textbox, { key: "Tab", code: "Tab" });
    expect(view.state.doc.toString()).toMatch(/^\s+if user/);
  });

  it("can apply a direct edit in one action while still using the proposal pipeline", async () => {
    const onCreateAndApply = vi.fn();
    render(
      <ReviewChangeProposalComposer
        selection={selection}
        proposal={null}
        loading={false}
        busy={false}
        directEdit
        replacementText="if user and user.active:\n    allow()\n"
        intent="Only active users may continue."
        onReplacementChange={vi.fn()}
        onIntentChange={vi.fn()}
        onCreate={vi.fn()}
        onCreateAndApply={onCreateAndApply}
        onApply={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("Edit source")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Apply to source" }));
    expect(onCreateAndApply).toHaveBeenCalledTimes(1);
  });

  it("shows the server-generated patch and applies only when safe", async () => {
    const onApply = vi.fn();
    render(
      <ReviewChangeProposalComposer
        selection={selection}
        proposal={proposal()}
        loading={false}
        busy={false}
        replacementText=""
        intent=""
        onReplacementChange={vi.fn()}
        onIntentChange={vi.fn()}
        onCreate={vi.fn()}
        onCreateAndApply={vi.fn()}
        onApply={onApply}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText(/if user and user\.active/)).toBeTruthy();
    expect(screen.getByText("based on Revision 2")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Apply suggestion" }));
    expect(onApply).toHaveBeenCalledTimes(1);
  });

  it("surfaces a stale-source conflict without offering apply again", () => {
    render(
      <ReviewChangeProposalComposer
        selection={selection}
        proposal={proposal({
          state: "conflicted",
          can_apply: false,
          conflict_reason: "source changed since this proposal was prepared",
        })}
        loading={false}
        busy={false}
        replacementText=""
        intent=""
        onReplacementChange={vi.fn()}
        onIntentChange={vi.fn()}
        onCreate={vi.fn()}
        onCreateAndApply={vi.fn()}
        onApply={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText(/source changed since this proposal was prepared/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Apply to source" })).toBeNull();
  });

  it("does not call a proposal the server will not apply 'Ready to apply'", () => {
    // Reopened from an older Snapshot (or without local source editing), the
    // server reports can_apply=false; the Reader bar calls it Review advanced /
    // Saved proposal, and the composer must not claim it is ready.
    const props = {
      selection,
      loading: false,
      busy: false,
      replacementText: "",
      intent: "",
      onReplacementChange: vi.fn(),
      onIntentChange: vi.fn(),
      onCreate: vi.fn(),
      onCreateAndApply: vi.fn(),
      onApply: vi.fn(),
      onClose: vi.fn(),
    };
    const saved = proposal({ base_revision_id: "rrv-0", base_revision_number: 0, can_apply: false });
    const view = render(<ReviewChangeProposalComposer {...props} proposal={saved} />);

    expect(screen.queryByText("Ready to apply")).toBeNull();
    expect(screen.getByText("Saved proposal")).toBeTruthy();
    expect(screen.getByText(/can only be applied from the latest revision/)).toBeTruthy();
    expect(screen.getByText("based on Revision 1")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Apply suggestion" })).toBeNull();

    view.rerender(<ReviewChangeProposalComposer {...props} proposal={saved} quickEdit allowApply />);
    expect(screen.getByRole("region", { name: "Quick source edit" })).toBeTruthy();
    expect(screen.queryByText("Ready to apply")).toBeNull();
    expect(screen.getByText("Saved proposal")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  });
});
