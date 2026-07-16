import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import CodeMap from "./CodeMap";

vi.mock("../components/CodeGraph", () => ({
  default: ({ nodes, followNodeId, onSelect, onExpand }: any) => (
    <div data-testid="code-graph" data-follow={followNodeId || ""}>
      {nodes.map((node: any) => (
        <button key={node.id} onClick={() => onSelect(node.id)}>
          {node.label}
        </button>
      ))}
      <button onClick={() => onExpand("charge")}>Focus charge</button>
    </div>
  ),
}));

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

const charge = {
  id: "charge",
  label: "chargeCard",
  qualified_name: "PaymentGateway.chargeCard",
  path: "src/payment.ts",
  kind: "method",
  language: "TypeScript",
  file_type: "source",
  community: "src",
  node_type: "symbol",
  line: 12,
  end_line: 24,
  focus: true,
  color: "#60a5fa",
};

const paymentFile = {
  id: "file::payment",
  label: "payment.ts",
  qualified_name: "src/payment.ts",
  path: "src/payment.ts",
  kind: "source",
  language: "TypeScript",
  file_type: "source",
  community: "src",
  node_type: "file",
  line: 1,
  end_line: 24,
  color: "#67e8f9",
};

describe("CodeMap", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders the full map, filters, details, search, and exact live targets", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/v1/code-map/projects"))
        return Promise.resolve(
          response({
            projects: [
              { root: "/repo", label: "repo", indexed: true, active: true },
            ],
          })
        );
      if (url.includes("/v1/code-map/full"))
        return Promise.resolve(
          response({
            project: { root: "/repo", label: "repo" },
            index: { files_indexed: 40, symbols_indexed: 320 },
            total_symbols: 320,
            total_files: 40,
            truncated: false,
            communities: [
              { id: "src", label: "src", color: "#60a5fa", count: 2 },
            ],
            file_types: [
              { id: "source", label: "Source", color: "#67e8f9", count: 1 },
            ],
            languages: [{ id: "TypeScript", label: "TypeScript", count: 1 }],
            graph: {
              focus: "charge",
              truncated: false,
              nodes: [paymentFile, charge],
              edges: [
                {
                  id: "contains",
                  source: "file::payment",
                  target: "charge",
                  kind: "contains",
                  depth: 0,
                },
              ],
            },
          })
        );
      if (url.includes("/v1/code-map/search"))
        return Promise.resolve(
          response({
            query: "checkout",
            results: [
              {
                ...charge,
                id: "checkout",
                label: "checkout",
                qualified_name: "checkout",
                path: "src/checkout.ts",
                line: 2,
                end_line: 9,
              },
            ],
          })
        );
      if (url.includes("/v1/code-map/symbol"))
        return Promise.resolve(
          response({
            ...charge,
            signature: "chargeCard(amount: number)",
            source: "chargeCard(amount: number) { return this.send(amount); }",
            source_truncated: false,
          })
        );
      if (url.includes("/v1/code-map/neighborhood"))
        return Promise.resolve(
          response({
            focus: "checkout",
            truncated: false,
            nodes: [
              {
                ...charge,
                id: "checkout",
                label: "checkout",
                path: "src/checkout.ts",
              },
            ],
            edges: [],
          })
        );
      if (url.includes("/v1/code-map/activity"))
        return Promise.resolve(
          response({
            session_id: "session-1",
            status: "running",
            cursor: "2026-07-16T12:00:00Z",
            events: [
              {
                id: "event-1",
                session_id: "session-1",
                kind: "edit",
                at: "2026-07-16T12:00:00Z",
                label: "Edited payment.ts",
                path: "src/payment.ts",
                symbol_ids: ["file::payment"],
              },
            ],
          })
        );
      return Promise.resolve(new Response("not found", { status: 404 }));
    });

    render(
      <MemoryRouter initialEntries={["/map?repo=%2Frepo"]}>
        <CodeMap />
      </MemoryRouter>
    );

    expect(
      await screen.findByRole("heading", { name: "Source map" })
    ).toBeInTheDocument();
    expect(await screen.findByText("320 indexed symbols")).toBeInTheDocument();
    expect(screen.getByText("40 tracked files")).toBeInTheDocument();
    expect(screen.getByText("Communities")).toBeInTheDocument();
    expect(await screen.findByText("Edited payment.ts")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("code-graph")).toHaveAttribute(
        "data-follow",
        "file::payment"
      )
    );
    await userEvent.click(screen.getByRole("button", { name: "Live on" }));
    await waitFor(() =>
      expect(screen.getByTestId("code-graph")).toHaveAttribute(
        "data-follow",
        ""
      )
    );

    await userEvent.click(screen.getByRole("button", { name: "chargeCard" }));
    expect(
      await screen.findByText("PaymentGateway.chargeCard")
    ).toBeInTheDocument();
    expect(screen.getByText(/this\.send/)).toBeInTheDocument();

    await userEvent.type(screen.getByRole("searchbox"), "checkout");
    await userEvent.click(
      await screen.findByRole("button", {
        name: /checkout.*src\/checkout\.ts/i,
      })
    );
    await waitFor(() =>
      expect(globalThis.fetch).toHaveBeenCalledWith(
        expect.stringContaining("symbol_id=checkout")
      )
    );
  });

  it("uses the active indexed project when opened from the dashboard nav", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/v1/code-map/projects"))
        return Promise.resolve(
          response({
            projects: [
              { root: "/repo", label: "repo", indexed: true, active: true },
            ],
          })
        );
      if (url.includes("/v1/code-map/full"))
        return Promise.resolve(
          response({
            project: { root: "/repo", label: "repo" },
            index: { files_indexed: 1, symbols_indexed: 1 },
            total_symbols: 1,
            total_files: 1,
            truncated: false,
            communities: [
              { id: "src", label: "src", color: "#60a5fa", count: 2 },
            ],
            file_types: [
              { id: "source", label: "Source", color: "#67e8f9", count: 1 },
            ],
            languages: [{ id: "TypeScript", label: "TypeScript", count: 1 }],
            graph: {
              focus: "charge",
              truncated: false,
              nodes: [paymentFile, charge],
              edges: [],
            },
          })
        );
      if (url.includes("/v1/code-map/activity"))
        return Promise.resolve(
          response({
            session_id: null,
            status: "idle",
            cursor: null,
            events: [],
          })
        );
      if (url.includes("/v1/code-map/symbol"))
        return Promise.resolve(
          response({
            ...charge,
            signature: "chargeCard()",
            source: "",
            source_truncated: false,
          })
        );
      return Promise.resolve(new Response("not found", { status: 404 }));
    });

    render(
      <MemoryRouter initialEntries={["/map"]}>
        <CodeMap />
      </MemoryRouter>
    );

    expect(await screen.findByText("1 indexed symbols")).toBeInTheDocument();
    await waitFor(() =>
      expect(globalThis.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/v1/code-map/full?project_root=%2Frepo")
      )
    );
  });
});
