import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import BriefArchitectureDelta from "./BriefArchitectureDelta";
import type { ReviewArchitectureDelta, ReviewTarget } from "./types";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function materialDelta(): ReviewArchitectureDelta {
  return {
    version: 1, digest: "old-snapshot", status: "ready", reason: "", base_identity: "base", snapshot_identity: "old",
    grouping: { strategy: "directory", version: 1, stable_for_identical_input: true, components: [] },
    summary: {
      material: true, status: "changed", changed_components: 1, changed_nodes: 1, changed_edges: 1,
      added_edges: 1, removed_edges: 0, modified_edges: 0, context_only_edges: 0, unknown_edges: 0,
      important_concerns: [], exactness: "snapshot", degraded: [],
    },
    flow: { applicable: false, kind: "", reason: "", steps: [] },
    graph: { level: "architecture", focus: "", nodes: [], edges: [], truncated: false },
  };
}

describe("lazy architecture Snapshot identity", () => {
  it.each([false, true])("ignores a response after Snapshot advancement (eager=%s)", async (eager) => {
    const old = deferred<ReviewArchitectureDelta | null>();
    const loadOld = vi.fn(() => old.promise);
    const loadNew = vi.fn().mockResolvedValue(null);
    const { rerender } = render(<BriefArchitectureDelta load={loadOld} onOpen={vi.fn()} eager={eager} />);
    if (!eager) await userEvent.click(screen.getByRole("button", { name: "Check architecture changes" }));
    await act(async () => {});
    expect(loadOld).toHaveBeenCalledTimes(1);
    rerender(<BriefArchitectureDelta load={loadNew} onOpen={vi.fn()} eager={eager} />);
    await act(async () => old.resolve(materialDelta()));
    expect(screen.queryByText("Architecture moved")).toBeNull();
    if (!eager) {
      expect(loadNew).not.toHaveBeenCalled();
      await userEvent.click(screen.getByRole("button", { name: "Check architecture changes" }));
    }
    expect(loadNew).toHaveBeenCalledTimes(1);
  });

  it("keeps failed manual loads retryable without claiming no movement", async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error("unavailable")).mockResolvedValueOnce(materialDelta());
    render(<BriefArchitectureDelta load={load} onOpen={vi.fn()} eager={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Check architecture changes" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Architecture details could not load. Try again.");
    expect(screen.queryByText("No material architecture changes")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Check architecture changes" }));
    expect(await screen.findByText("Architecture moved")).toBeTruthy();
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("cancels a manually requested response on unmount", async () => {
    const reply = deferred<ReviewArchitectureDelta | null>();
    const load = vi.fn(() => reply.promise);
    const { unmount } = render(<BriefArchitectureDelta load={load} onOpen={vi.fn()} eager={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Check architecture changes" }));
    unmount();
    await act(async () => reply.resolve(materialDelta()));
    expect(screen.queryByText("Architecture moved")).toBeNull();
  });
});

function delta(concern: string): ReviewArchitectureDelta {
  return {
    status: "ready",
    summary: {
      material: true,
      changed_components: 1,
      changed_edges: 1,
      added_edges: 1,
      removed_edges: 0,
      modified_edges: 0,
      important_concerns: [concern],
      exactness: "snapshot",
      degraded: [],
    },
    graph: { nodes: [], focus: "" },
  } as unknown as ReviewArchitectureDelta;
}

describe("BriefArchitectureDelta", () => {
  it("does not publish a manual response after its loader is superseded", async () => {
    const stale = deferred<ReviewArchitectureDelta | null>();
    const loadStale = vi.fn(() => stale.promise);
    const loadCurrent = vi.fn(async () => delta("current response"));
    const onOpen = vi.fn();
    const { rerender } = render(<BriefArchitectureDelta eager={false} load={loadStale} onOpen={onOpen} />);

    await userEvent.click(screen.getByRole("button", { name: "Check architecture changes" }));
    expect(loadStale).toHaveBeenCalledTimes(1);

    rerender(<BriefArchitectureDelta eager={false} load={loadCurrent} onOpen={onOpen} />);
    await userEvent.click(await screen.findByRole("button", { name: "Check architecture changes" }));
    expect(await screen.findByText("current response")).toBeInTheDocument();

    await act(async () => {
      stale.resolve(delta("stale response"));
      await stale.promise;
    });

    expect(screen.queryByText("stale response")).toBeNull();
    expect(screen.getByText("current response")).toBeInTheDocument();
  });

  it("walks through the change in place and jumps to the step's review target", async () => {
    const moved = materialDelta();
    moved.graph.nodes = [{
      id: "file:src/api.ts", label: "api.ts", kind: "file", path: "src/api.ts", line: 0, end_line: 0, status: "changed",
      delta_status: "changed", exactness: "snapshot", target_ids: [], important_public_contract: false, boundaries: [], boundary_basis: "",
      selection: { kind: "review_map_node", review_id: "r", revision_id: "v", node_id: "file:src/api.ts", path: "src/api.ts", line: 4, side: "new", exactness: "snapshot" },
    }];
    const onOpenTarget = vi.fn();
    const loadFindings = vi.fn().mockResolvedValue([]);
    const targets = [{ target_id: "t-api", path: "src/api.ts", start_line: 1, end_line: 9 }] as unknown as ReviewTarget[];
    render(<BriefArchitectureDelta load={async () => moved} onOpen={vi.fn()} story={{ targets, checks: null, loadFindings, onOpenTarget }} />);

    await userEvent.click(await screen.findByRole("button", { name: "Walk through the change" }));
    expect(await screen.findByTestId("change-story")).toHaveTextContent("api.ts");
    expect(loadFindings).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("No open P0 or P1 finding is anchored to this file.")).toBeInTheDocument();
    expect(screen.getByTestId("change-story-snapshot-checks")).toHaveTextContent("not available here");

    await userEvent.click(screen.getByRole("button", { name: "Open source" }));
    expect(onOpenTarget).toHaveBeenCalledWith("t-api");
    expect(screen.queryByTestId("change-story")).toBeNull();
  });
});

function movedNode(path: string, line: number) {
  return {
    id: `file:${path}`, label: path.split("/").pop(), kind: "file", path, line: 0, end_line: 0, status: "changed",
    delta_status: "changed", exactness: "snapshot", target_ids: [], important_public_contract: false, boundaries: [], boundary_basis: "",
    selection: { kind: "review_map_node", review_id: "r", revision_id: "v", node_id: `file:${path}`, path, line, side: "new", exactness: "snapshot" },
  };
}

function movedDelta(...nodes: ReturnType<typeof movedNode>[]): ReviewArchitectureDelta {
  const base = materialDelta();
  base.graph.nodes = nodes as unknown as ReviewArchitectureDelta["graph"]["nodes"];
  return base;
}

function target(id: string, path: string, start: number, end: number, spans: [number, number][] = []): ReviewTarget {
  return {
    target_id: id, path, start_line: start, end_line: end,
    spans: spans.map(([start_line, end_line]) => ({ side: "new", start_line, end_line, hunk_ordinal: 0 })),
  } as unknown as ReviewTarget;
}

describe("Change Story in the Brief", () => {
  it("says in words why there is no graph to walk, and offers a retry only when the load failed", async () => {
    const unavailable = { ...materialDelta(), status: "unavailable", reason: "Snapshot architecture comparison is unavailable (Boom).", summary: { ...materialDelta().summary, material: false } };
    const { unmount } = render(<BriefArchitectureDelta load={async () => unavailable} onOpen={vi.fn()} />);
    expect(await screen.findByTestId("brief-architecture-no-graph")).toHaveTextContent(
      "The architecture comparison is unavailable for this Snapshot (Snapshot architecture comparison is unavailable (Boom)), so there is no change to walk through.",
    );
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    unmount();

    const missing = render(<BriefArchitectureDelta load={async () => null} onOpen={vi.fn()} />);
    expect(await screen.findByTestId("brief-architecture-no-graph")).toHaveTextContent("This Snapshot has no architecture comparison");
    missing.unmount();

    const empty = { ...materialDelta(), summary: { ...materialDelta().summary, material: false } };
    const emptyView = render(<BriefArchitectureDelta load={async () => empty} onOpen={vi.fn()} />);
    expect(await screen.findByTestId("brief-architecture-no-graph")).toHaveTextContent("architecture graph is empty");
    emptyView.unmount();

    const load = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(movedDelta(movedNode("src/a.ts", 1)));
    const targets = [target("t-a", "src/a.ts", 1, 9)];
    render(<BriefArchitectureDelta load={load} onOpen={vi.fn()} story={{ targets, onOpenTarget: vi.fn() }} />);
    expect(await screen.findByTestId("brief-architecture-no-graph")).toHaveTextContent("could not load");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: "Walk through the change" })).toBeInTheDocument();
    expect(screen.queryByTestId("brief-architecture-no-graph")).toBeNull();
  });

  it("stays quiet when the comparison ran and found nothing material, and says when a material graph lists no changed file", async () => {
    const quiet = { ...movedDelta(movedNode("src/a.ts", 1)), status: "degraded", summary: { ...materialDelta().summary, material: false } };
    const { container, unmount } = render(<BriefArchitectureDelta load={async () => quiet} onOpen={vi.fn()} />);
    await act(async () => {});
    expect(container).toBeEmptyDOMElement();
    unmount();

    const none = movedDelta();
    render(<BriefArchitectureDelta load={async () => none} onOpen={vi.fn()} story={{ targets: [], onOpenTarget: vi.fn() }} />);
    expect(await screen.findByTestId("brief-architecture-no-graph")).toHaveTextContent("lists no changed file");
    expect(screen.queryByRole("button", { name: "Walk through the change" })).toBeNull();
  });

  it("closes an open walkthrough when the Reader moves to another Snapshot, says so, and does not reopen it", async () => {
    const first = movedDelta(movedNode("src/a.ts", 1));
    const second = movedDelta(movedNode("src/b.ts", 1));
    const story = { targets: [] as ReviewTarget[], onOpenTarget: vi.fn() };
    const { rerender } = render(<BriefArchitectureDelta load={async () => first} onOpen={vi.fn()} story={story} />);
    await userEvent.click(await screen.findByRole("button", { name: "Walk through the change" }));
    expect(await screen.findByTestId("change-story")).toHaveTextContent("a.ts");

    rerender(<BriefArchitectureDelta load={async () => second} onOpen={vi.fn()} story={story} />);
    expect(await screen.findByTestId("brief-story-closed")).toHaveTextContent("walkthrough of the previous one was closed");
    await act(async () => {});
    expect(screen.queryByTestId("change-story")).toBeNull();

    // Walking again is the new Snapshot's own path, and the notice is gone.
    await userEvent.click(screen.getByRole("button", { name: "Walk through the change" }));
    expect(await screen.findByTestId("change-story")).toHaveTextContent("b.ts");
    expect(screen.getByTestId("change-story")).not.toHaveTextContent("a.ts");
    expect(screen.queryByTestId("brief-story-closed")).toBeNull();
  });

  it("labels the open walkthrough as the older path once a newer Snapshot exists, without closing it", async () => {
    const delta = movedDelta(movedNode("src/a.ts", 1));
    const load = async () => delta;
    const story = { targets: [] as ReviewTarget[], onOpenTarget: vi.fn(), superseded: false, snapshot: 3 };
    const { rerender } = render(<BriefArchitectureDelta load={load} onOpen={vi.fn()} story={story} />);
    await userEvent.click(await screen.findByRole("button", { name: "Walk through the change" }));
    expect(await screen.findByTestId("change-story")).toBeInTheDocument();
    expect(screen.queryByTestId("change-story-stale")).toBeNull();
    // The Reader is told a newer Snapshot exists; its own Snapshot (and so its loader) is unchanged.
    rerender(<BriefArchitectureDelta load={load} onOpen={vi.fn()} story={{ ...story, superseded: true }} />);
    const stale = await screen.findByTestId("change-story-stale");
    expect(stale).toHaveTextContent("A newer Snapshot exists. This walkthrough shows Snapshot 3, which is no longer the current path.");
    expect(screen.getByTestId("change-story")).toHaveTextContent("a.ts");
  });

  it("opens the target holding the step's line, else the file's nearest target, else Snapshot source at that line", async () => {
    const targets = [target("t-top", "src/a.ts", 4, 19, [[7, 12]]), target("t-low", "src/a.ts", 35, 50, [[38, 43]])];
    const open = async (line: number, tgts: ReviewTarget[], onOpenLocation?: ReturnType<typeof vi.fn>) => {
      const onOpenTarget = vi.fn();
      const moved = movedDelta({ ...movedNode("src/a.ts", line) });
      const view = render(<BriefArchitectureDelta load={async () => moved} onOpen={vi.fn()} story={{ targets: tgts, onOpenTarget, onOpenLocation }} />);
      await userEvent.click(await screen.findByRole("button", { name: "Walk through the change" }));
      await userEvent.click(await screen.findByRole("button", { name: "Open source" }));
      view.unmount();
      return onOpenTarget;
    };
    // Inside the second symbol, on its signature line (not a changed line).
    expect(await open(36, targets)).toHaveBeenCalledWith("t-low");
    // Between the two: the nearer one by distance (28 is 9 past the first, 7 before the second).
    expect(await open(28, targets)).toHaveBeenCalledWith("t-low");
    expect(await open(22, targets)).toHaveBeenCalledWith("t-top");
    // A file with no review target is outside the patch: the Snapshot source at that line.
    const onOpenLocation = vi.fn();
    const noTarget = await open(7, [], onOpenLocation);
    expect(noTarget).not.toHaveBeenCalled();
    expect(onOpenLocation).toHaveBeenCalledWith({ path: "src/a.ts", line: 7, endLine: 7 });
  });
});
