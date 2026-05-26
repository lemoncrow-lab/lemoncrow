import { type ReactNode, useEffect, useState } from "react";
import type {
  AnalyticsDashboard,
  InsightsWindow,
  InsightsSessionSummary,
  OverviewStats,
  SavingsSummaryV2,
  TraceListResponse,
} from "../api";
import { api } from "../api";
import { Card, SectionHeader } from "../components/WorkbenchUI";
import { getTelemetrySummary, type TelemetrySummary } from "../lib/insightsApi";
import { useTimeRange } from "../lib/TimeRangeContext";

const fmt = new Intl.NumberFormat();
const usdFmt = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

const usd = (n: number) => usdFmt.format(n);
const pct = (n: number) => `${n.toFixed(1)}%`;

type OverviewTone =
  | "amber"
  | "cyan"
  | "emerald"
  | "violet"
  | "neutral"
  | "red"
  | "purple";

interface OverviewData {
  stats: OverviewStats | null;
  traces: TraceListResponse | null;
  savings: SavingsSummaryV2 | null;
  analytics: AnalyticsDashboard | null;
  telemetry: TelemetrySummary | null;
  insights: InsightsWindow | null;
}

interface SnapshotChipData {
  label: string;
  value: string;
  detail: string;
  tone: OverviewTone;
}

interface InsightRow {
  label: string;
  value: string;
  detail?: string;
}

interface BarSegment {
  label: string;
  value: number;
  color: string;
}

const EMPTY_DATA: OverviewData = {
  stats: null,
  traces: null,
  savings: null,
  analytics: null,
  telemetry: null,
  insights: null,
};

const TONE_STYLES: Record<
  OverviewTone,
  { chip: string; value: string; title: string }
> = {
  amber: {
    chip: "border-amber-900/40 bg-amber-950/20",
    value: "text-amber-200",
    title: "text-amber-300",
  },
  cyan: {
    chip: "border-cyan-900/40 bg-cyan-950/20",
    value: "text-cyan-100",
    title: "text-cyan-300",
  },
  emerald: {
    chip: "border-emerald-900/40 bg-emerald-950/20",
    value: "text-emerald-100",
    title: "text-emerald-300",
  },
  violet: {
    chip: "border-violet-900/40 bg-violet-950/20",
    value: "text-violet-100",
    title: "text-violet-300",
  },
  neutral: {
    chip: "border-neutral-800 bg-neutral-950/60",
    value: "text-neutral-100",
    title: "text-neutral-100",
  },
  red: {
    chip: "border-red-900/40 bg-red-950/20",
    value: "text-red-100",
    title: "text-red-300",
  },
  purple: {
    chip: "border-purple-900/40 bg-purple-950/20",
    value: "text-purple-100",
    title: "text-purple-300",
  },
};

function formatMetric(
  value: number | null | undefined,
  formatter: (value: number) => string = (input) => fmt.format(input)
): string {
  if (value == null || !Number.isFinite(value)) {
    return "…";
  }
  return formatter(value);
}

