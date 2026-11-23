import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { TelemetryEvent, TelemetrySummary } from "../lib/insightsApi";

const api = vi.hoisted(() => ({
  getTelemetryEvents: vi.fn(),
  getTelemetrySchema: vi.fn(),
  getTelemetrySummary: vi.fn(),
}));

vi.mock("../lib/insightsApi", () => api);
vi.mock("./TelemetryDestination", () => ({ default: () => null }));

import Telemetry from "./Telemetry";
import { TimeRangeProvider } from "../lib/TimeRangeContext";

const summary: TelemetrySummary = {
  events_total: 1,
  unique_event_types: 1,
  active_sessions: 1,
  first_event_ts: 1_700_000_000,
  last_event_ts: 1_700_000_000,
  event_counts: { session_start: 1 },
  commands_by_day: [],
  top_commands: [],
  agent_hosts: [{ name: "claude", count: 1 }],
  top_playbooks: [],
  retrieval_score_distribution: [],
  plan_checks: {},
  frustration_behavioral: [],
  frustration_lexical: [],
  value_estimate: {
    tokens_saved_estimate: 0,
    cache_hits: 0,
    total_tool_calls: 0,
    cache_hit_rate: 0,
    blocks_applied: 0,
  },
};

function event(id: number): TelemetryEvent {
  return {
    id,
    ts: 1_700_000_000 + id,
    event: "session_start",
    session_id: "s1",
    props: {},
    exported: false,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  api.getTelemetrySchema.mockResolvedValue({ events: {} });
  api.getTelemetrySummary.mockResolvedValue(summary);
  api.getTelemetryEvents.mockResolvedValue({ events: [event(1)] });
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

it("refetches the summary only when the feed changes or it ages out", async () => {
  render(
    <TimeRangeProvider>
      <Telemetry />
    </TimeRangeProvider>
  );
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(api.getTelemetrySummary).toHaveBeenCalledTimes(1);

  await act(() => vi.advanceTimersByTimeAsync(6_000));
  expect(api.getTelemetryEvents.mock.calls.length).toBeGreaterThanOrEqual(4);
  expect(api.getTelemetrySummary).toHaveBeenCalledTimes(1);

  api.getTelemetryEvents.mockResolvedValue({ events: [event(2), event(1)] });
  await act(() => vi.advanceTimersByTimeAsync(2_000));
  expect(api.getTelemetrySummary).toHaveBeenCalledTimes(2);

  await act(() => vi.advanceTimersByTimeAsync(30_000));
  expect(api.getTelemetrySummary).toHaveBeenCalledTimes(3);
});

it("labels every day of a week-long timeline, skipping none", async () => {
  const { axisTicks } = await import("./Telemetry");
  const start = new Date(2026, 10, 9, 15, 30).getTime() / 1000;
  const end = new Date(2026, 10, 16, 15, 30).getTime() / 1000;
  const days = axisTicks(start, end).map((ts) => new Date(ts * 1000).getDate());
  expect(days).toEqual([10, 11, 12, 13, 14, 15, 16]);
  // A short range keeps evenly spaced time labels.
  expect(axisTicks(0, 3600)).toHaveLength(7);
});
