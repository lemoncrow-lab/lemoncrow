import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import FinishSheet, { type FinishRecordFacts } from "./FinishSheet";
import {
  plainCode,
  plainCodeList,
  PrincipalNamesContext,
  principalLabel,
  unitKeyLabel,
  usePrincipalLabel,
  ViewerContext,
} from "./plainWords";
import ReviewReadinessSheet, { deriveReviewReadiness } from "./ReviewReadinessSheet";
import type { ReviewClosure, ReviewLandingReadiness } from "./types";

const SNAKE = /\b[a-z]+(?:_[a-z0-9]+)+\b/g;
const ID_LIKE = /#\d/g;

/**
 * Strings that are the reviewer's own content (code names, paths, titles, prose
 * written by an author or a check), collected from the fixture. Code
 * identifiers may legitimately be snake_case; internal codes (kinds, sources,
 * states, degraded reasons) are not in these fields, so they are still caught.
 */
const CONTENT_FIELDS = new Set([
  "symbol", "path", "qualified_name", "label", "title", "statement", "detail", "summary", "summary_text", "body",
  "text", "question", "name", "snippet", "scope", "reason", "rationale", "before", "after", "fact", "paragraphs",
  "statements", "key",
]);

function contentTokens(value: unknown, into = new Set<string>(), field = ""): Set<string> {
  if (typeof value === "string") {
    if (CONTENT_FIELDS.has(field)) for (const token of value.match(SNAKE) ?? []) into.add(token);
  } else if (Array.isArray(value)) {
    for (const item of value) contentTokens(item, into, field);
  } else if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) contentTokens(item, into, key);
  }
  return into;
}

/** Open every disclosure (rows inside some render only while open), then read text and accessible names. */
function visibleText(container: HTMLElement): string {
  for (let round = 0; round < 6; round += 1) {
    const closed = [...container.querySelectorAll("details:not([open]) > summary")];
    if (closed.length === 0) break;
    for (const summary of closed) fireEvent.click(summary);
  }
  // Text nodes joined with a space, so adjacent elements never fuse into one token.
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const texts: string[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) texts.push(node.textContent ?? "");
  const labels = [...container.querySelectorAll("[aria-label]")].map((node) => node.getAttribute("aria-label") ?? "");
  return [...texts, ...labels].join("\n");
}

function leaks(text: string, allowed: ReadonlySet<string>): string[] {
  const snake = (text.match(SNAKE) ?? []).filter((token) => !allowed.has(token));
  const ids = text.match(ID_LIKE) ?? [];
  return [...new Set([...snake, ...ids])];
}

afterEach(cleanup);

describe("plain words", () => {
  it("names known codes in words and never prints snake_case for an unknown one", () => {
    expect(plainCode("current_index_checkout_differs_from_snapshot")).toBe("your checkout has moved past this revision");
    expect(plainCode("document_section")).toBe("top-level code");
    expect(plainCode("hunk")).toBe("changed lines");
    expect(plainCode("NOT_RUN")).toBe("did not run");
    expect(plainCode("some_future_code")).toBe("some future code");
    expect(plainCode("current_index_rebuilding")).toBe("code index rebuilding");
    expect(plainCodeList(["current_index_unavailable", "current_index_missing", "packet_unavailable"])).toBe(
      "no code index for this repository and the change analysis is missing",
    );
    expect(unitKeyLabel("lib/cache.ts#0")).toBe("lib/cache.ts");
    expect(unitKeyLabel("unit:lib/cache.ts::wrap")).toBe("wrap in lib/cache.ts");
  });

  it("says you for the viewer and the local principal, a name otherwise, and never an opaque id", () => {
    expect(principalLabel("user_local", "")).toBe("you");
    expect(principalLabel("user_7f3a", "user_7f3a")).toBe("you");
    expect(principalLabel("user_alice", "user_local")).toBe("alice");
    expect(principalLabel("alice", "")).toBe("alice");
    expect(principalLabel("3f2a9c1e-77aa-4b1c-9d2e-0a1b2c3d4e5f", "")).toBe("a reviewer");
    expect(principalLabel("github|123456789", "")).toBe("a reviewer");
    expect(principalLabel("", "", "you")).toBe("you");
  });

  it("prefers a stored display name over the id's readable part, but never over you", () => {
    const names = { user_alice: "Alice Park", agent_billing: " Billing agent ", "3f2a9c1e-77aa-4b1c-9d2e-0a1b2c3d4e5f": "Review agent", user_blank: "  " };
    expect(principalLabel("user_alice", "", undefined, names)).toBe("Alice Park");
    expect(principalLabel("agent_billing", "", undefined, names)).toBe("Billing agent");
    // An opaque id with a stored name reads as the name, not the fallback.
    expect(principalLabel("3f2a9c1e-77aa-4b1c-9d2e-0a1b2c3d4e5f", "", undefined, names)).toBe("Review agent");
    // The viewer and the local principal stay "you" even when a name is stored.
    expect(principalLabel("user_alice", "user_alice", undefined, names)).toBe("you");
    expect(principalLabel("local", "", undefined, { local: "Someone" })).toBe("you");
    // No usable name: today's label.
    expect(principalLabel("user_blank", "", undefined, names)).toBe("blank");
    expect(principalLabel("user_bob", "", undefined, names)).toBe("bob");
    expect(principalLabel("github|123456789", "", "a reviewer", names)).toBe("a reviewer");
  });

  it("usePrincipalLabel binds the viewer and the names the Reader provides", () => {
    function Who({ id }: { id: string }) {
      const label = usePrincipalLabel();
      return <span>{label(id, "someone")}</span>;
    }
    render(
      <ViewerContext.Provider value="user_me">
        <PrincipalNamesContext.Provider value={{ agent_billing: "Billing agent" }}>
          <Who id="agent_billing" />
          <Who id="user_me" />
          <Who id="" />
        </PrincipalNamesContext.Provider>
      </ViewerContext.Provider>,
    );
    expect(screen.getByText("Billing agent")).toBeTruthy();
    expect(screen.getByText("you")).toBeTruthy();
    expect(screen.getByText("someone")).toBeTruthy();
    cleanup();
    render(<Who id="agent_billing" />);
    expect(screen.getByText("agent billing")).toBeTruthy();
  });
});

