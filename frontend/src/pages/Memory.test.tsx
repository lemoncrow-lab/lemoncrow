import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import Memory, { factSourceLabel } from "./Memory";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Memory page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("aggregates blocks and archival passages across visible agents", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        // New cross-vendor memory facts endpoint
        if (
          url.includes("/v1/memory/facts") &&
          !url.includes("/v1/memory/facts/")
        ) {
          return Promise.resolve(
            jsonResponse([
              {
                fact_id: "claude-b1c2d3e4",
                vendor: "claude",
                source_kind: "claude-md-global",
                content: "Prefer small, reviewable diffs.",
                source_path: "/Users/dev/.claude/CLAUDE.md",
                line_number: 4,
              },
            ])
          );
        }
        if (url.includes("/api/traces")) {
          return Promise.resolve(
            // api.traces() returns TraceListResponse { items: Trace[], total_traces: number }
            jsonResponse({
              items: [
                {
                  id: "trace-1",
                  session_id: "run-1",
                  agent: "lemoncrow:code",
                  task: "memory test",
                  status: "success",
                  files_touched: [],
                  tools_called: [],
                  commands_run: [],
                  errors_seen: [],
                  repeated_failures: [],
                  validation_results: [],
                  created_at: "2026-05-08T10:00:00Z",
                },
              ],
              total_traces: 1,
            })
          );
        }
        if (url.includes("/api/v1/memory/blocks?agent_id=lemoncrow%3Acode")) {
          return Promise.resolve(
            jsonResponse([
              {
                id: "mem-1",
                agent_id: "lemoncrow:code",
                label: "working-style",
                value: "Stay concise.",
                limit_chars: 8000,
                description: "core block",
                read_only: false,
                metadata: {},
                pinned: true,
                version: 2,
                created_at: "2026-05-08T09:00:00Z",
                updated_at: "2026-05-08T09:30:00Z",
              },
            ])
          );
        }
        if (url.includes("/api/v1/memory/blocks?agent_id=lc")) {
          return Promise.resolve(jsonResponse([]));
        }
        if (url.includes("/api/v1/memory/passages?agent_id=lemoncrow%3Acode")) {
          return Promise.resolve(jsonResponse([]));
        }
        if (url.includes("/api/v1/memory/passages?agent_id=lc")) {
          return Promise.resolve(
            jsonResponse([
              {
                id: "pas-1",
                agent_id: "lc",
                text: "A useful archived passage.",
                source: "trace",
                source_ref: "https://example.com/source",
                tags: ["memory"],
                created_at: "2026-05-08T08:00:00Z",
              },
            ])
          );
        }
        return Promise.resolve(new Response("not found", { status: 404 }));
      }
    );

    render(
      <MemoryRouter>
        <Memory />
      </MemoryRouter>
    );

    // The default tab lists what each agent's own memory files say, by plain source name.
    expect(await screen.findByRole("radio", { name: "From your agents" })).toBeInTheDocument();
    expect(await screen.findByText("Global CLAUDE.md")).toBeInTheDocument();
    expect(screen.getByText("Prefer small, reviewable diffs.")).toBeInTheDocument();
    expect(screen.queryByText("claude-b")).not.toBeInTheDocument();

    const knowledgeTab = await screen.findByRole("radio", {
      name: /Lessons/i,
    });
    await userEvent.click(knowledgeTab);

    expect(await screen.findByText("Core blocks")).toBeInTheDocument();
    expect(await screen.findByText("working-style")).toBeInTheDocument();
    expect(
      await screen.findByText("Recent archived passages")
    ).toBeInTheDocument();
    expect(
      await screen.findByText("A useful archived passage.")
    ).toBeInTheDocument();
  });

  it("names every source kind the adapters write and passes unknown ones through", () => {
    for (const kind of ["claude-md-global", "claude-md", "auto-memory", "codex-mem", "gemini-md-global", "gemini-md-project", "gemini-md-subdir"]) {
      expect(factSourceLabel(kind)).not.toBe(kind);
    }
    expect(factSourceLabel("cursor-rules")).toBe("cursor-rules");
  });
});
