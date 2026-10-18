import { act, render, screen, waitFor } from "@testing-library/react";
import { useLayoutEffect } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import BriefProjectContext, { MapProjectHistory } from "./ProjectHistory";
import { fetchReviewProjectContext } from "./reviewApi";
import { edgeHistorySelection } from "./ReviewMapInspector";
import type { ReviewMapNode, ReviewProjectContext, ReviewProjectContextEntry } from "./types";

function entry(overrides: Partial<ReviewProjectContextEntry> = {}): ReviewProjectContextEntry {
  return {
    entry_id: "knowledge:1",
    context_class: "project_knowledge",
    subject: { kind: "path", key: "src/session.py" },
    knowledge_kind: "responsibility",
    summary: "Session refresh rejects expired and revoked tokens.",
    summary_truncated: false,
    state: "current",
    state_label: "Landed on main",
    freshness_label: "Landed on main · predates this Change, which edits src/session.py",
    stale_for_snapshot: true,
    support: "deterministic",
    canonical_ref: "refs/heads/main",
    relevance: "changed_path",
    source_paths: ["src/session.py"],
    observed_at: "2026-10-02T10:00:00Z",
    history: {
      origin: { event_id: "event:1", kind: "merged_change", generation_id: "generation:2", previous_generation_id: "generation:1", observed_at: "2026-10-02T10:00:00Z" },
      superseded_by: null,
    },
    citations: [
      { ref: "generation:2", kind: "generation", resolved: true, label: "Landed Change · 2026-10-02" },
      { ref: "review:landed-7", kind: "review", resolved: true, review_id: "landed-7", label: "Review landed-7" },
      { ref: "revision:rv-9", kind: "revision", resolved: false, label: "Revision not held in this workspace" },
    ],
    ...overrides,
  };
}

