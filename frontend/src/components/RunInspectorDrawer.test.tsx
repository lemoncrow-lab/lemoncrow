import { render, screen } from "@testing-library/react";
import { waitFor } from "@testing-library/react";
import RunInspectorDrawer from "./RunInspectorDrawer";
import type { Trace } from "../api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("RunInspectorDrawer", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders pinned blocks, recalled passages, and summary metrics", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/ledgers/run-123")) {
          return Promise.resolve(
            jsonResponse({
              session_id: "run-123",
              active_reasonblocks: ["block.alpha", "block.beta"],
              events: [
                {
                  kind: "memory_recall",
                  payload: {
                    top_passages: ["pas-1"],
                    source_ref: "https://example.com/pas-1",
                  },
                },
                {
                  kind: "context_summary",
                  payload: {
                    tokens_pre: 100,
                    tokens_post: 42,
                    evicted_event_ids: ["e-1", "e-2", "e-3"],
                  },
                },
              ],
            })
          );
        }
        return Promise.resolve(new Response("not found", { status: 404 }));
      }
    );

    const trace = {
      id: "trace-1",
      session_id: "run-123",
      agent: "atelier:code",
      task: "Inspect this run",
      status: "success",
      files_touched: [],
      tools_called: [],
      commands_run: [],
      errors_seen: [],
      repeated_failures: [],
      validation_results: [],
      created_at: new Date().toISOString(),
    } as Trace;

    render(<RunInspectorDrawer open trace={trace} onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText("Pinned Blocks")).toBeInTheDocument();
    });

    expect(screen.getByText("block.alpha")).toBeInTheDocument();
    expect(screen.getByText("block.beta")).toBeInTheDocument();
    expect(screen.getByText("Recalled Passages")).toBeInTheDocument();
    expect(screen.getByText("pas-1")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Source" })).toHaveAttribute(
      "href",
      "https://example.com/pas-1"
    );
    expect(screen.getByText("Summarized events")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("100")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
  });
});
