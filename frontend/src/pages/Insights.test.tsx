import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Insights from "./Insights";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const config = {
  remote_enabled: true,
  lexical_frustration_enabled: true,
  posthog_key: "ph_test",
  posthog_host: "https://example.test",
  anon_id: "00000000-0000-4000-8000-000000000000",
  acknowledged: true,
  service_version: "0.1.0",
};

const schema = {
  events: {
    session_start: {
      props: ["agent_host", "session_id"],
      example: {
        agent_host: "cli",
        session_id: "00000000-0000-4000-8000-000000000000",
      },
    },
    cli_command_invoked: {
      props: ["command_name", "session_id", "anon_id"],
      example: { command_name: "context" },
    },
  },
  buckets: {},
};

const summary = {
  events_total: 2,
  event_counts: { session_start: 1, cli_command_invoked: 1 },
  commands_by_day: [{ day: "2026-05-06", count: 1 }],
  top_commands: [{ name: "context", count: 1 }],
  agent_hosts: [{ name: "cli", count: 1 }],
  top_reasonblocks: [
    { block_id_hash: "sha256:abc", count: 2, domain: "coding" },
  ],
  retrieval_score_distribution: [{ name: "0.75-1.0", count: 2 }],
  plan_checks: { plan_check_blocked: 1 },
  frustration_behavioral: [{ name: "loop_detected", count: 1 }],
  frustration_lexical: [{ name: "correction", count: 1 }],
  value_estimate: {
    tokens_saved_estimate: 1200,
    cache_hits: 3,
    blocks_applied: 2,
  },
};

describe("Insights page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders local event stream, rollups, and privacy schema", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/telemetry/config"))
          return Promise.resolve(jsonResponse(config));
        if (url.includes("/api/telemetry/local")) {
          return Promise.resolve(
            jsonResponse({
              events: [
                {
                  id: 1,
                  ts: 1778083200,
                  event: "cli_command_invoked",
                  session_id: "s1",
                  props: {
                    command_name: "context",
                    session_id: "s1",
                    anon_id: "anon",
                  },
                  exported: false,
                },
              ],
            })
          );
        }
        if (url.includes("/api/telemetry/summary"))
          return Promise.resolve(jsonResponse(summary));
        if (url.includes("/api/telemetry/schema"))
          return Promise.resolve(jsonResponse(schema));
        return Promise.resolve(new Response("not found", { status: 404 }));
      }
    );

    render(<Insights />);

    expect(await screen.findByText("2 local events")).toBeInTheDocument();
    expect(await screen.findByText("Live Event Stream")).toBeInTheDocument();
    expect(await screen.findByText("cli_command_invoked")).toBeInTheDocument();
    expect(await screen.findByText("Privacy Audit")).toBeInTheDocument();
    expect(await screen.findByText("session_start")).toBeInTheDocument();
  });

  it("updates remote telemetry toggle", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/telemetry/config") && init?.method === "POST") {
          return Promise.resolve(
            jsonResponse({ ...config, remote_enabled: false })
          );
        }
        if (url.includes("/api/telemetry/config"))
          return Promise.resolve(jsonResponse(config));
        if (url.includes("/api/telemetry/local"))
          return Promise.resolve(jsonResponse({ events: [] }));
        if (url.includes("/api/telemetry/summary"))
          return Promise.resolve(jsonResponse(summary));
        if (url.includes("/api/telemetry/schema"))
          return Promise.resolve(jsonResponse(schema));
        return Promise.resolve(new Response("not found", { status: 404 }));
      }
    );

    render(<Insights />);
    const toggle = await screen.findByRole("checkbox", {
      name: "Remote telemetry",
    });
    await userEvent.click(toggle);

    await waitFor(() => expect(toggle).not.toBeChecked());
  });
});
