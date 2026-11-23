/**
 * P11-3: a judgment that followed a symbol to another file says so on the part's own row.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import ReviewTargetHeader from "./ReviewTargetHeader";
import type { ReviewTarget } from "./types";

const TARGET: ReviewTarget = {
  target_id: "target:parse",
  unit_key: "sym:new",
  kind: "symbol",
  path: "pkg/parser.py",
  label: "parse_header",
  symbol: "parse_header",
  start_line: 1,
  end_line: 12,
  hunk_ordinals: [0],
  spans: [{ side: "new", start_line: 1, end_line: 12, hunk_ordinal: 0 }],
  state: "reviewed",
  changed_since_mark: false,
  reviewed_revision_id: "rrv-1",
  attention_rank: 1,
  attention_level: "normal",
  reasons: [],
  additions: 12,
  deletions: 0,
  fingerprint_method: "symbol_body_sha256",
  verification: { pass: 0, fail: 0, unknown: 0 },
  annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
};

function header(target: ReviewTarget) {
  return render(
    <ReviewTargetHeader
      target={target}
      active
      busy={false}
      onFocus={vi.fn()}
      onMark={vi.fn()}
      onComment={vi.fn()}
      onContext={vi.fn()}
    />
  );
}

describe("moved symbol label (P11-3)", () => {
  it("shows where a carried symbol came from", () => {
    header({ ...TARGET, moved_from: { path: "pkg/old/parser.py" } });
    expect(screen.getByTestId("target-moved-from").textContent).toBe(
      "Moved from pkg/old/parser.py"
    );
  });

  it("shows nothing for a part that did not move", () => {
    header(TARGET);
    expect(screen.queryByTestId("target-moved-from")).toBeNull();
  });

  it("does not call a renamed part moved: its judgment reopened rather than carried", () => {
    header({
      ...TARGET,
      state: "changed_since_review",
      changed_since_mark: true,
      renamed_from: { path: "pkg/parser.py", symbol: "parse_head" },
    });
    expect(screen.queryByTestId("target-moved-from")).toBeNull();
  });
});
