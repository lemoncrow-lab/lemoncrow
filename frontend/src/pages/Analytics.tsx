import { useEffect, useState, useMemo } from "react";
import {
  api,
  type GranularToolUsage,
  type AnalyticsDashboard,
  type AnalyticsSummary,
  type DashboardExternalLatest,
  type DashboardTool,
  type DashboardHostModelOverview,
} from "../api";
import { MetricCard } from "../components/WorkbenchUI";
import { useTimeRange } from "../lib/TimeRangeContext";

const AGENTS = ["Claude", "Codex", "Copilot", "Opencode", "Gemini"];
const CATEGORIES = [
  "Native / Unoptimized",
  "Atelier Optimized",
  "Other Third-Party / Minor",
  "Miscellaneous",
  "Token Usage",
];
const TABS = [
  "Overview",
  "Timeline",
  "Domains",
  "Tool Breakdown",
  "Analysis",
  "Details",
] as const;
const TIMELINE_BREAKDOWN_OPTIONS = [
  { value: "daily", label: "Daily" },
  { value: "hourly", label: "Hourly" },
] as const;
type Tab = (typeof TABS)[number];
type TimelineBreakdownValue =
  (typeof TIMELINE_BREAKDOWN_OPTIONS)[number]["value"];

// ---- Shared helpers --------------------------------------------------------

function defaultdict_int() {
  return new Proxy({} as Record<string, number>, {
    get: (target, name: string) => (name in target ? target[name] : 0),
  });
}

function fmt(n: number, decimals = 2) {
  return n.toFixed(decimals);
}

