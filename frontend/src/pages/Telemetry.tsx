import { useEffect, useMemo, useState } from "react";
import {
  getTelemetryEvents,
  getTelemetrySchema,
  getTelemetrySummary,
  type TelemetryEvent,
  type TelemetryQuery,
  type TelemetrySchema,
  type TelemetrySummary,
} from "../lib/insightsApi";
import { Chip, MetricCard, cx } from "../components/WorkbenchUI";
import { useTimeRange } from "../lib/TimeRangeContext";

const RECENT_EVENT_LIMIT = 20;
const TIMELINE_LIMIT = 5000;
const POLL_INTERVAL_MS = 2000;
const TIMELINE_WIDTH = 1120;
const TIMELINE_PAD_LEFT = 196;
const TIMELINE_PAD_RIGHT = 24;
const TIMELINE_PAD_TOP = 24;
const TIMELINE_PAD_BOTTOM = 36;
const EVENT_COLORS = [
  "#22d3ee",
  "#f59e0b",
  "#10b981",
  "#a78bfa",
  "#f472b6",
  "#fb7185",
  "#60a5fa",
  "#34d399",
  "#f97316",
  "#c084fc",
  "#facc15",
  "#2dd4bf",
];

const TIME_FORMAT = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

const DAY_TIME_FORMAT = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

const DAY_FORMAT = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
});

const fmt = new Intl.NumberFormat();

type MetricTone = Parameters<typeof MetricCard>[0]["tone"];

interface TimelinePoint {
  id: string;
  ts: number;
  lane: string;
  color: string;
  tooltip: string;
}

function HintButton({ label, hint }: { label: string; hint: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={hint}
      className="inline-flex h-5 w-5 items-center justify-center rounded-full border border-neutral-800 bg-neutral-950 text-[10px] font-mono text-neutral-500 transition hover:border-cyan-500/60 hover:text-cyan-300"
    >
      ?
    </button>
  );
}

function Section({
  title,
  hint,
  action,
  children,
}: {
  title: string;
  hint?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="border border-neutral-800 bg-neutral-950/70 p-5">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold text-neutral-100">{title}</h2>
          {hint && <HintButton label={`${title} hint`} hint={hint} />}
        </div>
        {action}
      </div>
      <div className="mt-5">{children}</div>
    </section>
  );
}

function MetricTile({
  label,
  value,
  tone = "neutral",
  hint,
}: {
  label: string;
  value: string;
  tone?: MetricTone;
  hint?: string;
}) {
  return (
    <div className="relative">
      {hint && (
        <div className="absolute right-3 top-3 z-10">
          <HintButton label={`${label} hint`} hint={hint} />
        </div>
      )}
      <MetricCard label={label} value={value} tone={tone} />
    </div>
  );
}

function FilterSelect({
  label,
  value,
  onChange,
  children,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  children: React.ReactNode;
}) {
  return (
    <label className="grid gap-2 text-[10px] font-mono uppercase tracking-[0.22em] text-neutral-500">
      <span>{label}</span>
      <select
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="min-w-[148px] border border-neutral-800 bg-neutral-950 px-3 py-2 text-sm normal-case tracking-normal text-neutral-100 outline-none transition hover:border-neutral-700 focus:border-cyan-500/70"
      >
        {children}
      </select>
    </label>
  );
}

function CountBars({
  items,
  emptyLabel,
}: {
  items: Array<{ name: string; count: number }>;
  emptyLabel: string;
}) {
  const max = Math.max(1, ...items.map((item) => item.count));
  if (items.length === 0) {
    return <div className="text-sm text-neutral-500">{emptyLabel}</div>;
  }
  return (
    <div className="space-y-3">
      {items.slice(0, 8).map((item) => (
        <div key={item.name}>
          <div className="mb-1 flex justify-between gap-3 text-xs text-neutral-400">
            <span className="truncate">{item.name}</span>
            <span>{fmt.format(item.count)}</span>
          </div>
          <svg viewBox="0 0 100 8" className="h-2 w-full" aria-hidden="true">
            <rect x="0" y="0" width="100" height="8" fill="#171717" />
            <rect
              x="0"
              y="0"
              width={Math.max(4, (item.count / max) * 100)}
              height="8"
              fill="#22d3ee"
            />
          </svg>
        </div>
      ))}
    </div>
  );
}

