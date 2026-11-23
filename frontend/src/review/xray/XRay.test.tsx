import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { INVESTIGATION_MAP_FIXTURES } from "../investigation/mapFixtures";
import type { ReviewMapGraph } from "../types";
import XRay, { type XRayProps } from "./XRay";
import type { XRayFlow, XRaySide } from "./xrayModel";

const PAYMENT_FLOW = INVESTIGATION_MAP_FIXTURES.paymentRetryMap().flow;

function Harness({
  graph,
  flow,
  walkSnapshot,
  rootId = "",
  ...props
}: Partial<XRayProps> & { graph: ReviewMapGraph; flow?: XRayFlow | null; walkSnapshot: string; rootId?: string }) {
  const [side, setSide] = useState<XRaySide>("after");
  const [snapshot, setSnapshot] = useState(walkSnapshot);
  const [root, setRoot] = useState(rootId);
  return (
    <XRay
      graph={graph}
      flow={flow === undefined ? PAYMENT_FLOW : flow}
      rootId={root}
      side={side}
      walkSnapshot={snapshot}
      onSideChange={setSide}
      onRootChange={(next) => { props.onRootChange?.(next); setRoot(next); }}
      onRebuild={() => setSnapshot("review-pay@rev-pay-1")}
      onOpenSource={props.onOpenSource ?? (() => undefined)}
      onOpenEvidence={props.onOpenEvidence}
      onOpenDataPath={props.onOpenDataPath}
      renderBehavior={props.renderBehavior}
      behaviorCount={props.behaviorCount}
    />
  );
}

const card = (id: string) => screen.queryAllByTestId("xray-card").find((item) => item.getAttribute("data-card-id") === id) ?? null;
const edge = (from: string, to: string) => screen.queryAllByTestId("xray-edge").find((item) => (item.getAttribute("data-edge-key") ?? "").endsWith(`:${from}\u0000${to}`)) ?? null;

