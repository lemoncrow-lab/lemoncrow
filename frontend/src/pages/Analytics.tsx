import { useEffect, useState, useMemo } from "react";
import {
  api,
  type GranularToolUsage,
  type AnalyticsDashboard,
  type DashboardTool,
  type DashboardHostModelOverview,
  type ExternalAnalyticsRun,
  type ExternalAnalyticsResponse,
} from "../api";
import { MetricCard } from "../components/WorkbenchUI";

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
  "External",
] as const;
const EXTERNAL_PERIOD_DAY_SPAN: Record<string, number> = {
  today: 1,
  week: 7,
  month: 30,
  "30days": 30,
  all: 3650,
};
type Tab = (typeof TABS)[number];

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

function fmtExternalValue(key: string, value: number) {
  if (key.includes("cost") || key.includes("usd"))
    return `$${value.toFixed(2)}`;
  if (key.includes("rate")) {
    const pct = value <= 1 ? value * 100 : value;
    return `${pct.toFixed(1)}%`;
  }
  if (key.includes("token")) return fmtM(value);
  if (Number.isInteger(value)) return value.toLocaleString();
  return value.toFixed(2);
}

function fmtTimestamp(value: string) {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

function titleCaseKey(key: string) {
  return key
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function isPrimitiveExternalValue(
  value: unknown
): value is string | number | boolean | null {
  return (
    value === null ||
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function formatMaybeExternalDate(value: string) {
  const looksTemporal = value.includes("T") || /^\d{4}-\d{2}-\d{2}/.test(value);
  if (!looksTemporal) return value;
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return value;
  return new Date(parsed).toLocaleString();
}

function formatExternalContentValue(key: string, value: unknown) {
  if (value == null) return "—";
  if (typeof value === "number") {
    return fmtExternalValue(key.toLowerCase(), value);
  }
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string") return formatMaybeExternalDate(value);
  if (Array.isArray(value)) return `${value.length} items`;
  if (isRecord(value)) return `${Object.keys(value).length} fields`;
  return String(value);
}

function summarizeExternalText(value: string, limit = 88) {
  if (value.length <= limit) return value;
  return `${value.slice(0, Math.max(limit - 3, 0))}...`;
}

function sortRunsByCollectedAtDesc<T extends { collected_at: string }>(
  items: T[]
) {
  return [...items].sort((a, b) =>
    String(b.collected_at).localeCompare(String(a.collected_at))
  );
}

function normalizeExternalPeriod(period: string | null | undefined) {
  return String(period || "").trim().toLowerCase();
}

function pickPreferredExternalPeriod(
  runs: ExternalAnalyticsRun[],
  days: number
) {
  const targetDays = Math.max(1, days);
  const periods = Array.from(
    new Set(runs.map((run) => normalizeExternalPeriod(run.period)).filter(Boolean))
  );

  if (!periods.length) return null;

  return [...periods].sort((left, right) => {
    const leftSpan = EXTERNAL_PERIOD_DAY_SPAN[left] ?? Number.POSITIVE_INFINITY;
    const rightSpan =
      EXTERNAL_PERIOD_DAY_SPAN[right] ?? Number.POSITIVE_INFINITY;
    const leftDiff = Math.abs(leftSpan - targetDays);
    const rightDiff = Math.abs(rightSpan - targetDays);

    if (leftDiff !== rightDiff) return leftDiff - rightDiff;

    const leftOvershoots = leftSpan >= targetDays ? 0 : 1;
    const rightOvershoots = rightSpan >= targetDays ? 0 : 1;
    if (leftOvershoots !== rightOvershoots) {
      return leftOvershoots - rightOvershoots;
    }

    return leftSpan - rightSpan;
  })[0];
}

function selectExternalRunsForDays(
  runs: ExternalAnalyticsRun[],
  days: number
) {
  const sortedRuns = sortRunsByCollectedAtDesc(runs);
  const selectedPeriod = pickPreferredExternalPeriod(sortedRuns, days);
  if (!selectedPeriod) {
    return {
      selectedPeriod: null,
      selectedRuns: sortedRuns,
      observedPeriods: [] as string[],
    };
  }

  const selectedRuns = sortedRuns.filter(
    (run) => normalizeExternalPeriod(run.period) === selectedPeriod
  );

  return {
    selectedPeriod,
    selectedRuns: selectedRuns.length ? selectedRuns : sortedRuns,
    observedPeriods: Array.from(
      new Set(
        sortedRuns.map((run) => normalizeExternalPeriod(run.period)).filter(Boolean)
      )
    ),
  };
}

function formatExternalHighlightsPreview(
  metrics: ExternalAnalyticsRun["summary"]["highlights"]
) {
  if (!metrics.length) return "—";
  return metrics
    .slice(0, 2)
    .map(
      (metric) =>
        `${titleCaseKey(metric.label)} ${fmtExternalValue(
          metric.key,
          Number(metric.value)
        )}`
    )
    .join(" · ");
}

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

// ---- Daily activity chart --------------------------------------------------

function DailyChart({ daily }: { daily: AnalyticsDashboard["daily"] }) {
  if (!daily.length)
    return (
      <div className="text-neutral-600 italic text-xs p-4">No daily data.</div>
    );

  const maxCost = Math.max(...daily.map((d) => d.cost), 0.0001);
  const recent = daily.slice(-30);

  return (
    <section className="border border-neutral-800 bg-neutral-950/40 p-5 space-y-3">
      <div className="text-[11px] uppercase tracking-widest text-neutral-400 font-bold">
        Spend by Day
      </div>
      <div className="flex items-end gap-1 h-20 overflow-x-auto pb-1">
        {recent.map((d, i) => {
          const h = Math.max(4, (d.cost / maxCost) * 80);
          return (
            <div
              key={i}
              className="flex flex-col items-center gap-0.5 shrink-0"
              title={`${d.date}: $${d.cost.toFixed(3)} · ${d.sessions} sessions`}
            >
              <div
                className="w-4 bg-emerald-500/60 rounded-sm hover:bg-emerald-400/80 transition-colors cursor-default"
                style={{ height: `${h}px` }}
              />
            </div>
          );
        })}
      </div>
      <div className="flex justify-between text-[9px] text-neutral-600 font-mono">
        <span>{recent[0]?.date}</span>
        <span>{recent[recent.length - 1]?.date}</span>
      </div>
      <div className="grid grid-cols-3 gap-3 pt-2 border-t border-neutral-800/60">
        <div>
          <div className="text-[9px] uppercase text-neutral-500 mb-0.5">
            Total Days
          </div>
          <div className="text-sm font-mono text-neutral-200">
            {daily.length}
          </div>
        </div>
        <div>
          <div className="text-[9px] uppercase text-neutral-500 mb-0.5">
            Avg/Day
          </div>
          <div className="text-sm font-mono text-neutral-200">
            ${(daily.reduce((a, d) => a + d.cost, 0) / daily.length).toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-[9px] uppercase text-neutral-500 mb-0.5">
            Peak Day
          </div>
          <div className="text-sm font-mono text-emerald-300">
            ${Math.max(...daily.map((d) => d.cost)).toFixed(2)}
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
  color = "bg-orange-500/50",
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
            <div className="border border-orange-900/40 bg-orange-950/20 p-3 rounded">
              <div className="text-[10px] text-orange-400 font-bold uppercase mb-1">
                🟠 {heavyContextSessions.length} Context-Heavy Session
                {heavyContextSessions.length > 1 ? "s" : ""} (&gt;500k input
                tokens)
              </div>
              <div className="text-[10px] text-orange-300/70 space-y-0.5">
                {heavyContextSessions.slice(0, 3).map((s, i) => (
                  <div key={i}>
                    {s.date} · {s.host} — {fmtM(s.input_tokens)} input tokens
                  </div>
                ))}
              </div>
              <div className="text-[9px] text-orange-400/50 mt-2">
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
  data,
  stats,
}: {
  data: GranularToolUsage[];
  stats: any;
}) {
  const baseInput = data
    .filter((d) => d.event_type === "prompt")
    .reduce((acc, d) => acc + d.input_tokens, 0);
  const cachedInput = data
    .filter((d) => d.event_type === "cached_prompt")
    .reduce((acc, d) => acc + d.input_tokens, 0);
  const cacheCreate = data
    .filter((d) => d.event_type === "cache_create")
    .reduce((acc, d) => acc + d.input_tokens, 0);

  const totalGrossInputTokens = baseInput + cachedInput + cacheCreate || 1;
  const contextWindowShare = (cachedInput / totalGrossInputTokens) * 100;
  const totalOutputTokens = stats.totalOutputTokens || 1;

  const toolOutputs = defaultdict_int();
  data
    .filter((d) => d.event_type === "tool_call")
    .forEach((d) => {
      toolOutputs[d.tool_name] += d.output_tokens;
    });

  const topTools = Object.entries(toolOutputs)
    .map(([name, tokens]) => ({
      name,
      tokens,
      share: (tokens / totalOutputTokens) * 100,
    }))
    .sort((a, b) => b.tokens - a.tokens)
    .slice(0, 5);

  return (
    <section className="border border-neutral-800 bg-neutral-950/70 p-5 space-y-4">
      <div className="text-[11px] uppercase tracking-widest text-neutral-400 font-bold">
        Cost Drivers
      </div>
      <div className="space-y-3">
        <div className="space-y-1">
          <div className="flex justify-between text-[10px]">
            <span className="text-neutral-300">Context Window Usage</span>
            <span className="font-mono text-neutral-400">
              {contextWindowShare.toFixed(1)}% of input
            </span>
          </div>
          <div className="h-2 bg-neutral-900 overflow-hidden rounded">
            <div
              className="h-full bg-red-500/40"
              style={{ width: `${Math.min(contextWindowShare, 100)}%` }}
            />
          </div>
        </div>
        {topTools.map((tool, i) => (
          <div key={i} className="space-y-1">
            <div className="flex justify-between text-[10px]">
              <span className="text-neutral-300">{tool.name}</span>
              <span className="font-mono text-neutral-400">
                {tool.share.toFixed(1)}% of output
              </span>
            </div>
            <div className="h-2 bg-neutral-900 overflow-hidden rounded">
              <div
                className="h-full bg-orange-500/60"
                style={{ width: `${tool.share}%` }}
              />
            </div>
          </div>
        ))}
      </div>
      <div className="text-[9px] text-neutral-500 pt-2 border-t border-neutral-800">
        <p>
          💡 {contextWindowShare.toFixed(1)}% of context is cached. Top 5 tools
          generate {topTools.reduce((acc, t) => acc + t.share, 0).toFixed(1)}%
          of output tokens.
        </p>
      </div>
    </section>
  );
}

// ---- Optimization alerts ---------------------------------------------------

function OptimizationCards({ data }: { data: GranularToolUsage[] }) {
  const baseInput = data
    .filter((d) => d.event_type === "prompt")
    .reduce((acc, d) => acc + d.input_tokens, 0);
  const cachedInput = data
    .filter((d) => d.event_type === "cached_prompt")
    .reduce((acc, d) => acc + d.input_tokens, 0);
  const cacheCreate = data
    .filter((d) => d.event_type === "cache_create")
    .reduce((acc, d) => acc + d.input_tokens, 0);

  const totalGrossInputTokens = baseInput + cachedInput + cacheCreate || 1;
  const contextWindowShare = (cachedInput / totalGrossInputTokens) * 100;

  const highOutputTools = defaultdict_int();
  const toolCalls = defaultdict_int();
  data
    .filter((d) => d.event_type === "tool_call")
    .forEach((d) => {
      highOutputTools[d.tool_name] += d.output_tokens;
      toolCalls[d.tool_name] += d.call_count ?? 1;
    });

  const toolsPerCall = Object.entries(highOutputTools)
    .map(([name, tokens]) => ({
      name,
      tokensPerCall: tokens / (toolCalls[name] || 1),
      calls: toolCalls[name],
      totalTokens: tokens,
    }))
    .sort((a, b) => b.tokensPerCall - a.tokensPerCall);

  return (
    <div className="grid gap-4 md:grid-cols-3">
      <section className="border border-red-900/30 bg-red-950/20 p-4">
        <div className="text-[10px] uppercase tracking-widest text-red-400 font-bold mb-2">
          ⚠️ Context Window Alert
        </div>
        <div className="space-y-2">
          <div className="text-2xl font-mono text-red-300">
            {(cachedInput / 1_000_000).toFixed(1)}M
          </div>
          <div className="text-[10px] text-red-400/80">
            {contextWindowShare.toFixed(1)}% of all input tokens
          </div>
          <div className="text-[9px] text-red-300/60 leading-relaxed pt-2">
            Recommendation: Add summarization, file chunking, and context
            pruning to reduce context window size.
          </div>
        </div>
      </section>
      <section className="border border-orange-900/30 bg-orange-950/20 p-4">
        <div className="text-[10px] uppercase tracking-widest text-orange-400 font-bold mb-2">
          ⚠️ Noisy Tool Output
        </div>
        <div className="space-y-2">
          <div className="text-[10px] text-orange-400/80 font-mono">
            Top offenders:
          </div>
          {toolsPerCall
            .filter((t) => t.tokensPerCall > 100_000)
            .slice(0, 3)
            .map((tool, i) => (
              <div key={i} className="text-[9px] text-orange-300/70">
                {tool.name}: ~{(tool.tokensPerCall / 1000).toFixed(0)}k per call
              </div>
            ))}
          <div className="text-[9px] text-orange-300/60 leading-relaxed pt-2">
            Recommendation: Add max output length, log truncation, and
            preview-only modes.
          </div>
        </div>
      </section>
      <section className="border border-amber-900/30 bg-amber-950/20 p-4">
        <div className="text-[10px] uppercase tracking-widest text-amber-400 font-bold mb-2">
          💰 Most Expensive Calls
        </div>
        <div className="space-y-2">
          {toolsPerCall.slice(0, 3).map((tool, i) => (
            <div key={i} className="text-[9px]">
              <div className="text-amber-300/80 font-mono">{tool.name}</div>
              <div className="text-amber-300/60">
                ~{(tool.tokensPerCall / 1000).toFixed(0)}k tokens/call
              </div>
            </div>
          ))}
          <div className="text-[9px] text-amber-300/60 leading-relaxed pt-2">
            Show stderr/stdout size and truncate repeated logs.
          </div>
        </div>
      </section>
    </div>
  );
}

function ExternalStatusBadge({
  ok,
  returncode,
}: Pick<ExternalAnalyticsRun, "ok" | "returncode">) {
  return (
    <span
      className={`px-2 py-1 text-[10px] uppercase tracking-wider font-bold border ${
        ok
          ? "border-emerald-800/80 text-emerald-300 bg-emerald-950/30"
          : "border-red-900/80 text-red-300 bg-red-950/30"
      }`}
    >
      {ok ? "ok" : `error ${returncode ?? ""}`.trim()}
    </span>
  );
}

function ExternalWindowMetrics({ runs }: { runs: ExternalAnalyticsRun[] }) {
  const metricMap = sortRunsByCollectedAtDesc(runs).reduce<
    Record<string, { key: string; label: string; values: number[] }>
  >((acc, run) => {
    run.summary.highlights.forEach((metric) => {
      const value = Number(metric.value);
      if (!Number.isFinite(value)) return;
      if (!acc[metric.key]) {
        acc[metric.key] = {
          key: metric.key,
          label: metric.label,
          values: [],
        };
      }
      acc[metric.key].values.push(value);
    });
    return acc;
  }, {});

  const metrics = Object.values(metricMap)
    .map((metric) => ({
      key: metric.key,
      label: metric.label,
      latest: metric.values[0],
      average:
        metric.values.reduce((sum, value) => sum + value, 0) /
        metric.values.length,
      samples: metric.values.length,
    }))
    .sort((a, b) => b.samples - a.samples || b.latest - a.latest);

  if (!metrics.length) {
    return (
      <div className="text-xs text-neutral-500 italic">
        No normalized metrics were captured across this window.
      </div>
    );
  }

  return (
    <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
      {metrics.slice(0, 4).map((metric) => (
        <div
          key={metric.key}
          className="border border-neutral-900 bg-black/30 p-3"
        >
          <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">
            {titleCaseKey(metric.label)}
          </div>
          <div className="mt-1 font-mono text-lg text-cyan-300">
            {fmtExternalValue(metric.key, metric.latest)}
          </div>
          <div className="mt-1 text-[10px] text-neutral-500">
            Avg {fmtExternalValue(metric.key, metric.average)} across{" "}
            {metric.samples} run{metric.samples === 1 ? "" : "s"}
          </div>
        </div>
      ))}
    </div>
  );
}

function ExternalObjectSnapshot({
  title,
  value,
}: {
  title: string;
  value: Record<string, unknown>;
}) {
  const primitiveEntries = Object.entries(value).filter((entry) =>
    isPrimitiveExternalValue(entry[1])
  );
  const objectEntries = Object.entries(value).filter(
    (entry): entry is [string, Record<string, unknown>] => isRecord(entry[1])
  );
  const arrayEntries = Object.entries(value).filter(
    (entry): entry is [string, unknown[]] => Array.isArray(entry[1])
  );

  return (
    <section className="border border-neutral-900 bg-black/20 p-4 space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
          {titleCaseKey(title)}
        </div>
        <div className="text-[10px] font-mono text-neutral-600">
          {Object.keys(value).length} fields
        </div>
      </div>

      {primitiveEntries.length ? (
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {primitiveEntries.slice(0, 8).map(([key, entryValue]) => (
            <div
              key={`${title}-${key}`}
              className="border border-neutral-900 bg-neutral-950/50 p-3"
            >
              <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">
                {titleCaseKey(key)}
              </div>
              <div className="mt-1 text-sm font-mono text-neutral-200 break-words">
                {formatExternalContentValue(key, entryValue)}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-xs text-neutral-500 italic">
          No scalar fields in this section.
        </div>
      )}

      {objectEntries.slice(0, 2).map(([key, nested]) => {
        const nestedEntries = Object.entries(nested).filter((entry) =>
          isPrimitiveExternalValue(entry[1])
        );
        return (
          <div
            key={`${title}-${key}`}
            className="border-t border-neutral-900 pt-3"
          >
            <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">
              {titleCaseKey(key)}
            </div>
            {nestedEntries.length ? (
              <div className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                {nestedEntries.slice(0, 8).map(([nestedKey, nestedValue]) => (
                  <div
                    key={`${title}-${key}-${nestedKey}`}
                    className="border border-neutral-900 bg-neutral-950/50 p-3"
                  >
                    <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">
                      {titleCaseKey(nestedKey)}
                    </div>
                    <div className="mt-1 text-sm font-mono text-neutral-200 break-words">
                      {formatExternalContentValue(nestedKey, nestedValue)}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="mt-2 text-xs text-neutral-500 italic">
                Nested object captured without scalar fields.
              </div>
            )}
          </div>
        );
      })}

      {arrayEntries.length ? (
        <div className="border-t border-neutral-900 pt-3">
          <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">
            Nested Collections
          </div>
          <div className="mt-2 flex flex-wrap gap-2">
            {arrayEntries.slice(0, 6).map(([key, items]) => (
              <span
                key={`${title}-${key}-collection`}
                className="border border-neutral-800 bg-neutral-900/60 px-2 py-1 text-[10px] font-mono text-neutral-400"
              >
                {titleCaseKey(key)} · {items.length} item
                {items.length === 1 ? "" : "s"}
              </span>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}

function ExternalListSnapshot({
  title,
  items,
}: {
  title: string;
  items: unknown[];
}) {
  const preview = items.slice(0, 5);
  const objectRows = preview.filter((item): item is Record<string, unknown> =>
    isRecord(item)
  );
  const primitiveRows = preview.filter((item) =>
    isPrimitiveExternalValue(item)
  );
  const allObjects = preview.length > 0 && objectRows.length === preview.length;
  const allPrimitives =
    preview.length > 0 && primitiveRows.length === preview.length;

  return (
    <section className="border border-neutral-900 bg-black/20 overflow-hidden">
      <div className="bg-neutral-900/60 border-b border-neutral-900 p-4 flex items-center justify-between gap-3">
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
          {titleCaseKey(title)}
        </div>
        <div className="text-[10px] font-mono text-neutral-600">
          Showing {preview.length} of {items.length}
        </div>
      </div>

      {items.length === 0 ? (
        <div className="p-4 text-xs text-neutral-500 italic">No items.</div>
      ) : allObjects ? (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs border-collapse">
            <thead>
              <tr className="border-b border-neutral-900 bg-neutral-950/40 text-[10px] uppercase tracking-widest text-neutral-500 font-mono">
                {Array.from(
                  new Set(objectRows.flatMap((row) => Object.keys(row)))
                )
                  .slice(0, 6)
                  .map((column) => (
                    <th key={`${title}-${column}`} className="px-4 py-3">
                      {titleCaseKey(column)}
                    </th>
                  ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-900">
              {objectRows.map((row, index) => {
                const columns = Array.from(
                  new Set(objectRows.flatMap((entry) => Object.keys(entry)))
                ).slice(0, 6);
                return (
                  <tr
                    key={`${title}-row-${index}`}
                    className="hover:bg-neutral-900/20"
                  >
                    {columns.map((column) => (
                      <td
                        key={`${title}-${index}-${column}`}
                        className="px-4 py-3 text-neutral-300 font-mono text-[11px] align-top"
                      >
                        {formatExternalContentValue(column, row[column])}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : allPrimitives ? (
        <div className="p-4 flex flex-wrap gap-2">
          {primitiveRows.map((item, index) => (
            <span
              key={`${title}-primitive-${index}`}
              className="border border-neutral-800 bg-neutral-900/60 px-2 py-1 text-[10px] font-mono text-neutral-300"
            >
              {formatExternalContentValue(title, item)}
            </span>
          ))}
        </div>
      ) : (
        <div className="p-4 space-y-2">
          {preview.map((item, index) => (
            <div
              key={`${title}-mixed-${index}`}
              className="border border-neutral-900 bg-neutral-950/40 p-3 text-[11px] text-neutral-300"
            >
              {isRecord(item) ? (
                <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                  {Object.entries(item)
                    .slice(0, 6)
                    .map(([key, entryValue]) => (
                      <div key={`${title}-${index}-${key}`}>
                        <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">
                          {titleCaseKey(key)}
                        </div>
                        <div className="mt-1 font-mono break-words">
                          {formatExternalContentValue(key, entryValue)}
                        </div>
                      </div>
                    ))}
                </div>
              ) : (
                <div className="font-mono">
                  {formatExternalContentValue(title, item)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function ExternalPayloadView({ run }: { run: ExternalAnalyticsRun }) {
  const payload = run.payload;

  if (Array.isArray(payload)) {
    return <ExternalListSnapshot title="Items" items={payload} />;
  }

  if (!isRecord(payload)) {
    return (
      <section className="border border-neutral-900 bg-black/20 p-4">
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
          Payload
        </div>
        <div className="mt-2 text-sm font-mono text-neutral-300 break-words">
          {formatExternalContentValue("payload", payload)}
        </div>
      </section>
    );
  }

  const primitiveEntries = Object.entries(payload).filter((entry) =>
    isPrimitiveExternalValue(entry[1])
  );
  const objectEntries = Object.entries(payload).filter(
    (entry): entry is [string, Record<string, unknown>] => isRecord(entry[1])
  );
  const arrayEntries = Object.entries(payload).filter(
    (entry): entry is [string, unknown[]] => Array.isArray(entry[1])
  );

  return (
    <div className="space-y-4">
      {primitiveEntries.length ? (
        <section className="border border-neutral-900 bg-black/20 p-4">
          <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold mb-3">
            Report Metadata
          </div>
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            {primitiveEntries.slice(0, 8).map(([key, entryValue]) => (
              <div
                key={`payload-meta-${key}`}
                className="border border-neutral-900 bg-neutral-950/50 p-3"
              >
                <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">
                  {titleCaseKey(key)}
                </div>
                <div className="mt-1 text-sm font-mono text-neutral-200 break-words">
                  {formatExternalContentValue(key, entryValue)}
                </div>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {objectEntries.slice(0, 4).map(([key, value]) => (
        <ExternalObjectSnapshot
          key={`payload-object-${key}`}
          title={key}
          value={value}
        />
      ))}

      {arrayEntries.slice(0, 6).map(([key, items]) => (
        <ExternalListSnapshot
          key={`payload-list-${key}`}
          title={key}
          items={items}
        />
      ))}

      {run.stderr ? (
        <section className="border border-red-950/80 bg-red-950/20 p-4">
          <div className="text-[10px] uppercase tracking-widest text-red-300 font-bold">
            Latest stderr
          </div>
          <div className="mt-2 text-[11px] leading-relaxed text-red-200 whitespace-pre-wrap font-mono">
            {run.stderr}
          </div>
        </section>
      ) : null}

      {!primitiveEntries.length &&
      !objectEntries.length &&
      !arrayEntries.length &&
      !run.stderr ? (
        <div className="border border-neutral-900 bg-black/20 p-4 text-xs text-neutral-500 italic">
          No structured payload sections were stored for this run.
        </div>
      ) : null}
    </div>
  );
}

function ExternalSchemaSummary({ run }: { run: ExternalAnalyticsRun }) {
  return (
    <section className="border border-neutral-900 bg-black/20 p-4 space-y-4">
      <div>
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
          Captured Schema
        </div>
        <div className="mt-1 text-[11px] text-neutral-500">
          Derived from the newest stored run in the selected window.
        </div>
      </div>

      <div>
        <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">
          Top-Level Keys
        </div>
        <div className="mt-2 flex flex-wrap gap-2">
          {run.summary.top_level_keys.length ? (
            run.summary.top_level_keys.slice(0, 10).map((key) => (
              <span
                key={`${run.id}-${key}`}
                className="border border-neutral-800 bg-neutral-900/60 px-2 py-1 text-[10px] font-mono text-neutral-400"
              >
                {key}
              </span>
            ))
          ) : (
            <div className="text-xs text-neutral-500 italic">
              No top-level keys captured.
            </div>
          )}
        </div>
      </div>

      <div>
        <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">
          Sections
        </div>
        <div className="mt-2 space-y-2">
          {run.summary.sections.length ? (
            run.summary.sections.slice(0, 6).map((section) => (
              <div
                key={`${run.id}-${section.name}`}
                className="flex items-center justify-between gap-3 border border-neutral-900 bg-neutral-950/40 px-3 py-2 text-[11px] font-mono text-neutral-300"
              >
                <span>{section.name}</span>
                <span className="text-neutral-500">
                  {section.kind}:{section.count}
                </span>
              </div>
            ))
          ) : (
            <div className="text-xs text-neutral-500 italic">
              No normalized sections captured.
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function ExternalRecentRuns({ runs }: { runs: ExternalAnalyticsRun[] }) {
  const items = sortRunsByCollectedAtDesc(runs).slice(0, 5);

  return (
    <section className="border border-neutral-900 bg-black/20 p-4 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
          Recent Runs
        </div>
        <div className="text-[10px] font-mono text-neutral-600">
          Latest {items.length}
        </div>
      </div>

      {items.map((run) => (
        <div
          key={run.id}
          className="border border-neutral-900 bg-neutral-950/40 p-3 space-y-2"
        >
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-[11px] font-mono text-neutral-300">
                {fmtTimestamp(run.collected_at)}
              </div>
              <div className="mt-1 text-[10px] uppercase tracking-widest text-neutral-500">
                Source period: {run.period}
              </div>
            </div>
            <ExternalStatusBadge ok={run.ok} returncode={run.returncode} />
          </div>

          <div className="text-[10px] font-mono text-neutral-500 break-all">
            {summarizeExternalText(run.command_display || "—", 96)}
          </div>

          <div className="text-[10px] text-cyan-300/80">
            {formatExternalHighlightsPreview(run.summary.highlights)}
          </div>
        </div>
      ))}
    </section>
  );
}

function ExternalToolPanels({
  runs,
  days,
}: {
  runs: ExternalAnalyticsResponse["runs"];
  days: number;
}) {
  const grouped = Object.values(
    runs.reduce<Record<string, ExternalAnalyticsRun[]>>((acc, run) => {
      const tool = run.tool || "unknown";
      if (!acc[tool]) acc[tool] = [];
      acc[tool].push(run);
      return acc;
    }, {})
  )
    .map((toolRuns) => sortRunsByCollectedAtDesc(toolRuns))
    .sort((a, b) =>
      String(a[0]?.collected_at || "").localeCompare(
        String(b[0]?.collected_at || "")
      )
    )
    .reverse();

  if (!grouped.length) {
    return (
      <div className="border border-neutral-800 bg-neutral-950/40 p-5 text-sm text-neutral-500 italic">
        No external analyzer runs were captured in the last {days} day
        {days === 1 ? "" : "s"}.
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {grouped.map((toolRuns) => {
        const { selectedPeriod, selectedRuns, observedPeriods } =
          selectExternalRunsForDays(toolRuns, days);
        const latest = selectedRuns[0];
        const earliest = selectedRuns[selectedRuns.length - 1];
        const successCount = selectedRuns.filter((run) => run.ok).length;
        const selectedPeriodLabel = titleCaseKey(
          selectedPeriod || latest.period || "latest"
        );

        return (
          <section
            key={`external-tool-${latest.tool}`}
            className="border border-neutral-800 bg-neutral-950/40 p-5 space-y-5"
          >
            <div className="flex items-start justify-between gap-4">
              <div>
                <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
                  {latest.tool}
                </div>
                <div className="mt-2 text-xl font-semibold text-white capitalize">
                  {selectedRuns.length} run
                  {selectedRuns.length === 1 ? "" : "s"} matched to the
                  selected window
                </div>
                <div className="mt-1 text-xs text-neutral-500">
                  {fmtTimestamp(earliest.collected_at)} to{" "}
                  {fmtTimestamp(latest.collected_at)}
                </div>
                <div className="mt-1 text-[11px] text-neutral-500">
                  Using {selectedPeriodLabel.toLowerCase()} snapshots for the {days}
                  -day view.
                </div>
              </div>
              <ExternalStatusBadge
                ok={latest.ok}
                returncode={latest.returncode}
              />
            </div>

            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
              <div className="border border-neutral-900 bg-black/30 p-3">
                <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">
                  Runs Used
                </div>
                <div className="mt-1 text-xl font-mono text-neutral-100">
                  {selectedRuns.length}
                </div>
                <div className="text-[10px] text-neutral-500">
                  Chosen from {toolRuns.length} stored run
                  {toolRuns.length === 1 ? "" : "s"}
                </div>
              </div>
              <div className="border border-neutral-900 bg-black/30 p-3">
                <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">
                  Success Rate
                </div>
                <div className="mt-1 text-xl font-mono text-emerald-300">
                  {(
                    (successCount / Math.max(toolRuns.length, 1)) *
                    100
                  ).toFixed(1)}
                  %
                </div>
                <div className="text-[10px] text-neutral-500">
                  {successCount} ok / {toolRuns.length - successCount} failed
                </div>
              </div>
              <div className="border border-neutral-900 bg-black/30 p-3">
                <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">
                  Periods Observed
                </div>
                <div className="mt-1 text-sm font-mono text-neutral-100 break-words">
                  {observedPeriods.map(titleCaseKey).join(", ") || "—"}
                </div>
                <div className="text-[10px] text-neutral-500">
                  Stored period labels from upstream runs
                </div>
              </div>
              <div className="border border-neutral-900 bg-black/30 p-3">
                <div className="text-[9px] uppercase tracking-widest text-neutral-500 font-bold">
                  Selected Period
                </div>
                <div className="mt-1 text-sm font-mono text-cyan-300 break-words">
                  {selectedPeriodLabel}
                </div>
                <div className="text-[10px] text-neutral-500">
                  Closest stored match for the {days}-day view
                </div>
              </div>
            </div>

            <div className="grid gap-4 xl:grid-cols-[1.2fr,0.8fr]">
              <section className="border border-neutral-900 bg-black/20 p-4 space-y-4">
                <div>
                  <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
                    Window Metrics
                  </div>
                  <div className="mt-1 text-[11px] text-neutral-500">
                    Latest value plus the average across selected {" "}
                    {selectedPeriodLabel.toLowerCase()} snapshots.
                  </div>
                </div>
                <ExternalWindowMetrics runs={selectedRuns} />
              </section>

              <div className="space-y-4">
                <ExternalRecentRuns runs={selectedRuns} />
                <ExternalSchemaSummary run={latest} />
              </div>
            </div>

            <section className="space-y-4">
              <div>
                <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
                  Selected Report Content
                </div>
                <div className="mt-1 text-[11px] text-neutral-500">
                  Structured view of the newest {selectedPeriodLabel.toLowerCase()}
                  {" "}snapshot within the selected window.
                </div>
              </div>
              <ExternalPayloadView run={latest} />
            </section>
          </section>
        );
      })}
    </div>
  );
}

function ExternalRunsTable({
  runs,
}: {
  runs: ExternalAnalyticsResponse["runs"];
}) {
  const items = sortRunsByCollectedAtDesc(runs);

  return (
    <section className="border border-neutral-800 bg-neutral-950/40 overflow-hidden">
      <div className="bg-neutral-900/80 border-b border-neutral-800 p-4">
        <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-bold">
          External Run History
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs border-collapse">
          <thead>
            <tr className="border-b border-neutral-800 text-[10px] uppercase tracking-widest text-neutral-500 font-mono bg-neutral-900/50">
              <th className="px-4 py-3">Tool</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3">Period</th>
              <th className="px-4 py-3">Collected</th>
              <th className="px-4 py-3">Command</th>
              <th className="px-4 py-3">Metric Snapshot</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-900">
            {!items.length ? (
              <tr>
                <td
                  colSpan={6}
                  className="px-4 py-8 text-center text-neutral-600 italic"
                >
                  No external runs stored yet.
                </td>
              </tr>
            ) : (
              items.map((run) => (
                <tr
                  key={run.id}
                  className="hover:bg-neutral-800/20 transition-colors align-top"
                >
                  <td className="px-4 py-3 font-mono text-cyan-300/80">
                    {run.tool}
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={`text-[10px] uppercase tracking-wider font-bold ${
                        run.ok ? "text-emerald-300" : "text-red-300"
                      }`}
                    >
                      {run.ok ? "ok" : `error ${run.returncode ?? ""}`.trim()}
                    </span>
                  </td>
                  <td className="px-4 py-3 font-mono text-neutral-400">
                    {run.period}
                  </td>
                  <td className="px-4 py-3 text-neutral-500">
                    {fmtTimestamp(run.collected_at)}
                  </td>
                  <td className="px-4 py-3 text-neutral-500 font-mono text-[10px] max-w-[280px] break-all">
                    {run.command_display || "—"}
                  </td>
                  <td className="px-4 py-3 text-neutral-400 text-[11px] max-w-[320px]">
                    {formatExternalHighlightsPreview(run.summary.highlights)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ---- Main component --------------------------------------------------------

export default function Analytics() {
  const [data, setData] = useState<GranularToolUsage[]>([]);
  const [dashboard, setDashboard] = useState<AnalyticsDashboard | null>(null);
  const [externalData, setExternalData] =
    useState<ExternalAnalyticsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [dashLoading, setDashLoading] = useState(true);
  const [externalLoading, setExternalLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [externalErr, setExternalErr] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>("Overview");

  // Filters
  const [agentFilter, setAgentFilter] = useState("all");
  const [modelFilter, setModelFilter] = useState("all");
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [dateRange, setDateRange] = useState({ days: 30 });

  useEffect(() => {
    const externalLimit = Math.min(1000, Math.max(180, dateRange.days * 12));

    setLoading(true);
    api
      .granularAnalytics(undefined, undefined, 5000, dateRange.days)
      .then(setData)
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));

    setDashLoading(true);
    api
      .analyticsDashboard(dateRange.days)
      .then(setDashboard)
      .catch(() => setDashboard(null))
      .finally(() => setDashLoading(false));

    setExternalLoading(true);
    setExternalErr(null);
    api
      .externalAnalytics(dateRange.days, undefined, externalLimit)
      .then(setExternalData)
      .catch((e) => {
        setExternalErr(String(e));
        setExternalData(null);
      })
      .finally(() => setExternalLoading(false));
  }, [dateRange.days]);

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

  const stats = useMemo(() => {
    const totalOutputTokens = filteredData
      .filter((d) => ["result", "thinking", "tool_call"].includes(d.event_type))
      .reduce((acc, item) => acc + item.output_tokens, 0);
    const toolCalls = filteredData
      .filter((d) => d.event_type === "tool_call")
      .reduce((acc, item) => acc + (item.call_count ?? 1), 0);
    const uniqueTools = new Set(
      filteredData
        .filter((d) => d.event_type === "tool_call")
        .map((item) => item.tool_name)
    ).size;
    const cachedPromptTokens = filteredData
      .filter((d) => d.event_type === "cached_prompt")
      .reduce((acc, item) => acc + item.input_tokens, 0);
    const toolOutputTokens = filteredData
      .filter((d) => d.event_type === "tool_call")
      .reduce((acc, item) => acc + item.output_tokens, 0);
    const totalCost = filteredData.reduce((acc, item) => {
      if (
        [
          "prompt",
          "cached_prompt",
          "cache_create",
          "result",
          "thinking",
        ].includes(item.event_type)
      ) {
        return acc + (item.cost || 0);
      }
      return acc;
    }, 0);
    const estimatedMonthlyCost = totalCost * (30 / (dateRange.days || 1));
    const toolCosts = defaultdict_int();
    filteredData.forEach((item) => {
      toolCosts[item.tool_name] += item.cost || 0;
    });
    const topCostDriver =
      Object.entries(toolCosts).sort((a, b) => b[1] - a[1])[0]?.[0] || "—";
    return {
      totalCost,
      estimatedMonthlyCost,
      topCostDriver,
      toolOutputTokens,
      cachedPromptTokens,
      toolCalls,
      uniqueTools,
      totalOutputTokens,
    };
  }, [filteredData, dateRange.days]);

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
            (item.output_tokens / (stats.totalOutputTokens || 1)) * 100,
        };
      })
      .sort((a, b) => b.cost - a.cost);
  }, [filteredData, stats.totalOutputTokens]);

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
          <p className="text-neutral-500 text-sm mt-1">
            Real-time token attribution and economic breakdown.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase font-bold text-neutral-500">
              Days
            </span>
            <input
              type="number"
              value={dateRange.days}
              onChange={(e) =>
                setDateRange({ days: parseInt(e.target.value) || 30 })
              }
              className="w-16 bg-neutral-900 border border-neutral-700 px-2 py-1 text-xs font-mono text-neutral-300 focus:outline-none focus:border-emerald-500"
            />
          </div>
          <div className="h-4 w-px bg-neutral-800 mx-1 hidden md:block" />
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

      {/* Summary metrics */}
      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard
          label="Total Estimated Cost"
          value={`$${stats.totalCost.toFixed(2)}`}
          tone="emerald"
        />
        <MetricCard
          label="Projected Month-End"
          value={`$${stats.estimatedMonthlyCost.toFixed(2)}`}
          tone="emerald"
        />
        <MetricCard
          label="Total Tool Calls"
          value={stats.toolCalls.toLocaleString()}
          tone="cyan"
        />
        <MetricCard
          label="Unique Tools"
          value={stats.uniqueTools.toString()}
          tone="cyan"
        />
      </section>

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
                        No data.
                      </td>
                    </tr>
                  ) : (
                    hostModelStats.map((row: DashboardHostModelOverview, idx) => (
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
                        <td className="px-4 py-2 text-right font-mono text-orange-400/80">
                          {(row.cache_write_tokens / 1_000_000).toFixed(1)}
                        </td>
                        <td className="px-4 py-2 text-right font-mono text-violet-400/80">
                          {(row.billable_output_tokens / 1_000_000).toFixed(1)}
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
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <CostDriversChart data={filteredData} stats={stats} />
          <OptimizationCards data={filteredData} />

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
                                (item.tokens / (stats.toolOutputTokens || 1)) *
                                100
                              ).toFixed(1)}
                              %
                            </span>
                            <div className="w-12 h-1 bg-neutral-900 rounded-full overflow-hidden">
                              <div
                                className="h-full bg-amber-500/50"
                                style={{
                                  width: `${(item.tokens / (stats.toolOutputTokens || 1)) * 100}%`,
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

      {/* Timeline tab */}
      {activeTab === "Timeline" && (
        <div className="space-y-6">
          {dashLoading ? (
            <div className="text-neutral-500 italic text-sm animate-pulse">
              Loading...
            </div>
          ) : dashboard ? (
            <>
              <DailyChart daily={dashboard.daily} />
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
            <SavingsInsights dashboard={dashboard} />
          ) : (
            <div className="text-neutral-600 italic text-sm">
              Data unavailable.
            </div>
          )}
        </div>
      )}

      {/* External tab */}
      {activeTab === "External" && (
        <div className="space-y-6">
          {externalLoading ? (
            <div className="text-neutral-500 italic text-sm animate-pulse">
              Loading external analyzer snapshots...
            </div>
          ) : externalData ? (
            <>
              <section className="grid gap-4 md:grid-cols-4">
                <MetricCard
                  label={`Captured Runs (${dateRange.days}d)`}
                  value={externalData.totals.runs_total.toString()}
                  tone="emerald"
                />
                <MetricCard
                  label="Successful In Window"
                  value={externalData.totals.successful_runs.toString()}
                  tone="cyan"
                />
                <MetricCard
                  label="Failed In Window"
                  value={externalData.totals.failed_runs.toString()}
                  tone="amber"
                />
                <MetricCard
                  label="Tools In Window"
                  value={Object.keys(
                    externalData.latest_by_tool
                  ).length.toString()}
                  tone="neutral"
                />
              </section>

              <section className="border border-neutral-800 bg-neutral-950/40 p-4 text-sm text-neutral-500 leading-relaxed">
                <span className="font-mono text-neutral-300">servicectl</span>{" "}
                now stores multiple upstream periods per analyzer run. This view
                chooses the closest stored period for the selected {dateRange.days}
                -day range, so a 30-day window prefers monthly snapshots when
                they exist, then falls back to the nearest available period.
              </section>

              <ExternalToolPanels
                runs={externalData.runs}
                days={dateRange.days}
              />
              <ExternalRunsTable runs={externalData.runs} />
            </>
          ) : (
            <div className="border border-neutral-800 bg-neutral-950/40 p-5 text-sm text-neutral-500 italic">
              {externalErr || "No external analyzer data available yet."}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
