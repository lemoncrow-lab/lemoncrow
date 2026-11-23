import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const { api } = vi.hoisted(() => ({
  api: {
    codeMapSearch: vi.fn(),
    codeMapReferences: vi.fn(),
  },
}));

vi.mock("../api", () => ({ api }));

import { LOCAL_PRODUCT_CAPABILITIES, ProductCapabilitiesProvider, REVIEW_ONLY_PRODUCT_CAPABILITIES } from "../lib/RuntimeCapabilities";
import ReviewCodePeek from "./ReviewCodePeek";

const DEFINITION = {
  query: "checkout",
  text_results: [],
  results: [{
    id: "sym-checkout",
    label: "checkout",
    qualified_name: "payments.checkout",
    path: "src/payments.py",
    kind: "function",
    line: 20,
    end_line: 40,
    node_type: "symbol",
  }],
};
const USAGES = {
  symbol_id: "sym-checkout",
  reference_count: 1,
  truncated: false,
  references: [{
    path: "src/api.py",
    line: 88,
    column: 3,
    end_line: 88,
    snippet: "checkout(order)",
    caller: "place_order",
    edge_kind: "call",
    provenance: "ast",
  }],
};

describe("ReviewCodePeek product-capability gating (isolation §25)", () => {
  it("renders nothing and requests nothing when the Runtime is not declared", async () => {
    api.codeMapSearch.mockClear();
    api.codeMapReferences.mockClear();
    const { container } = render(
      <ProductCapabilitiesProvider value={REVIEW_ONLY_PRODUCT_CAPABILITIES}>
        <ReviewCodePeek projectRoot="/repo" path="src/payments.py" symbol="checkout" line={22} onOpenInCode={vi.fn()} />
      </ProductCapabilitiesProvider>,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(container.innerHTML).toBe("");
    expect(screen.queryByTestId("review-code-peek")).toBeNull();
    expect(api.codeMapSearch).not.toHaveBeenCalled();
    expect(api.codeMapReferences).not.toHaveBeenCalled();
  });

  it("keeps the degraded label when the Runtime is present but its index is down", async () => {
    api.codeMapSearch.mockRejectedValueOnce(new Error("index unavailable"));
    render(
      <ProductCapabilitiesProvider value={LOCAL_PRODUCT_CAPABILITIES}>
        <ReviewCodePeek projectRoot="/repo" path="src/payments.py" symbol="checkout" line={22} />
      </ProductCapabilitiesProvider>,
    );
    expect(await screen.findByText("Current index unavailable.")).toBeInTheDocument();
    expect(screen.getByTestId("review-code-peek")).toBeInTheDocument();
  });

  it("jumps inside the Review first and keeps Open in Code secondary", async () => {
    api.codeMapSearch.mockResolvedValue(DEFINITION);
    api.codeMapReferences.mockResolvedValue(USAGES);
    const onJump = vi.fn();
    const onOpenInCode = vi.fn();
    render(<ReviewCodePeek projectRoot="/repo" path="src/payments.py" symbol="checkout" line={22} onJump={onJump} onOpenInCode={onOpenInCode} />);

    await userEvent.click(await screen.findByRole("button", { name: "Go to definition" }));
    expect(onJump).toHaveBeenLastCalledWith({ path: "src/payments.py", line: 20, endLine: 40 });
    await userEvent.click(await screen.findByRole("button", { name: "Go to src/api.py:L88" }));
    expect(onJump).toHaveBeenLastCalledWith({ path: "src/api.py", line: 88, endLine: 88 });
    expect(onOpenInCode).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Open src/api.py:L88 in Code" }));
    expect(onOpenInCode).toHaveBeenLastCalledWith({ path: "src/api.py", line: 88, endLine: 88 });
    await userEvent.click(screen.getByRole("button", { name: "Open definition in Code" }));
    expect(onOpenInCode).toHaveBeenLastCalledWith({ path: "src/payments.py", line: 20, endLine: 40 });
    expect(onJump).toHaveBeenCalledTimes(2);
  });
});

describe("ReviewCodePeek", () => {
  it("labels current-index definition/usages as navigation context, not revision evidence", async () => {
    api.codeMapSearch.mockResolvedValue({
      query: "checkout",
      text_results: [],
      results: [{
        id: "sym-checkout",
        label: "checkout",
        qualified_name: "payments.checkout",
        path: "src/payments.py",
        kind: "function",
        line: 20,
        end_line: 40,
        node_type: "symbol",
      }],
    });
    api.codeMapReferences.mockResolvedValue({
      symbol_id: "sym-checkout",
      reference_count: 1,
      truncated: false,
      references: [{
        path: "src/api.py",
        line: 88,
        column: 3,
        end_line: 88,
        snippet: "checkout(order)",
        caller: "place_order",
        edge_kind: "call",
        provenance: "ast",
      }],
    });
    const onOpenInCode = vi.fn();

    render(
      <ReviewCodePeek
        projectRoot="/repo"
        path="src/payments.py"
        symbol="checkout"
        line={22}
        onOpenInCode={onOpenInCode}
      />,
    );

    expect(screen.getByText(/Current index context · navigation only · not Review evidence exact for this revision/)).toBeInTheDocument();
    expect(await screen.findByText("payments.checkout")).toBeInTheDocument();
    expect(screen.getByText("Usages · 1")).toBeInTheDocument();
    await userEvent.click(screen.getByText("src/api.py"));
    expect(onOpenInCode).toHaveBeenCalledWith({ path: "src/api.py", line: 88, endLine: 88 });
    await waitFor(() => expect(api.codeMapReferences).toHaveBeenCalledWith("sym-checkout", "/repo", 20));
  });
});
