import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchHistoricalReviewMap } from "../reviewApi";
import type { ReviewDataPathReport, ReviewMapPayload, ReviewOverview, ReviewTarget } from "../types";
import { ReviewFlowMapScreen } from "./ReviewFlowMapScreen";

const payload = vi.hoisted(() => ({ current: null as unknown }));
const dataPath = vi.hoisted(() => ({ gate: vi.fn(), run: vi.fn() }));
vi.mock("../reviewApi", () => ({
  fetchHistoricalReviewMap: vi.fn(async () => payload.current),
  fetchReviewProjectContext: vi.fn(async () => ({ project_context: { available: false, entries: [], narration: [] } })),
  reviewHref: (id: string) => `/r/${id}`,
}));
vi.mock("../usePreviewCount", () => ({ usePreviewCount: () => 0 }));
vi.mock("./dataPathApi", () => ({ fetchDataPathGate: dataPath.gate, postDataPathRun: dataPath.run }));

function part(index: number, dir: string): ReviewTarget {
  const name = `fn_${dir}_${String(index).padStart(3, "0")}`;
  return {
    target_id: name, unit_key: `sym:${name}`, kind: "symbol", path: `src/${dir}/f${index}.py`, label: name, symbol: name,
    start_line: 1, end_line: 3, hunk_ordinals: [0], spans: [], state: "unreviewed", changed_since_mark: false, reviewed_revision_id: "",
    attention_rank: index, attention_level: "normal", reasons: [], additions: 1, deletions: 1, fingerprint_method: "ast",
    verification: { pass: 0, fail: 0, unknown: 1 }, annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    answer: { category: "decision", rule: "", rule_label: "", reason: "" },
    priority: { version: 1, tier: 3, attention: "routine", outstanding: true, position: index, reasons: [] },
  } as ReviewTarget;
}

const OVERVIEW = { title: "Large change", session: { id: "canonical-review-id", ref: "shop" }, revision: { id: "rev1", revision_number: 1 } } as ReviewOverview;
const BIG = Array.from({ length: 150 }, (_, index) => part(index, "big"));
const SMALL = Array.from({ length: 5 }, (_, index) => part(index, "small"));

function node(path: string, module: string) {
  return {
    id: path, label: path, kind: "file", path, module, line: 1, end_line: 3, status: "changed", exactness: "snapshot", delta_status: "changed",
    target_ids: [], important_public_contract: false, boundaries: [], boundary_basis: "",
    selection: { kind: "review_map_node", review_id: "r", revision_id: "rev1", node_id: path, exactness: "snapshot" },
  };
}

async function moduleRow(name: string): Promise<HTMLElement> {
  const rows = await screen.findAllByTestId("map-list-row", {}, { timeout: 8000 });
  const row = rows.find((item) => item.textContent?.includes(name));
  if (!row) throw new Error(`no module row for ${name}: ${rows.map((item) => item.textContent).join(" | ")}`);
  return (row.querySelector("button") ?? row) as HTMLElement;
}

beforeEach(() => {
  window.localStorage.clear();
  payload.current = {
    review_id: "r", revision_id: "rev1", revision_number: 1, level: "architecture", projection: "architecture_delta",
    architecture_delta: { status: "ready" },
    graph: { nodes: [node("src/big/f0.py", "src/big"), node("src/small/f0.py", "src/small")], edges: [], level: "architecture", focus: null, truncated: true, omitted_node_count: 150 },
    context: { source: "current_index", exact_for_snapshot: false, snapshot_head_matches_checkout: false, index: {}, note: "" },
    snapshot: { exactness: "snapshot", tree_fingerprint: "t", source_fingerprint: "s", head_sha: "head", base_sha: "base", degraded: [],
      provenance: { host: "test", model: "", certainty: "exact", method: "" }, verification: { pass: 0, fail: 0, not_run: 0, unknown: 0, checks: [] } },
  } as unknown as ReviewMapPayload;
});
afterEach(cleanup);

