/**
 * The comment card's one non-negotiable line: how this comment got to this line.
 *
 * The field was typed, on the wire and rendered nowhere. A comment re-found by
 * its text alone — inside a function that has since been renamed and
 * re-documented — was drawn with exactly the same weight as one whose blob
 * never changed, and the reader had no way to tell the two apart. That is plan
 * §4.4's "never silently move a comment to a different line after a revision":
 * the silence is the failure, not the line number.
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import AnnotationCard, { OrphanedComments } from "./AnnotationCard";
import type { Annotation } from "./types";

function annotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: "ann-1",
    review_id: "rev-1",
    revision_id: "rrv-1",
    parent_id: "",
    kind: "comment",
    state: "open",
    body: "prices can be None here",
    created_by: "local",
    created_by_actor: "human",
    source: "human",
    source_id: "local",
    created_at: "",
    updated_at: "",
    anchor_method: "identical_blob",
    anchor_method_label: "file unchanged since the comment",
    anchor_exact: true,
    anchor_detail: "file unchanged in this revision",
    anchored: true,
    path: "src/svc.py",
    side: "new",
    start_line: 3,
    end_line: 3,
    unit_key: "sym:abc",
    symbol: "total_price",
    origin_symbol: "",
    ...overrides,
  };
}

function mount(comments: Annotation[]) {
  return render(
    <AnnotationCard
      comments={comments}
      all={comments}
      label="Comment"
      composing={false}
      busy={false}
      onSubmit={vi.fn()}
      onCancel={vi.fn()}
      onResolve={vi.fn()}
      onReopen={vi.fn()}
    />,
  );
}

describe("AnnotationCard", () => {
  it("offers Comment, Request change and Suggestion for new comments, not Looks good", () => {
    render(
      <AnnotationCard
        comments={[]}
        all={[]}
        label="New comment"
        composing
        busy={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Comment" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Request change" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Suggestion" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Looks good" })).toBeNull();
  });

  it("defaults Request change to marking the current target needs changes", async () => {
    const onSubmit = vi.fn();
    render(
      <AnnotationCard
        comments={[]}
        all={[]}
        label="New comment"
        composing
        busy={false}
        targetMarkAvailable
        onSubmit={onSubmit}
        onCancel={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
      />,
    );
    await userEvent.type(screen.getByPlaceholderText("What does the reviewer need to know?"), "Guard the retry path.");
    await userEvent.click(screen.getByRole("button", { name: "Request change" }));
    const checkbox = screen.getByRole("checkbox", { name: "Mark target needs changes" }) as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).toHaveBeenCalledWith("Guard the retry path.", "request_change", "", true);
  });

  it("lets the reviewer leave a blocking comment without changing target state", async () => {
    const onSubmit = vi.fn();
    render(
      <AnnotationCard
        comments={[]}
        all={[]}
        label="New comment"
        composing
        busy={false}
        targetMarkAvailable
        onSubmit={onSubmit}
        onCancel={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
      />,
    );
    await userEvent.type(screen.getByPlaceholderText("What does the reviewer need to know?"), "Question this behavior.");
    await userEvent.click(screen.getByRole("button", { name: "Request change" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Mark target needs changes" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).toHaveBeenCalledWith("Question this behavior.", "request_change", "", false);
  });

  it("never carries a top-level draft into a reply", async () => {
    // The composer sits at one place in the tree, so without a key React hands
    // the reply the text and the kind the reviewer wrote for the line itself --
    // and Save posts it against a thread they only meant to answer.
    const comment = annotation();
    render(
      <AnnotationCard
        comments={[comment]}
        all={[comment]}
        label="New comment"
        composing
        busy={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        onResolve={vi.fn()}
        onReopen={vi.fn()}
      />,
    );
    await userEvent.type(screen.getByPlaceholderText("What does the reviewer need to know?"), "TOP LEVEL DRAFT");
    await userEvent.click(screen.getByRole("button", { name: "Request change" }));
    await userEvent.click(screen.getByRole("button", { name: "Reply" }));

    const reply = screen.getByPlaceholderText("What does the reviewer need to know?") as HTMLTextAreaElement;
    expect(reply.value).toBe("");
    expect(screen.getByRole("button", { name: "Comment" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Request change" }).getAttribute("aria-pressed")).toBe("false");
  });

  it("names the rung on a comment whose file never changed", () => {
    mount([annotation()]);
    expect(screen.getByText(/anchor: file unchanged since the comment/)).toBeTruthy();
  });

  it("names the rung on a heuristic re-find, in words rather than a code", () => {
    mount([
      annotation({
        anchor_method: "unique_text",
        anchor_method_label: "re-found by its text alone",
        anchor_exact: false,
        anchor_detail: "selected text occurs exactly once",
      }),
    ]);
    const chip = screen.getByText(/anchor: re-found by its text alone/);
    expect(chip).toBeTruthy();
    // Never the identifier.
    expect(screen.queryByText(/unique_text/)).toBeNull();
    // The full sentence stays reachable without spending a line on it.
    expect(chip.getAttribute("title")).toBe("selected text occurs exactly once");
  });

  it("gives the re-find a different weight from the untouched file", () => {
    const { container: exact } = mount([annotation()]);
    const { container: heuristic } = mount([
      annotation({ anchor_method: "unique_text", anchor_method_label: "re-found by its text alone", anchor_exact: false }),
    ]);
    const className = (root: HTMLElement) =>
      root.querySelector("[title]")?.getAttribute("class") ?? "";
    expect(className(exact)).not.toBe(className(heuristic));
  });

  it("shows the definition the server re-derived, not the one it was written against", () => {
    mount([annotation({ symbol: "total_price" })]);
    expect(screen.getByText(/in total_price/)).toBeTruthy();
    expect(screen.queryByText(/compute_total/)).toBeNull();
  });

  it("keeps author rationale visibly separate from human judgment", () => {
    mount([
      annotation({
        source: "author",
        source_id: "claude/session-7",
        created_by: "claude",
        created_by_actor: "agent",
        title: "Why context became required",
        body: "Background refresh has no request object.",
      }),
    ]);
    expect(screen.getByText("AUTHOR")).toBeTruthy();
    expect(screen.getByText("claude/session-7")).toBeTruthy();
    expect(screen.getByText("Why context became required")).toBeTruthy();
    expect(screen.queryByText("Request change")).toBeNull();
    expect(screen.queryByRole("button", { name: "Resolve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reply" })).toBeNull();
  });

  it("labels AI hypotheses and exposes evidence only as evidence", () => {
    mount([
      annotation({
        source: "ai_review",
        source_id: "correctness",
        created_by: "correctness",
        created_by_actor: "agent",
        title: "Possible stale-cache behavior",
        evidence: ["caller:src/cache.py:L44"],
        confidence: 0.72,
      }),
    ]);
    expect(screen.getByText("AI REVIEW")).toBeTruthy();
    expect(screen.getByText(/confidence 72%/)).toBeTruthy();
    expect(screen.getByText(/evidence · 1/)).toBeTruthy();
  });

  it("shows an author-addressed claim without resolving the human comment", () => {
    mount([
      annotation({
        author_response: "addressed",
        author_response_source_id: "claude/session-7",
        author_response_at: "2026-09-09T12:00:00+00:00",
      }),
    ]);
    expect(screen.getByText(/author says addressed · re-review/)).toBeTruthy();
    expect(screen.getByText(/claimed by claude\/session-7/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Resolve" })).toBeTruthy();
    expect(screen.queryByText(/^resolved$/)).toBeNull();
  });

  it("an orphan says where it was written, never where it is", () => {
    // "not relocated" and "in gone_forever" on one card is a claim the badge
    // above it has already denied. The name is still worth showing -- it is how
    // a reader finds the old code -- but only as history.
    mount([
      annotation({
        anchor_method: "orphaned",
        anchor_method_label: "not relocated",
        anchor_exact: false,
        anchored: false,
        symbol: "",
        origin_symbol: "gone_forever",
      }),
    ]);
    expect(screen.getByText(/originally in gone_forever/)).toBeTruthy();
    expect(screen.queryByText(/^in gone_forever/)).toBeNull();
  });
});

describe("OrphanedComments", () => {
  function orphan(overrides: Partial<Annotation> = {}): Annotation {
    return annotation({
      anchored: false,
      anchor_method: "orphaned",
      anchor_method_label: "not relocated",
      anchor_exact: false,
      anchor_detail: "more than one place fit",
      symbol: "",
      origin_symbol: "total_price",
      ...overrides,
    });
  }

  it("keeps the source, state and title an anchored row is given", () => {
    // An orphan has no line for the reader to weigh the claim against, so the
    // source badge, the state and the title are all that separate a machine
    // finding from a colleague's question. Stripped to a body and a rung, the
    // two are indistinguishable at exactly the moment that matters.
    render(
      <OrphanedComments
        comments={[
          orphan({
            id: "orphan-ai",
            source: "ai_review",
            created_by: "correctness",
            created_by_actor: "agent",
            title: "Possible stale-cache behavior",
            state: "open",
            start_line: 41,
            end_line: 41,
          }),
        ]}
      />,
    );
    expect(screen.getByText("AI REVIEW")).toBeTruthy();
    expect(screen.getByText("L41")).toBeTruthy();
    expect(screen.getByText("open")).toBeTruthy();
    expect(screen.getByText("Possible stale-cache behavior")).toBeTruthy();
    expect(screen.getByText("prices can be None here")).toBeTruthy();
    expect(screen.getByText(/not relocated: more than one place fit/)).toBeTruthy();
  });

  it("says a file-level orphan was written against the file, not against L0", () => {
    render(<OrphanedComments comments={[orphan({ file_level: true, start_line: 0, end_line: 0 })]} />);
    expect(screen.getByText("file")).toBeTruthy();
    expect(screen.queryByText(/^L0$/)).toBeNull();
  });

  it("agrees in number with the comments it lists", () => {
    const one = render(<OrphanedComments comments={[orphan()]} />).container;
    expect(one.textContent).toContain("1 comment lost its anchor");
    const two = render(
      <OrphanedComments comments={[orphan({ id: "a" }), orphan({ id: "b" })]} />,
    ).container;
    expect(two.textContent).toContain("2 comments lost their anchor");
  });
});
