import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { INVESTIGATION_MAP_FIXTURES } from "../investigation/mapFixtures";
import type { ReviewMapGraph } from "../types";
import XRay from "./XRay";
import type { XRaySide } from "./xrayModel";

function Harness({ graph, walkSnapshot, onOpenSource, onOpenEvidence }: { graph: ReviewMapGraph; walkSnapshot: string; onOpenSource: () => void; onOpenEvidence?: () => void }) {
  const { flow } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
  const [side, setSide] = useState<XRaySide>("after");
  const [snapshot, setSnapshot] = useState(walkSnapshot);
  return (
    <XRay
      graph={graph}
      flow={flow}
      rootId="retry"
      side={side}
      walkSnapshot={snapshot}
      onSideChange={setSide}
      onRootChange={() => undefined}
      onRebuild={() => setSnapshot("review-pay@rev-pay-1")}
      onOpenSource={onOpenSource}
      onOpenEvidence={onOpenEvidence}
      onClose={() => undefined}
    />
  );
}

describe("XRay", () => {
  it("toggles Before/After, marks change in words, and opens the selected step's source", async () => {
    const user = userEvent.setup();
    const onOpenSource = vi.fn();
    const { graph } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    render(<Harness graph={graph} walkSnapshot="review-pay@rev-pay-1" onOpenSource={onOpenSource} />);
    const steps = screen.getByTestId("xray-steps");
    expect(within(steps).getByText("idempotency.py")).toBeInTheDocument();
    expect(within(steps).queryByText("legacy_backoff.py")).toBeNull();
    expect(within(steps).getAllByLabelText("link added").length).toBe(2);
    expect(within(steps).getByText("observed #2")).toBeInTheDocument();

    await user.click(within(steps).getByText("ledger_store.py"));
    const detail = screen.getByTestId("xray-detail");
    expect(detail).toHaveTextContent("1 → 2 references");
    expect(within(detail).getByTestId("xray-detail-order")).toHaveTextContent("Possible/reachable");
    await user.click(within(detail).getByRole("button", { name: "Open source" }));
    expect(onOpenSource).toHaveBeenCalledWith(expect.objectContaining({ node_id: "ledger", path: "src/payments/ledger_store.py" }));

    await user.click(screen.getByRole("radio", { name: "Before" }));
    expect(screen.getByTestId("xray")).toHaveAttribute("data-side", "before");
    expect(within(screen.getByTestId("xray-steps")).getByText("legacy_backoff.py")).toBeInTheDocument();
    expect(within(screen.getByTestId("xray-steps")).getByLabelText("link removed by this change")).toBeInTheDocument();
    expect(screen.getByTestId("xray-diff")).toHaveTextContent("2 links only in After");
  });

  it("opens the recorded run behind a traced link, and only there", async () => {
    const user = userEvent.setup();
    const onOpenEvidence = vi.fn();
    const { graph } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    render(<Harness graph={graph} walkSnapshot="review-pay@rev-pay-1" onOpenSource={() => undefined} onOpenEvidence={onOpenEvidence} />);
    const steps = screen.getByTestId("xray-steps");
    await user.click(within(steps).getByText("ledger_store.py"));
    expect(screen.queryByTestId("xray-detail-evidence")).toBeNull();
    await user.click(within(steps).getByText("idempotency.py"));
    expect(screen.getByTestId("xray-detail-trace")).toBeInTheDocument();
    await user.click(screen.getByTestId("xray-detail-evidence"));
    expect(onOpenEvidence).toHaveBeenCalledWith(expect.objectContaining({ node_id: "key" }));
  });

  it("hides a walk from an older Snapshot until it is rebuilt", async () => {
    const user = userEvent.setup();
    const { graph } = INVESTIGATION_MAP_FIXTURES.paymentRetryMap();
    render(<Harness graph={graph} walkSnapshot="review-pay@rev-pay-0" onOpenSource={() => undefined} />);
    expect(screen.getByTestId("xray-status")).toHaveTextContent("A newer Snapshot replaced");
    expect(screen.queryByTestId("xray-steps")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Rebuild on this Snapshot" }));
    expect(screen.getByTestId("xray-steps")).toBeInTheDocument();
  });
});
