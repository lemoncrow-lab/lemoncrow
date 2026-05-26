import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import System, { SystemHosts, SystemMcp } from "./System";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockSystemApis() {
  vi.spyOn(globalThis, "fetch").mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);

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
  });
}

describe("System pages", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("redirects /system to the hosts page", async () => {
    mockSystemApis();

    render(
      <MemoryRouter initialEntries={["/system"]}>
        <Routes>
          <Route path="/system" element={<System />} />
          <Route path="/system/hosts" element={<SystemHosts />} />
        </Routes>
      </MemoryRouter>
    );

    expect(
      await screen.findByRole("heading", { name: "Host adapters" })
    ).toBeInTheDocument();
  });

  it("redirects legacy system tab links to dedicated routes", async () => {
    mockSystemApis();

    render(
      <MemoryRouter initialEntries={["/system?tab=hosts"]}>
        <Routes>
          <Route path="/system" element={<System />} />
          <Route path="/system/hosts" element={<SystemHosts />} />
        </Routes>
      </MemoryRouter>
    );

    expect(
      await screen.findByRole("heading", { name: "Host adapters" })
    ).toBeInTheDocument();
  });

  it("renders enum params for MCP dispatch tools", async () => {
    mockSystemApis();

    render(
      <MemoryRouter>
        <SystemMcp />
      </MemoryRouter>
    );

    expect(await screen.findByText("code")).toBeInTheDocument();
    expect(await screen.findByText("3 ops")).toBeInTheDocument();
    await userEvent.click(screen.getByText("code"));
    expect(await screen.findByText("enum params")).toBeInTheDocument();
    expect(await screen.findByText("context")).toBeInTheDocument();
    expect(await screen.findByText("search")).toBeInTheDocument();
    expect(await screen.findByText("node")).toBeInTheDocument();
  });
});