function formatShare(
  value: number | null | undefined,
  total: number | null | undefined
) {
  if (value == null || total == null || total <= 0) {
    return undefined;
  }
  return pct((value / total) * 100);
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

function SnapshotChip({ label, value, detail, tone }: SnapshotChipData) {
  const palette = TONE_STYLES[tone];

  return (
    <div className={`border px-3 py-3 ${palette.chip}`}>
      <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
        {label}
      </div>
      <div className={`mt-2 text-xl font-semibold ${palette.value}`}>
        {value}
      </div>
      <div className="mt-1 text-xs leading-relaxed text-neutral-500">
        {detail}
      </div>
    </div>
  );
}

function StackedBar({
  segments,
  emptyMessage = "No breakdown available yet.",
}: {
  segments: BarSegment[];
  emptyMessage?: string;
}) {
  const total = segments.reduce((sum, segment) => sum + segment.value, 0);

  if (!total) {
    return (
      <div className="text-xs italic text-neutral-600">{emptyMessage}</div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex h-2 overflow-hidden rounded-full bg-neutral-900">
        {segments.map((segment, index) => (
          <div
            key={`${segment.label}:${index}`}
            className={segment.color}
            style={{ width: `${(segment.value / total) * 100}%` }}
            title={`${segment.label}: ${fmt.format(segment.value)} (${pct(
              (segment.value / total) * 100
            )})`}
          />
        ))}
      </div>
      <div className="grid gap-2 md:grid-cols-3">
        {segments.map((segment, index) => (
          <div
            key={`${segment.label}:legend:${index}`}
            className="border border-neutral-900 bg-black/20 p-2"
          >
            <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-neutral-500">
              <span className={`h-2 w-2 rounded-full ${segment.color}`} />
              {segment.label}
            </div>
            <div className="mt-1 font-mono text-sm text-neutral-100">
              {fmt.format(segment.value)}
            </div>
            <div className="text-[10px] text-neutral-600">
              {pct((segment.value / total) * 100)}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function InsightCard({
  eyebrow,
  title,
  rows,
  footer,
  tone = "neutral",
  children,
}: {
  eyebrow?: string;
  title: string;
  rows: InsightRow[];
  footer?: string;
  tone?: OverviewTone;
  children?: ReactNode;
}) {
  const palette = TONE_STYLES[tone];

  return (
    <section className="border border-neutral-800 bg-neutral-950/60 p-5 space-y-4">
      <div>
        {eyebrow && (
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
            {eyebrow}
          </div>
        )}
        <h3 className={`mt-1 text-lg font-semibold ${palette.title}`}>
          {title}
        </h3>
      </div>

      {children}

      <dl className="grid gap-3 sm:grid-cols-2">
        {rows.map((row) => (
          <div
            key={`${title}:${row.label}`}
            className="border border-neutral-900 bg-black/20 p-3"
          >
            <dt className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              {row.label}
            </dt>
            <dd className="mt-1 text-base font-semibold text-neutral-100">
              {row.value}
            </dd>
            {row.detail && (
              <div className="mt-1 text-xs text-neutral-500">{row.detail}</div>
            )}
          </div>
        ))}
      </dl>

      {footer && (
        <div className="border-t border-neutral-800 pt-3 text-xs leading-relaxed text-neutral-500">
          {footer}
        </div>
      )}
    </section>
  );
}

export default function Overview() {
  const [data, setData] = useState<OverviewData>(EMPTY_DATA);
  const [err, setErr] = useState<string | null>(null);
  const { days, seconds, range } = useTimeRange();

  useEffect(() => {
    let active = true;

    void Promise.allSettled([
      api.overview(days),
      api.traces(1, 0, undefined, undefined, undefined, days),
      api.savingsSummary(days),
      api.analyticsDashboard(days),
      getTelemetrySummary({ since: Date.now() / 1000 - seconds }),
      api.insightsWindow(range),
    ]).then((results) => {
      if (!active) return;

      const [
        statsResult,
        tracesResult,
        savingsResult,
        analyticsResult,
        telemetryResult,
        insightsResult,
      ] = results;

      const nextData: OverviewData = {
        stats: statsResult.status === "fulfilled" ? statsResult.value : null,
        traces: tracesResult.status === "fulfilled" ? tracesResult.value : null,
        savings:
          savingsResult.status === "fulfilled" ? savingsResult.value : null,
        analytics:
          analyticsResult.status === "fulfilled" ? analyticsResult.value : null,
        telemetry:
          telemetryResult.status === "fulfilled" ? telemetryResult.value : null,
        insights:
          insightsResult.status === "fulfilled" ? insightsResult.value : null,
      };

      setData(nextData);

      const loaded = Object.values(nextData).some(Boolean);
      if (loaded) {
        setErr(null);
        return;
      }

      const firstFailure = results.find(
        (result): result is PromiseRejectedResult =>
          result.status === "rejected"
      );
      setErr(firstFailure ? errorMessage(firstFailure.reason) : "Unavailable");
    });

    return () => {
      active = false;
    };
  }, [days, seconds, range]);

  const runStats = data.traces?.metrics.stats;
  const sessionTotal = runStats?.total ?? data.stats?.total_traces ?? null;
  const sessionSuccessRate =
    runStats && runStats.total > 0
      ? (runStats.success / runStats.total) * 100
      : null;
  const savedTokens =
    data.savings?.total_naive_tokens != null &&
    data.savings?.total_actual_tokens != null
      ? data.savings.total_naive_tokens - data.savings.total_actual_tokens
      : null;
  const analyticsTools = data.analytics
    ? [
        ...data.analytics.tools.core,
        ...data.analytics.tools.shell,
        ...data.analytics.tools.mcp,
      ]
    : [];
  const analyticsSessions = data.analytics
    ? data.analytics.summary.total_sessions
    : null;
  const analyticsSpend = data.analytics
    ? data.analytics.summary.total_cost
    : null;
  const analyticsToolCalls = data.analytics
    ? analyticsTools.reduce((sum, tool) => sum + tool.calls, 0)
    : null;
  const topAnalyticsHost = data.analytics?.by_host[0] ?? null;
  const commandEvents = data.telemetry
    ? (data.telemetry.event_counts.cli_command_invoked ?? 0) +
      (data.telemetry.event_counts.cli_command_completed ?? 0)
    : null;
  const topCommand = data.telemetry?.top_commands[0] ?? null;
  const topTelemetryHost = data.telemetry?.agent_hosts[0] ?? null;
  const topReasonBlock = data.telemetry?.top_reasonblocks[0] ?? null;
  const planChecks = data.telemetry
    ? Object.values(data.telemetry.plan_checks).reduce(
        (sum, value) => sum + value,
        0
      )
    : null;

  const snapshotChips: SnapshotChipData[] = [
    {
      label: "Captured Sessions",
      value: formatMetric(sessionTotal),
      detail:
        sessionSuccessRate != null
          ? `${pct(sessionSuccessRate)} success`
          : "Status pending",
      tone: "cyan",
    },
    {
      label: "Estimated Spend",
      value: formatMetric(data.stats?.estimated_total_cost_usd, usd),
      detail:
        data.stats?.is_estimate === false
          ? "Recorded"
          : "Estimated from tokens",
      tone: "emerald",
    },
    {
      label: `Saved In ${data.savings?.window_days ?? days}d`,
      value:
        data.savings?.saved_usd != null
          ? usd(data.savings.saved_usd)
          : formatMetric(data.stats?.estimated_saved_cost_usd, usd),
      detail:
        savedTokens != null && data.savings?.reduction_pct != null
          ? `${formatMetric(savedTokens)} tokens · ${pct(
              data.savings.reduction_pct
            )}`
          : "Savings pending",
      tone: "amber",
    },
    {
      label: "Coverage",
      value: data.traces ? fmt.format(data.traces.metrics.hosts.length) : "…",
      detail: data.traces
        ? `${fmt.format(data.traces.metrics.domains.length)} domains · ${formatMetric(
            data.stats?.total_blocks
          )} blocks`
        : "Coverage pending",
      tone: "violet",
    },
  ];

  const sessionSegments: BarSegment[] = [
    {
      label: "Successful",
      value: runStats?.success ?? 0,
      color: "bg-emerald-500/70",
    },
    {
      label: "Failed",
      value: runStats?.failed ?? 0,
      color: "bg-amber-500/70",
    },
    {
      label: "Partial",
      value: runStats?.partial ?? 0,
      color: "bg-violet-500/70",
    },
  ].filter((segment) => segment.value > 0);

  const efficiencySegments: BarSegment[] = [
    {
      label: "Actual",
      value: data.savings?.total_actual_tokens ?? 0,
      color: "bg-cyan-500/70",
    },
    {
      label: "Avoided",
      value: savedTokens ?? 0,
      color: "bg-emerald-500/70",
    },
  ].filter((segment) => segment.value > 0);

  const sessionFooterParts: string[] = [];
  if (data.stats?.total_clusters != null) {
    sessionFooterParts.push(
      `${fmt.format(data.stats.total_clusters)} failure clusters tracked`
    );
  }
  if (topTelemetryHost) {
    sessionFooterParts.push(
      `${topTelemetryHost.name} is the busiest recorded host`
    );
  }

  const efficiencyFooterParts: string[] = [];
  if (data.telemetry?.value_estimate.tokens_saved_estimate != null) {
    efficiencyFooterParts.push(
      `${formatMetric(
        data.telemetry.value_estimate.tokens_saved_estimate
      )} tokens saved by telemetry heuristics`
    );
  }
  if (data.telemetry?.value_estimate.cache_hits != null) {
    efficiencyFooterParts.push(
      `${formatMetric(
        data.telemetry.value_estimate.cache_hits
      )} cache hits observed`
    );
  }
  if (data.telemetry?.value_estimate.cache_hit_rate != null) {
    efficiencyFooterParts.push(
      `${formatMetric(
        data.telemetry.value_estimate.cache_hit_rate * 100,
        pct
      )} cache hit rate`
    );
  }
  if (data.telemetry?.value_estimate.blocks_applied != null) {
    efficiencyFooterParts.push(
      `${formatMetric(
        data.telemetry.value_estimate.blocks_applied
      )} blocks applied`
    );
  }

  const coverageFooterParts: string[] = [];
  if (topReasonBlock?.domain) {
    coverageFooterParts.push(
      `Top reusable knowledge domain: ${topReasonBlock.domain}`
    );
  }
  if (planChecks != null) {
    coverageFooterParts.push(`${formatMetric(planChecks)} plan checks logged`);
  }

  const analyticsFooterParts: string[] = [];
  if (data.analytics?.external.runs_total != null) {
    analyticsFooterParts.push(
      `${formatMetric(
        data.analytics.external.runs_total
      )} external snapshots in the last 30 days`
    );
  }
  if (data.analytics?.external.latest.length != null) {
    analyticsFooterParts.push(
      `${formatMetric(
        data.analytics?.external.latest.length
      )} external tools currently represented`
    );
  }

  const telemetryFooterParts: string[] = [];
  if (topCommand) {
    telemetryFooterParts.push(
      `Most common command: ${topCommand.name} (${fmt.format(topCommand.count)})`
    );
  }
  if (topTelemetryHost) {
    telemetryFooterParts.push(
      `Top host signal: ${topTelemetryHost.name} (${fmt.format(
        topTelemetryHost.count
      )})`
    );
  }

  return (
    <div className="space-y-6">
      {err && <div className="text-sm text-red-400">{err}</div>}

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {snapshotChips.map((chip) => (
          <SnapshotChip key={chip.label} {...chip} />
        ))}
      </section>

      <section className="space-y-3">
        <SectionHeader title="Status" />
        <div className="grid gap-6 xl:grid-cols-2">
          <InsightCard
            title="Sessions"
            rows={[
              {
                label: "Captured",
                value: formatMetric(sessionTotal),
              },
              {
                label: "Successful",
                value: formatMetric(runStats?.success),
                detail: formatShare(runStats?.success, sessionTotal),
              },
              {
                label: "Failed",
                value: formatMetric(runStats?.failed),
                detail: formatShare(runStats?.failed, sessionTotal),
              },
              {
                label: "Partial",
                value: formatMetric(runStats?.partial),
                detail: formatShare(runStats?.partial, sessionTotal),
              },
            ]}
            footer={sessionFooterParts.join(" · ") || undefined}
            tone="emerald"
          >
            <StackedBar
              segments={sessionSegments}
              emptyMessage="No session distribution available yet."
            />
          </InsightCard>

          <InsightCard
            title="Savings"
            rows={[
              {
                label: "Saved Cost",
                value:
                  data.savings?.saved_usd != null
                    ? usd(data.savings.saved_usd)
                    : formatMetric(data.stats?.estimated_saved_cost_usd, usd),
              },
              {
                label: "Saved Tokens",
                value: formatMetric(savedTokens),
              },
              {
                label: "Reduction",
                value: formatMetric(data.savings?.reduction_pct, pct),
              },
              {
                label: "Compression Ratio",
                value: formatMetric(
                  data.stats?.average_compression_ratio,
                  (value) => value.toFixed(3)
                ),
              },
            ]}
            footer={efficiencyFooterParts.join(" · ") || undefined}
            tone="amber"
          >
            <StackedBar
              segments={efficiencySegments}
              emptyMessage="No token split available for the current savings window."
            />
          </InsightCard>
        </div>
      </section>

      <section className="space-y-3">
        <SectionHeader title="Signals" />
        <div className="grid gap-6 xl:grid-cols-3">
          <InsightCard
            title="Coverage"
            rows={[
              {
                label: "Hosts",
                value: data.traces
                  ? fmt.format(data.traces.metrics.hosts.length)
                  : "…",
              },
              {
                label: "Domains",
                value: data.traces
                  ? fmt.format(data.traces.metrics.domains.length)
                  : "…",
              },
              {
                label: "Blocks",
                value: formatMetric(data.stats?.total_blocks),
              },
              {
                label: "Rubrics",
                value: formatMetric(data.stats?.total_rubrics),
              },
            ]}
            footer={coverageFooterParts.join(" · ") || undefined}
            tone="violet"
          />

          <InsightCard
            title="Analytics"
            rows={[
              {
                label: "Spend",
                value: formatMetric(analyticsSpend, usd),
              },
              {
                label: "Sessions",
                value: formatMetric(analyticsSessions),
              },
              {
                label: "Tool Calls",
                value: formatMetric(analyticsToolCalls),
              },
              {
                label: "Top Host",
                value: topAnalyticsHost?.host || "—",
                detail: topAnalyticsHost
                  ? `${usd(topAnalyticsHost.cost)} · ${fmt.format(
                      topAnalyticsHost.sessions
                    )} sessions`
                  : undefined,
              },
            ]}
            footer={analyticsFooterParts.join(" · ") || undefined}
            tone="emerald"
          />

          <InsightCard
            title="Telemetry"
            rows={[
              {
                label: "Events",
                value: formatMetric(data.telemetry?.events_total),
              },
              {
                label: "Active Sessions",
                value: formatMetric(data.telemetry?.active_sessions),
              },
              {
                label: "Command Events",
                value: formatMetric(commandEvents),
              },
              {
                label: "Value Estimate",
                value: formatMetric(
                  data.telemetry?.value_estimate.tokens_saved_estimate
                ),
                detail:
                  data.telemetry?.value_estimate.cache_hit_rate != null
                    ? `${formatMetric(
                        data.telemetry.value_estimate.cache_hit_rate * 100,
                        pct
                      )} cache hit rate`
                    : data.telemetry?.value_estimate.cache_hits != null
                      ? `${formatMetric(
                          data.telemetry.value_estimate.cache_hits
                        )} cache hits`
                      : undefined,
              },
            ]}
            footer={telemetryFooterParts.join(" · ") || undefined}
            tone="cyan"
          />
        </div>
      </section>

      {data.insights !== null && data.insights.session_count > 0 && (
        <section className="space-y-3">
          <SectionHeader title="Session Activity" />
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <div className="border border-neutral-800 bg-neutral-950/60 p-4">
              <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
                Sessions
              </div>
              <div className="mt-2 text-xl font-semibold text-violet-200">
                {data.insights.session_count}
              </div>
            </div>
            <div className="border border-neutral-800 bg-neutral-950/60 p-4">
              <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
                Total Cost
              </div>
              <div className="mt-2 text-xl font-semibold text-amber-200">
                {usd(data.insights.total_cost_usd)}
              </div>
            </div>
            <div className="border border-neutral-800 bg-neutral-950/60 p-4">
              <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
                Total Savings
              </div>
              <div className="mt-2 text-xl font-semibold text-emerald-200">
                {usd(data.insights.total_atelier_savings_usd)}
              </div>
            </div>
            <div className="border border-neutral-800 bg-neutral-950/60 p-4">
              <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
                Avg Session Cost
              </div>
              <div className="mt-2 text-xl font-semibold text-neutral-100">
                {data.insights.session_count > 0
                  ? usd(data.insights.total_cost_usd / data.insights.session_count)
                  : "—"}
              </div>
            </div>
          </div>

          <div className="grid gap-4 xl:grid-cols-3">
            {data.insights.top_sessions.length > 0 && (
              <Card className="p-4">
                <h3 className="mb-3 text-[10px] font-mono font-bold uppercase tracking-widest text-neutral-500">
                  Top Cost Sessions
                </h3>
                <div className="space-y-2">
                  {data.insights.top_sessions.map((s: InsightsSessionSummary) => {
                    const maxCost = data.insights!.top_sessions[0]?.cost_usd ?? 1;
                    return (
                      <div key={s.session_id} className="flex flex-col gap-0.5">
                        <div className="flex justify-between text-xs">
                          <span className="font-mono text-violet-400/80">
                            {s.session_id.slice(0, 14)}…
                          </span>
                          <span className="text-amber-300">{usd(s.cost_usd)}</span>
                        </div>
                        <div className="h-1 w-full bg-neutral-800">
                          <div
                            className="h-full bg-violet-600"
                            style={{
                              width: `${maxCost > 0 ? Math.min(100, (s.cost_usd / maxCost) * 100) : 0}%`,
                            }}
                          />
                        </div>
                      </div>
                    );
                  })}
                </div>
              </Card>
            )}

            {Object.keys(data.insights.cost_by_vendor).length > 0 && (
              <Card className="p-4">
                <h3 className="mb-3 text-[10px] font-mono font-bold uppercase tracking-widest text-neutral-500">
                  Cost by Vendor
                </h3>
                <div className="space-y-2">
                  {Object.entries(data.insights.cost_by_vendor)
                    .sort((a, b) => b[1] - a[1])
                    .map(([vendor, cost]) => {
                      const maxCost = Math.max(...Object.values(data.insights!.cost_by_vendor));
                      return (
                        <div key={vendor} className="flex flex-col gap-0.5">
                          <div className="flex justify-between text-xs">
                            <span className="text-neutral-300">{vendor}</span>
                            <span className="text-amber-300">{usd(cost)}</span>
                          </div>
                          <div className="h-1 w-full bg-neutral-800">
                            <div
                              className="h-full bg-amber-600"
                              style={{
                                width: `${maxCost > 0 ? Math.min(100, (cost / maxCost) * 100) : 0}%`,
                              }}
                            />
                          </div>
                        </div>
                      );
                    })}
                </div>
              </Card>
            )}

            {data.insights.opportunities.length > 0 && (
              <Card tone="amber" className="p-4">
                <h3 className="mb-2 text-[10px] font-mono font-bold uppercase tracking-widest text-amber-500">
                  Optimization Opportunities
                </h3>
                <ul className="space-y-2">
                  {data.insights.opportunities.map((opp) => (
                    <li key={opp.kind} className="text-xs">
                      <div className="flex justify-between">
                        <span className="font-semibold text-neutral-200">{opp.kind}</span>
                        <span className="text-emerald-400">
                          {usd(opp.estimated_savings_usd)}
                        </span>
                      </div>
                      <p className="mt-0.5 text-neutral-500">{opp.message}</p>
                    </li>
                  ))}
                </ul>
              </Card>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
