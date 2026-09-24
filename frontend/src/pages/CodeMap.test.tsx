import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
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
vi.mock("../components/CodeGraph3D", () => ({
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
vi.mock("@pierre/diffs/react", () => ({
  File: ({ file, selectedLines }: any) => (
    <pre data-testid="highlighted-source">
      {String(file.contents)
        .split("\n")
        .map((line, index) => {
          const lineNumber = index + 1;
          const selected =
            selectedLines &&
            lineNumber >= selectedLines.start &&
            lineNumber <= selectedLines.end;
          return (
            <span
              key={lineNumber}
              data-code-line={lineNumber}
              className={selected ? "border-cyan-400 bg-cyan-400/10" : ""}
            >
              {line || " "}
            </span>
          );
        })}
    </pre>
  ),
}));

function HistoryProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="location-path">{location.pathname}</output>
      <output data-testid="location-search">{location.search}</output>
      <button type="button" onClick={() => navigate(-1)}>
        Browser back
      </button>
    </>
  );
}

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

  it("renders source navigation, local editor, exact agent handoff, map, and review navigation", async () => {
    let editorBody: Record<string, unknown> | null = null;
    let handoffBody: Record<string, unknown> | null = null;
    let reviewBody: Record<string, unknown> | null = null;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url.includes("/v1/code-map/projects"))
        return Promise.resolve(
          response({
            projects: [
              {
                project_id: "proj_repo",
                root: "/repo",
                label: "repo",
                indexed: true,
                active: true,
              },
            ],
          })
        );
      if (url.includes("/v1/code-map/files"))
        return Promise.resolve(
          response({
            project: { root: "/repo", label: "repo" },
            total_files: 1,
            files: [paymentFile],
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
            groups: [{ id: "src", label: "src", color: "#60a5fa", count: 2 }],
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
            text_results: [
              {
                id: "text::src/payment.ts:2:38",
                path: "src/payment.ts",
                line: 2,
                column: 38,
                text: "export function chargeCard() { return payment; }",
                language: "TypeScript",
                file_type: "source",
              },
            ],
          })
        );
      if (url.includes("/v1/files/content"))
        return Promise.resolve(
          new Response(
            "export const payment = true;\nexport function chargeCard() { return payment; }",
            { status: 200, headers: { "Content-Type": "text/plain" } }
          )
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
      if (url.includes("/v1/code-map/editor") && init?.method === "POST") {
        editorBody = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
        return Promise.resolve(
          response({
            opened: true,
            editor: "cursor",
            label: "Cursor",
            line: 2,
            column: 1,
            pid: 42,
          })
        );
      }
      if (url.includes("/v1/code-map/editor"))
        return Promise.resolve(
          response({
            available: true,
            preferred: { id: "cursor", label: "Cursor" },
            editors: [{ id: "cursor", label: "Cursor" }],
          })
        );
      if (url.includes("/v1/code-map/review")) {
        reviewBody = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
        return Promise.resolve(
          response({
            review_id: "r-deadbeefcafebabe",
            short_id: "r-deadbeef",
            review_path: "/r/deadbeef",
            review_url: "http://127.0.0.1:7420/r/deadbeef",
            revision_id: "rr-feedface",
            revision_number: 3,
            revision_created: true,
          })
        );
      }
      if (url.includes("/v1/code-map/agent-handoff")) {
        handoffBody = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
        return Promise.resolve(
          response({
            state: "sent",
            host: "codex",
            session_id: "session-1",
            target_ref: "codex:session-1",
            remote_ref: "pid:42",
            message: "prompt sent to the exact Codex session",
          })
        );
      }
      if (url.includes("/v1/code-map/activity"))
        return Promise.resolve(
          response({
            session_id: "session-1",
            host: "codex",
            handoff_supported: true,
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
                line: 2,
                symbol_ids: ["file::payment"],
              },
            ],
          })
        );
      return Promise.resolve(new Response("not found", { status: 404 }));
    });

    render(
      <MemoryRouter initialEntries={["/code?repo=%2Frepo"]}>
        <CodeMap />
        <HistoryProbe />
      </MemoryRouter>
    );

    expect(await screen.findByText("320 symbols")).toBeInTheDocument();
    expect(screen.getByText("40 files")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^source$/i })).toHaveAttribute(
      "aria-pressed",
      "true"
    );
    expect(screen.queryByText("Groups")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Ask agent about code" })).not.toBeInTheDocument();
    expect(await screen.findByText("Edited payment.ts")).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: /edited payment\.ts/i })
    );
    expect(
      await screen.findByText("export const payment = true;")
    ).toBeInTheDocument();
    const activityLine = screen
      .getByLabelText("Source viewer")
      .querySelector('[data-code-line="2"]');
    expect(activityLine).toHaveClass("border-cyan-400", "bg-cyan-400/10");
    expect(screen.getByRole("button", { name: "Line" })).toHaveAttribute(
      "title",
      "L2"
    );
    expect(
      screen.getByRole("button", { name: /chargecard.*12/i })
    ).toBeInTheDocument();
    expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining(
        "/v1/files/content?path=%2Frepo%2Fsrc%2Fpayment.ts"
      )
    );
    await userEvent.click(screen.getByRole("button", { name: "Open in Cursor" }));
    await waitFor(() =>
      expect(editorBody).toEqual({
        project_root: "proj_repo",
        path: "src/payment.ts",
        line: 2,
        column: 1,
      })
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "Ask agent about code" }),
      "Check the retry behavior here."
    );
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("prompt sent to the exact Codex session")).toBeInTheDocument();
    expect(handoffBody).toMatchObject({
      project_root: "proj_repo",
      expected_session_id: "session-1",
      path: "src/payment.ts",
      line: 2,
      end_line: 2,
      message: "Check the retry behavior here.",
    });

    await userEvent.type(
      screen.getByRole("searchbox", { name: "Find code" }),
      "checkout"
    );
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
    expect(screen.queryByText("prompt sent to the exact Codex session")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Ask agent about code" })).toHaveValue("");

    await userEvent.type(
      screen.getByRole("searchbox", { name: "Find code" }),
      "payment"
    );
    await userEvent.click(
      await screen.findByRole("button", {
        name: /export function chargecard.*src\/payment\.ts/i,
      })
    );
    const textLine = screen
      .getByLabelText("Source viewer")
      .querySelector('[data-code-line="2"]');
    expect(textLine).toHaveClass("border-cyan-400", "bg-cyan-400/10");
    expect(screen.getByRole("button", { name: "Line" })).toHaveAttribute(
      "title",
      "L2"
    );

    await userEvent.click(screen.getByRole("button", { name: /^map$/i }));
    expect(screen.getByText("Groups")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("code-graph")).toHaveAttribute(
        "data-follow",
        "file::payment"
      )
    );
    await userEvent.click(screen.getByRole("button", { name: "Live on" }));
    await waitFor(() =>
      expect(screen.getByTestId("code-graph")).toHaveAttribute("data-follow", "")
    );

    await userEvent.click(screen.getByRole("button", { name: "Review changes" }));
    await waitFor(() =>
      expect(reviewBody).toEqual({ project_root: "proj_repo" })
    );
    await waitFor(() =>
      expect(screen.getByTestId("location-path")).toHaveTextContent("/r/deadbeef")
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
      if (url.includes("/v1/code-map/files"))
        return Promise.resolve(
          response({
            project: { root: "/repo", label: "repo" },
            total_files: 1,
            files: [paymentFile],
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
            groups: [{ id: "src", label: "src", color: "#60a5fa", count: 2 }],
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
      <MemoryRouter initialEntries={["/code"]}>
        <CodeMap />
      </MemoryRouter>
    );

    expect(await screen.findByText("1 symbols")).toBeInTheDocument();
    await waitFor(() =>
      expect(globalThis.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/v1/code-map/full?project_root=%2Frepo")
      )
    );
  });

  it("opens a deep-linked symbol in the full-file source viewer", async () => {
    const fileContent = Array.from({ length: 24 }, (_, index) =>
      index === 11
        ? "export function chargeCard() {}"
        : "// line " + (index + 1)
    ).join("\n");

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
      if (url.includes("/v1/code-map/files"))
        return Promise.resolve(
          response({
            project: { root: "/repo", label: "repo" },
            total_files: 1,
            files: [paymentFile],
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
            groups: [{ id: "src", label: "src", color: "#60a5fa", count: 2 }],
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
      if (url.includes("/v1/code-map/references"))
        return Promise.resolve(
          response({
            symbol_id: "charge",
            reference_count: 1,
            truncated: false,
            references: [
              {
                path: "src/payment.ts",
                line: 18,
                column: 5,
                end_line: 18,
                snippet: "const result = chargeCard();",
                caller: "checkout",
                edge_kind: "call",
                provenance: "ast",
                confidence: 1,
              },
            ],
          })
        );
      if (url.includes("/v1/files/content"))
        return Promise.resolve(
          new Response(fileContent, {
            status: 200,
            headers: { "Content-Type": "text/plain" },
          })
        );
      return Promise.resolve(new Response("not found", { status: 404 }));
    });

    render(
      <MemoryRouter
        initialEntries={[
          "/code?repo=%2Frepo&file=src%2Fpayment.ts&symbol=charge&view=source",
        ]}
      >
        <CodeMap />
      </MemoryRouter>
    );

    expect(
      await screen.findByText("export function chargeCard() {}")
    ).toBeInTheDocument();
    const line12 = screen
      .getByLabelText("Source viewer")
      .querySelector('[data-code-line="12"]');
    expect(line12).toHaveClass("border-cyan-400", "bg-cyan-400/10");
    expect(
      screen.getByRole("button", { name: /chargecard.*12/i })
    ).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("button", { name: "Range" })).toBeInTheDocument();
    expect(await screen.findByText("const result = chargeCard();")).toBeInTheDocument();
    await userEvent.click(screen.getByTitle("src/payment.ts:18"));
    const line18 = screen
      .getByLabelText("Source viewer")
      .querySelector('[data-code-line="18"]');
    expect(line18).toHaveClass("border-cyan-400", "bg-cyan-400/10");
    expect(line12).not.toHaveClass("border-cyan-400", "bg-cyan-400/10");
    expect(screen.getByRole("button", { name: "Line" })).toHaveAttribute(
      "title",
      "L18"
    );

    const lineInput = screen.getByRole("spinbutton", { name: "Go to line" });
    await userEvent.clear(lineInput);
    await userEvent.type(lineInput, "3");
    await userEvent.click(screen.getByRole("button", { name: "Open source line" }));
    const line3 = screen
      .getByLabelText("Source viewer")
      .querySelector('[data-code-line="3"]');
    expect(line3).toHaveClass("border-cyan-400", "bg-cyan-400/10");
    expect(screen.getByRole("button", { name: "Line" })).toHaveAttribute(
      "title",
      "L3"
    );
    expect(
      screen.getByRole("button", { name: /chargecard.*12/i })
    ).not.toHaveAttribute("aria-current", "true");
  });

  it("opens a source file that is outside the bounded graph", async () => {
    const hiddenFile = {
      ...paymentFile,
      id: "file::hidden",
      label: "hidden.ts",
      qualified_name: "src/hidden.ts",
      path: "src/hidden.ts",
      end_line: 1,
    };
    const hiddenSymbol = {
      ...charge,
      id: "hidden-symbol",
      label: "hiddenValue",
      qualified_name: "hiddenValue",
      path: "src/hidden.ts",
      line: 1,
      end_line: 1,
    };

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
      if (url.includes("/v1/code-map/files"))
        return Promise.resolve(
          response({
            project: { root: "/repo", label: "repo" },
            total_files: 2,
            files: [paymentFile, hiddenFile],
          })
        );
      if (url.includes("/v1/code-map/file-symbols"))
        return Promise.resolve(
          response({
            path: "src/hidden.ts",
            total_symbols: 1,
            symbols: [hiddenSymbol],
            truncated: false,
          })
        );
      if (url.includes("/v1/code-map/full"))
        return Promise.resolve(
          response({
            project: { root: "/repo", label: "repo" },
            index: { files_indexed: 2, symbols_indexed: 1 },
            total_symbols: 1,
            total_files: 2,
            truncated: true,
            groups: [{ id: "src", label: "src", color: "#60a5fa", count: 2 }],
            file_types: [
              { id: "source", label: "Source", color: "#67e8f9", count: 1 },
            ],
            languages: [{ id: "TypeScript", label: "TypeScript", count: 1 }],
            graph: {
              focus: "charge",
              truncated: true,
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
      if (url.includes("/v1/files/content"))
        return Promise.resolve(
          new Response("export const hidden = true;", {
            status: 200,
            headers: { "Content-Type": "text/plain" },
          })
        );
      return Promise.resolve(new Response("not found", { status: 404 }));
    });

    render(
      <MemoryRouter
        initialEntries={[
          "/code?repo=%2Frepo&file=src%2Fhidden.ts&view=source",
        ]}
      >
        <CodeMap />
      </MemoryRouter>
    );

    expect(await screen.findByText("export const hidden = true;")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /hidden\.ts/i })).toHaveAttribute(
      "aria-current",
      "true"
    );
    expect(
      await screen.findByRole("button", { name: /hiddenvalue.*1/i })
    ).toBeInTheDocument();
    expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining(
        "/v1/files/content?path=%2Frepo%2Fsrc%2Fhidden.ts"
      )
    );
  });

  it("keeps explicit code navigation in browser history", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/v1/code-map/projects"))
        return Promise.resolve(
          response({
            projects: [
              {
                project_id: "proj_repo",
                root: "/repo",
                label: "repo",
                indexed: true,
                active: true,
              },
            ],
          })
        );
      if (url.includes("/v1/code-map/files"))
        return Promise.resolve(
          response({
            project: { root: "/repo", label: "repo" },
            total_files: 1,
            files: [paymentFile],
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
            groups: [{ id: "src", label: "src", color: "#60a5fa", count: 2 }],
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
          response({ session_id: null, status: "idle", cursor: null, events: [] })
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
      <MemoryRouter initialEntries={["/code?repo=%2Frepo&view=map"]}>
        <CodeMap />
        <HistoryProbe />
      </MemoryRouter>
    );

    const graph = await screen.findByTestId("code-graph");
    await waitFor(() => {
      const location = screen.getByTestId("location-search");
      expect(location).toHaveTextContent("repo=proj_repo");
      expect(location).not.toHaveTextContent("repo=%2Frepo");
    });
    await userEvent.click(within(graph).getByRole("button", { name: "payment.ts" }));
    await waitFor(() =>
      expect(screen.getByTestId("location-search")).toHaveTextContent(
        "file=src%2Fpayment.ts"
      )
    );

    await userEvent.click(within(graph).getByRole("button", { name: "chargeCard" }));
    await waitFor(() =>
      expect(screen.getByTestId("location-search")).toHaveTextContent("symbol=charge")
    );

    await userEvent.click(screen.getByRole("button", { name: "Browser back" }));
    await waitFor(() => {
      const location = screen.getByTestId("location-search");
      expect(location).toHaveTextContent("file=src%2Fpayment.ts");
      expect(location).not.toHaveTextContent("symbol=charge");
    });
  });

  it("reuses cached source and symbols when revisiting a file", async () => {
    const hiddenFile = {
      ...paymentFile,
      id: "file::hidden-cache",
      label: "hidden.ts",
      qualified_name: "src/hidden.ts",
      path: "src/hidden.ts",
      end_line: 1,
    };
    const hiddenSymbol = {
      ...charge,
      id: "hidden-cache-symbol",
      label: "hiddenValue",
      qualified_name: "hiddenValue",
      path: "src/hidden.ts",
      line: 1,
      end_line: 1,
    };

    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/v1/code-map/projects"))
        return Promise.resolve(
          response({
            projects: [
              {
                project_id: "proj_repo",
                root: "/repo",
                label: "repo",
                indexed: true,
                active: true,
              },
            ],
          })
        );
      if (url.includes("/v1/code-map/files"))
        return Promise.resolve(
          response({
            project: { root: "/repo", label: "repo" },
            total_files: 2,
            files: [paymentFile, hiddenFile],
          })
        );
      if (url.includes("/v1/code-map/file-symbols")) {
        const hidden = url.includes("path=src%2Fhidden.ts");
        return Promise.resolve(
          response({
            path: hidden ? "src/hidden.ts" : "src/payment.ts",
            total_symbols: 1,
            symbols: [hidden ? hiddenSymbol : charge],
            truncated: false,
          })
        );
      }
      if (url.includes("/v1/code-map/full"))
        return Promise.resolve(
          response({
            project: { root: "/repo", label: "repo" },
            index: { files_indexed: 2, symbols_indexed: 2 },
            total_symbols: 2,
            total_files: 2,
            truncated: true,
            groups: [{ id: "src", label: "src", color: "#60a5fa", count: 2 }],
            file_types: [
              { id: "source", label: "Source", color: "#67e8f9", count: 2 },
            ],
            languages: [{ id: "TypeScript", label: "TypeScript", count: 2 }],
            graph: {
              focus: "charge",
              truncated: true,
              nodes: [paymentFile, charge],
              edges: [],
            },
          })
        );
      if (url.includes("/v1/code-map/activity"))
        return Promise.resolve(
          response({ session_id: null, status: "idle", cursor: null, events: [] })
        );
      if (url.includes("/v1/files/content")) {
        const hidden = url.includes("src%2Fhidden.ts");
        return Promise.resolve(
          new Response(
            hidden ? "export const hidden = true;" : "export const payment = true;",
            { status: 200, headers: { "Content-Type": "text/plain" } }
          )
        );
      }
      return Promise.resolve(new Response("not found", { status: 404 }));
    });

    render(
      <MemoryRouter
        initialEntries={[
          "/code?repo=proj_repo&file=src%2Fhidden.ts&view=source",
        ]}
      >
        <CodeMap />
      </MemoryRouter>
    );

    expect(await screen.findByText("export const hidden = true;")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^payment\.ts$/i }));
    expect(await screen.findByText("export const payment = true;")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^hidden\.ts$/i }));
    expect(await screen.findByText("export const hidden = true;")).toBeInTheDocument();

    const calls = vi.mocked(globalThis.fetch).mock.calls.map(([input]) => String(input));
    expect(
      calls.filter((url) =>
        url.includes("/v1/files/content?path=%2Frepo%2Fsrc%2Fhidden.ts")
      )
    ).toHaveLength(1);
    expect(
      calls.filter(
        (url) =>
          url.includes("/v1/code-map/file-symbols") &&
          url.includes("path=src%2Fhidden.ts")
      )
    ).toHaveLength(1);
  });
});