function formatRelative(ts: number): string {
  const delta = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

function formatTimestamp(ts: number, rangeSeconds?: number): string {
  const date = new Date(ts * 1000);
  if ((rangeSeconds ?? 0) > 86400) {
    return DAY_TIME_FORMAT.format(date);
  }
  return TIME_FORMAT.format(date);
}

function formatAxisTick(ts: number, rangeSeconds: number): string {
  const date = new Date(ts * 1000);
  if (rangeSeconds <= 7200) return TIME_FORMAT.format(date);
  if (rangeSeconds <= 172800) return DAY_TIME_FORMAT.format(date);
  return DAY_FORMAT.format(date);
}

function buildQuery(
  windowSeconds: number | null,
  host: string,
  event: string
): Omit<TelemetryQuery, "limit"> {
  const query: Omit<TelemetryQuery, "limit"> = {};
  if (windowSeconds !== null) {
    query.since = Date.now() / 1000 - windowSeconds;
  }
  if (host !== "all") {
    query.host = host;
  }
  if (event !== "all") {
    query.event = event;
  }
  return query;
}

function TimelineChart({
  points,
  rangeStart,
  rangeEnd,
  ariaLabel,
  emptyLabel,
}: {
  points: TimelinePoint[];
  rangeStart: number;
  rangeEnd: number;
  ariaLabel: string;
  emptyLabel: string;
}) {
  if (points.length === 0) {
    return (
      <div className="flex min-h-[280px] items-center justify-center border border-dashed border-neutral-800 bg-black/20 text-sm text-neutral-500">
        {emptyLabel}
      </div>
    );
  }

  const counts = new Map<string, number>();
  for (const point of points) {
    counts.set(point.lane, (counts.get(point.lane) ?? 0) + 1);
  }
  const eventTypes = Array.from(counts.entries())
    .sort(
      (left, right) => right[1] - left[1] || left[0].localeCompare(right[0])
    )
    .map(([name]) => name);
  const laneHeight = 36;
  const height =
    TIMELINE_PAD_TOP +
    TIMELINE_PAD_BOTTOM +
    Math.max(1, eventTypes.length) * laneHeight;
  const chartWidth = TIMELINE_WIDTH - TIMELINE_PAD_LEFT - TIMELINE_PAD_RIGHT;
  const rangeSeconds = Math.max(1, rangeEnd - rangeStart);
  const ticks = 6;

  const xFor = (ts: number) =>
    TIMELINE_PAD_LEFT +
    ((Math.max(rangeStart, Math.min(rangeEnd, ts)) - rangeStart) /
      rangeSeconds) *
      chartWidth;

  const yFor = (index: number) =>
    TIMELINE_PAD_TOP + index * laneHeight + laneHeight / 2;

  return (
    <div className="overflow-x-auto border border-neutral-800 bg-black/35">
      <svg
        viewBox={`0 0 ${TIMELINE_WIDTH} ${height}`}
        className="min-w-[820px] w-full"
        role="img"
        aria-label={ariaLabel}
      >
        <rect
          x="0"
          y="0"
          width={TIMELINE_WIDTH}
          height={height}
          fill="#050505"
        />
        {eventTypes.map((eventName, index) => {
          const y = yFor(index);
          return (
            <g key={eventName}>
              <line
                x1={TIMELINE_PAD_LEFT}
                x2={TIMELINE_WIDTH - TIMELINE_PAD_RIGHT}
                y1={y}
                y2={y}
                stroke="#1f1f1f"
                strokeWidth="1"
              />
              <text
                x={TIMELINE_PAD_LEFT - 12}
                y={y + 4}
                fill="#a3a3a3"
                fontSize="11"
                textAnchor="end"
              >
                {eventName}
              </text>
              <text
                x={TIMELINE_WIDTH - TIMELINE_PAD_RIGHT + 8}
                y={y + 4}
                fill="#737373"
                fontSize="11"
              >
                {counts.get(eventName)}
              </text>
            </g>
          );
        })}

        {Array.from({ length: ticks + 1 }, (_, index) => {
          const ratio = index / ticks;
          const x = TIMELINE_PAD_LEFT + ratio * chartWidth;
          const tickTs = rangeStart + ratio * rangeSeconds;
          return (
            <g key={index}>
              <line
                x1={x}
                x2={x}
                y1={TIMELINE_PAD_TOP - 8}
                y2={height - TIMELINE_PAD_BOTTOM + 4}
                stroke="#171717"
                strokeDasharray="4 6"
              />
              <text
                x={x}
                y={height - 10}
                fill="#737373"
                fontSize="10"
                textAnchor="middle"
              >
                {formatAxisTick(tickTs, rangeSeconds)}
              </text>
            </g>
          );
        })}

        {points.map((point) => {
          const index = eventTypes.indexOf(point.lane);
          const x = xFor(point.ts);
          const y = yFor(index);
          return (
            <circle
              key={point.id}
              cx={x}
              cy={y}
              r="5"
              fill={point.color}
              stroke="#050505"
              strokeWidth="1.5"
              opacity="0.92"
            >
              <title>{point.tooltip}</title>
            </circle>
          );
        })}

        <line
          x1={TIMELINE_PAD_LEFT}
          x2={TIMELINE_PAD_LEFT}
          y1={TIMELINE_PAD_TOP - 8}
          y2={height - TIMELINE_PAD_BOTTOM + 4}
          stroke="#3f3f46"
          strokeWidth="1"
        />
      </svg>
    </div>
  );
}

function EventRow({
  item,
  color,
  rangeSeconds,
}: {
  item: TelemetryEvent;
  color: string;
  rangeSeconds: number | null;
}) {
  const commandName =
    typeof item.props.command_name === "string"
      ? item.props.command_name
      : null;
  const hostName =
    typeof item.props.agent_host === "string" ? item.props.agent_host : null;

  return (
    <details className="border border-neutral-800 bg-black/20 transition hover:border-neutral-700">
      <summary className="flex cursor-pointer list-none items-start justify-between gap-4 px-4 py-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <svg
              className="mt-0.5 h-2.5 w-2.5"
              viewBox="0 0 10 10"
              aria-hidden="true"
            >
              <circle cx="5" cy="5" r="5" fill={color} />
            </svg>
            <span className="font-mono text-xs uppercase tracking-widest text-neutral-100">
              {item.event}
            </span>
            {commandName && <Chip tone="amber">{commandName}</Chip>}
            {hostName && <Chip tone="cyan">{hostName}</Chip>}
            {item.exported && <Chip tone="emerald">Exported</Chip>}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-neutral-500">
            <span>{formatTimestamp(item.ts, rangeSeconds ?? undefined)}</span>
            <span>{formatRelative(item.ts)}</span>
            {item.session_id && <span>session {item.session_id}</span>}
          </div>
        </div>
        <span className="pt-0.5 text-[10px] font-mono uppercase tracking-[0.22em] text-neutral-500">
          JSON
        </span>
      </summary>
      <div className="border-t border-neutral-800 px-4 py-4">
        <pre className="overflow-x-auto bg-neutral-950 p-4 text-xs leading-relaxed text-neutral-300">
          {JSON.stringify(
            {
              id: item.id,
              ts: item.ts,
              event: item.event,
              session_id: item.session_id,
              exported: item.exported,
              props: item.props,
            },
            null,
            2
          )}
        </pre>
      </div>
    </details>
  );
}

function toOperationPoint(
  event: TelemetryEvent,
  rangeSeconds: number
): TimelinePoint | null {
  const props = event.props;
  if (typeof props.command_name === "string") {
    return {
      id: `command-${event.id}`,
      ts: event.ts,
      lane: `cmd:${props.command_name}`,
      color: "#f59e0b",
      tooltip: `${event.event} · ${props.command_name} · ${formatTimestamp(event.ts, rangeSeconds)}`,
    };
  }
  if (typeof props.tool_name === "string") {
    return {
      id: `tool-${event.id}`,
      ts: event.ts,
      lane: `tool:${props.tool_name}`,
      color: "#10b981",
      tooltip: `${event.event} · ${props.tool_name} · ${formatTimestamp(event.ts, rangeSeconds)}`,
    };
  }
  if (typeof props.endpoint === "string") {
    return {
      id: `api-${event.id}`,
      ts: event.ts,
      lane: `api:${props.endpoint}`,
      color: "#60a5fa",
      tooltip: `${event.event} · ${props.endpoint} · ${formatTimestamp(event.ts, rangeSeconds)}`,
    };
  }
  return null;
}

export default function Telemetry() {
  const [events, setEvents] = useState<TelemetryEvent[]>([]);
  const [summary, setSummary] = useState<TelemetrySummary | null>(null);
  const [schema, setSchema] = useState<TelemetrySchema | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { seconds: windowSeconds, range } = useTimeRange();
  const [hostFilter, setHostFilter] = useState("all");
  const [eventFilter, setEventFilter] = useState("all");
  const [knownHosts, setKnownHosts] = useState<string[]>([]);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);

  useEffect(() => {
    let active = true;
    getTelemetrySchema()
      .then((payload) => {
        if (!active) return;
        setSchema(payload);
      })
      .catch((err: unknown) => {
        if (!active) return;
        setError(String(err));
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      const query = buildQuery(windowSeconds, hostFilter, eventFilter);
      const [nextEvents, nextSummary] = await Promise.all([
        getTelemetryEvents({ ...query, limit: TIMELINE_LIMIT }),
        getTelemetrySummary(query),
      ]);
      if (!active) return;
      setEvents(nextEvents.events);
      setSummary(nextSummary);
      setLastUpdated(Date.now());
      setError(null);
      setKnownHosts((current) => {
        const merged = new Set(current);
        nextSummary.agent_hosts.forEach((item) => merged.add(item.name));
        if (hostFilter !== "all") merged.add(hostFilter);
        return Array.from(merged).sort((left, right) =>
          left.localeCompare(right)
        );
      });
    };

    void refresh().catch((err: unknown) => {
      if (!active) return;
      setError(String(err));
    });

    const id = window.setInterval(() => {
      void refresh().catch(() => undefined);
    }, POLL_INTERVAL_MS);

    return () => {
      active = false;
      window.clearInterval(id);
    };
  }, [windowSeconds, hostFilter, eventFilter]);

  const rangeEnd = Math.max(Date.now() / 1000, summary?.last_event_ts ?? 0);
  const rangeStart =
    windowSeconds === null
      ? (summary?.first_event_ts ?? Math.max(0, rangeEnd - 300))
      : rangeEnd - windowSeconds;
  const rangeSeconds = windowSeconds ?? Math.max(1, rangeEnd - rangeStart);

  const sortedTimelineEvents = useMemo(
    () => [...events].sort((left, right) => left.ts - right.ts),
    [events]
  );

  const timelinePalette = useMemo(() => {
    const names = Array.from(
      new Set(sortedTimelineEvents.map((item) => item.event))
    ).sort();
    return names.reduce<Record<string, string>>((palette, name, index) => {
      palette[name] = EVENT_COLORS[index % EVENT_COLORS.length];
      return palette;
    }, {});
  }, [sortedTimelineEvents]);

  const telemetryTimelinePoints = useMemo(
    () =>
      sortedTimelineEvents.map((item) => ({
        id: `event-${item.id}`,
        ts: item.ts,
        lane: item.event,
        color: timelinePalette[item.event] ?? EVENT_COLORS[0],
        tooltip: `${item.event} · ${formatTimestamp(item.ts, rangeSeconds)}`,
      })),
    [rangeSeconds, sortedTimelineEvents, timelinePalette]
  );

  const operationTimelinePoints = useMemo(
    () =>
      sortedTimelineEvents
        .map((item) => toOperationPoint(item, rangeSeconds))
        .filter((item): item is TimelinePoint => item !== null),
    [rangeSeconds, sortedTimelineEvents]
  );

  const recentEvents = useMemo(
    () => events.slice(0, RECENT_EVENT_LIMIT),
    [events]
  );

  const eventBreakdown = useMemo(() => {
    if (!summary) return [];
    return Object.entries(summary.event_counts)
      .map(([name, count]) => ({ name, count }))
      .sort(
        (left, right) =>
          right.count - left.count || left.name.localeCompare(right.name)
      );
  }, [summary]);

  const commandEvents =
    (summary?.event_counts.cli_command_invoked ?? 0) +
    (summary?.event_counts.cli_command_completed ?? 0);

  const hostOptions = useMemo(() => {
    const merged = new Set(knownHosts);
    if (hostFilter !== "all") {
      merged.add(hostFilter);
    }
    return Array.from(merged).sort((left, right) => left.localeCompare(right));
  }, [hostFilter, knownHosts]);

  if (error && !summary) {
    return <div className="text-red-400">Error: {error}</div>;
  }

  if (!summary || !schema) {
    return <div className="text-neutral-500">Loading...</div>;
  }

  const liveHint =
    "Filters the telemetry window. Default is the last 5 minutes, and larger windows query retained storage instead of only the sliding live feed.";
  const refreshHint = lastUpdated
    ? `Polling every 2 seconds. Last updated ${TIME_FORMAT.format(new Date(lastUpdated))}.`
    : "Polling every 2 seconds. Waiting for the first refresh.";
  const eventTimelineHint = [
    "X-axis is time and Y-axis is telemetry event type.",
    `Current plotted events: ${fmt.format(telemetryTimelinePoints.length)}.`,
    `Range: ${formatTimestamp(rangeStart, rangeSeconds)} to ${formatTimestamp(rangeEnd, rangeSeconds)}.`,
    Object.entries(timelinePalette).length > 0
      ? `Colors: ${Object.entries(timelinePalette)
          .map(([name, color]) => `${name} ${color}`)
          .join(" · ")}`
      : "",
  ]
    .filter(Boolean)
    .join("\n");
  const operationTimelineHint = [
    "Shows only command, tool, and API activity derived from telemetry props.",
    `Current plotted operations: ${fmt.format(operationTimelinePoints.length)}.`,
    "Amber is command activity, emerald is tool activity, blue is API activity.",
  ].join("\n");

  return (
    <div className="space-y-6">
      <Section
        title="Telemetry Live View"
        hint={liveHint}
        action={
          <div className="flex flex-wrap items-end gap-3">
            <FilterSelect
              label="Host"
              value={hostFilter}
              onChange={setHostFilter}
            >
              <option value="all">All hosts</option>
              {hostOptions.map((host) => (
                <option key={host} value={host}>
                  {host}
                </option>
              ))}
            </FilterSelect>
            <FilterSelect
              label="Event type"
              value={eventFilter}
              onChange={setEventFilter}
            >
              <option value="all">All events</option>
              {Object.keys(schema.events)
                .sort((left, right) => left.localeCompare(right))
                .map((eventName) => (
                  <option key={eventName} value={eventName}>
                    {eventName}
                  </option>
                ))}
            </FilterSelect>
            <div className="flex items-center gap-2 pb-2">
              <Chip tone="emerald">Live</Chip>
              <HintButton label="Refresh status hint" hint={refreshHint} />
            </div>
          </div>
        }
      >
        <div className="grid gap-4 md:grid-cols-4">
          <MetricTile
            label="Events in window"
            value={fmt.format(summary.events_total)}
            tone="cyan"
            hint={
              windowSeconds === null
                ? "All retained telemetry currently in storage."
                : range === "1d"
                  ? "Today so far."
                  : range === "7d"
                    ? "This week so far."
                    : range === "30d"
                      ? "This month so far."
                      : "This quarter so far."
            }
          />
          <MetricTile
            label="Event types"
            value={fmt.format(summary.unique_event_types)}
            tone="amber"
            hint="Distinct telemetry event names currently visible in the selected filter set."
          />
          <MetricTile
            label="Active sessions"
            value={fmt.format(summary.active_sessions)}
            tone="emerald"
            hint="Sessions represented by the currently visible telemetry rows."
          />
          <MetricTile
            label="Command events"
            value={fmt.format(commandEvents)}
            tone="violet"
            hint={
              summary.last_event_ts
                ? `Combined cli command invoke and complete events. Latest event ${formatRelative(summary.last_event_ts)}.`
                : "No recent command events."
            }
          />
        </div>
      </Section>

      {error && (
        <div className="border border-red-900/40 bg-red-950/20 px-4 py-3 text-sm text-red-200">
          Refresh error: {error}
        </div>
      )}

      <Section title="Live Timeline" hint={eventTimelineHint}>
        <TimelineChart
          points={telemetryTimelinePoints}
          rangeStart={rangeStart}
          rangeEnd={rangeEnd}
          ariaLabel="Live telemetry timeline"
          emptyLabel="No events in the selected window."
        />
      </Section>

      <Section title="Commands & Tools" hint={operationTimelineHint}>
        <TimelineChart
          points={operationTimelinePoints}
          rangeStart={rangeStart}
          rangeEnd={rangeEnd}
          ariaLabel="Command and tool activity timeline"
          emptyLabel="No command, tool, or API activity in the selected window."
        />
      </Section>

      <div className="grid gap-6 xl:grid-cols-3">
        <Section title="Event Mix">
          <CountBars items={eventBreakdown} emptyLabel="No event mix yet." />
        </Section>
        <Section title="Top Commands">
          <CountBars
            items={summary.top_commands}
            emptyLabel="No command events in this window."
          />
        </Section>
        <Section title="Hosts In Window">
          <CountBars
            items={summary.agent_hosts}
            emptyLabel="No host data for this selection."
          />
        </Section>
      </div>

      <Section
        title="Recent Events"
        hint="Newest matching rows are shown first. Expand a row only when you want the raw event JSON. The list is capped at the newest 20 rows."
      >
        <div className="space-y-3">
          {recentEvents.length === 0 ? (
            <div className="text-sm text-neutral-500">
              No telemetry rows match the current filters.
            </div>
          ) : (
            recentEvents.map((item) => (
              <EventRow
                key={item.id}
                item={item}
                color={timelinePalette[item.event] ?? EVENT_COLORS[0]}
                rangeSeconds={windowSeconds}
              />
            ))
          )}
        </div>
      </Section>

      <Section
        title="Privacy Audit"
        hint="Allowlisted telemetry properties and example payloads for each event type."
      >
        <div className="overflow-auto">
          <table className="min-w-full border-collapse text-left text-sm">
            <thead>
              <tr className="border-b border-neutral-800 text-xs uppercase tracking-widest text-neutral-500">
                <th className="py-2 pr-4">Event</th>
                <th className="py-2 pr-4">Allowlisted properties</th>
                <th className="py-2">Example payload</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(schema.events).map(([eventName, spec]) => (
                <tr
                  key={eventName}
                  className="border-b border-neutral-900 align-top"
                >
                  <td className="py-3 pr-4 font-mono text-cyan-300">
                    {eventName}
                  </td>
                  <td className="py-3 pr-4 text-neutral-300">
                    {spec.props.join(", ")}
                  </td>
                  <td className="py-3">
                    <code
                      className={cx(
                        "whitespace-pre-wrap text-xs text-neutral-400"
                      )}
                    >
                      {JSON.stringify(spec.example)}
                    </code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}
