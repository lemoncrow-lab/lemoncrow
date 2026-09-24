import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import RevisionDeltaBar from "./RevisionDeltaBar";
import type { RefreshInfo, RevisionTargetRef } from "./types";

function ref(id: string, state: RevisionTargetRef["state"] = "unreviewed"): RevisionTargetRef {
  return {
    target_id: id,
    unit_key: `unit:${id}`,
    kind: "symbol",
    path: `src/${id}.py`,
    label: `src/${id}.py::${id}`,
    symbol: id,
    start_line: 10,
    state,
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
  };
}

function info(): RefreshInfo {
  const reopened = ref("changed", "changed_since_review");
  const added = ref("fresh");
  return {
    created: true,
    revision_number: 2,
    previous_revision_number: 1,
    reopened: 1,
    carried: 4,
    added: 1,
    removed: [],
    discarded: [],
    notes: ["src/app.py: hunk count changed 1 -> 2; hunk marks reset"],
    target_delta: {
      preserved: [ref("p1", "reviewed"), ref("p2", "reviewed"), ref("p3", "reviewed"), ref("p4", "reviewed")],
      reopened: [reopened],
      added: [added],
      removed: [ref("gone", "reviewed")],
      active: [reopened, added],
    },
  };
}

describe("RevisionDeltaBar", () => {
  it("centres the compact summary on work that needs the reviewer again", () => {
    render(
      <RevisionDeltaBar
        info={info()}
        discarded={[]}
        focused
        onShowAll={vi.fn()}
        onSelectTarget={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.getByText("2 returned")).toBeTruthy();
    expect(screen.getByText("1 changed")).toBeTruthy();
    expect(screen.getByText("1 new")).toBeTruthy();
    expect(screen.queryByText(/still pending/)).toBeNull();
    expect(screen.queryByText("4 preserved")).toBeNull();
    expect(screen.getByRole("button", { name: "Show all" })).toBeTruthy();
  });

  it("explains the active-queue remainder as carried pending work", async () => {
    const state = info();
    const pending = ref("pending");
    state.target_delta!.active = [...state.target_delta!.active, pending];
    render(
      <RevisionDeltaBar
        info={state}
        discarded={[]}
        focused
        onShowAll={vi.fn()}
        onSelectTarget={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.getByText("2 returned")).toBeTruthy();
    expect(screen.getByText("1 still pending")).toBeTruthy();
    await userEvent.click(screen.getByText("Details"));
    expect(screen.getByText("Still pending · 1")).toBeTruthy();
    expect(screen.getByText(/already needed review before this revision/i)).toBeTruthy();
  });

  it("keeps captured reviewer proposals in revision delta and links them to returned targets", async () => {
    const state = info();
    state.applied_proposals = [{
      id: "rcp-1",
      review_id: "review-1",
      base_revision_id: "rrv-1",
      base_revision_number: 1,
      path: "src/changed.py",
      start_line: 10,
      end_line: 12,
      original_text: "old\n",
      replacement_text: "new\n",
      patch: "-old\n+new\n",
      state: "applied",
      target_unit_key: "unit:changed",
      annotation_id: "",
      intent: "Use the corrected implementation.",
      conflict_reason: "",
      created_by: "local",
      created_at: "",
      updated_at: "",
      applied_at: "",
      result_revision_id: "rrv-2",
      result_target_ids: ["changed"],
      can_apply: false,
    }];

    const onSelectTarget = vi.fn();
    render(
      <RevisionDeltaBar
        info={state}
        discarded={[]}
        focused={false}
        onShowAll={vi.fn()}
        onSelectTarget={onSelectTarget}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.getByText("1 proposal captured")).toBeTruthy();
    await userEvent.click(screen.getByText("Details"));
    expect(screen.getByText("Reviewer source proposals")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Proposal result: src/changed.py::changed" }));
    expect(onSelectTarget).toHaveBeenCalledWith("changed");
  });

  it("shows target queues, hunk reset explanations, and discarded verdicts on demand", async () => {
    render(
      <RevisionDeltaBar
        info={info()}
        discarded={[{ unit_key: "old", kind: "symbol", state: "reviewed", label: "src/old.py::run", reason: "left the review" }]}
        focused={false}
        onShowAll={vi.fn()}
        onSelectTarget={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByText("Details"));
    expect(screen.getByText("Changed since review · 1")).toBeTruthy();
    expect(screen.getByText("New · 1")).toBeTruthy();
    expect(screen.getByText("Removed · 1")).toBeTruthy();
    expect(screen.getByText(/hunk count changed 1 -> 2/)).toBeTruthy();
    expect(screen.getByText("Past judgments no longer apply · 1")).toBeTruthy();
    expect(screen.getByText("src/old.py::run")).toBeTruthy();
  });

  it("still presents discarded human verdicts after the refresh sheet itself is gone", async () => {
    render(
      <RevisionDeltaBar
        info={null}
        discarded={[{ unit_key: "old", kind: "symbol", state: "reviewed", label: "src/old.py::run", reason: "left the review" }]}
        focused={false}
        onShowAll={vi.fn()}
        onSelectTarget={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.getByTestId("discarded-verdicts")).toBeTruthy();
    await userEvent.click(screen.getByText("Past judgments no longer apply · 1"));
    expect(screen.getByText("src/old.py::run")).toBeTruthy();
  });

  it("navigates to a queue target without pretending removed targets still exist", async () => {
    const onSelectTarget = vi.fn();
    render(
      <RevisionDeltaBar
        info={info()}
        discarded={[]}
        focused={false}
        onShowAll={vi.fn()}
        onSelectTarget={onSelectTarget}
        onDismiss={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByText("Details"));
    await userEvent.click(screen.getByRole("button", { name: "src/changed.py::changed" }));
    expect(onSelectTarget).toHaveBeenCalledWith("changed");
  });
});
