import { render, screen } from "@testing-library/react";
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
    const replacement = screen.getByRole("textbox", { name: "Replacement code" });
    await userEvent.clear(replacement);
    await userEvent.type(replacement, "if user and user.active:\n    allow()");
    expect(onReplacementChange).toHaveBeenCalled();
    expect(screen.getByText("Patch only · source unchanged")).toBeTruthy();
    expect(screen.getByText(/Saving creates a review patch only/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save suggestion" })).toBeTruthy();
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
});