function fmtM(n: number) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}k`;
  return String(n);
}

const EMPTY_SUMMARY: AnalyticsSummary = {
  total_cost: 0,
  estimated_monthly_cost: 0,
  top_cost_driver: "—",
  user_input_tokens: 0,
  model_thinking_tokens: 0,
  llm_output_tokens: 0,
  tool_output_tokens: 0,
  cached_prompt_tokens: 0,
  tool_calls: 0,
  unique_tools: 0,
  total_output_tokens: 0,
  row_count: 0,
};

// ---- Mini bar chart --------------------------------------------------------

function MiniBar({
  value,
  max,
  color = "bg-emerald-500/50",
}: {
  value: number;
  max: number;
  color?: string;
}) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  return (
    <div className="w-24 h-1.5 bg-neutral-800 rounded-full overflow-hidden">
      <div
        className={`h-full ${color} rounded-full`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

type CompactLeaderboardRow = {
  label: string;
  sublabel?: string;
  value: string;
  detail?: string;
  barValue: number;
};

function OverviewBar({
  value,
  max,
  color = "bg-emerald-500/50",
}: {
  value: number;
  max: number;
  color?: string;
}) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  return (
    <div className="h-1.5 w-full bg-neutral-900 rounded-full overflow-hidden">
      <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

function CompactLeaderboard({
  title,
  rows,
  color = "bg-cyan-500/60",
  emptyMessage = "No data.",
}: {
  title: string;
  rows: CompactLeaderboardRow[];
  color?: string;
  emptyMessage?: string;
}) {
  const maxValue = Math.max(...rows.map((row) => row.barValue), 0.0001);

  return (
    <section className="border border-neutral-800 bg-neutral-950/40 p-4 space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
          {title}
        </div>
        <div className="text-[9px] font-mono text-neutral-600">
          Top {rows.length}
        </div>
      </div>

      {rows.length ? (
        <div className="space-y-4">
          {rows.map((row, index) => (
            <div key={`${title}:${row.label}:${index}`} className="space-y-1.5">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="w-5 shrink-0 font-mono text-[9px] text-neutral-600">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    <span className="truncate text-sm text-neutral-200">
                      {row.label}
                    </span>
                  </div>
                  {row.sublabel && (
                    <div className="pl-7 pt-0.5 text-[10px] text-neutral-500 truncate">
                      {row.sublabel}
                    </div>
                  )}
                </div>
                <div className="shrink-0 text-right">
                  <div className="font-mono text-sm text-neutral-100">
                    {row.value}
                  </div>
                  {row.detail && (
                    <div className="text-[10px] text-neutral-500">
                      {row.detail}
                    </div>
                  )}
                </div>
              </div>
              <div className="pl-7">
                <OverviewBar
                  value={row.barValue}
                  max={maxValue}
                  color={color}
                />
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-neutral-600 italic text-xs">{emptyMessage}</div>
      )}
    </section>
  );
}

function ExternalSnapshotCard({
  snapshot,
  internalCost,
}: {
  snapshot: DashboardExternalLatest | null;
  internalCost: number;
}) {
  const externalCost =
    snapshot?.summary.highlights.find((item) => item.key === "cost_usd")
      ?.value ?? null;
  const externalCalls =
    snapshot?.summary.highlights.find((item) => item.key === "calls")?.value ??
    null;
  const externalSessions =
    snapshot?.summary.highlights.find((item) => item.key === "sessions")
      ?.value ?? null;
  const delta = externalCost != null ? externalCost - internalCost : null;

  return (
    <section className="border border-neutral-800 bg-neutral-950/40 p-4 space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
          External Reference
        </div>
        <div className="text-[9px] font-mono text-neutral-600">
          {snapshot?.tool ?? "No snapshot"}
        </div>
      </div>

      {snapshot ? (
        <div className="space-y-3 text-xs text-neutral-300">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="border border-neutral-900 bg-black/20 p-3">
              <div className="text-[9px] uppercase tracking-widest text-neutral-500">
                Atelier
              </div>
              <div className="mt-1 font-mono text-emerald-300">
                ${fmt(internalCost)}
              </div>
            </div>
            <div className="border border-neutral-900 bg-black/20 p-3">
              <div className="text-[9px] uppercase tracking-widest text-neutral-500">
                CodeBurn
              </div>
              <div className="mt-1 font-mono text-cyan-300">
                {externalCost == null ? "—" : `$${fmt(externalCost)}`}
              </div>
            </div>
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <div className="text-[9px] uppercase tracking-widest text-neutral-500">
                Delta
              </div>
              <div className="mt-1 font-mono text-amber-300">
                {delta == null ? "—" : `$${fmt(delta)}`}
              </div>
            </div>
            <div>
              <div className="text-[9px] uppercase tracking-widest text-neutral-500">
                Calls
              </div>
              <div className="mt-1 font-mono text-neutral-200">
                {externalCalls == null ? "—" : externalCalls.toLocaleString()}
              </div>
            </div>
            <div>
              <div className="text-[9px] uppercase tracking-widest text-neutral-500">
                Sessions
              </div>
              <div className="mt-1 font-mono text-neutral-200">
                {externalSessions == null
                  ? "—"
                  : externalSessions.toLocaleString()}
              </div>
            </div>
          </div>
          <div className="border-t border-neutral-800 pt-3 text-[10px] text-neutral-500">
            Snapshot period: {snapshot.period} · collected{" "}
            {new Date(snapshot.collected_at).toLocaleString()}
          </div>
        </div>
      ) : (
        <div className="text-neutral-600 italic text-xs">
          No external snapshot available.
        </div>
      )}
    </section>
  );
}

function OverviewHostModelSpotlight({
  rows,
  emptyMessage = "No data.",
}: {
  rows: DashboardHostModelOverview[];
  emptyMessage?: string;
}) {
  const spotlight = rows.slice(0, 4);
  const maxCost = Math.max(...spotlight.map((row) => row.cost), 0.0001);

  return (
    <section className="border border-neutral-800 bg-neutral-950/40 p-5 space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
          Heavy Host / Model Pairs
        </div>
        <div className="text-[9px] font-mono text-neutral-600">
          Top {spotlight.length}
        </div>
      </div>

      {spotlight.length ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          {spotlight.map((row, index) => (
            <article
              key={`${row.host}:${row.model}:${index}`}
              className="border border-neutral-900 bg-black/20 p-4 space-y-3"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-[9px] uppercase tracking-widest text-cyan-300/80 font-bold">
                    {row.host}
                  </div>
                  <div className="mt-1 text-sm font-semibold text-neutral-100 break-words">
                    {row.model || "—"}
                  </div>
                </div>
                <div className="text-[9px] font-mono text-neutral-600">
                  #{index + 1}
                </div>
              </div>

              <OverviewBar
                value={row.cost}
                max={maxCost}
                color="bg-violet-500/60"
              />

              <div className="grid grid-cols-2 gap-3 text-[10px]">
                <div>
                  <div className="uppercase tracking-widest text-neutral-500">
                    Cost
                  </div>
                  <div className="mt-1 font-mono text-emerald-300">
                    ${fmt(row.cost)}
                  </div>
                </div>
                <div>
                  <div className="uppercase tracking-widest text-neutral-500">
                    Sessions
                  </div>
                  <div className="mt-1 font-mono text-neutral-200">
                    {row.sessions.toLocaleString()}
                  </div>
                </div>
                <div>
                  <div className="uppercase tracking-widest text-neutral-500">
                    Tool Calls
                  </div>
                  <div className="mt-1 font-mono text-neutral-200">
                    {row.tool_calls.toLocaleString()}
                  </div>
                </div>
                <div>
                  <div className="uppercase tracking-widest text-neutral-500">
                    Billable Out
                  </div>
                  <div className="mt-1 font-mono text-neutral-200">
                    {(row.billable_output_tokens / 1_000_000).toFixed(1)}M
                  </div>
                </div>
              </div>

              <div className="text-[10px] text-neutral-500">
                {(row.base_context_tokens / 1_000_000).toFixed(1)}M base context
                · {(row.tool_output_tokens / 1_000_000).toFixed(1)}M tool out
              </div>
            </article>
          ))}
        </div>
      ) : (
        <div className="text-neutral-600 italic text-xs">{emptyMessage}</div>
      )}
    </section>
  );
}

// ---- Timeline activity chart ----------------------------------------------

type TimelineBucket = AnalyticsDashboard["daily"][number];

function utcDateKey(date: Date) {
  return date.toISOString().slice(0, 10);
}

function addUtcDays(date: Date, days: number) {
  const next = new Date(date);
  next.setUTCDate(next.getUTCDate() + days);
  return next;
}

function addUtcHours(date: Date, hours: number) {
  const next = new Date(date);
  next.setUTCHours(next.getUTCHours() + hours);
  return next;
}

function utcHourKey(date: Date) {
  return `${utcDateKey(date)} ${String(date.getUTCHours()).padStart(2, "0")}:00`;
}

function timelineValueLabel(cost: number) {
  if (cost > 0 && cost < 0.01) return "<$0.01";
  return `$${cost.toFixed(2)}`;
}

function timelineBucketLabel(date: string, breakdown: TimelineBreakdownValue) {
  if (breakdown === "hourly") {
    const [, time = date] = date.split(" ");
    return time.slice(0, 5);
  }
  return date.slice(5);
}

function timelineEndDate(daily: AnalyticsDashboard["daily"]) {
  const lastDate = daily
    .map((bucket) => bucket.date)
    .filter(Boolean)
    .sort()
    .at(-1);
  return lastDate ? new Date(`${lastDate}T00:00:00Z`) : new Date();
}

function fillDailyBuckets(
  daily: AnalyticsDashboard["daily"],
  days: number
): TimelineBucket[] {
  const byDate = new Map(daily.map((bucket) => [bucket.date, bucket]));
  const totalDays = Math.min(Math.max(days, 1), 365);
  const end = timelineEndDate(daily);
  const start = addUtcDays(end, -(totalDays - 1));

  return Array.from({ length: totalDays }, (_, index) => {
    const date = utcDateKey(addUtcDays(start, index));
    return (
      byDate.get(date) ?? {
        date,
        sessions: 0,
        cost: 0,
        input_tokens: 0,
        output_tokens: 0,
      }
    );
  });
}

function fillHourlyBuckets(
  hourly: AnalyticsDashboard["hourly"],
  daily: AnalyticsDashboard["daily"],
  days: number
): TimelineBucket[] {
  const byHour = new Map(hourly.map((bucket) => [bucket.date, bucket]));
  const lastHour = hourly
    .map((bucket) => bucket.date)
    .filter(Boolean)
    .sort()
    .at(-1);
  const end = lastHour
    ? new Date(`${lastHour.replace(" ", "T")}:00Z`)
    : addUtcHours(timelineEndDate(daily), 23);
  const totalHours = Math.min(Math.max(days, 1), 365) * 24;
  const start = addUtcHours(end, -(totalHours - 1));

  return Array.from({ length: totalHours }, (_, index) => {
    const date = utcHourKey(addUtcHours(start, index));
    return (
      byHour.get(date) ?? {
        date,
        sessions: 0,
        cost: 0,
        input_tokens: 0,
        output_tokens: 0,
      }
    );
  });
}

function SpendTimelineChart({
  dashboard,
  breakdown,
  days,
  onBreakdownChange,
}: {
  dashboard: AnalyticsDashboard;
  breakdown: TimelineBreakdownValue;
  days: number;
  onBreakdownChange: (breakdown: TimelineBreakdownValue) => void;
}) {
  const buckets =
    breakdown === "hourly"
      ? fillHourlyBuckets(dashboard.hourly ?? [], dashboard.daily, days)
      : fillDailyBuckets(dashboard.daily, days);

  if (!buckets.length)
    return (
      <div className="text-neutral-600 italic text-xs p-4">
        No timeline data.
      </div>
    );

  const maxCost = Math.max(...buckets.map((d) => d.cost), 0.0001);
  const totalCost = buckets.reduce((a, d) => a + d.cost, 0);
  const unitLabel = breakdown === "hourly" ? "hourly" : "daily";
  const isHourly = breakdown === "hourly";

  return (
    <section className="border border-neutral-800 bg-neutral-950/40 p-5 space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
            {breakdown === "hourly" ? "Hourly" : "Daily"} Activity
          </div>
          <div className="mt-1 text-sm text-neutral-400">
            Last {buckets.length} {unitLabel} snapshots in the selected window.
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {TIMELINE_BREAKDOWN_OPTIONS.map((option) => {
            const selected = option.value === breakdown;
            return (
              <button
                key={option.value}
                type="button"
                onClick={() => onBreakdownChange(option.value)}
                className={`border px-2 py-1 text-[9px] font-bold uppercase tracking-widest transition-colors ${
                  selected
                    ? "border-emerald-500/60 bg-emerald-500/15 text-emerald-200"
                    : "border-neutral-800 bg-neutral-950 text-neutral-500 hover:border-neutral-700 hover:text-neutral-300"
                }`}
                aria-pressed={selected}
              >
                {option.label}
              </button>
            );
          })}
        </div>
      </div>

      <div
        className={`flex items-end overflow-x-auto pb-2 ${
          isHourly ? "gap-px" : "gap-2"
        }`}
      >
        {buckets.map((d) => {
          const h = Math.max(6, (d.cost / maxCost) * (isHourly ? 72 : 112));
          return (
            <div
              key={d.date}
              className={`flex flex-col items-center ${
                isHourly
                  ? "min-w-[34px] flex-1 gap-1"
                  : "min-w-[52px] flex-1 gap-2"
              }`}
              title={`${d.date}: ${timelineValueLabel(d.cost)} · ${d.sessions} sessions`}
            >
              <div
                className={`max-w-full truncate font-mono ${
                  d.cost > 0 ? "text-emerald-300" : "text-neutral-600"
                } ${isHourly ? "text-[8px]" : "text-[10px]"}`}
              >
                {timelineValueLabel(d.cost)}
              </div>
              <div className={`flex items-end ${isHourly ? "h-20" : "h-28"}`}>
                <div
                  className={`rounded-t-sm transition-colors cursor-default ${
                    d.sessions > 0
                      ? "bg-emerald-500/60 hover:bg-emerald-400/80"
                      : "bg-neutral-800/70"
                  } ${isHourly ? "w-5" : "w-7"}`}
                  style={{ height: `${h}px` }}
                />
              </div>
              <div
                className={`max-w-full truncate font-mono ${
                  d.sessions > 0 ? "text-neutral-500" : "text-neutral-700"
                } ${isHourly ? "h-3 text-[8px]" : "text-[10px]"}`}
              >
                {timelineBucketLabel(d.date, breakdown)}
              </div>
            </div>
          );
        })}
      </div>
      <div className="grid grid-cols-2 gap-3 pt-2 border-t border-neutral-800/60">
        <div>
          <div className="text-[9px] uppercase text-neutral-500 mb-0.5">
            Total
          </div>
          <div className="text-sm font-mono text-emerald-300">
            ${totalCost.toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-[9px] uppercase text-neutral-500 mb-0.5">
            Avg/Bucket
          </div>
          <div className="text-sm font-mono text-emerald-300">
            ${(totalCost / buckets.length).toFixed(2)}
          </div>
        </div>
      </div>
    </section>
  );
}

// ---- By Host table ---------------------------------------------------------

function ByHostTable({ byHost }: { byHost: AnalyticsDashboard["by_host"] }) {
  const maxCost = Math.max(...byHost.map((r) => r.cost), 0.0001);
  return (
    <section className="border border-neutral-800 bg-neutral-950/40">
      <div className="bg-neutral-900/80 border-b border-neutral-800 p-3">
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
          Agent Host Breakdown
        </div>
      </div>
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr className="border-b border-neutral-800 text-[10px] uppercase text-neutral-500 bg-neutral-900/50">
            <th className="px-4 py-2 text-left">Host</th>
            <th className="px-4 py-2 text-right">Sessions</th>
            <th className="px-4 py-2 text-right">Cost</th>
            <th className="px-4 py-2 text-right">Cache %</th>
            <th className="px-4 py-2">Rel. Cost</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-neutral-900">
          {byHost.map((r, i) => (
            <tr key={i} className="hover:bg-neutral-800/20">
              <td className="px-4 py-2 font-mono text-cyan-300/80 capitalize">
                {r.host}
              </td>
              <td className="px-4 py-2 text-right font-mono text-neutral-400">
                {r.sessions}
              </td>
              <td className="px-4 py-2 text-right font-mono text-emerald-300">
                ${fmt(r.cost)}
              </td>
              <td className="px-4 py-2 text-right font-mono text-amber-300/80">
                {fmt(r.cache_pct, 1)}%
              </td>
              <td className="px-4 py-2">
                <MiniBar value={r.cost} max={maxCost} color="bg-cyan-500/50" />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

// ---- By Model table --------------------------------------------------------

function ByModelTable({
  byModel,
}: {
  byModel: AnalyticsDashboard["by_model"];
}) {
  const maxCost = Math.max(...byModel.map((r) => r.cost), 0.0001);
  return (
    <section className="border border-neutral-800 bg-neutral-950/40">
      <div className="bg-neutral-900/80 border-b border-neutral-800 p-3">
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
          By Model
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs border-collapse">
          <thead>
            <tr className="border-b border-neutral-800 text-[10px] uppercase text-neutral-500 bg-neutral-900/50">
              <th className="px-4 py-2 text-left">Model</th>
              <th className="px-4 py-2 text-right">Sessions</th>
              <th className="px-4 py-2 text-right">Input (M)</th>
              <th className="px-4 py-2 text-right">Output (M)</th>
              <th className="px-4 py-2 text-right">Cache %</th>
              <th className="px-4 py-2 text-right">Cost</th>
              <th className="px-4 py-2">Rel. Cost</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-900">
            {byModel.map((r, i) => (
              <tr key={i} className="hover:bg-neutral-800/20">
                <td className="px-4 py-2 font-mono text-neutral-300 text-[10px]">
                  {r.model || "—"}
                </td>
                <td className="px-4 py-2 text-right font-mono text-neutral-400">
                  {r.sessions}
                </td>
                <td className="px-4 py-2 text-right font-mono text-neutral-400">
                  {(r.input_tokens / 1_000_000).toFixed(2)}
                </td>
                <td className="px-4 py-2 text-right font-mono text-neutral-400">
                  {(r.output_tokens / 1_000_000).toFixed(2)}
                </td>
                <td className="px-4 py-2 text-right">
                  <span
                    className={`font-mono text-[10px] ${
                      r.cache_pct > 60
                        ? "text-emerald-400"
                        : r.cache_pct > 30
                          ? "text-amber-400"
                          : "text-red-400/80"
                    }`}
                  >
                    {fmt(r.cache_pct, 1)}%
                  </span>
                </td>
                <td className="px-4 py-2 text-right font-mono text-emerald-300">
                  ${fmt(r.cost)}
                </td>
                <td className="px-4 py-2">
                  <MiniBar value={r.cost} max={maxCost} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ---- Top Sessions ----------------------------------------------------------

function TopSessions({
  sessions,
}: {
  sessions: AnalyticsDashboard["top_sessions"];
}) {
  const maxCost = Math.max(...sessions.map((s) => s.cost), 0.0001);
  return (
    <section className="border border-neutral-800 bg-neutral-950/40">
      <div className="bg-neutral-900/80 border-b border-neutral-800 p-3">
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
          Costliest Sessions
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs border-collapse">
          <thead>
            <tr className="border-b border-neutral-800 text-[10px] uppercase text-neutral-500 bg-neutral-900/50">
              <th className="px-4 py-2">#</th>
              <th className="px-4 py-2 text-left">Date</th>
              <th className="px-4 py-2 text-left">Host</th>
              <th className="px-4 py-2 text-left">Project</th>
              <th className="px-4 py-2 text-left">Model</th>
              <th className="px-4 py-2 text-right">Input</th>
              <th className="px-4 py-2 text-right">Output</th>
              <th className="px-4 py-2 text-right">Cache</th>
              <th className="px-4 py-2 text-right">Cost</th>
              <th className="px-4 py-2">Rel.</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-900">
            {sessions.map((s, i) => (
              <tr key={i} className="hover:bg-neutral-800/20">
                <td className="px-4 py-2 font-mono text-neutral-600">
                  {i + 1}
                </td>
                <td className="px-4 py-2 font-mono text-neutral-500 text-[10px]">
                  {s.date}
                </td>
                <td className="px-4 py-2 font-mono text-cyan-300/80 capitalize">
                  {s.host}
                </td>
                <td
                  className="px-4 py-2 text-neutral-400 max-w-[140px] truncate"
                  title={s.domain}
                >
                  {s.domain}
                </td>
                <td className="px-4 py-2 font-mono text-neutral-500 text-[10px]">
                  {s.model || "—"}
                </td>
                <td className="px-4 py-2 text-right font-mono text-neutral-400">
                  {fmtM(s.input_tokens)}
                </td>
                <td className="px-4 py-2 text-right font-mono text-neutral-400">
                  {fmtM(s.output_tokens)}
                </td>
                <td className="px-4 py-2 text-right font-mono text-amber-400/70">
                  {fmtM(s.cached_tokens)}
                </td>
                <td className="px-4 py-2 text-right font-mono text-emerald-300 font-bold">
                  ${fmt(s.cost)}
                </td>
                <td className="px-4 py-2">
                  <MiniBar
                    value={s.cost}
                    max={maxCost}
                    color="bg-amber-500/50"
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ---- By Project ------------------------------------------------------------

function ByProjectTable({
  domains,
}: {
  domains: AnalyticsDashboard["by_domain"];
}) {
  const maxCost = Math.max(...domains.map((d) => d.cost), 0.0001);
  return (
    <section className="border border-neutral-800 bg-neutral-950/40">
      <div className="bg-neutral-900/80 border-b border-neutral-800 p-3">
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
          Domain Spend
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs border-collapse">
          <thead>
            <tr className="border-b border-neutral-800 text-[10px] uppercase text-neutral-500 bg-neutral-900/50">
              <th className="px-4 py-2 text-left">Project</th>
              <th className="px-4 py-2 text-right">Sessions</th>
              <th className="px-4 py-2 text-right">Total Cost</th>
              <th className="px-4 py-2 text-right">Avg / Session</th>
              <th className="px-4 py-2">Rel. Cost</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-900">
            {domains.map((d, i) => (
              <tr key={i} className="hover:bg-neutral-800/20">
                <td
                  className="px-4 py-2 text-neutral-300 font-medium max-w-[200px] truncate"
                  title={d.domain}
                >
                  {d.domain}
                </td>
                <td className="px-4 py-2 text-right font-mono text-neutral-400">
                  {d.sessions}
                </td>
                <td className="px-4 py-2 text-right font-mono text-emerald-300">
                  ${fmt(d.cost)}
                </td>
                <td className="px-4 py-2 text-right font-mono text-neutral-400">
                  ${fmt(d.avg_cost, 3)}
                </td>
                <td className="px-4 py-2">
                  <MiniBar
                    value={d.cost}
                    max={maxCost}
                    color="bg-violet-500/50"
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ---- Tool breakdown section ------------------------------------------------

function ToolTable({
  title,
  tools,
  color = "bg-purple-500/50",
}: {
  title: string;
  tools: DashboardTool[];
  color?: string;
}) {
  const maxCalls = Math.max(...tools.map((t) => t.calls), 1);
  if (!tools.length)
    return (
      <section className="border border-neutral-800 bg-neutral-950/40 p-4">
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold mb-2">
          {title}
        </div>
        <div className="text-neutral-600 italic text-xs">No data.</div>
      </section>
    );
  return (
    <section className="border border-neutral-800 bg-neutral-950/40">
      <div className="bg-neutral-900/80 border-b border-neutral-800 p-3">
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
          {title}
        </div>
      </div>
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr className="border-b border-neutral-800 text-[10px] uppercase text-neutral-500 bg-neutral-900/50">
            <th className="px-4 py-2 text-left">Tool</th>
            <th className="px-4 py-2 text-right">Calls</th>
            <th className="px-4 py-2 text-right">Out Tokens</th>
            <th className="px-4 py-2">Usage</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-neutral-900">
          {tools.map((t, i) => (
            <tr key={i} className="hover:bg-neutral-800/20">
              <td className="px-4 py-2 font-mono text-neutral-300">{t.name}</td>
              <td className="px-4 py-2 text-right font-mono text-neutral-400">
                {t.calls.toLocaleString()}
              </td>
              <td className="px-4 py-2 text-right font-mono text-neutral-400">
                {fmtM(t.output_tokens)}
              </td>
              <td className="px-4 py-2">
                <MiniBar value={t.calls} max={maxCalls} color={color} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

// ---- Savings Insights ------------------------------------------------------

function SavingsInsights({ dashboard }: { dashboard: AnalyticsDashboard }) {
  const { top_sessions, by_model } = dashboard;

  const highCostSessions = top_sessions.filter((s) => s.cost > 1.0);
  const noCacheSessions = top_sessions.filter(
    (s) =>
      s.cost > 0.5 &&
      s.input_tokens > 0 &&
      s.cached_tokens / (s.input_tokens + s.cached_tokens) < 0.1
  );
  const heavyContextSessions = top_sessions.filter(
    (s) => s.input_tokens > 500_000
  );
  const multiModelCount = by_model.filter((m) => m.cost > 0.1).length;

  return (
    <div className="space-y-4">
      <section className="border border-neutral-800 bg-neutral-950/40 p-5">
        <div className="text-[11px] uppercase tracking-widest text-neutral-400 font-bold mb-4">
          Session Analysis
        </div>
        <div className="space-y-3">
          {highCostSessions.length > 0 && (
            <div className="border border-red-900/40 bg-red-950/20 p-3 rounded">
              <div className="text-[10px] text-red-400 font-bold uppercase mb-1">
                🔴 {highCostSessions.length} High-Cost Session
                {highCostSessions.length > 1 ? "s" : ""} (&gt;$1.00 each)
              </div>
              <div className="text-[10px] text-red-300/70 space-y-0.5">
                {highCostSessions.slice(0, 3).map((s, i) => (
                  <div key={i}>
                    {s.date} · {s.host} · {s.domain} —{" "}
                    <span className="text-red-300">${fmt(s.cost)}</span>
                  </div>
                ))}
              </div>
              <div className="text-[9px] text-red-400/50 mt-2">
                Consider adding context pruning, summarization, or session
                splitting.
              </div>
            </div>
          )}

          {noCacheSessions.length > 0 && (
            <div className="border border-amber-900/40 bg-amber-950/20 p-3 rounded">
              <div className="text-[10px] text-amber-400 font-bold uppercase mb-1">
                🟡 {noCacheSessions.length} Session
                {noCacheSessions.length > 1 ? "s" : ""} with Low Cache
                Utilization
              </div>
              <div className="text-[10px] text-amber-300/70">
                These sessions have &lt;10% cache hit rate on expensive prompts.
              </div>
              <div className="text-[9px] text-amber-400/50 mt-2">
                Use long-lived system prompts and structured prefixes to improve
                caching.
              </div>
            </div>
          )}

          {heavyContextSessions.length > 0 && (
            <div className="border border-purple-900/40 bg-purple-950/20 p-3 rounded">
              <div className="text-[10px] text-purple-400 font-bold uppercase mb-1">
                🟣 {heavyContextSessions.length} Context-Heavy Session
                {heavyContextSessions.length > 1 ? "s" : ""} (&gt;500k input
                tokens)
              </div>
              <div className="text-[10px] text-purple-300/70 space-y-0.5">
                {heavyContextSessions.slice(0, 3).map((s, i) => (
                  <div key={i}>
                    {s.date} · {s.host} — {fmtM(s.input_tokens)} input tokens
                  </div>
                ))}
              </div>
              <div className="text-[9px] text-purple-400/50 mt-2">
                Add file chunking, selective context inclusion, and compact
                intermediate results.
              </div>
            </div>
          )}

          {multiModelCount > 2 && (
            <div className="border border-blue-900/40 bg-blue-950/20 p-3 rounded">
              <div className="text-[10px] text-blue-400 font-bold uppercase mb-1">
                🔵 {multiModelCount} Active Models — Consider Consolidation
              </div>
              <div className="text-[10px] text-blue-300/70">
                You're using {multiModelCount} models with non-trivial cost.
                Routing cheaper tasks to smaller models could reduce spend.
              </div>
            </div>
          )}

          {highCostSessions.length === 0 &&
            noCacheSessions.length === 0 &&
            heavyContextSessions.length === 0 && (
              <div className="text-neutral-500 italic text-xs">
                ✅ No significant optimization opportunities detected in this
                period.
              </div>
            )}
        </div>
      </section>

      {top_sessions.length > 0 && <TopSessions sessions={top_sessions} />}
    </div>
  );
}

// ---- Cost Drivers Chart ----------------------------------------------------

function CostDriversChart({
  stats,
}: {
  stats: {
    userInputTokens: number;
    modelThinkingTokens: number;
    llmOutputTokens: number;
    toolOutputTokens: number;
  };
}) {
  const breakdown = [
    {
      label: "User Input",
      tokens: stats.userInputTokens,
      color: "bg-emerald-500/60",
      accent: "text-emerald-300",
    },
    {
      label: "Thinking",
      tokens: stats.modelThinkingTokens,
      color: "bg-cyan-500/60",
      accent: "text-cyan-300",
    },
    {
      label: "Tool Output",
      tokens: stats.toolOutputTokens,
      color: "bg-amber-500/60",
      accent: "text-amber-300",
    },
    {
      label: "Output",
      tokens: stats.llmOutputTokens,
      color: "bg-violet-500/60",
      accent: "text-violet-300",
    },
  ].filter((item) => item.tokens > 0);

  const totalTrackedTokens =
    breakdown.reduce((sum, item) => sum + item.tokens, 0) || 1;

  if (!breakdown.length) {
    return (
      <section className="border border-neutral-800 bg-neutral-950/70 p-5 space-y-4">
        <div className="text-[11px] uppercase tracking-widest text-neutral-400 font-bold">
          Token Flow
        </div>
        <div className="text-xs text-neutral-500 italic">
          No input or output token activity found for the current filters.
        </div>
      </section>
    );
  }

  return (
    <section className="border border-neutral-800 bg-neutral-950/70 p-5 space-y-4">
      <div className="text-[11px] uppercase tracking-widest text-neutral-400 font-bold">
        Token Flow
      </div>
      <div className="space-y-3">
        {breakdown.map((item) => {
          const share = (item.tokens / totalTrackedTokens) * 100;
          return (
            <div key={item.label} className="space-y-1">
              <div className="flex justify-between text-[10px] gap-4">
                <span className="text-neutral-300">{item.label}</span>
                <span className={`font-mono ${item.accent}`}>
                  {fmtM(item.tokens)} tokens · {share.toFixed(1)}%
                </span>
              </div>
              <div className="h-2 bg-neutral-900 overflow-hidden rounded">
                <div
                  className={`h-full ${item.color}`}
                  style={{ width: `${share}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>
      <div className="text-[9px] text-neutral-500 pt-2 border-t border-neutral-800 space-y-1">
        <p>
          Output is model-generated text, including assistant responses plus
          tool call arguments. Tool output stays separate.
        </p>
      </div>
    </section>
  );
}