describe("no internal code reaches the reviewer", () => {
  it("Review Status", () => {
    const model = deriveReviewReadiness({
      progress: { target_count: 4, reviewed: 2, changed_since_review: 1, needs_changes: 1, unreviewed: 0, unknown: 0, mechanical: 0 },
      openComments: 2,
      commentsState: "failed",
      verification: { pass: 1, fail: 1, not_run: 1, unknown: 1 },
      evidence: [
        { name: "Unit tests", status: "PASS", detail: "42 passed", source: "pytest", scope: "review" },
        { name: "API contract", status: "FAIL", detail: "response changed", source: "integration", scope: "review" },
        { name: "Browser flow", status: "NOT_RUN", detail: "service unavailable", source: "playwright", scope: "review" },
        { name: "Migration rollback", status: "UNKNOWN", detail: "", source: "manual", scope: "review" },
      ],
      staleArtifacts: 2,
      outcome: { id: "rot-1", review_id: "rev-1", revision_id: "rrv-1", reviewer_id: "user_local", outcome: "changes_requested", summary: "", created_at: "", stale: false },
    });
    const landing: ReviewLandingReadiness = {
      review_id: "review-1", snapshot_id: "rr-2", snapshot_number: 2, tree_fingerprint: "tree-2", source_fingerprint: "source-2",
      conditions: [{ id: "checks", label: "Required checks", state: "pending", detail: "GitHub is waiting on CI.", owner: "scm", category: "scm", url: "" }],
      ready: false, armable: true, blocker_count: 1, local_blocker_count: 0, conditions_hash: "conditions-2", work_items: [],
      landing: { supported: true, provider: "github", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "" },
    };
    const { container } = render(
      <ViewerContext.Provider value="user_local">
        <ReviewReadinessSheet model={model} landing={landing} onArmLanding={vi.fn()} onClose={vi.fn()} />
      </ViewerContext.Provider>,
    );
    expect(leaks(visibleText(container), new Set())).toEqual([]);
  });

  it("Finish sheet, and its verdict says by you for the viewer", () => {
    const summary: ReviewClosure = {
      status: "open", target_count: 6, reviewed_targets: 0, unreviewed_targets: 6, changed_since_review: 0, needs_changes: 0,
      unknown_targets: 0, open_comments: 0, orphaned_comments: 0, failed_verification: 0, unresolved_verification: 5,
      previous_revision_evidence: 0, discarded_verdicts: 0,
    };
    const record: FinishRecordFacts = {
      revisionNumber: 0,
      headSha: "",
      dirty: true,
      targets: [
        { id: "lib/cache.ts#0", label: "Other changed lines", path: "lib/cache.ts", reviewed: false },
        { id: "invoices/list.ts::listInvoices", label: "listInvoices", path: "invoices/list.ts", reviewed: false },
      ],
      passedChecks: [],
      openConditions: [],
      reviewer: "user_local",
    };
    const { container } = render(
      <ViewerContext.Provider value="user_local">
        <FinishSheet record={record} summary={summary} busy={false} onClose={vi.fn()} onFinish={vi.fn()} />
      </ViewerContext.Provider>,
    );
    // The verdict reads as decided only once it is picked, and names the viewer as "you".
    expect(document.body.textContent).not.toContain("Decided");
    fireEvent.click(screen.getByRole("radio", { name: "Comment" }));
    const text = visibleText(document.body);
    expect(document.body.textContent).toContain("Comment · by you");
    expect(leaks(text, contentTokens(record))).toEqual([]);
    expect(container).toBeTruthy();
  });

  it("Finish sheet names another reviewer by their stored display name", () => {
    const summary: ReviewClosure = {
      status: "open", target_count: 1, reviewed_targets: 0, unreviewed_targets: 1, changed_since_review: 0, needs_changes: 0,
      unknown_targets: 0, open_comments: 0, orphaned_comments: 0, failed_verification: 0, unresolved_verification: 0,
      previous_revision_evidence: 0, discarded_verdicts: 0,
    };
    const record: FinishRecordFacts = {
      revisionNumber: 0,
      headSha: "",
      dirty: false,
      targets: [{ id: "lib/cache.ts#0", label: "Other changed lines", path: "lib/cache.ts", reviewed: false }],
      passedChecks: [],
      openConditions: [],
      reviewer: "agent_review",
    };
    render(
      <ViewerContext.Provider value="user_me">
        <PrincipalNamesContext.Provider value={{ agent_review: "Review agent" }}>
          <FinishSheet record={record} summary={summary} busy={false} onClose={vi.fn()} onFinish={vi.fn()} />
        </PrincipalNamesContext.Provider>
      </ViewerContext.Provider>,
    );
    fireEvent.click(screen.getByRole("radio", { name: "Comment" }));
    expect(document.body.textContent).toContain("Comment · by Review agent");
  });
});
