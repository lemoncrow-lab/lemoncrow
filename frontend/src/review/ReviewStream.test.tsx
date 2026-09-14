import { describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import ReviewStream from "./ReviewStream";
import type { Annotation, FileDetail, ReviewTarget } from "./types";

const pierre = vi.hoisted(() => ({
  parsePatchFiles: vi.fn((_patch: string, id: string) => [{ files: [{ id }] }]),
}));

vi.mock("@pierre/diffs", () => ({
  parsePatchFiles: pierre.parsePatchFiles,
}));

const codeView = vi.hoisted(() => ({ props: null as any }));

vi.mock("@pierre/diffs/react", async () => {
  const React = await import("react");
  return {
    CodeView: React.forwardRef((_props: any, ref: any) => {
      const props = _props;
      codeView.props = props;
      React.useImperativeHandle(ref, () => ({ scrollTo: vi.fn() }));
      /*
        The double reconciles the item list the way the real viewer does, and it
        has to: `CodeView.syncItemRecord` keeps the record it already holds
        whenever the incoming `version` is unchanged, so the new markers and the
        new `collapsed` flag are dropped on the floor. A double that always
        renders the newest payload cannot see that happen, which is how a
        length-only version key and a `collapsed` flag applied after the version
        was computed both shipped. `reconcileItems` releases the record of an
        item that leaves the list, so one that comes back is built fresh from
        whatever version it carries.
      */
      const records = React.useRef(new Map<string, any>());
      const rendered = props.items.map((next: any) => {
        const previous = records.current.get(next.id);
        return previous && previous.type === next.type && previous.version === next.version ? previous : next;
      });
      records.current = new Map(rendered.map((item: any) => [item.id, item]));
      return (
    <div data-testid="mock-code-view">
      {rendered.map((item: any) => (
        <section key={item.id} data-testid={`code-item:${item.id}`} data-collapsed={String(Boolean(item.collapsed))}>
          <div>{item.id}</div>
          {props.renderHeaderMetadata?.(item)}
          {!item.collapsed && (item.annotations ?? []).map((annotation: any, index: number) => (
            <div
              key={`${annotation.side}:${annotation.lineNumber}:${index}`}
              data-testid={`marker:${item.id}:${annotation.side}:${annotation.lineNumber}`}
            >
              {props.renderAnnotation?.(annotation, item)}
            </div>
          ))}
        </section>
      ))}
      {props.renderCodeViewFooter?.()}
    </div>
      );
    }),
  };
});

function target(id: string, path: string, line: number): ReviewTarget {
  return {
    target_id: `target:${id}`,
    unit_key: id,
    kind: "symbol",
    path,
    label: `${path}::${id}`,
    symbol: id,
    start_line: line,
    end_line: line + 2,
    hunk_ordinals: [0],
    spans: [{ side: "new", start_line: line, end_line: line, hunk_ordinal: 0 }],
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: 1,
    attention_level: "high",
    reasons: ["public contract changed"],
    additions: 1,
    deletions: 1,
    fingerprint_method: "symbol_body_sha256",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
  };
}

function detail(path: string): FileDetail {
  return {
    path,
    status: "modified",
    additions: 1,
    deletions: 1,
    patch: `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@ -1 +1 @@\n-old\n+new\n`,
    renderable: true,
    refusal: "",
    detail: "",
    degraded: [],
  };
}

/** Everything a ReviewStream needs except the two props a test is actually about. */
function streamProps(
  details: Record<string, FileDetail>,
  outstanding: [string, number][],
  activeTargetId: string,
) {
  return {
    details,
    errors: {},
    annotations: [] as Annotation[],
    diffStyle: "split" as const,
    activeTargetId,
    busy: false,
    remainingFileCount: 0,
    outstandingByPath: new Map(outstanding),
    onActiveTarget: vi.fn(),
    onDraft: vi.fn(),
    onCreate: vi.fn(),
    onSetAnnotationState: vi.fn(),
    onMark: vi.fn(),
    onComment: vi.fn(),
    onContext: vi.fn(),
    onBulkReview: vi.fn(),
    onLoadMore: vi.fn(),
  };
}

function fileAnnotation(id: string, path: string, body: string, extra: Partial<Annotation> = {}): Annotation {
  return {
    id,
    review_id: "review",
    revision_id: "revision",
    parent_id: "",
    kind: "comment",
    state: "open",
    body,
    created_by: "reviewer",
    created_by_actor: "human",
    created_at: "",
    updated_at: "",
    anchor_method: "file",
    anchor_method_label: "file",
    anchor_exact: true,
    anchor_detail: "",
    anchored: true,
    path,
    side: "new",
    start_line: 0,
    end_line: 0,
    file_level: true,
    unit_key: `file:${path}`,
    symbol: "",
    origin_symbol: "",
    ...extra,
  };
}

describe("continuous review stream", () => {
  it("renders several file diffs in one CodeView with target boundaries", async () => {
    const targets = [target("a_one", "src/a.py", 1), target("a_two", "src/a.py", 2), target("b_one", "src/b.py", 1)];
    const onMark = vi.fn();
    const onBulkReview = vi.fn();
    render(
      <ReviewStream
        targets={targets}
        details={{ "src/a.py": detail("src/a.py"), "src/b.py": detail("src/b.py") }}
        errors={{}}
        annotations={[]}
        draft={null}
        diffStyle="split"
        activeTargetId="target:a_one"
        busy={false}
        remainingFileCount={0}
        outstandingByPath={new Map([["src/a.py", 2], ["src/b.py", 1]])}
        onActiveTarget={vi.fn()}
        onDraft={vi.fn()}
        onCreate={vi.fn()}
        onSetAnnotationState={vi.fn()}
        onMark={onMark}
        onComment={vi.fn()}
        onContext={vi.fn()}
        onBulkReview={onBulkReview}
        onLoadMore={vi.fn()}
      />,
    );

    expect(screen.getByTestId("code-item:src/a.py")).toBeTruthy();
    expect(screen.getByTestId("code-item:src/b.py")).toBeTruthy();
    expect(screen.getByText("a_one")).toBeTruthy();
    expect(screen.getByText("a_two")).toBeTruthy();
    expect(screen.getByText("b_one")).toBeTruthy();
    // Nothing is filtered here, so neither header owes the reader a second
    // number: what the stream shows is everything the file holds.
    expect(screen.getByText("0/2 targets in view")).toBeTruthy();
    expect(screen.getByText("0/1 targets in view")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Mark a_one reviewed" }));
    expect(onMark).toHaveBeenCalledWith(targets[0], "reviewed", true);

    await userEvent.click(screen.getByRole("button", { name: "Mark eligible remainder reviewed in src/a.py" }));
    expect(onBulkReview).toHaveBeenCalledWith("src/a.py");
  });

  it("keeps parsed diff identity stable across judgment clicks and versions real item payload changes", async () => {
    pierre.parsePatchFiles.mockClear();
    const first = target("stable", "src/stable.py", 10);
    const common = {
      details: { "src/stable.py": detail("src/stable.py") },
      errors: {},
      annotations: [],
      diffStyle: "split" as const,
      activeTargetId: "target:stable",
      busy: false,
      remainingFileCount: 0,
      outstandingByPath: new Map([["src/stable.py", 1]]),
      onActiveTarget: vi.fn(),
      onDraft: vi.fn(),
      onCreate: vi.fn(),
      onSetAnnotationState: vi.fn(),
      onMark: vi.fn(),
      onComment: vi.fn(),
      onContext: vi.fn(),
      onBulkReview: vi.fn(),
      onLoadMore: vi.fn(),
    };
    const draft = {
      path: "src/stable.py",
      startLine: 1,
      endLine: 1,
      side: "additions" as const,
      targetUnitKey: first.unit_key,
    };
    const { rerender } = render(<ReviewStream {...common} targets={[first]} draft={draft} />);

    const initial = codeView.props.items[0];
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(1);

    rerender(<ReviewStream {...common} targets={[{ ...first, state: "reviewed" }]} draft={draft} />);
    const afterJudgment = codeView.props.items[0];
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(1);
    expect(afterJudgment.fileDiff).toBe(initial.fileDiff);
    expect(afterJudgment.version).toBe(initial.version);

    rerender(
      <ReviewStream
        {...common}
        targets={[{ ...first, state: "reviewed" }]}
        draft={{ ...draft, startLine: 2, endLine: 2 }}
      />,
    );
    const afterMarkerMove = codeView.props.items[0];
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(1);
    expect(afterMarkerMove.fileDiff).toBe(initial.fileDiff);
    expect(afterMarkerMove.version).toBeGreaterThan(afterJudgment.version);

    await userEvent.click(screen.getByRole("button", { name: "Collapse" }));
    const afterCollapse = codeView.props.items[0];
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(1);
    expect(afterCollapse.fileDiff).toBe(initial.fileDiff);
    expect(afterCollapse.version).toBeGreaterThan(afterMarkerMove.version);
  });

  it("hands a moved marker to the viewer even when the signature keeps its length", () => {
    const only = target("stable", "src/stable.py", 10);
    const common = streamProps({ "src/stable.py": detail("src/stable.py") }, [["src/stable.py", 1]], "target:stable");
    const draft = {
      path: "src/stable.py",
      startLine: 1,
      endLine: 1,
      side: "additions" as const,
      targetUnitKey: only.unit_key,
    };
    const { rerender } = render(<ReviewStream {...common} targets={[only]} draft={draft} />);
    expect(screen.getByTestId("marker:src/stable.py:additions:1")).toBeTruthy();

    // `additions:1-1` and `additions:2-2` are the same number of characters, so
    // a version derived from the signature's *length* cannot tell the two marker
    // states apart and the viewer keeps the record it already has — the composer
    // stays stranded on line 1 while the reader is looking at line 2.
    rerender(<ReviewStream {...common} targets={[only]} draft={{ ...draft, startLine: 2, endLine: 2 }} />);
    expect(screen.queryByTestId("marker:src/stable.py:additions:1")).toBeNull();
    expect(screen.getByTestId("marker:src/stable.py:additions:2")).toBeTruthy();
  });

  it("collapses and expands a file for real, with a version that only ever climbs", async () => {
    const only = target("stable", "src/stable.py", 10);
    const common = streamProps({ "src/stable.py": detail("src/stable.py") }, [["src/stable.py", 1]], "target:stable");
    render(<ReviewStream {...common} targets={[only]} draft={null} />);
    const section = () => screen.getByTestId("code-item:src/stable.py");
    const version = () => codeView.props.items[0].version;

    expect(section().dataset.collapsed).toBe("false");
    const opened = version();

    await userEvent.click(screen.getByRole("button", { name: "Collapse" }));
    expect(section().dataset.collapsed).toBe("true");
    const collapsed = version();
    expect(collapsed).toBeGreaterThan(opened);

    // Coming back to a state the item has already been in must not come back to
    // a version it has already had: the viewer skips an item whose version is
    // unchanged, so a counter that returned to `opened` here would leave the
    // file collapsed while the button says Collapse.
    await userEvent.click(screen.getByRole("button", { name: "Expand" }));
    expect(section().dataset.collapsed).toBe("false");
    expect(version()).toBeGreaterThan(collapsed);
  });

  it("starts a file's revision over after a search drops it from the stream", () => {
    const stable = target("stable", "src/stable.py", 10);
    const other = target("other", "src/other.py", 4);
    const common = streamProps(
      { "src/stable.py": detail("src/stable.py"), "src/other.py": detail("src/other.py") },
      [["src/stable.py", 1], ["src/other.py", 1]],
      "target:other",
    );
    const draft = { path: "src/stable.py", startLine: 1, endLine: 1, side: "additions" as const };
    const itemFor = (path: string) => codeView.props.items.find((item: any) => item.id === path);

    const { rerender } = render(<ReviewStream {...common} targets={[stable, other]} draft={draft} />);
    expect(itemFor("src/stable.py").version).toBe(1);

    rerender(<ReviewStream {...common} targets={[other]} draft={null} />);
    expect(screen.queryByTestId("code-item:src/stable.py")).toBeNull();

    // The revision cache is pruned when a path leaves, which is what stops it
    // growing one entry per file ever filtered in and out. Restarting at 1 is
    // safe only because the viewer released its record for the same item at the
    // same moment, so the item that comes back is built from scratch.
    rerender(<ReviewStream {...common} targets={[stable, other]} draft={{ ...draft, startLine: 5, endLine: 5 }} />);
    expect(itemFor("src/stable.py").version).toBe(1);
    expect(screen.getByTestId("marker:src/stable.py:additions:5")).toBeTruthy();
    expect(screen.queryByTestId("marker:src/stable.py:additions:1")).toBeNull();
  });

  it("leaves LemonCrow's attention note out of the diff's comment column", () => {
    const only = target("a_one", "src/a.py", 1);
    const common = streamProps({ "src/a.py": detail("src/a.py") }, [["src/a.py", 1]], "target:a_one");
    // Exactly what the server writes for an attention note: preparation.py's
    // project_lemoncrow_annotations passes created_by="lemoncrow" and
    // created_by_actor="unknown", never a human actor.
    const note = fileAnnotation("note", "src/a.py", "• public contract changed", {
      source: "lemoncrow",
      created_by: "lemoncrow",
      created_by_actor: "unknown",
      title: "Why this file deserves attention",
    });
    const human = fileAnnotation("human", "src/a.py", "Please rename this module", { source: "human" });
    render(<ReviewStream {...common} targets={[only]} draft={null} annotations={[note, human]} />);

    expect(screen.getByText("Please rename this module")).toBeTruthy();
    expect(screen.queryByText("• public contract changed")).toBeNull();
  });

  it("refuses to pass a search-narrowed count off as the file's own", () => {
    const only = target("a_one", "src/a.py", 1);
    // What the reader's search left in the stream is one of the seven targets
    // src/a.py still owes a judgment. `outstandingByPath` is the caller's
    // whole-review tally and is not narrowed by the search, so the header can
    // prove the stream is showing a slice without being told a second time.
    const common = streamProps({ "src/a.py": detail("src/a.py") }, [["src/a.py", 7]], "target:a_one");
    render(<ReviewStream {...common} targets={[only]} draft={null} />);
    expect(screen.queryByText("0/1 targets")).toBeNull();
    expect(screen.getByText("0/1 targets in view · 7 unreviewed in file")).toBeTruthy();
  });

  it("keeps CodeView options stable when only the active target changes", () => {
    const targets = [target("a", "src/a.py", 1), target("b", "src/a.py", 20)];
    const common = {
      targets,
      details: { "src/a.py": detail("src/a.py") },
      errors: {},
      annotations: [],
      draft: null,
      diffStyle: "split" as const,
      busy: false,
      remainingFileCount: 0,
      outstandingByPath: new Map([["src/a.py", 2]]),
      onActiveTarget: vi.fn(),
      onDraft: vi.fn(),
      onCreate: vi.fn(),
      onSetAnnotationState: vi.fn(),
      onMark: vi.fn(),
      onComment: vi.fn(),
      onContext: vi.fn(),
      onBulkReview: vi.fn(),
      onLoadMore: vi.fn(),
    };
    const { rerender } = render(<ReviewStream {...common} activeTargetId="target:a" />);
    const firstOptions = codeView.props.options;

    rerender(<ReviewStream {...common} activeTargetId="target:b" />);

    expect(codeView.props.options).toBe(firstOptions);
    expect(codeView.props.options.onGutterUtilityClick).toBe(firstOptions.onGutterUtilityClick);
  });

  it("updates the active target from manual scroll position", () => {
    const targets = [
      target("a_one", "src/a.py", 1),
      target("a_two", "src/a.py", 2),
      target("b_one", "src/b.py", 1),
    ];
    const onActiveTarget = vi.fn();
    const raf = vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(0);
      return 1;
    });
    render(
      <ReviewStream
        targets={targets}
        details={{ "src/a.py": detail("src/a.py"), "src/b.py": detail("src/b.py") }}
        errors={{}}
        annotations={[]}
        draft={null}
        diffStyle="split"
        activeTargetId="target:a_one"
        busy={false}
        remainingFileCount={0}
        outstandingByPath={new Map([["src/a.py", 2], ["src/b.py", 1]])}
        onActiveTarget={onActiveTarget}
        onDraft={vi.fn()}
        onCreate={vi.fn()}
        onSetAnnotationState={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
        onBulkReview={vi.fn()}
        onLoadMore={vi.fn()}
      />,
    );

    screen.getByTestId("review-stream").getBoundingClientRect = () => ({ top: 0 } as DOMRect);
    const positions = new Map([
      ["target:a_one", -100],
      ["target:a_two", 100],
      ["target:b_one", 260],
    ]);
    for (const node of document.querySelectorAll<HTMLElement>("[data-review-target-id]")) {
      const top = positions.get(node.dataset.reviewTargetId ?? "") ?? 1000;
      node.getBoundingClientRect = () => ({ top } as DOMRect);
    }

    act(() => codeView.props.onScroll());
    expect(onActiveTarget).toHaveBeenCalledWith("target:a_two");
    raf.mockRestore();
  });

  it("keeps an unrenderable file in the stream as an explicit refusal", () => {
    const fallback = target("binary", "logo.png", 0);
    const binary: FileDetail = {
      path: "logo.png",
      patch: "",
      renderable: false,
      refusal: "binary_file",
      detail: "Binary file — no text to review",
      degraded: [],
    };
    render(
      <ReviewStream
        targets={[fallback]}
        details={{ "logo.png": binary }}
        errors={{}}
        annotations={[]}
        draft={null}
        diffStyle="unified"
        activeTargetId="target:binary"
        busy={false}
        remainingFileCount={0}
        outstandingByPath={new Map([["logo.png", 1]])}
        onActiveTarget={vi.fn()}
        onDraft={vi.fn()}
        onCreate={vi.fn()}
        onSetAnnotationState={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
        onBulkReview={vi.fn()}
        onLoadMore={vi.fn()}
      />,
    );
    expect(screen.getByTestId("code-item:logo.png")).toBeTruthy();
  });

  it("hands only loaded files to CodeView for a 500-file / 1,500-target review", () => {
    const targets = Array.from({ length: 1500 }, (_, index) =>
      target(`large_${index}`, `src/large-${Math.floor(index / 3)}.py`, (index % 3) + 1),
    );
    const details = Object.fromEntries(
      Array.from({ length: 5 }, (_, index) => [`src/large-${index}.py`, detail(`src/large-${index}.py`)]),
    );

    render(
      <ReviewStream
        targets={targets}
        details={details}
        errors={{}}
        annotations={[]}
        draft={null}
        diffStyle="split"
        activeTargetId="target:large_0"
        busy={false}
        remainingFileCount={495}
        outstandingByPath={new Map(Array.from({ length: 500 }, (_, index) => [`src/large-${index}.py`, 3]))}
        onActiveTarget={vi.fn()}
        onDraft={vi.fn()}
        onCreate={vi.fn()}
        onSetAnnotationState={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
        onBulkReview={vi.fn()}
        onLoadMore={vi.fn()}
      />,
    );

    expect(screen.getAllByTestId(/^code-item:/)).toHaveLength(5);
    expect(screen.queryByTestId("code-item:src/large-5.py")).toBeNull();
    expect(screen.getByRole("button", { name: "Load next files · 495 remaining" })).toBeTruthy();
  });

  it("surfaces progressive loading without replacing the current stream", async () => {
    const onLoadMore = vi.fn();
    render(
      <ReviewStream
        targets={[target("a", "a.py", 1)]}
        details={{ "a.py": detail("a.py") }}
        errors={{}}
        annotations={[]}
        draft={null}
        diffStyle="split"
        activeTargetId="target:a"
        busy={false}
        remainingFileCount={7}
        outstandingByPath={new Map([["a.py", 1]])}
        onActiveTarget={vi.fn()}
        onDraft={vi.fn()}
        onCreate={vi.fn()}
        onSetAnnotationState={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
        onBulkReview={vi.fn()}
        onLoadMore={onLoadMore}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Load next files · 7 remaining" }));
    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });
});
