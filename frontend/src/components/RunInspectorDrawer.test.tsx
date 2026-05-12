import { render, screen } from "@testing-library/react";
import { waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
        if (url.includes("/api/ledgers/trace-1")) {
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

  it("searches only inside the current session and filters visible results", async () => {
    const user = userEvent.setup();

    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/ledgers/trace-search")) {
          return Promise.resolve(
            jsonResponse({
              session_id: "run-search",
              active_reasonblocks: ["block.timeout", "block.safe"],
              source_paths: ["/tmp/timeout.log", "/tmp/healthy.log"],
              events: [],
              conversations: [
                {
                  kind: "agent_message",
                  summary: "Timeout detected",
                  content: "shell timeout hit while waiting for deploy",
                },
                {
                  kind: "agent_message",
                  summary: "Healthy run",
                  content: "all checks passed",
                },
              ],
            })
          );
        }
        return Promise.resolve(new Response("not found", { status: 404 }));
      }
    );

    const trace = {
      id: "trace-search",
      session_id: "run-search",
      agent: "atelier:code",
      host: "copilot",
      task: "Inspect timeout",
      status: "failed",
      files_touched: ["frontend/src/pages/Traces.tsx"],
      tools_called: [
        {
          name: "search_logs",
          args_hash: "abc",
          count: 1,
          result_summary: "timeout in deploy log",
        },
      ],
      commands_run: ["tail -n 50 timeout.log"],
      errors_seen: [],
      repeated_failures: [],
      validation_results: [],
      created_at: new Date().toISOString(),
    } as Trace;

    render(<RunInspectorDrawer open trace={trace} onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText("Source Files")).toBeInTheDocument();
    });

    await user.type(
      screen.getByPlaceholderText(
        /Search this run: timeline, files, commands, tools, passages/i
      ),
      "timeout"
    );

    expect(screen.getByText("Session Search Results")).toBeInTheDocument();
    expect(
      screen.getAllByText(
        (_content, element) =>
          element?.textContent?.includes(
            "shell timeout hit while waiting for deploy"
          ) ?? false
      ).length
    ).toBeGreaterThan(0);
    expect(screen.getByText("/tmp/timeout.log")).toBeInTheDocument();
    expect(screen.queryByText("/tmp/healthy.log")).not.toBeInTheDocument();
    expect(screen.queryByText("block.safe")).not.toBeInTheDocument();
  });
});
