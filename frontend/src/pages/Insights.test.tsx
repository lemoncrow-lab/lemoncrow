import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Insights from "./Insights";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

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
  unique_event_types: 2,
  active_sessions: 1,
  first_event_ts: 1778083200,
  last_event_ts: 1778083300,
  event_counts: { session_start: 1, cli_command_invoked: 1 },
  commands_by_day: [{ day: "2026-05-06", count: 1 }],
  top_commands: [{ name: "reasoning", count: 1 }],
  agent_hosts: [{ name: "cli", count: 2 }],
  top_reasonblocks: [],
  retrieval_score_distribution: [],
  plan_checks: {},
  frustration_behavioral: [],
  frustration_lexical: [],
  value_estimate: {
    tokens_saved_estimate: 0,
    cache_hits: 0,
    blocks_applied: 0,
  },
};

const localEvents = [
  {
    id: 2,
    ts: 1778083300,
    event: "cli_command_invoked",
    session_id: "s1",
    props: {
      command_name: "reasoning",
      session_id: "s1",
      anon_id: "anon",
    },
    exported: false,
  },
  {
    id: 1,
    ts: 1778083200,
    event: "session_start",
    session_id: "s1",
    props: {
      agent_host: "cli",
      atelier_version: "0.1.0",
      os: "linux",
      py_version: "3.13.0",
      anon_id: "anon",
      session_id: "s1",
    },
    exported: false,
  },
];

describe("Insights page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the live telemetry dashboard and expandable event rows", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/api/telemetry/local")) {
          return Promise.resolve(
            jsonResponse({
              events: localEvents,
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

    expect(await screen.findByText("Telemetry Live View")).toBeInTheDocument();
    expect(await screen.findByText("Live Timeline")).toBeInTheDocument();
    expect(await screen.findByText("Commands & Tools")).toBeInTheDocument();
    expect(await screen.findByText("Recent Events")).toBeInTheDocument();
    expect(await screen.findByText("Privacy Audit")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Telemetry Live View hint")
    ).toBeInTheDocument();
    expect(
      screen.queryByText(
        /A sliding telemetry window that updates every two seconds/i
      )
    ).not.toBeInTheDocument();

    const recentEventsSection = screen
      .getByText("Recent Events")
      .closest("section") as HTMLElement;
    expect(
      within(recentEventsSection).getByText("reasoning")
    ).toBeInTheDocument();
    const firstSummary = recentEventsSection.querySelector("summary");
    expect(firstSummary).not.toBeNull();
    await userEvent.click(firstSummary as HTMLElement);

    expect(
      within(recentEventsSection).getByText(/"command_name": "reasoning"/)
    ).toBeInTheDocument();
  });

  it("refetches telemetry when the host filter changes", async () => {
    const urls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        urls.push(url);
        if (url.includes("/api/telemetry/local"))
          return Promise.resolve(jsonResponse({ events: localEvents }));
        if (url.includes("/api/telemetry/summary"))
          return Promise.resolve(jsonResponse(summary));
        if (url.includes("/api/telemetry/schema"))
          return Promise.resolve(jsonResponse(schema));
        return Promise.resolve(new Response("not found", { status: 404 }));
      }
    );

    render(<Insights />);
    const hostSelect = await screen.findByLabelText("Host");
    await userEvent.selectOptions(hostSelect, "cli");

    await waitFor(() => {
      expect(
        urls.some(
          (url) =>
            url.includes("/api/telemetry/local?") && url.includes("host=cli")
        )
      ).toBe(true);
      expect(
        urls.some(
          (url) =>
            url.includes("/api/telemetry/summary?") && url.includes("host=cli")
        )
      ).toBe(true);
    });
  });
});