describe("Map module rows on a large change", () => {
  it("lists at most 100 files of an opened module and says how many more", async () => {
    render(
      <MemoryRouter initialEntries={["/r/shop/map"]}>
        <ReviewFlowMapScreen reviewRef="shop" apiReviewId="review-1" overview={OVERVIEW} targets={[...BIG, ...SMALL]} lens="dependencies" view="changes" />
      </MemoryRouter>,
    );
    const row = await moduleRow("src/big");
    fireEvent.click(row);
    const files = await screen.findAllByTestId("map-module-file");
    expect(files.length).toBe(100);
    expect(screen.getByTestId("map-module-more")).toHaveTextContent("50 more files in this module");
    expect(within(screen.getByTestId("map-module-files")).getAllByRole("listitem").length).toBe(101);
  });

  it("lists a small module whole, with no more line", async () => {
    render(
      <MemoryRouter initialEntries={["/r/shop/map"]}>
        <ReviewFlowMapScreen reviewRef="shop" apiReviewId="review-1" overview={OVERVIEW} targets={[...BIG, ...SMALL]} lens="dependencies" view="changes" />
      </MemoryRouter>,
    );
    fireEvent.click(await moduleRow("src/small"));
    await waitFor(() => expect(screen.getAllByTestId("map-module-file").length).toBe(5));
    expect(screen.queryByTestId("map-module-more")).toBeNull();
  });
});

describe("the bigger-change example (review-12.2)", () => {
  const show = (overview: ReviewOverview) => render(
    <MemoryRouter initialEntries={["/r/shop/map"]}>
      <ReviewFlowMapScreen reviewRef="shop" apiReviewId="review-1" overview={overview} targets={SMALL} lens="dependencies" view="changes" />
    </MemoryRouter>,
  );

  it("is not offered on a real review", async () => {
    show(OVERVIEW);
    await screen.findAllByTestId("map-list-row", {}, { timeout: 8000 });
    expect(screen.queryByTestId("map-bigger-example")).toBeNull();
    expect(screen.queryByText("See a bigger-change example")).toBeNull();
  });

  it("is offered on the demo's review, and says once that it is an illustration", async () => {
    show({ ...OVERVIEW, example_review: true });
    fireEvent.click(await screen.findByTestId("map-bigger-example"));
    expect(screen.getByTestId("map-example-warning")).toHaveTextContent(/^Illustrative example only\./);
    expect(screen.queryByText(/This is an illustration/)).toBeNull();
    expect(screen.getByTestId("map-bigger-example")).toHaveTextContent("Back to this change");
  });
});

const DISCLOSURE = "Experimental: shows only the flows Opengrep's taint analysis found with LemonCrow's rules.";
const NOT_RUN: ReviewDataPathReport = {
  schema: 1, entry: "data_path.diff", state: "not_run", experimental: true, summary: "Not run: the data path runs only when requested", disclosure: DISCLOSURE,
};
const SETUP = "Download https://example.test/opengrep, check that its SHA-256 is abc, make it executable, and set LEMONCROW_DATA_PATH_OPENGREP_BIN to its path (or put it on PATH as opengrep). A review never downloads it.";
const NOT_AVAILABLE: ReviewDataPathReport = {
  ...NOT_RUN, state: "not_available", summary: "Data path not available: opengrep is not on PATH", setup: SETUP,
  not_measured: [{ scope: "all flows", reason: "engine unavailable: opengrep is not on PATH" }],
};
const RAN: ReviewDataPathReport = {
  ...NOT_RUN, state: "ran", summary: "1 added · 0 removed · 0 rerouted",
  counts: { added: 1, removed: 0, rerouted: 0, unchanged: 2 },
  flows: { added: [{
    change: "added", kind: "db", framework: "django", entry_point: "GET /search",
    source: { symbol: "search", at: "shop/views.py:12", label: "request.GET" },
    sink: { symbol: "run_query", at: "shop/services.py:8", callee: "cursor.execute" },
    found_by: "Opengrep v2.0.0 taint, depth 4",
    head: { path: ["search", "run_query"], steps: [], omitted_steps: 0 },
  }], removed: [], rerouted: [] },
  omitted: { added: 0, removed: 0, rerouted: 0 },
  not_measured: [],
};
const withReport = (report: ReviewDataPathReport) => ({ ...(payload.current as ReviewMapPayload), data_path: report });
const renderMap = (canRun: boolean) => render(
  <MemoryRouter initialEntries={["/r/shop/map?lens=data-path"]}>
    <ReviewFlowMapScreen reviewRef="shop" apiReviewId="review-1" overview={OVERVIEW} targets={SMALL} lens="data-path" view="changes" canRun={canRun} />
  </MemoryRouter>,
);

