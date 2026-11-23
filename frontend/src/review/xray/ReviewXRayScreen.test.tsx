import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { INVESTIGATION_MAP_FIXTURES } from "../investigation/mapFixtures";
import type { ReviewArchitectureDelta, ReviewBehaviorSummary, ReviewOverview, ReviewTarget } from "../types";
import { MINIMAL_CHANGE, PRICING_CHANGE } from "./graph/fixtures";
import type { XRayGraphState } from "./graph/xrayGraphApi";
import ReviewXRayScreen, { anchorSelection, defaultXRayLocation, type ReviewXRayScreenProps } from "./ReviewXRayScreen";
import { encodeXRayLocation, graphSnapshot } from "./xrayModel";

const { graph, flow } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
const delta = { graph, flow } as unknown as ReviewArchitectureDelta;
const retry = graph.nodes.find((node) => node.id === "retry")!;
const overview = { title: "Retry payments", session: { id: "r1", title: "Retry payments" }, revision: { id: "rev-pay-1", revision_number: 1 } } as unknown as ReviewOverview;
const part = {
  target_id: "t-retry", path: retry.path, symbol: "retry", label: "retry", start_line: 1, end_line: 900, state: "unreviewed",
  verification: { pass: 0, fail: 0, unknown: 0 }, answer: { category: "decision" }, annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
} as unknown as ReviewTarget;
const at = encodeXRayLocation({ side: "after", snapshot: graphSnapshot(graph), rootId: "retry" });

function behavior(paths: string[]): ReviewBehaviorSummary {
  return {
    total: 1, unchanged: 0, degraded: [], progress: { reviewed: 0, critical: 0 }, narrative: null,
    targets: [{ behavior_key: "b1", kind_label: "API", title: "POST /pay", summary: "200 to 500", state: "unreviewed", critical: false, executor_class: "", fingerprint: "f", environment_fingerprint: "", paths, unverified_integrations: [] }],
  } as unknown as ReviewBehaviorSummary;
}

function Where() {
  const location = useLocation();
  return <output data-testid="where">{`${location.pathname}${location.search}`}</output>;
}

// The file picture tests run on a server without the X-Ray graph endpoint. Module-level, so the graph read does not
// restart on every address change.
const noGraph = async (): Promise<XRayGraphState> => ({ state: "unavailable", reason: "This server does not draw X-Ray graphs yet.", missing: true });
const ready = (graph: XRayGraphState["graph"]) => async (): Promise<XRayGraphState> => ({ state: "ready", graph });

function Routed(props: Partial<ReviewXRayScreenProps>) {
  const location = useLocation();
  if (!location.pathname.endsWith("/xray")) return null;
  return <ReviewXRayScreen reviewRef="shop" apiReviewId="r1" at={new URLSearchParams(location.search).get("at") ?? ""} overview={overview} targets={[part]} load={async () => delta} loadGraph={noGraph} {...props} />;
}

function show(props: Partial<ReviewXRayScreenProps> = {}, entry = `/r/shop/xray?at=${encodeURIComponent(at)}`) {
  return render(<MemoryRouter initialEntries={[entry]}><Routed {...props} /><Where /></MemoryRouter>);
}

afterEach(cleanup);

