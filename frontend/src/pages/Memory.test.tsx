import { render, screen } from "@testing-library/react";
import Memory from "./Memory";

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
        if (url.includes("/api/traces")) {
          return Promise.resolve(
            jsonResponse([
              {
                id: "trace-1",
                session_id: "run-1",
                agent: "atelier:code",
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
            ])
          );
        }
        if (url.includes("/api/v1/memory/blocks?agent_id=atelier%3Acode")) {
          return Promise.resolve(
            jsonResponse([
              {
                id: "mem-1",
                agent_id: "atelier:code",
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
        if (url.includes("/api/v1/memory/blocks?agent_id=atelier")) {
          return Promise.resolve(jsonResponse([]));
        }
        if (url.includes("/api/v1/memory/passages?agent_id=atelier%3Acode")) {
          return Promise.resolve(jsonResponse([]));
        }
        if (url.includes("/api/v1/memory/passages?agent_id=atelier")) {
          return Promise.resolve(
            jsonResponse([
              {
                id: "pas-1",
                agent_id: "atelier",
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

    render(<Memory />);

    expect(await screen.findByText("Core blocks")).toBeInTheDocument();
    expect(await screen.findByText("working-style")).toBeInTheDocument();
    expect(
      await screen.findByText("claude · atelier:code")
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Recent archived passages")
    ).toBeInTheDocument();
    expect(
      await screen.findByText("A useful archived passage.")
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Filter memory by agent")
    ).not.toBeInTheDocument();
  });
});
