import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Navigate, Route, Routes } from "react-router-dom";
import System, { agentSummary } from "./System";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockSystemApis() {
  vi.spyOn(globalThis, "fetch").mockImplementation(
    (input: RequestInfo | URL) => {
      const url = String(input);

      if (url.includes("/api/health")) {
        return Promise.resolve(
          jsonResponse({ status: "ok", timestamp: "2026-05-08T09:00:00Z" })
        );
      }
      if (url.includes("/api/telemetry/config")) {
        return Promise.resolve(
          jsonResponse({
            remote_enabled: false,
            lexical_frustration_enabled: false,
            posthog_key: "",
            posthog_host: "",
            anon_id: "test",
            acknowledged: true,
            service_version: "test",
            dev_mode: false,
          })
        );
      }
      if (url.includes("/api/hosts")) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.includes("/api/agents")) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.includes("/api/skills")) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.includes("/api/mcp/status")) {
        return Promise.resolve(
          jsonResponse([
            {
              tool_name: "code",
              available: true,
              description: "Code intel",
              mode: "active",
              enum_params: [
                {
                  name: "op",
                  options: ["context", "search", "node"],
                  description: "Operation to perform.",
                },
              ],
            },
          ])
        );
      }

      return Promise.resolve(new Response("not found", { status: 404 }));
    }
  );
}

describe("agentSummary", () => {
  it("turns model routing text into a one-line summary", () => {
    expect(agentSummary("Always use for read-only implementation planner. Returns a plan.")).toBe(
      "Read-only implementation planner."
    );
    expect(agentSummary("Always use for fast read-only explore agent for locating code.")).toBe(
      "Fast read-only explore agent for locating code."
    );
    expect(agentSummary("Reviews diffs for bugs.")).toBe("Reviews diffs for bugs.");
  });
});

describe("System page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("redirects /system to the health section", async () => {
    mockSystemApis();

    render(
      <MemoryRouter initialEntries={["/system"]}>
        <Routes>
          <Route
            path="/system"
            element={<Navigate to="/system/health" replace />}
          />
          <Route path="/system/:section" element={<System />} />
        </Routes>
      </MemoryRouter>
    );

    expect(await screen.findByRole("heading", { name: "Diagnostics" })).toBeInTheDocument();
    expect(await screen.findByText("Local server")).toBeInTheDocument();
    expect(await screen.findByText("Running")).toBeInTheDocument();
    // Plain words, not the adapter jargon the page used to open with.
    expect(screen.queryByText(/adapter/i)).not.toBeInTheDocument();
  });

  it("sends the unbuilt Watchdogs page back to Advanced", async () => {
    mockSystemApis();

    render(
      <MemoryRouter initialEntries={["/system/watchdogs"]}>
        <Routes>
          <Route path="/system/:section" element={<System />} />
          <Route path="/settings/advanced" element={<p>Advanced settings</p>} />
        </Routes>
      </MemoryRouter>
    );

    expect(await screen.findByText("Advanced settings")).toBeInTheDocument();
    expect(screen.queryByText("Coming soon")).not.toBeInTheDocument();
  });

  it("renders the hosts section", async () => {
    mockSystemApis();

    render(
      <MemoryRouter initialEntries={["/system/hosts"]}>
        <Routes>
          <Route path="/system/:section" element={<System />} />
        </Routes>
      </MemoryRouter>
    );

    expect(
      await screen.findByText("No supported hosts found")
    ).toBeInTheDocument();
  });

  it("renders enum params for MCP dispatch tools in the mcp section", async () => {
    mockSystemApis();

    render(
      <MemoryRouter initialEntries={["/system/mcp"]}>
        <Routes>
          <Route path="/system/:section" element={<System />} />
        </Routes>
      </MemoryRouter>
    );

    expect(await screen.findByText("code")).toBeInTheDocument();
    expect(await screen.findByText("3 operations")).toBeInTheDocument();
    expect(screen.queryByText(/stdio/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByText("code"));
    expect(await screen.findByText("enum params")).toBeInTheDocument();
    expect(await screen.findByText("context")).toBeInTheDocument();
    expect(await screen.findByText("search")).toBeInTheDocument();
    expect(await screen.findByText("node")).toBeInTheDocument();
  });
});