// ---- Main component --------------------------------------------------------

export default function Analytics() {
  const [data, setData] = useState<GranularToolUsage[]>([]);
  const [dashboard, setDashboard] = useState<AnalyticsDashboard | null>(null);
  const [summary, setSummary] = useState<AnalyticsSummary>(EMPTY_SUMMARY);
  const [loading, setLoading] = useState(true);
  const [dashLoading, setDashLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>("Overview");

  // Filters
  const [agentFilter, setAgentFilter] = useState("all");
  const [modelFilter, setModelFilter] = useState("all");
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [search, setSearch] = useState("");
  const { days, range } = useTimeRange();
  const [timelineBreakdown, setTimelineBreakdown] =
    useState<TimelineBreakdownValue>("daily");

  useEffect(() => {
    setTimelineBreakdown(range === "1d" ? "hourly" : "daily");
  }, [range]);

  useEffect(() => {
    setLoading(true);
    api
      .granularAnalytics(undefined, undefined, 5000, days)
      .then(setData)
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));

    setDashLoading(true);
    api
      .analyticsDashboard(days)
      .then(setDashboard)
      .catch(() => setDashboard(null))
      .finally(() => setDashLoading(false));
  }, [days]);

  useEffect(() => {
    let active = true;

    api
      .analyticsSummary(
        agentFilter !== "all" ? agentFilter : undefined,
        modelFilter !== "all" ? modelFilter : undefined,
        categoryFilter !== "all" ? categoryFilter : undefined,
        search || undefined,
        5000,
        days
      )
      .then((nextSummary) => {
        if (active) setSummary(nextSummary);
      })
      .catch(() => {
        if (active) setSummary(EMPTY_SUMMARY);
      });

    return () => {
      active = false;
    };
  }, [days, agentFilter, modelFilter, categoryFilter, search]);

  const filteredData = useMemo(() => {
    const agentMatch = agentFilter.toLowerCase();
    const modelMatch = modelFilter.toLowerCase();
    return data.filter((item) => {
      const itemAgent = (item.agent || "").toLowerCase();
      const itemModel = (item.model || "").toLowerCase();
      if (agentFilter !== "all" && itemAgent !== agentMatch) return false;
      if (modelFilter !== "all" && itemModel !== modelMatch) return false;
      if (categoryFilter !== "all" && item.category !== categoryFilter)
        return false;
      if (search) {
        const s = search.toLowerCase();
        return (
          item.tool_name.toLowerCase().includes(s) ||
          (item.sub_command?.toLowerCase() || "").includes(s)
        );
      }
      return true;
    });
  }, [data, agentFilter, modelFilter, categoryFilter, search]);

  const models = useMemo(() => {
    const set = new Set<string>();
    data.forEach((d) => {
      if (d.model) set.add(d.model);
    });
    return Array.from(set).sort();
  }, [data]);

  const stats = summary;

  const hostModelStats = dashboard?.host_model_overview ?? [];

  const costDriversData = useMemo(() => {
    const toolCosts = defaultdict_int();
    const toolCalls = defaultdict_int();
    const toolTokens = defaultdict_int();
    filteredData
      .filter((d) => d.event_type === "tool_call")
      .forEach((d) => {
        toolCosts[d.tool_name] += d.cost || 0;
        toolCalls[d.tool_name] += d.call_count ?? 1;
        toolTokens[d.tool_name] += d.output_tokens;
      });
    return Object.entries(toolCosts)
      .map(([tool, cost]) => ({
        tool,
        cost,
        calls: toolCalls[tool],
        tokens: toolTokens[tool],
        costPerCall: cost / (toolCalls[tool] || 1),
      }))
      .sort((a, b) => b.cost - a.cost)
      .slice(0, 10);
  }, [filteredData]);

  const tableData = useMemo(() => {
    return filteredData
      .map((item) => {
        const cost = item.cost || 0;
        const calls = item.call_count || 1;
        return {
          ...item,
          outPerCall: item.output_tokens / calls,
          cost,
          costPerCall: cost / calls,
          pctOfTotal:
            (item.output_tokens / (stats.total_output_tokens || 1)) * 100,
        };
      })
      .sort((a, b) => b.cost - a.cost);
  }, [filteredData, stats.total_output_tokens]);

  const overviewCards = useMemo(() => {
    const topHost = dashboard?.by_host[0];
    const topModel = dashboard?.by_model[0];
    const topDomain = dashboard?.by_domain[0];
    const topTool = costDriversData[0];

    return [
      {
        label: "Top Host",
        value: topHost?.host || "—",
        detail: topHost
          ? `$${fmt(topHost.cost)} over ${topHost.sessions.toLocaleString()} sessions · ${fmt(topHost.cache_pct, 1)}% cache`
          : dashLoading
            ? "Loading dashboard..."
            : "No host data.",
        tone: "cyan" as const,
      },
      {
        label: "Top Model",
        value: topModel?.model || "—",
        detail: topModel
          ? `$${fmt(topModel.cost)} over ${topModel.sessions.toLocaleString()} sessions · ${fmt(topModel.cache_pct, 1)}% cache`
          : dashLoading
            ? "Loading dashboard..."
            : "No model data.",
        tone: "emerald" as const,
      },
      {
        label: "Top Domain",
        value: topDomain?.domain || "—",
        detail: topDomain
          ? `$${fmt(topDomain.cost)} total · $${fmt(topDomain.avg_cost, 3)}/session`
          : dashLoading
            ? "Loading dashboard..."
            : "No domain data.",
        tone: "violet" as const,
      },
      {
        label: "Top Tool Driver",
        value: topTool?.tool || "—",
        detail: topTool
          ? `$${fmt(topTool.cost)} · ${topTool.calls.toLocaleString()} calls · ${fmtM(topTool.tokens)} out`
          : "No tool usage found.",
        tone: "amber" as const,
      },
    ];
  }, [costDriversData, dashboard, dashLoading]);

  const topHostRows = useMemo<CompactLeaderboardRow[]>(() => {
    return (dashboard?.by_host ?? []).slice(0, 5).map((row) => ({
      label: row.host,
      sublabel: `${row.sessions.toLocaleString()} sessions`,
      value: `$${fmt(row.cost)}`,
      detail: `${fmt(row.cache_pct, 1)}% cache`,
      barValue: row.cost,
    }));
  }, [dashboard]);

  const topModelRows = useMemo<CompactLeaderboardRow[]>(() => {
    return (dashboard?.by_model ?? []).slice(0, 5).map((row) => ({
      label: row.model || "—",
      sublabel: `${(row.input_tokens / 1_000_000).toFixed(2)}M in · ${(row.output_tokens / 1_000_000).toFixed(2)}M out`,
      value: `$${fmt(row.cost)}`,
      detail: `${row.sessions.toLocaleString()} sessions · ${fmt(row.cache_pct, 1)}% cache`,
      barValue: row.cost,
    }));
  }, [dashboard]);

  const topDomainRows = useMemo<CompactLeaderboardRow[]>(() => {
    return (dashboard?.by_domain ?? []).slice(0, 5).map((row) => ({
      label: row.domain,
      sublabel: `${row.sessions.toLocaleString()} sessions`,
      value: `$${fmt(row.cost)}`,
      detail: `$${fmt(row.avg_cost, 3)}/session`,
      barValue: row.cost,
    }));
  }, [dashboard]);

  const topToolRows = useMemo<CompactLeaderboardRow[]>(() => {
    return costDriversData.slice(0, 5).map((row) => ({
      label: row.tool,
      sublabel: `${row.calls.toLocaleString()} calls`,
      value: `$${fmt(row.cost)}`,
      detail: `${fmtM(row.tokens)} out · $${row.costPerCall.toFixed(4)}/call`,
      barValue: row.cost,
    }));
  }, [costDriversData]);

  const codeburnSnapshot = useMemo(() => {
    return (
      dashboard?.external.latest.find((item) => item.tool === "codeburn") ??
      null
    );
  }, [dashboard]);

  const externalProviderRows = useMemo<CompactLeaderboardRow[]>(() => {
    return (dashboard?.external.by_provider ?? []).slice(0, 5).map((row) => ({
      label: row.providerDisplayName || row.provider,
      sublabel: `${row.calls.toLocaleString()} calls · ${row.models.toLocaleString()} models`,
      value: `$${fmt(row.costUSD)}`,
      detail: `${(row.inputTokens / 1_000_000).toFixed(2)}M in · ${(row.outputTokens / 1_000_000).toFixed(2)}M out`,
      barValue: row.costUSD,
    }));
  }, [dashboard]);

  if (err) return <div className="text-red-400 p-6">Error: {err}</div>;
  if (loading && data.length === 0)
    return (
      <div className="text-neutral-400 p-6 italic animate-pulse">
        Loading analytics...
      </div>
    );

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-6 bg-black min-h-screen text-neutral-200 font-sans">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-neutral-800 pb-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-white">
            Cost & Efficiency
          </h1>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase font-bold text-neutral-500">
              Agent
            </span>
            <select
              value={agentFilter}
              onChange={(e) => {
                setAgentFilter(e.target.value);
                setModelFilter("all");
              }}
              className="bg-neutral-900 border border-neutral-700 px-2 py-1 text-xs text-neutral-300 focus:outline-none"
            >
              <option value="all">All Agents</option>
              {AGENTS.map((a) => (
                <option key={a} value={a.toLowerCase()}>
                  {a}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase font-bold text-neutral-500">
              Model
            </span>
            <select
              value={modelFilter}
              onChange={(e) => setModelFilter(e.target.value)}
              className="bg-neutral-900 border border-neutral-700 px-2 py-1 text-xs text-neutral-300 focus:outline-none max-w-[150px]"
            >
              <option value="all">All Models</option>
              {models.map((m) => (
                <option key={m} value={m.toLowerCase()}>
                  {m}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase font-bold text-neutral-500">
              Category
            </span>
            <select
              value={categoryFilter}
              onChange={(e) => setCategoryFilter(e.target.value)}
              className="bg-neutral-900 border border-neutral-700 px-2 py-1 text-xs text-neutral-300 focus:outline-none"
            >
              <option value="all">All Categories</option>
              {CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* Tab navigation */}
      <div className="flex gap-1 border-b border-neutral-800">
        {TABS.map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-4 py-2 text-[11px] uppercase tracking-wider font-semibold transition-colors ${
              activeTab === tab
                ? "text-emerald-400 border-b-2 border-emerald-500 -mb-px"
                : "text-neutral-500 hover:text-neutral-300"
            }`}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* Overview tab */}
      {activeTab === "Overview" && (
        <div className="space-y-6">
          {/* Summary metrics */}
          <section className="grid gap-4 md:grid-cols-4">
            <MetricCard
              label="Total Estimated Cost"
              value={`$${stats.total_cost.toFixed(2)}`}
              tone="emerald"
            />
            <MetricCard
              label="Projected Month-End"
              value={`$${stats.estimated_monthly_cost.toFixed(2)}`}
              tone="emerald"
            />
            <MetricCard
              label="Total Tool Calls"
              value={stats.tool_calls.toLocaleString()}
              tone="cyan"
            />
            <MetricCard
              label="Unique Tools"
              value={stats.unique_tools.toString()}
              tone="cyan"
            />
          </section>
          <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            {overviewCards.map((card) => (
              <MetricCard
                key={card.label}
                label={card.label}
                value={card.value}
                detail={card.detail}
                tone={card.tone}
              />
            ))}
          </section>

          <CostDriversChart
            stats={{
              userInputTokens: stats.user_input_tokens,
              modelThinkingTokens: stats.model_thinking_tokens,
              llmOutputTokens: stats.llm_output_tokens,
              toolOutputTokens: stats.tool_output_tokens,
            }}
          />

          <div className="grid gap-6 xl:grid-cols-2">
            <CompactLeaderboard
              title="Top Hosts"
              rows={topHostRows}
              color="bg-cyan-500/60"
              emptyMessage={
                dashLoading ? "Loading dashboard..." : "No host data."
              }
            />
            <CompactLeaderboard
              title="Top Models"
              rows={topModelRows}
              color="bg-emerald-500/60"
              emptyMessage={
                dashLoading ? "Loading dashboard..." : "No model data."
              }
            />
          </div>

          <div className="grid gap-6 xl:grid-cols-2">
            <CompactLeaderboard
              title="Top Domains"
              rows={topDomainRows}
              color="bg-violet-500/60"
              emptyMessage={
                dashLoading ? "Loading dashboard..." : "No domain data."
              }
            />
            <CompactLeaderboard
              title="Top Tool Drivers"
              rows={topToolRows}
              color="bg-amber-500/60"
              emptyMessage="No tool usage found."
            />
          </div>

          <div className="grid gap-6 xl:grid-cols-2">
            <CompactLeaderboard
              title="CodeBurn Providers"
              rows={externalProviderRows}
              color="bg-fuchsia-500/60"
              emptyMessage={
                dashLoading
                  ? "Loading external snapshot..."
                  : "No provider breakdown available."
              }
            />
            <ExternalSnapshotCard
              snapshot={codeburnSnapshot}
              internalCost={stats.total_cost}
            />
          </div>

          <OverviewHostModelSpotlight
            rows={hostModelStats}
            emptyMessage={
              dashLoading ? "Loading dashboard..." : "No host/model data."
            }
          />
        </div>
      )}

      {/* Timeline tab */}
      {activeTab === "Timeline" && (
        <div className="space-y-6">
          {dashLoading ? (
            <div className="text-neutral-500 italic text-sm animate-pulse">
              Loading...
            </div>
          ) : dashboard ? (
            <>
              <SpendTimelineChart
                dashboard={dashboard}
                breakdown={timelineBreakdown}
                days={days}
                onBreakdownChange={setTimelineBreakdown}
              />
              <div className="grid md:grid-cols-2 gap-6">
                <ByHostTable byHost={dashboard.by_host} />
                <ByModelTable byModel={dashboard.by_model} />
              </div>
            </>
          ) : (
            <div className="text-neutral-600 italic text-sm">
              Data unavailable.
            </div>
          )}
        </div>
      )}

      {/* Domains tab */}
      {activeTab === "Domains" && (
        <div className="space-y-6">
          {dashLoading ? (
            <div className="text-neutral-500 italic text-sm animate-pulse">
              Loading...
            </div>
          ) : dashboard ? (
            <ByProjectTable domains={dashboard.by_domain} />
          ) : (
            <div className="text-neutral-600 italic text-sm">
              Data unavailable.
            </div>
          )}
        </div>
      )}

      {/* Tool Breakdown tab */}
      {activeTab === "Tool Breakdown" && (
        <div className="space-y-6">
          {dashLoading ? (
            <div className="text-neutral-500 italic text-sm animate-pulse">
              Loading...
            </div>
          ) : dashboard ? (
            <>
              <ToolTable
                title="File & Search Tools"
                tools={dashboard.tools.core}
                color="bg-blue-500/50"
              />
              <ToolTable
                title="Bash & Exec Usage"
                tools={dashboard.tools.shell}
                color="bg-yellow-500/50"
              />
              <ToolTable
                title="MCP Tool Usage"
                tools={dashboard.tools.mcp}
                color="bg-purple-500/50"
              />
            </>
          ) : (
            <div className="text-neutral-600 italic text-sm">
              Data unavailable.
            </div>
          )}
        </div>
      )}

      {/* Analysis tab */}
      {activeTab === "Analysis" && (
        <div className="space-y-6">
          {dashLoading ? (
            <div className="text-neutral-500 italic text-sm animate-pulse">
              Loading...
            </div>
          ) : dashboard ? (
            <>
              <SavingsInsights dashboard={dashboard} />
            </>
          ) : (
            <div className="text-neutral-600 italic text-sm">
              Data unavailable.
            </div>
          )}
        </div>
      )}

      {/* Details tab */}
      {activeTab === "Details" && (
        <div className="space-y-6">
          <section className="border border-neutral-800 bg-neutral-950/40 p-4 text-sm leading-relaxed text-neutral-500">
            Granular tables live here so the Overview tab stays readable. Use
            the filters above plus search to inspect host/model groups, tool
            rankings, and individual raw rows.
          </section>

          <section className="border border-neutral-800 bg-neutral-950/40 overflow-hidden">
            <div className="bg-neutral-900/80 border-b border-neutral-800 p-4 flex items-center justify-between">
              <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
                Host / Model Overview
              </div>
              <div className="text-[9px] text-neutral-600 font-mono">
                {hostModelStats.length} host/model groups
              </div>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs border-collapse">
                <thead>
                  <tr className="border-b border-neutral-800 text-[10px] uppercase tracking-widest text-neutral-500 font-mono bg-neutral-900/50">
                    <th className="px-4 py-3">Host</th>
                    <th className="px-4 py-3">Model</th>
                    <th className="px-4 py-3 text-right">Sessions</th>
                    <th className="px-4 py-3 text-right">User Typed (k)</th>
                    <th className="px-4 py-3 text-right">Base Context (M)</th>
                    <th className="px-4 py-3 text-right">Cached (M)</th>
                    <th className="px-4 py-3 text-right">Cache Write (M)</th>
                    <th className="px-4 py-3 text-right">Billable Out (M)</th>
                    <th className="px-4 py-3 text-right">Tool Out (M)</th>
                    <th className="px-4 py-3 text-right">Thinking (M)</th>
                    <th className="px-4 py-3 text-right">Calls</th>
                    <th className="px-4 py-3 text-right">Cost</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-neutral-900">
                  {hostModelStats.length === 0 ? (
                    <tr>
                      <td
                        colSpan={12}
                        className="px-4 py-8 text-center text-neutral-600 italic"
                      >
                        {dashLoading ? "Loading dashboard..." : "No data."}
                      </td>
                    </tr>
                  ) : (
                    hostModelStats.map(
                      (row: DashboardHostModelOverview, idx) => (
                        <tr
                          key={idx}
                          className="hover:bg-neutral-800/20 transition-colors"
                        >
                          <td className="px-4 py-2 font-mono text-cyan-300/80">
                            {row.host}
                          </td>
                          <td className="px-4 py-2 font-mono text-neutral-400">
                            {row.model}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-neutral-400">
                            {row.sessions.toLocaleString()}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-emerald-300/80">
                            {(row.user_typed_tokens / 1000).toFixed(1)}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-emerald-400/80">
                            {(row.base_context_tokens / 1_000_000).toFixed(1)}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-red-400/80">
                            {(row.cached_prompt_tokens / 1_000_000).toFixed(1)}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-purple-400/80">
                            {(row.cache_write_tokens / 1_000_000).toFixed(1)}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-violet-400/80">
                            {(row.billable_output_tokens / 1_000_000).toFixed(
                              1
                            )}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-amber-400/80">
                            {(row.tool_output_tokens / 1_000_000).toFixed(1)}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-cyan-400/80">
                            {(row.thinking_tokens / 1_000_000).toFixed(1)}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-neutral-400">
                            {row.tool_calls.toLocaleString()}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-emerald-300 font-bold">
                            ${row.cost.toFixed(2)}
                          </td>
                        </tr>
                      )
                    )
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className="border border-neutral-800 bg-neutral-950/40">
            <div className="bg-neutral-900/80 border-b border-neutral-800 p-4">
              <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
                Cost Drivers Ranking
              </div>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs border-collapse">
                <thead>
                  <tr className="border-b border-neutral-800 text-[10px] uppercase tracking-widest text-neutral-500 font-mono bg-neutral-900/50">
                    <th className="px-4 py-3">Rank</th>
                    <th className="px-4 py-3">Tool</th>
                    <th className="px-4 py-3 text-right">Calls</th>
                    <th className="px-4 py-3 text-right">Output (M)</th>
                    <th className="px-4 py-3 text-right">Out/Call</th>
                    <th className="px-4 py-3 text-right">Est. Cost</th>
                    <th className="px-4 py-3 text-right">Cost/Call</th>
                    <th className="px-4 py-3 text-right">% Total</th>
                    <th className="px-4 py-3">Hint</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-neutral-900">
                  {costDriversData.length === 0 ? (
                    <tr>
                      <td
                        colSpan={9}
                        className="px-4 py-8 text-center text-neutral-600 italic"
                      >
                        No tool usage found.
                      </td>
                    </tr>
                  ) : (
                    costDriversData.map((item, i) => (
                      <tr
                        key={i}
                        className="hover:bg-neutral-800/20 transition-colors"
                      >
                        <td className="px-4 py-3 font-mono text-neutral-600">
                          {i + 1}
                        </td>
                        <td className="px-4 py-3 font-medium text-neutral-300">
                          {item.tool}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-neutral-400">
                          {(item.calls || 0).toLocaleString()}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-neutral-400">
                          {(item.tokens / 1_000_000).toFixed(1)}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-neutral-400">
                          {item.tokens / (item.calls || 1) > 10_000
                            ? `${(item.tokens / (item.calls || 1) / 1000).toFixed(0)}k`
                            : (item.tokens / (item.calls || 1)).toFixed(0)}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-amber-300/80">
                          ${item.cost.toFixed(2)}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-amber-300/80">
                          ${item.costPerCall.toFixed(4)}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-2">
                            <span className="font-mono text-[10px] text-neutral-500">
                              {(
                                (item.tokens /
                                  (stats.tool_output_tokens || 1)) *
                                100
                              ).toFixed(1)}
                              %
                            </span>
                            <div className="w-12 h-1 bg-neutral-900 rounded-full overflow-hidden">
                              <div
                                className="h-full bg-amber-500/50"
                                style={{
                                  width: `${(item.tokens / (stats.tool_output_tokens || 1)) * 100}%`,
                                }}
                              />
                            </div>
                          </div>
                        </td>
                        <td className="px-4 py-3 text-[10px] text-neutral-500 italic">
                          Review output size
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className="border border-neutral-800 bg-neutral-950/40">
            <div className="bg-neutral-900/80 border-b border-neutral-800 p-4 flex items-center justify-between">
              <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
                Full Data Table
              </div>
              <div className="relative">
                <input
                  type="text"
                  placeholder="Search Tool / Sub-command"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="bg-neutral-900 border border-neutral-700 px-3 py-1.5 text-xs text-neutral-300 focus:outline-none focus:border-emerald-500 w-64 pl-8"
                />
                <svg
                  className="absolute left-2.5 top-2 w-3.5 h-3.5 text-neutral-600"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
                  />
                </svg>
              </div>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs border-collapse">
                <thead>
                  <tr className="border-b border-neutral-800 text-[10px] uppercase tracking-widest text-neutral-500 font-mono bg-neutral-900/50">
                    <th className="px-4 py-3">Agent</th>
                    <th className="px-4 py-3">Model</th>
                    <th className="px-4 py-3">Category</th>
                    <th className="px-4 py-3">Tool</th>
                    <th className="px-4 py-3">Sub-command</th>
                    <th className="px-4 py-3 text-right">Calls</th>
                    <th className="px-4 py-3 text-right">In (M)</th>
                    <th className="px-4 py-3 text-right">Out (M)</th>
                    <th className="px-4 py-3 text-right">Out/Call</th>
                    <th className="px-4 py-3 text-right">Est. Cost</th>
                    <th className="px-4 py-3 text-right">Cost/Call</th>
                    <th className="px-4 py-3 text-right">% Total</th>
                    <th className="px-4 py-3">Date Range</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-neutral-900">
                  {tableData.length === 0 ? (
                    <tr>
                      <td
                        colSpan={13}
                        className="px-4 py-8 text-center text-neutral-600 italic"
                      >
                        No records found.
                      </td>
                    </tr>
                  ) : (
                    tableData.map((item, i) => {
                      const dr =
                        item.first_seen && item.last_seen
                          ? `${new Date(item.first_seen).toLocaleDateString("en-GB")} – ${new Date(item.last_seen).toLocaleDateString("en-GB")}`
                          : "—";
                      return (
                        <tr
                          key={i}
                          className="hover:bg-neutral-800/20 transition-colors"
                        >
                          <td className="px-4 py-2 font-mono text-neutral-400">
                            {item.agent}
                          </td>
                          <td className="px-4 py-2 font-mono text-neutral-500 text-[10px]">
                            {item.model || "—"}
                          </td>
                          <td className="px-4 py-2">
                            <span
                              className={`text-[9px] px-1.5 py-0.5 border ${
                                item.category.includes("Optimized")
                                  ? "border-emerald-900/50 text-emerald-400 bg-emerald-950/20"
                                  : "border-neutral-800 text-neutral-500 bg-neutral-900/20"
                              }`}
                            >
                              {item.category}
                            </span>
                          </td>
                          <td className="px-4 py-2 font-medium text-neutral-300">
                            {item.tool_name}
                          </td>
                          <td className="px-4 py-2 text-neutral-500 font-mono italic">
                            {item.sub_command || "—"}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-neutral-400">
                            {(item.call_count ?? 1).toLocaleString()}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-neutral-400">
                            {(item.input_tokens / 1_000_000).toFixed(1)}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-neutral-400">
                            {(item.output_tokens / 1_000_000).toFixed(1)}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-neutral-500">
                            {(item.outPerCall / 1000).toFixed(0)}k
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-emerald-500/80">
                            ${(item.cost || 0).toFixed(2)}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-neutral-500">
                            $
                            {(
                              (item.cost || 0) / (item.call_count || 1)
                            ).toFixed(4)}
                          </td>
                          <td className="px-4 py-2 text-right font-mono text-neutral-500">
                            {item.pctOfTotal.toFixed(1)}%
                          </td>
                          <td className="px-4 py-2 text-neutral-600 text-[10px]">
                            {dr}
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