describe("XRay", () => {
  it("opens on the whole change and redraws the same picture for Before and After", async () => {
    const user = userEvent.setup();
    const { graph } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    render(<Harness graph={graph} walkSnapshot="review-pay@rev-pay-1" />);
    expect(screen.getByTestId("xray")).toHaveAttribute("data-mode", "change");
    expect(screen.getAllByTestId("xray-lane").map((lane) => lane.getAttribute("data-lane"))).toEqual(["api", "domain", "storage", "tests"]);
    expect(card("settle")).toHaveAttribute("data-tone", "added");
    expect(card("backoff")).toBeNull();
    expect(edge("retry", "key")).toHaveAttribute("data-line", "solid");
    expect(edge("retry", "key")).toHaveAttribute("data-sequence", "2");
    expect(edge("retry", "ledger")).toHaveAttribute("data-line", "dashed");
    expect(edge("retry", "ledger")).toHaveAttribute("data-tone", "changed");
    expect(screen.getAllByTestId("xray-caveat")).toHaveLength(1);
    expect(screen.getByTestId("xray-caveat")).toHaveTextContent("3 of 6 arrows observed in a recorded run");

    await user.click(screen.getByRole("radio", { name: "Before" }));
    expect(screen.getByTestId("xray")).toHaveAttribute("data-side", "before");
    expect(card("settle")).toBeNull();
    expect(card("backoff")).toHaveAttribute("data-tone", "removed");
    expect(edge("retry", "backoff")).toHaveAttribute("data-line", "solid");
    expect(edge("retry", "key")).toBeNull();
  });

  it("opens an arrow's caller, callee, code site, basis and the behavior on either end", async () => {
    const user = userEvent.setup();
    const onOpenSource = vi.fn();
    const onOpenEvidence = vi.fn();
    const renderBehavior = vi.fn((paths: string[]) => <p data-testid="behavior-stub">{paths.join(" + ")}</p>);
    const { graph } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    render(<Harness graph={graph} walkSnapshot="review-pay@rev-pay-1" onOpenSource={onOpenSource} onOpenEvidence={onOpenEvidence} renderBehavior={renderBehavior} />);
    await user.click(edge("retry", "key")!);
    const detail = screen.getByTestId("xray-edge-detail");
    expect(within(detail).getByTestId("xray-detail-trace")).toHaveTextContent("charge_with_retry() → idempotency_key()");
    expect(within(detail).getByTestId("xray-detail-order")).toHaveTextContent("Observed: call #2");
    expect(within(detail).getByTestId("behavior-stub")).toHaveTextContent("src/payments/retry.py + src/payments/idempotency.py");
    await user.click(within(detail).getByTestId("xray-detail-site"));
    expect(onOpenSource).toHaveBeenCalledWith(expect.objectContaining({ kind: "review_map_edge", edge_id: "retry->key", path: "src/payments/retry.py", line: 14 }));
    await user.click(within(detail).getByTestId("xray-detail-evidence"));
    expect(onOpenEvidence).toHaveBeenCalledWith(expect.objectContaining({ node_id: "key" }));

    await user.click(edge("retry", "ledger")!);
    const inferred = screen.getByTestId("xray-edge-detail");
    expect(within(inferred).getByTestId("xray-detail-order")).toHaveTextContent("Static: a relationship in the code, not observed");
    expect(inferred).toHaveTextContent("1 → 2 references");
    expect(within(inferred).queryByTestId("xray-detail-evidence")).toBeNull();
  });

  it("zooms into a file's path from its card and comes back to the whole change", async () => {
    const user = userEvent.setup();
    const onRootChange = vi.fn();
    const onOpenSource = vi.fn();
    const { graph } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    render(<Harness graph={graph} walkSnapshot="review-pay@rev-pay-1" onRootChange={onRootChange} onOpenSource={onOpenSource} />);
    // Before anything is chosen, the panel offers where the walkthrough starts.
    expect(screen.getByTestId("xray-detail")).toHaveTextContent("Start here");
    await user.click(card("retry")!);
    expect(onRootChange).toHaveBeenLastCalledWith("retry");
    expect(screen.getByTestId("xray")).toHaveAttribute("data-mode", "focus");
    expect(screen.getByTestId("xray-root-title")).toHaveTextContent("retry.py");
    expect(card("retry")).toHaveAttribute("data-root", "true");
    await user.click(card("ledger")!);
    const detail = screen.getByTestId("xray-detail");
    expect(detail).toHaveTextContent("ledger_store.py");
    expect(detail).toHaveTextContent("Reaches a storage boundary");
    await user.click(within(detail).getByRole("button", { name: "Open source" }));
    expect(onOpenSource).toHaveBeenCalledWith(expect.objectContaining({ node_id: "ledger", path: "src/payments/ledger_store.py" }));
    await user.click(screen.getByTestId("xray-whole-change"));
    expect(onRootChange).toHaveBeenLastCalledWith("");
    expect(screen.getByTestId("xray")).toHaveAttribute("data-mode", "change");
  });

  it("walks through observed calls first with W, one card and arrow at a time, and stops on Escape", async () => {
    const user = userEvent.setup();
    const { graph } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    render(<Harness graph={graph} walkSnapshot="review-pay@rev-pay-1" />);
    fireEvent.keyDown(window, { key: "w" });
    expect(screen.getByTestId("xray-walk")).toHaveTextContent("Step 1 of 6");
    expect(card("checkout")).toHaveAttribute("data-current", "true");
    fireEvent.keyDown(window, { key: "w" });
    fireEvent.keyDown(window, { key: "w" });
    expect(screen.getByTestId("xray-walk-step")).toHaveTextContent("Observed #2 · retry.py → idempotency.py");
    expect(card("key")).toHaveAttribute("data-current", "true");
    expect(card("retry")).toHaveAttribute("data-current", "false");
    expect(edge("retry", "key")).toHaveAttribute("data-current", "true");
    expect(screen.getByTestId("xray-detail")).toHaveTextContent("idempotency.py");
    // Shift and modifiers do not step; neither does typing in a field.
    fireEvent.keyDown(window, { key: "W", shiftKey: true });
    fireEvent.keyDown(window, { key: "w", metaKey: true });
    expect(screen.getByTestId("xray-walk")).toHaveTextContent("Step 3 of 6");
    await user.click(screen.getByRole("button", { name: "Previous step" }));
    expect(screen.getByTestId("xray-walk")).toHaveTextContent("Step 2 of 6");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("xray-walk")).toBeNull();
    expect(screen.getByTestId("xray-walk-play")).toHaveTextContent("Walk through");
  });

  it("with no recorded run draws dashed arrows only and offers one call to action", async () => {
    const user = userEvent.setup();
    const onOpenDataPath = vi.fn();
    const auth = INVESTIGATION_MAP_FIXTURES.authRefreshMap();
    render(<Harness graph={auth.graph} flow={auth.flow} walkSnapshot="" onOpenDataPath={onOpenDataPath} />);
    const edges = screen.getAllByTestId("xray-edge");
    expect(edges.length).toBeGreaterThan(0);
    expect(edges.every((item) => item.getAttribute("data-line") === "dashed")).toBe(true);
    expect(screen.getByTestId("xray-caveat")).toHaveTextContent("No recorded run on After");
    await user.click(screen.getByTestId("xray-caveat-action"));
    expect(onOpenDataPath).toHaveBeenCalledTimes(1);
  });

  it("never draws Before arrows when Before has no base comparison", async () => {
    const user = userEvent.setup();
    const { graph } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    const headOnly: ReviewMapGraph = { ...graph, edges: graph.edges.map(({ delta_status: _d, base_weight: _b, snapshot_weight: _s, ...rest }) => rest) };
    render(<Harness graph={headOnly} flow={null} walkSnapshot="review-pay@rev-pay-1" />);
    await user.click(screen.getByRole("radio", { name: "Before" }));
    expect(screen.queryAllByTestId("xray-edge")).toHaveLength(0);
    expect(screen.getByTestId("xray-caveat")).toHaveTextContent("Before has no base comparison");
    expect(screen.getByTestId("xray-not-measured")).toHaveTextContent("Before (base) relationships");
  });

  it("hides a picture from an older revision until it is rebuilt", async () => {
    const user = userEvent.setup();
    const { graph } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    render(<Harness graph={graph} walkSnapshot="review-pay@rev-pay-0" />);
    expect(screen.getByTestId("xray-status")).toHaveTextContent("A newer revision replaced");
    expect(screen.queryByTestId("xray-canvas")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Rebuild on this revision" }));
    expect(screen.getByTestId("xray-canvas")).toBeInTheDocument();
  });
});
