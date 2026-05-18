import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import Sessions from "./Sessions";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Traces page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("searches all sessions in place and updates the visible session cards", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input: RequestInfo | URL) => {
        const url = String(input);

        if (url.includes("/api/traces")) {
          const hasQuery = url.includes("query=timeout");
          return Promise.resolve(
            jsonResponse({
              items: [
                {
                  id: hasQuery ? "trace-timeout" : "trace-base",
                  session_id: hasQuery ? "run-timeout" : "run-base",
                  agent: "copilot",
                  host: "copilot",
                  domain: "coding",
                  task:
                    hasQuery ? "Search shell timeout" : "Baseline session",
                  status: "failed",
                  files_touched: [],
                  tools_called: [],
                  commands_run: [],
                  errors_seen: [],
                  repeated_failures: [],
                  validation_results: [],
                  created_at: "2026-05-12T00:00:00Z",
                  snippets: hasQuery
                    ? [
                        "Commands: tail deploy.log ... [[timeout]] while waiting",
                      ]
                    : [],
                },
              ],
              metrics: {
                stats: {
                  total: hasQuery ? 1 : 2,
                  success: 1,
                  failed: 1,
                  partial: 0,
                },
                hosts: ["copilot"],
                domains: ["coding"],
              },
            })
          );
        }

        return Promise.resolve(new Response("not found", { status: 404 }));
      });

    render(
      <MemoryRouter initialEntries={["/sessions"]}>
        <Routes>
          <Route path="/sessions" element={<Sessions />} />
        </Routes>
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByText("Baseline session")).toBeInTheDocument();
    });

    await user.type(
      screen.getByPlaceholderText(/Search sessions, tasks, models/i),
      "timeout"
    );

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("query=timeout")
        )
      ).toBe(true);
    });

    expect(
      await screen.findByText((_, element) =>
        element?.textContent === "Search shell timeout"
      )
    ).toBeInTheDocument();
    expect(screen.queryByText("Baseline session")).not.toBeInTheDocument();
    expect(screen.getByText(/Select History/i)).toBeInTheDocument();
  });

  it("does not leak unrelated snippets into the current search results", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);

        if (url.includes("/api/traces")) {
          const params = new URL(url, "http://localhost").searchParams;
          const query = params.get("query");
          return Promise.resolve(
            jsonResponse({
              items: [
                {
                  id: query ? "trace-sidecar" : "trace-base",
                  session_id: query ? "run-sidecar" : "run-base",
                  agent: "codex",
                  host: "codex",
                  domain: "coding",
                  task: query ? "Investigate sidecar session" : "Base session",
                  status: "success",
                  files_touched: [],
                  tools_called: [],
                  commands_run: [],
                  errors_seen: [],
                  repeated_failures: [],
                  validation_results: [],
                  created_at: "2026-05-12T00:00:00Z",
                  snippets: query
                    ? [
                        "Tools: [[shopify]] sync service skills",
                        "Commands: inspect [[sidecar]] process logs",
                      ]
                    : [],
                },
              ],
              metrics: {
                stats: {
                  total: 1,
                  success: 1,
                  failed: 0,
                  partial: 0,
                },
                hosts: ["codex"],
                domains: ["coding"],
              },
            })
          );
        }

        return Promise.resolve(new Response("not found", { status: 404 }));
      }
    );

    render(
      <MemoryRouter initialEntries={["/sessions"]}>
        <Routes>
          <Route path="/sessions" element={<Sessions />} />
        </Routes>
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByText("Base session")).toBeInTheDocument();
    });

    await user.type(
      screen.getByPlaceholderText(/Search sessions, tasks, models/i),
      "sidecar"
    );

    expect(
      await screen.findByText(
        (_, element) => element?.textContent === "Investigate sidecar session"
      )
    ).toBeInTheDocument();
    expect(screen.queryByText("Base session")).not.toBeInTheDocument();
    expect(
      screen.queryByText((_, element) =>
        element?.textContent?.includes("shopify") ?? false
      )
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText((_, element) =>
        element?.textContent?.includes("inspect") ?? false
      )
    ).not.toBeInTheDocument();
  });
});
