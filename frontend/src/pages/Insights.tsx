import { useEffect, useMemo, useState } from "react";
import {
  getTelemetryEvents,
  getTelemetrySchema,
  getTelemetrySummary,
  type TelemetryEvent,
  type TelemetrySchema,
  type TelemetrySummary,
} from "../lib/insightsApi";
import { MetricCard, SectionHeader } from "../components/WorkbenchUI";

const fmt = new Intl.NumberFormat();

function MiniLine({ data }: { data: Array<{ day: string; count: number }> }) {
  const width = 520;
  const height = 132;
  const pad = 18;
  const max = Math.max(1, ...data.map((item) => item.count));
  const points = data
    .map((item, index) => {
      const x =
        pad + (index * (width - pad * 2)) / Math.max(1, data.length - 1);
      const y = height - pad - (item.count / max) * (height - pad * 2);
      return `${x},${y}`;
    })
    .join(" ");
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="w-full"
      aria-label="commands per day"
    >
      <rect x="0" y="0" width={width} height={height} fill="#0a0a0a" />
      <polyline fill="none" stroke="#22d3ee" strokeWidth="3" points={points} />
      {data.map((item, index) => {
        const x =
          pad + (index * (width - pad * 2)) / Math.max(1, data.length - 1);
        const y = height - pad - (item.count / max) * (height - pad * 2);
        return <circle key={item.day} cx={x} cy={y} r="3" fill="#f59e0b" />;
      })}
    </svg>
  );
}

function Bars({ items }: { items: Array<{ name: string; count: number }> }) {
  const max = Math.max(1, ...items.map((item) => item.count));
  if (items.length === 0) {
    return <div className="text-sm text-neutral-500">No data yet.</div>;
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
              fill="#525252"
            />
          </svg>
        </div>
      ))}
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="border border-neutral-800 bg-neutral-950/70 p-5">
      <h2 className="mb-4 font-mono text-xs uppercase tracking-widest text-neutral-400">
        {title}
      </h2>
      {children}
    </section>
  );
}

export default function Insights() {
  const [events, setEvents] = useState<TelemetryEvent[]>([]);
  const [summary, setSummary] = useState<TelemetrySummary | null>(null);
  const [schema, setSchema] = useState<TelemetrySchema | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    const [nextEvents, nextSummary, nextSchema] = await Promise.all([
      getTelemetryEvents(100),
      getTelemetrySummary(),
      getTelemetrySchema(),
    ]);
    setEvents(nextEvents.events);
    setSummary(nextSummary);
    setSchema(nextSchema);
  };

  useEffect(() => {
    refresh().catch((err: unknown) => setError(String(err)));
    const id = window.setInterval(() => {
      refresh().catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(id);
  }, []);

  const eventJson = useMemo(
    () => events.map((item) => ({ event: item.event, props: item.props })),
    [events]
  );

  if (error) return <div className="text-red-400">Error: {error}</div>;
  if (!summary || !schema) {
    return <div className="text-neutral-500">Loading...</div>;
  }

  return (
    <div className="space-y-6">
      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard
          label="Events"
          value={fmt.format(summary.events_total)}
          detail="Local telemetry currently retained."
          tone="cyan"
        />
        <MetricCard
          label="Commands"
          value={fmt.format(
            summary.top_commands.reduce((acc, item) => acc + item.count, 0)
          )}
          detail="Top command invocations recorded."
          tone="amber"
        />
        <MetricCard
          label="Blocks applied"
          value={fmt.format(summary.value_estimate.blocks_applied)}
          detail="Reasoning value surfaced directly."
          tone="emerald"
        />
        <MetricCard
          label="Plans gated"
          value={fmt.format(summary.plan_checks.plan_check_blocked ?? 0)}
          detail="Verification pressure from the live system."
          tone="violet"
        />
      </section>

      <div className="grid gap-6 xl:grid-cols-[1.2fr_0.8fr]">
        <Section title="Live Event Stream">
          {events.length === 0 ? (
            <div className="text-sm text-neutral-500">
              No local telemetry events yet.
            </div>
          ) : (
            <pre className="max-h-[460px] overflow-auto bg-black p-4 text-xs leading-relaxed text-neutral-300">
              {JSON.stringify(eventJson, null, 2)}
            </pre>
          )}
        </Section>

        <Section title="Usage">
          <div className="space-y-5">
            <MiniLine data={summary.commands_by_day} />
            <div>
              <div className="mb-3 text-xs uppercase tracking-widest text-neutral-500">
                Top commands
              </div>
              <Bars items={summary.top_commands} />
            </div>
            <div>
              <div className="mb-3 text-xs uppercase tracking-widest text-neutral-500">
                Agent hosts
              </div>
              <Bars items={summary.agent_hosts} />
            </div>
          </div>
        </Section>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Section title="Reasoning Value">
          <div className="space-y-5">
            <Bars
              items={summary.top_reasonblocks.map((item) => ({
                name: `${item.block_id_hash} ${item.domain}`.trim(),
                count: item.count,
              }))}
            />
            <Bars items={summary.retrieval_score_distribution} />
          </div>
        </Section>

        <Section title="Frustration Trends">
          <div className="space-y-5">
            <Bars items={summary.frustration_behavioral} />
            <Bars items={summary.frustration_lexical} />
          </div>
        </Section>

        <Section title="Estimated Value">
          <div className="grid gap-3">
            {Object.entries(summary.value_estimate).map(([key, value]) => (
              <div
                key={key}
                className="border border-neutral-800 bg-neutral-950 px-4 py-3"
              >
                <div className="text-[10px] uppercase tracking-widest text-neutral-500">
                  {key.replaceAll("_", " ")}
                </div>
                <div className="mt-1 text-2xl font-semibold text-neutral-100">
                  {fmt.format(value)}
                </div>
              </div>
            ))}
            <div className="border border-neutral-800 bg-neutral-950 px-4 py-3">
              <div className="text-[10px] uppercase tracking-widest text-neutral-500">
                Plans gated
              </div>
              <div className="mt-1 text-2xl font-semibold text-neutral-100">
                {fmt.format(summary.plan_checks.plan_check_blocked ?? 0)}
              </div>
            </div>
          </div>
        </Section>
      </div>

      <Section title="Privacy Audit">
        <SectionHeader
          eyebrow="Schema"
          title="Allowlisted events and example payloads"
          description="This table exists so telemetry stays explainable. Every event type lists exactly which properties are allowed and shows a representative example payload."
        />
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
              {Object.entries(schema.events).map(([event, spec]) => (
                <tr
                  key={event}
                  className="border-b border-neutral-900 align-top"
                >
                  <td className="py-3 pr-4 font-mono text-cyan-300">{event}</td>
                  <td className="py-3 pr-4 text-neutral-300">
                    {spec.props.join(", ")}
                  </td>
                  <td className="py-3">
                    <code className="whitespace-pre-wrap text-xs text-neutral-400">
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