describe("P10-7 Data path on the Map", () => {
  beforeEach(() => {
    dataPath.gate.mockReset().mockResolvedValue({ runnable: true, reason: "" });
    dataPath.run.mockReset();
    vi.mocked(fetchHistoricalReviewMap).mockClear();
  });

  it("offers Run data path where the run plan runs it, shows progress, then reloads the map and turns the lens on", async () => {
    payload.current = withReport(NOT_RUN);
    let finish = () => {};
    dataPath.run.mockImplementation(() => new Promise((resolve) => { finish = () => { payload.current = withReport(RAN); resolve(RAN); }; }));
    renderMap(true);
    expect(await screen.findByTestId("map-data-path-status")).toHaveTextContent("Data path has not run for this revision.");
    expect(screen.getByRole("button", { name: "Data path" })).toHaveAttribute("aria-disabled", "true");
    expect(await screen.findByTestId("map-data-path-note")).toHaveTextContent("The Data path lens needs this run first.");
    fireEvent.click(await screen.findByRole("button", { name: "Run data path" }));
    expect(dataPath.gate).toHaveBeenCalledWith("review-1", "rev1");
    expect(dataPath.run).toHaveBeenCalledWith("review-1", "rev1");
    expect(screen.getByTestId("map-data-path-status")).toHaveTextContent(/^Running the data path with Opengrep on this machine/);
    expect(screen.getByTestId("map-live")).toHaveTextContent(/^Running the data path/);
    expect(screen.getByTestId("map-data-path-run-button")).toBeDisabled();
    const reads = vi.mocked(fetchHistoricalReviewMap).mock.calls.length;
    finish();
    const flows = await screen.findByTestId("map-data-path-flows");
    expect(vi.mocked(fetchHistoricalReviewMap).mock.calls.length).toBeGreaterThan(reads);
    expect(within(flows).getByTestId("map-data-path-summary")).toHaveTextContent("1 added · 0 removed · 0 rerouted");
    expect(within(flows).getByTestId("map-data-path-flow")).toHaveTextContent("search → run_query");
    expect(within(flows).getByTestId("map-data-path-flow")).toHaveTextContent("request.GET at shop/views.py:12 → cursor.execute at shop/services.py:8");
    expect(screen.queryByTestId("map-data-path-run")).toBeNull();
    expect(screen.getByRole("button", { name: "Data path" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("map-provenance-note")).toHaveTextContent(DISCLOSURE);
    expect(screen.getByTestId("map-live")).toHaveTextContent("Map · Data path · 1 added · 0 removed · 0 rerouted");
  });

  it("shows a not_available report's stored setup line after a run without the pinned binary", async () => {
    payload.current = withReport(NOT_RUN);
    dataPath.run.mockImplementation(async () => { payload.current = withReport(NOT_AVAILABLE); return NOT_AVAILABLE; });
    renderMap(true);
    fireEvent.click(await screen.findByRole("button", { name: "Run data path" }));
    // The visible line says what it is in plain words; the install steps stay folded under Setup details.
    await waitFor(() => expect(screen.getByTestId("map-data-path-status")).toHaveTextContent("Data path needs a one-time setup on this machine before it can run."));
    expect(screen.getByTestId("map-data-path-status")).not.toHaveTextContent(SETUP);
    expect(screen.getByTestId("map-data-path-setup")).toHaveTextContent(`Data path not available: opengrep is not on PATH. ${SETUP}`);
    // The install steps are only under Setup details; the note under the controls does not print them again.
    expect(screen.getByTestId("map-provenance-note")).not.toHaveTextContent(SETUP);
    expect(screen.getByTestId("map-provenance-note")).not.toHaveTextContent("LemonCrow records which files use which");
    expect(screen.getByTestId("map-live")).toHaveTextContent(`The data path finished. Data path not available: opengrep is not on PATH. ${SETUP}`);
    expect(screen.getByRole("button", { name: "Data path" })).toHaveAttribute("aria-disabled", "true");
    // Set up the binary, then run it again from the same place.
    expect(screen.getByRole("button", { name: "Run data path" })).toBeEnabled();
  });

  it("says a failed run in plain words and offers it again", async () => {
    payload.current = withReport(NOT_RUN);
    dataPath.run.mockRejectedValue(new Error("data_path.diff is not in the effective run plan: planned as skipped"));
    renderMap(true);
    fireEvent.click(await screen.findByRole("button", { name: "Run data path" }));
    const failure = "The data path did not run: data_path.diff is not in the effective run plan: planned as skipped. Your review is unchanged.";
    await waitFor(() => expect(screen.getByTestId("map-data-path-status")).toHaveTextContent(failure));
    expect(screen.getByTestId("map-live")).toHaveTextContent(failure);
    expect(screen.getByRole("button", { name: "Run data path" })).toBeEnabled();
    expect(screen.queryByTestId("map-data-path-flows")).toBeNull();
  });

  it("offers no run where the run plan does not run it locally, and says why", async () => {
    payload.current = withReport(NOT_RUN);
    const reason = "Data path difference (experimental) runs only where the revision is held; this deployment places it on the worker executor, which does not run it";
    dataPath.gate.mockResolvedValue({ runnable: false, reason });
    renderMap(true);
    expect(await screen.findByTestId("map-data-path-note")).toHaveTextContent("It cannot run on this machine yet.");
    expect(screen.getByTestId("map-data-path-setup")).toHaveTextContent(reason);
    expect(screen.queryByRole("button", { name: "Run data path" })).toBeNull();
  });

  it("offers no run on a read-only or historical view and never asks the run plan", async () => {
    payload.current = withReport(NOT_AVAILABLE);
    renderMap(false);
    expect(await screen.findByTestId("map-data-path-note")).toHaveTextContent("This view is read-only, so the data path cannot be run from here.");
    expect(screen.getByTestId("map-data-path-setup")).toHaveTextContent(SETUP);
    expect(screen.queryByRole("button", { name: "Run data path" })).toBeNull();
    expect(dataPath.gate).not.toHaveBeenCalled();
  });

  it("keeps the generic reason and no run on a server whose map has no data path report", async () => {
    renderMap(true);
    await screen.findAllByTestId("map-list-row");
    expect(screen.getByTestId("map-provenance-note")).toHaveTextContent("LemonCrow records which files use which");
    expect(screen.queryByTestId("map-data-path-run")).toBeNull();
    expect(dataPath.gate).not.toHaveBeenCalled();
  });
});

describe("the Map does not wait for the comparison", () => {
  const Where = () => <output data-testid="where">{useLocation().search}</output>;
  const staged = () => {
    const plain = { ...(payload.current as ReviewMapPayload), projection: "change_map", architecture_delta: undefined } as unknown as ReviewMapPayload;
    const compared = { ...(payload.current as ReviewMapPayload), graph: { ...(payload.current as ReviewMapPayload).graph, truncated: false } } as ReviewMapPayload;
    let settle: { resolve: (value: ReviewMapPayload) => void; reject: (error: Error) => void } = { resolve: () => {}, reject: () => {} };
    const comparison = new Promise<ReviewMapPayload>((resolve, reject) => { settle = { resolve, reject }; });
    vi.mocked(fetchHistoricalReviewMap).mockImplementation((_id, _number, _level, _node, projection) => projection === "architecture_delta" ? comparison : Promise.resolve(plain));
    render(
      <MemoryRouter initialEntries={["/r/shop/map?lens=dependencies&view=before"]}>
        <ReviewFlowMapScreen reviewRef="shop" apiReviewId="review-1" overview={OVERVIEW} targets={SMALL} lens="dependencies" view="before" />
        <Where />
      </MemoryRouter>,
    );
    return { compared, settle };
  };
  afterEach(() => { vi.mocked(fetchHistoricalReviewMap).mockReset().mockImplementation(async () => payload.current as ReviewMapPayload); });

  it("does not call the data path unavailable before the map has answered", async () => {
    vi.mocked(fetchHistoricalReviewMap).mockImplementation(() => new Promise(() => {}));
    render(
      <MemoryRouter initialEntries={["/r/shop/map"]}>
        <ReviewFlowMapScreen reviewRef="shop" apiReviewId="review-1" overview={OVERVIEW} targets={SMALL} lens="data-path" view="changes" />
      </MemoryRouter>,
    );
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByTestId("map-provenance-note")?.textContent ?? "").not.toMatch(/not available here/);
  });

  it("shows the plain map at once, keeps a Before link, and upgrades when the comparison lands", async () => {
    const { compared, settle } = staged();
    expect(await screen.findByTestId("map-provenance-note")).toHaveTextContent(/while the comparison with the base finishes/);
    expect((await screen.findAllByTestId("map-list-row")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Loading the captured code map…")).toBeNull();
    expect(screen.getByRole("button", { name: "Before" })).toBeDisabled();
    expect(screen.getByTestId("where")).toHaveTextContent("view=before");
    settle.resolve(compared);
    await waitFor(() => expect(screen.getByRole("button", { name: "Before" })).toBeEnabled());
    expect(screen.getByTestId("map-provenance-note")).not.toHaveTextContent(/comparison with the base finishes/);
    expect(screen.getByTestId("where")).toHaveTextContent("view=before");
  });

  it("stays on the plain map, without claiming a comparison, when the comparison fails", async () => {
    const { settle } = staged();
    await screen.findByTestId("map-provenance-note");
    settle.reject(new Error("timed out"));
    await waitFor(() => expect(screen.getByTestId("map-provenance-note")).toHaveTextContent(/Before and After require a verified comparison/));
    expect(screen.queryByText(/could not load/)).toBeNull();
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("view=changes"));
  });
});

describe("the Map while the code index still builds", () => {
  const withGraph = (state: string) => {
    const base = payload.current as ReviewMapPayload;
    return { ...base, context: { ...base.context, readiness: { graph: { state } } } } as ReviewMapPayload;
  };
  const calls = () => vi.mocked(fetchHistoricalReviewMap).mock.calls.length;
  const flush = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
  const mount = async () => {
    render(
      <MemoryRouter initialEntries={["/r/shop/map"]}>
        <ReviewFlowMapScreen reviewRef="shop" apiReviewId="review-1" overview={OVERVIEW} targets={SMALL} lens="dependencies" view="changes" />
      </MemoryRouter>,
    );
    await flush(0);
  };
  const DELAYS = [500, 1000, 2000, 4000, 5000, 5000, 5000, 5000, 5000, 5000, 5000, 5000, 5000, 5000];
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    payload.current = withGraph("building");
    vi.mocked(fetchHistoricalReviewMap).mockClear();
  });
  afterEach(() => { vi.useRealTimers(); vi.mocked(fetchHistoricalReviewMap).mockReset().mockImplementation(async () => payload.current as ReviewMapPayload); });

  it("says so, reads again with backoff 14 times, then stops and offers Check again", async () => {
    await mount();
    expect(screen.getByTestId("map-indexing-note")).toHaveTextContent("Still indexing the surrounding code, so some connections may be missing. Checking again…");
    const start = calls();
    for (const [index, delay] of DELAYS.entries()) {
      await flush(delay - 1);
      expect(calls(), `refresh ${index + 1} waits ${delay} ms`).toBe(start + index);
      await flush(1);
      expect(calls(), `refresh ${index + 1}`).toBe(start + index + 1);
    }
    // The same comparison read the screen holds.
    expect(fetchHistoricalReviewMap).toHaveBeenLastCalledWith("review-1", 1, "architecture", "", "architecture_delta");
    // Past the bound: no 15th read, however long the screen stays open.
    await flush(10 * 60_000);
    expect(calls()).toBe(start + DELAYS.length);
    const note = screen.getByTestId("map-indexing-note");
    expect(note).toHaveAttribute("data-stopped", "true");
    expect(note).not.toHaveTextContent("Checking again");
    // Check again starts one more bounded round from the first wait.
    fireEvent.click(within(note).getByRole("button", { name: "Check again" }));
    await flush(500);
    expect(calls()).toBe(start + DELAYS.length + 1);
    expect(screen.getByTestId("map-indexing-note")).toHaveTextContent("Checking again…");
  });

  it("keeps polling through failed reads, still bounded", async () => {
    await mount();
    const start = calls();
    vi.mocked(fetchHistoricalReviewMap).mockImplementation(async () => { throw new Error("offline"); });
    // One read settles per act scope, so the screen schedules the next wait after it.
    for (const delay of DELAYS) await flush(delay);
    expect(calls()).toBe(start + DELAYS.length);
    await flush(10 * 60_000);
    expect(calls()).toBe(start + DELAYS.length);
    expect(screen.queryByText(/could not load/)).toBeNull();
    expect(screen.getByTestId("map-indexing-note")).toHaveAttribute("data-stopped", "true");
  });

  it("stops and drops the note once the index is ready", async () => {
    await mount();
    const start = calls();
    payload.current = withGraph("ready");
    await flush(500);
    expect(calls()).toBe(start + 1);
    expect(screen.queryByTestId("map-indexing-note")).toBeNull();
    await flush(10 * 60_000);
    expect(calls()).toBe(start + 1);
  });

  it("shows no note and never polls when the graph is ready or degraded", async () => {
    for (const state of ["ready", "degraded"]) {
      payload.current = withGraph(state);
      vi.mocked(fetchHistoricalReviewMap).mockClear();
      await mount();
      const start = calls();
      expect(screen.queryByTestId("map-indexing-note")).toBeNull();
      await flush(60_000);
      expect(calls(), state).toBe(start);
      cleanup();
    }
  });
});