function context(overrides: Partial<ReviewProjectContext> = {}): ReviewProjectContext {
  return {
    purpose: "brief",
    available: true,
    reason: "",
    context_class: "project_knowledge",
    authority: "advisory",
    authority_note: "Landed project history.",
    current_generation: "generation:2",
    entries: [entry()],
    narration: [],
    omitted: 0,
    truncated: false,
    filtered_by_content_access: 0,
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("BriefProjectContext", () => {
  it("renders nothing without Project Knowledge, so the Brief is unchanged", async () => {
    for (const value of [null, context({ available: false, reason: "not_configured", entries: [] }), context({ entries: [] })]) {
      const load = vi.fn(async () => value);
      const { container, unmount } = render(<BriefProjectContext load={load} />);
      await waitFor(() => expect(load).toHaveBeenCalledTimes(1));
      expect(container).toBeEmptyDOMElement();
      unmount();
    }
    const failing = vi.fn(async () => { throw new Error("503"); });
    const { container } = render(<BriefProjectContext load={failing} />);
    await waitFor(() => expect(failing).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("labels history as advisory and stale, and links each entry to its source history", async () => {
    render(<BriefProjectContext load={async () => context({ omitted: 2 })} />);
    expect(await screen.findByText("Relevant history")).toBeInTheDocument();
    expect(screen.getByText("Landed history · advisory, not evidence for this Snapshot")).toBeInTheDocument();
    expect(screen.getByTestId("project-history-freshness")).toHaveTextContent("predates this Change");
    expect(screen.getByText("2 more not shown · history is bounded")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Source history" }));
    const citations = screen.getByTestId("project-history-citations");
    expect(citations).toHaveTextContent("Introduced: Landed Change · 2026-10-02");
    expect(screen.getByRole("link", { name: "Review landed-7" })).toHaveAttribute("href", "/r/landed-7");
    expect(citations).toHaveTextContent("Revision not held in this workspace");
  });

  it("mounting and remounting reads with GET only and never starts work", async () => {
    const spy = vi.fn(async (_url: string, _init?: RequestInit) =>
      new Response(JSON.stringify({ review_id: "rev-1", revision_id: "r", revision_number: 3, project_context: context() }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", spy);
    const load = () => fetchReviewProjectContext("rev-1", 3, "brief").then((payload) => payload.project_context);
    for (let mount = 0; mount < 3; mount += 1) {
      const { unmount } = render(<BriefProjectContext load={load} />);
      expect(await screen.findByText("Relevant history")).toBeInTheDocument();
      unmount();
    }
    const calls = spy.mock.calls as unknown as [string, RequestInit | undefined][];
    expect(calls).toHaveLength(3);
    for (const [url, init] of calls) {
      expect(url).toBe("/api/reviews/rev-1/project-context?purpose=brief&revision_number=3");
      expect((init?.method ?? "GET").toUpperCase()).toBe("GET");
      expect(init?.body ?? null).toBeNull();
    }
  });
});

describe("MapProjectHistory", () => {
  it("loads only on request, stays visibly apart from current edges, and resets per selection", async () => {
    const superseded = entry({
      entry_id: "knowledge:old",
      state: "superseded",
      state_label: "Superseded on main",
      freshness_label: "Superseded on main",
      stale_for_snapshot: false,
      history: {
        origin: { event_id: "event:0", kind: "baseline", generation_id: "generation:1", previous_generation_id: "", observed_at: "2026-10-01T10:00:00Z" },
        superseded_by: { event_id: "event:1", kind: "merged_change", generation_id: "generation:2", previous_generation_id: "generation:1", observed_at: "2026-10-02T10:00:00Z" },
      },
    });
    const load = vi.fn(async () => context({ purpose: "map", entries: [superseded] }));
    const { rerender } = render(<MapProjectHistory selectionKey="node-a" load={load} />);
    expect(load).not.toHaveBeenCalled();
    expect(screen.getByText("Landed history · not part of this Snapshot")).toBeInTheDocument();
    expect(screen.getByTestId("map-project-history").className).toContain("border-dashed");

    await userEvent.click(screen.getByRole("button", { name: "Show landed history" }));
    expect(load).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Superseded on main")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Source history" }));
    expect(screen.getByTestId("project-history-citations")).toHaveTextContent("Superseded: Landed Change · 2026-10-02");
    expect(screen.getByTestId("project-history-citations")).not.toHaveTextContent("more sources not shown");

    rerender(<MapProjectHistory selectionKey="node-b" load={load} />);
    expect(await screen.findByRole("button", { name: "Show landed history" })).toBeInTheDocument();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("never paints the previous selection's history under a new selection, even for one commit", async () => {
    const load = vi.fn(async () => context({ purpose: "map", entries: [entry({ state_label: "Superseded on main", freshness_label: "Superseded on main" })] }));
    const seen: boolean[] = [];
    function Probe({ selectionKey }: { selectionKey: string }) {
      // A parent layout effect runs after the child's DOM commit and before any
      // passive effect, so it sees exactly what the first frame would paint.
      useLayoutEffect(() => {
        seen.push(Boolean(document.body.textContent?.includes("Superseded on main")));
      }, [selectionKey]);
      return <MapProjectHistory selectionKey={selectionKey} load={load} />;
    }
    const { rerender } = render(<Probe selectionKey="node-a" />);
    await userEvent.click(screen.getByRole("button", { name: "Show landed history" }));
    expect(await screen.findByText("Superseded on main")).toBeInTheDocument();

    rerender(<Probe selectionKey="node-b" />);
    expect(seen.at(-1)).toBe(false);
    expect(screen.queryByText("Superseded on main")).not.toBeInTheDocument();
  });

  it("says when a bounded citation list left sources out", async () => {
    render(<MapProjectHistory selectionKey="node-a" load={async () => context({ purpose: "map", entries: [entry({ citations_omitted: 3 })] })} />);
    await userEvent.click(screen.getByRole("button", { name: "Show landed history" }));
    await userEvent.click(await screen.findByRole("button", { name: "Source history" }));
    expect(screen.getByTestId("project-history-citations")).toHaveTextContent("3 more sources not shown");
  });

  it("drops an answer that arrives after its selection is gone", async () => {
    let resolve: (value: ReviewProjectContext) => void = () => undefined;
    const load = vi.fn(() => new Promise<ReviewProjectContext>((done) => { resolve = done; }));
    const { rerender } = render(<MapProjectHistory selectionKey="node-a" load={load} />);
    await userEvent.click(screen.getByRole("button", { name: "Show landed history" }));
    expect(screen.getByText("Loading history…")).toBeInTheDocument();

    rerender(<MapProjectHistory selectionKey="node-b" load={load} />);
    await act(async () => { resolve(context({ purpose: "map", entries: [entry({ state_label: "Late answer", freshness_label: "Late answer" })] })); });
    expect(screen.queryByText("Late answer")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show landed history" })).toBeInTheDocument();
  });

  it("asks for an edge's history by its endpoints only, never an invented edge key", () => {
    const node = (id: string, path: string, component: string): ReviewMapNode => ({
      id, label: id, kind: "file", path, component, module: "src", line: 1, end_line: 1, status: "modified",
    } as unknown as ReviewMapNode);
    const selection = edgeHistorySelection(node("a", "src/auth.py", "auth"), node("b", "src/db.py", "db"));
    expect(selection).toEqual({ paths: ["src/auth.py", "src/db.py"], components: ["auth", "src", "db"] });
    expect(selection).not.toHaveProperty("edges");
    expect(edgeHistorySelection(undefined, undefined)).toEqual({ paths: [], components: [] });
  });

  it("says when nothing is known rather than implying the selection has no history", async () => {
    render(<MapProjectHistory selectionKey="edge-a" load={async () => context({ purpose: "map", entries: [] })} />);
    await userEvent.click(screen.getByRole("button", { name: "Show landed history" }));
    expect(await screen.findByText("No landed history for this selection.")).toBeInTheDocument();
  });
});

describe("fetchReviewProjectContext", () => {
  it("encodes a Map selection as repeated, bounded query keys on a GET", async () => {
    const spy = vi.fn(async (_url: string, _init?: RequestInit) =>
      new Response(JSON.stringify({ project_context: context() }), { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    vi.stubGlobal("fetch", spy);
    await fetchReviewProjectContext("rev 1", 2, "map", { paths: ["src/a.py", "src/b.py"], components: ["auth"], edges: ["auth->db"] });
    const [url, init] = (spy.mock.calls as unknown as [string, RequestInit | undefined][])[0];
    expect(url).toBe(
      "/api/reviews/rev%201/project-context?purpose=map&revision_number=2&path=src%2Fa.py&path=src%2Fb.py&component=auth&edge=auth-%3Edb",
    );
    expect((init?.method ?? "GET").toUpperCase()).toBe("GET");
  });
});