describe("ReviewXRayScreen", () => {
  it("walks the node in the address and keeps the side in the address", async () => {
    show();
    expect(await screen.findByTestId("xray")).toHaveAttribute("data-side", "after");
    await userEvent.click(screen.getByRole("radio", { name: "Before" }));
    await waitFor(() => expect(screen.getByTestId("xray")).toHaveAttribute("data-side", "before"));
    expect(decodeURIComponent(screen.getByTestId("where").textContent ?? "")).toContain("at=before~");
  });

  it("starts a bare address on the whole change, without a history entry to bounce on", async () => {
    show({}, "/r/shop/xray");
    expect(await screen.findByTestId("xray")).toHaveAttribute("data-mode", "change");
    expect(decodeURIComponent(screen.getByTestId("where").textContent ?? "")).toMatch(/at=after~review-pay@rev-pay-1~$/);
    expect(defaultXRayLocation(graph)).toEqual({ side: "after", snapshot: "review-pay@rev-pay-1", rootId: "" });
  });

  it("zooms into a file's path from the whole change and back", async () => {
    show({}, "/r/shop/xray");
    await screen.findByTestId("xray");
    const retryCard = screen.getAllByTestId("xray-card").find((item) => item.getAttribute("data-card-id") === "retry")!;
    await userEvent.click(retryCard);
    await waitFor(() => expect(screen.getByTestId("xray")).toHaveAttribute("data-mode", "focus"));
    expect(decodeURIComponent(screen.getByTestId("where").textContent ?? "")).toContain("~retry");
    await userEvent.click(screen.getByTestId("xray-whole-change"));
    await waitFor(() => expect(screen.getByTestId("xray")).toHaveAttribute("data-mode", "change"));
  });

  it("sends a change with no recorded run to the Map's Data path", async () => {
    const auth = INVESTIGATION_MAP_FIXTURES.authRefreshMap();
    show({ load: async () => ({ graph: auth.graph, flow: auth.flow }) as unknown as ReviewArchitectureDelta }, "/r/shop/xray");
    await userEvent.click(await screen.findByTestId("xray-caveat-action"));
    expect(screen.getByTestId("where")).toHaveTextContent("/r/shop/map?lens=data-path&view=changes");
  });

  it("opens a step's source on its part", async () => {
    const exact = { ...graph, nodes: graph.nodes.map((node) => (node.id === "retry" ? { ...node, exactness: "snapshot" as const } : node)) };
    show({ load: async () => ({ graph: exact, flow }) as unknown as ReviewArchitectureDelta });
    await screen.findByTestId("xray");
    const open = within(screen.getByTestId("xray-detail")).getByRole("button", { name: "Open source" });
    await userEvent.click(open);
    expect(screen.getByTestId("where")).toHaveTextContent("/r/shop/one/retry");
  });

  const arrow = (from: string, to: string) => screen.getAllByTestId("xray-edge").find((item) => (item.getAttribute("data-edge-key") ?? "").endsWith(`:${from}\u0000${to}`))!;

  it("opens the recorded run behind a traced arrow on the checks of the part it reached", async () => {
    const keyPart = { ...part, target_id: "t-key", path: "src/payments/idempotency.py", symbol: "idempotency_key", label: "idempotency_key" } as ReviewTarget;
    show({ targets: [part, keyPart] }, "/r/shop/xray");
    await screen.findByTestId("xray");
    await userEvent.click(arrow("retry", "key"));
    await userEvent.click(within(screen.getByTestId("xray-edge-detail")).getByRole("button", { name: "See the recorded run" }));
    expect(screen.getByTestId("where")).toHaveTextContent("/r/shop/one/idempotency_key");
  });

  it("opens the Brief's recorded runs when the traced file has no part", async () => {
    show({}, "/r/shop/xray");
    await screen.findByTestId("xray");
    await userEvent.click(arrow("retry", "key"));
    await userEvent.click(within(screen.getByTestId("xray-edge-detail")).getByTestId("xray-detail-evidence"));
    expect(screen.getByTestId("where")).toHaveTextContent("/r/shop/brief");
  });

  it("lists only the behavior changes on the walked files, and never reads silence as safety", async () => {
    const { unmount } = show({ behavior: behavior([retry.path]) });
    const onPath = await screen.findByTestId("xray-behavior");
    expect(onPath).toHaveAttribute("data-count", "1");
    expect(within(onPath).getByText("API · POST /pay")).toBeInTheDocument();
    unmount();
    const other = show({ behavior: behavior(["src/unrelated.py"]) });
    expect(await screen.findByTestId("xray-behavior-none")).toHaveTextContent("not proof");
    // No "· 0" on an empty list; the sentence says why. The top bar's Back is the only exit.
    expect(screen.getByTestId("xray-behavior")).toHaveTextContent(/Behavior changes on this file(?! ·)/);
    expect(within(screen.getByTestId("xray-behavior")).queryByText(/· 0/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Close the X-Ray" })).toBeNull();
    expect(within(screen.getByTestId("xray-detail")).queryByText("Exact for this revision")).toBeNull();
    other.unmount();
    show({ behavior: null });
    expect(await screen.findByTestId("xray-behavior-none")).toHaveTextContent("was not observed");
  });

  it("says why when there is no Map, and retries a failed read", async () => {
    const { unmount } = show({ load: undefined });
    expect(await screen.findByText(/no Map to walk/)).toBeInTheDocument();
    unmount();
    const load = vi.fn<() => Promise<ReviewArchitectureDelta | null>>().mockRejectedValueOnce(new Error("no")).mockResolvedValue(delta);
    show({ load });
    await userEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("xray")).toBeInTheDocument();
  });

  it("says when the Map has no changed file", async () => {
    show({ load: async () => ({ graph: { ...graph, nodes: [], edges: [] }, flow: null }) as unknown as ReviewArchitectureDelta }, "/r/shop/xray");
    expect(await screen.findByTestId("xray-note")).toHaveTextContent("lists no changed file");
  });
});

describe("ReviewXRayScreen with an X-Ray graph", () => {
  const plansBehavior = {
    total: 1, unchanged: 0, degraded: [], progress: { reviewed: 0, critical: 0 }, narrative: null,
    targets: [{ behavior_key: "api:GET /api/plans", kind_label: "API", title: "GET /api/plans", summary: "Team 49 to 59", state: "unreviewed", critical: false, executor_class: "", fingerprint: "f", environment_fingerprint: "", paths: [], unverified_integrations: [] }],
  } as unknown as ReviewBehaviorSummary;

  it("draws the graph instead of the file picture when the server has one", async () => {
    const load = vi.fn(async () => delta);
    show({ loadGraph: ready(PRICING_CHANGE), load }, "/r/shop/xray");
    expect(await screen.findByTestId("xg-root")).toBeInTheDocument();
    expect(screen.queryByTestId("xray")).toBeNull();
    expect(load).not.toHaveBeenCalled();
  });

  it("falls back to the file picture on a 404, and while the graph is drawn shows that it is drawing", async () => {
    show({}, "/r/shop/xray");
    expect(await screen.findByTestId("xray")).toBeInTheDocument();
    cleanup();
    let settle: (state: XRayGraphState) => void = () => {};
    const pending = (onUpdate: (state: XRayGraphState) => void) => {
      onUpdate({ state: "pending" });
      return new Promise<XRayGraphState>((resolve) => { settle = resolve; });
    };
    show({ loadGraph: pending }, "/r/shop/xray");
    expect(await screen.findByTestId("xray-graph-note")).toHaveTextContent("Drawing the X-Ray");
    settle({ state: "ready", graph: PRICING_CHANGE });
    expect(await screen.findByTestId("xg-root")).toBeInTheDocument();
  });

  it("shows the index drawing while AI is still adding to it", async () => {
    const pending = (onUpdate: (state: XRayGraphState) => void) => {
      onUpdate({ state: "pending", graph: MINIMAL_CHANGE });
      return new Promise<XRayGraphState>(() => {});
    };
    show({ loadGraph: pending }, "/r/shop/xray");
    expect(await screen.findByTestId("xg-caveat")).toHaveTextContent(/AI is still adding/);
    expect(screen.queryByTestId("xg-draw-ai")).toBeNull();
  });

  it("lists a payload's behavior change by its key, and opens an anchor on the Reader", async () => {
    show({ loadGraph: ready(PRICING_CHANGE), behavior: plansBehavior }, "/r/shop/xray");
    await screen.findByTestId("xg-root");
    await userEvent.click(document.querySelector('[data-testid="xg-edge"][data-edge-id="e_pricing_plans"]')!);
    expect(within(screen.getByTestId("xg-behavior")).getByText(/GET \/api\/plans/)).toBeInTheDocument();
    await userEvent.click(within(screen.getByTestId("xg-anchors")).getAllByRole("button")[0]);
    expect(screen.getByTestId("where")).toHaveTextContent("/r/shop/map?lens=dependencies&view=changes");
    expect(anchorSelection({ path: "a.py", start_line: 3, end_line: 9, side: "base" }, "r1", "rev1")).toEqual({
      kind: "review_map_node", review_id: "r1", revision_id: "rev1", exactness: "snapshot", side: "old", path: "a.py", line: 3, end_line: 9,
    });
  });

  it("opens on the side and the card in the address, and keeps both there", async () => {
    show({ loadGraph: ready(PRICING_CHANGE) }, "/r/shop/xray?at=before~s1~api_plans");
    expect(await screen.findByTestId("xg-root")).toHaveAttribute("data-side", "before");
    expect(screen.getByTestId("xg-inspector-title")).toHaveTextContent("GET /api/plans");
    await userEvent.click(within(screen.getByRole("radiogroup", { name: "Side of the change" })).getByRole("radio", { name: "After" }));
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/r/shop/xray?at=after~s1~api_plans"));
    await userEvent.click(screen.getByRole("button", { name: "Close details" }));
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/r/shop/xray?at=after~s1~"));
  });

  it("places a Map node from a link on the card in its file, at its line", async () => {
    const map = { graph: { nodes: [{ id: "map:file:app", path: "examples/demo/backend/app.py", line: 35, end_line: 40 }], edges: [] } } as unknown as ReviewArchitectureDelta;
    const load = vi.fn(async () => map);
    show({ loadGraph: ready(PRICING_CHANGE), load }, "/r/shop/xray?at=after~s1~map:file:app");
    expect(await screen.findByTestId("xg-inspector-title")).toHaveTextContent("GET /api/settings");
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/r/shop/xray?at=after~s1~api_settings"));
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("a selected card lists the behavior changes on its file, and says when nothing observed them", async () => {
    const onFile = { ...plansBehavior, targets: [{ ...plansBehavior.targets[0], behavior_key: "api:GET /orders", title: "GET /orders", paths: ["examples/demo/backend/app.py"] }] } as unknown as ReviewBehaviorSummary;
    show({ loadGraph: ready(PRICING_CHANGE), behavior: onFile }, "/r/shop/xray?at=after~~api_settings");
    expect(await screen.findByTestId("xray-behavior")).toHaveTextContent("GET /orders");
    cleanup();
    show({ loadGraph: ready(PRICING_CHANGE) }, "/r/shop/xray?at=after~~api_settings");
    expect(await screen.findByTestId("xray-behavior-none")).toHaveTextContent("was not observed");
  });

  it("asks for an AI drawing and shows the reason when it is refused", async () => {
    const requestGraph = vi.fn(async () => ({ ok: false as const, reason: "Review AI is over budget this month." }));
    show({ loadGraph: ready(MINIMAL_CHANGE), requestGraph }, "/r/shop/xray");
    await userEvent.click(await screen.findByTestId("xg-draw-ai"));
    expect(requestGraph).toHaveBeenCalledWith(true);
    expect(await screen.findByTestId("xg-ai-reason")).toHaveTextContent("Review AI is over budget this month.");
  });

  it("reads the graph again after an accepted AI request", async () => {
    const loadGraph = vi.fn().mockResolvedValueOnce({ state: "ready", graph: MINIMAL_CHANGE }).mockResolvedValue({ state: "ready", graph: PRICING_CHANGE });
    const requestGraph = vi.fn(async () => ({ ok: true as const, state: { state: "pending" as const } }));
    show({ loadGraph, requestGraph }, "/r/shop/xray");
    await userEvent.click(await screen.findByTestId("xg-draw-ai"));
    await waitFor(() => expect(screen.getByRole("heading", { name: PRICING_CHANGE.title })).toBeInTheDocument());
    expect(loadGraph).toHaveBeenCalledTimes(2);
  });
});
