import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  api,
  type ExternalAnalyticsResponse,
  type ExternalAnalyticsRun,
} from "../api";
import { MetricCard } from "../components/WorkbenchUI";
import { useTimeRange } from "../lib/TimeRangeContext";

const TOOL_PRIORITY = [
  "codeburn",
  "codeburn:optimize",
  "tokscale",
  "ccusage",
] as const;
const TOKSCALE_TABS = [
  "Overview",
  "Models",
  "Daily",
  "Hourly",
  "Stats",
  "Agents",
] as const;
const EXTERNAL_PERIOD_DAY_SPAN: Record<string, number> = {
  today: 1,
  week: 7,
  month: 30,
  "30days": 30,
  all: 3650,
};
const EXTERNAL_MAX_LIMIT = 200;

type TokscaleTab = (typeof TOKSCALE_TABS)[number];
type TableRow = Record<string, unknown>;
type TableColumn = {
  label: string;
  align?: "left" | "right";
  render: (row: TableRow) => ReactNode;
};
type ToolWindow = {
  tool: string;
  allRuns: ExternalAnalyticsRun[];
  selectedRuns: ExternalAnalyticsRun[];
  latest: ExternalAnalyticsRun;
  selectedPeriod: string | null;
  observedPeriods: string[];
};

function isRecord(value: unknown): value is TableRow {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asRecord(value: unknown): TableRow {
  return isRecord(value) ? value : {};
}

function asRecordArray(value: unknown): TableRow[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value
        .map((item) => (item == null ? "" : String(item).trim()))
        .filter(Boolean)
    : [];
}

function toNumber(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const cleaned = value.trim().replace(/[^0-9.+-]/g, "");
    const parsed = Number(cleaned);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

function toText(value: unknown) {
  if (value == null || value === "") return "-";
  if (typeof value === "string") return value;
  if (typeof value === "number") {
    return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2);
  }
  return String(value);
}

function fmtCurrency(value: unknown, decimals = 2) {
  return `$${toNumber(value).toFixed(decimals)}`;
}

function fmtTokens(value: unknown) {
  const n = toNumber(value);
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return Math.round(n).toLocaleString();
}

function fmtPercent(value: unknown, decimals = 1) {
  const raw = toNumber(value);
  const pct = raw <= 1 ? raw * 100 : raw;
  return `${pct.toFixed(decimals)}%`;
}

function fmtTimestamp(value: string) {
  if (!value) return "-";
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

function displayToolName(tool: string) {
  switch (tool) {
    case "codeburn":
      return "CodeBurn";
    case "codeburn:optimize":
      return "CodeBurn Optimize";
    case "tokscale":
      return "Tokscale";
    case "ccusage":
      return "ccusage";
    default:
      return titleCaseKey(tool);
  }
}

function toolDescription(tool: string) {
  switch (tool) {
    case "codeburn":
      return "CodeBurn is best at showing where spend clusters by project, model, activity, and session. This view keeps the high-signal slices visible without dumping the whole payload at once.";
    case "codeburn:optimize":
      return "CodeBurn Optimize captures historical waste patterns and explicit next actions. This keeps the recommendations readable instead of burying them in one long terminal dump.";
    case "tokscale":
      return "Tokscale is now captured as a bundled report: the overview snapshot plus native models, hourly, monthly, and graph outputs. Older stored snapshots still show limited data until they are refreshed.";
    case "ccusage":
      return "ccusage is the most popular community-built Claude Code usage tracker. Atelier captures its daily + session bundles as an independent cross-check on Anthropic dedup logic — if ccusage and Atelier disagree on Claude totals, suspect chunked-message accounting rather than pricing.";
    default:
      return "This tool does not have a specialized Atelier renderer yet, so this page keeps the important capture metadata visible and falls back to a shallow payload summary.";
  }
}

function sortRunsByCollectedAtDesc<T extends { collected_at: string }>(
  items: T[]
) {
  return [...items].sort((a, b) =>
    String(b.collected_at).localeCompare(String(a.collected_at))
  );
}

function normalizeExternalPeriod(period: string | null | undefined) {
  return String(period || "")
    .trim()
    .toLowerCase();
}

function pickPreferredExternalPeriod(
  runs: ExternalAnalyticsRun[],
  days: number
) {
  const targetDays = Math.max(1, days);
  const periods = Array.from(
    new Set(
      runs.map((run) => normalizeExternalPeriod(run.period)).filter(Boolean)
    )
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

function selectExternalRunsForDays(runs: ExternalAnalyticsRun[], days: number) {
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
        sortedRuns
          .map((run) => normalizeExternalPeriod(run.period))
          .filter(Boolean)
      )
    ),
  };
}

function toolRank(tool: string) {
  const idx = TOOL_PRIORITY.indexOf(tool as (typeof TOOL_PRIORITY)[number]);
  return idx === -1 ? Number.POSITIVE_INFINITY : idx;
}

function buildToolWindows(runs: ExternalAnalyticsRun[], days: number) {
  const grouped = runs.reduce<Record<string, ExternalAnalyticsRun[]>>(
    (acc, run) => {
      const tool = run.tool || "unknown";
      if (!acc[tool]) acc[tool] = [];
      acc[tool].push(run);
      return acc;
    },
    {}
  );

  return Object.entries(grouped)
    .map(([tool, toolRuns]) => {
      const allRuns = sortRunsByCollectedAtDesc(toolRuns);
      const { selectedPeriod, selectedRuns, observedPeriods } =
        selectExternalRunsForDays(allRuns, days);
      return {
        tool,
        allRuns,
        selectedRuns,
        latest: selectedRuns[0] ?? allRuns[0],
        selectedPeriod,
        observedPeriods,
      } satisfies ToolWindow;
    })
    .filter((window) => window.latest)
    .sort(
      (left, right) =>
        toolRank(left.tool) - toolRank(right.tool) ||
        left.tool.localeCompare(right.tool)
    );
}

function RelativeBar({
  value,
  max,
  tone = "bg-cyan-400/60",
}: {
  value: number;
  max: number;
  tone?: string;
}) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  return (
    <div className="h-1.5 w-20 overflow-hidden rounded-full bg-neutral-900">
      <div
        className={`h-full rounded-full ${tone}`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function toolTabClasses(tool: string, active: boolean) {
  if (tool === "tokscale") {
    return active
      ? "border-cyan-500/50 bg-cyan-500/10 text-cyan-200"
      : "border-neutral-800 bg-neutral-900/40 text-neutral-400 hover:border-cyan-900 hover:text-cyan-200";
  }
  if (tool === "codeburn:optimize") {
    return active
      ? "border-amber-500/50 bg-amber-500/10 text-amber-100"
      : "border-neutral-800 bg-neutral-900/40 text-neutral-400 hover:border-amber-900 hover:text-amber-100";
  }
  if (tool === "ccusage") {
    return active
      ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-200"
      : "border-neutral-800 bg-neutral-900/40 text-neutral-400 hover:border-emerald-900 hover:text-emerald-200";
  }
  return active
    ? "border-purple-500/50 bg-purple-500/10 text-purple-100"
    : "border-neutral-800 bg-neutral-900/40 text-neutral-400 hover:border-purple-900 hover:text-purple-100";
}

function CompactTableSection({
  title,
  subtitle,
  rows,
  columns,
  emptyLabel = "No rows captured.",
}: {
  title: string;
  subtitle?: string;
  rows: TableRow[];
  columns: TableColumn[];
  emptyLabel?: string;
}) {
  return (
    <section className="border border-neutral-800 bg-neutral-950/50 overflow-hidden">
      <div className="border-b border-neutral-800 bg-neutral-900/70 p-4">
        <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
          {title}
        </div>
        {subtitle && (
          <div className="mt-1 text-xs text-neutral-500">{subtitle}</div>
        )}
      </div>
      {!rows.length ? (
        <div className="p-4 text-sm italic text-neutral-500">{emptyLabel}</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr className="border-b border-neutral-800 bg-neutral-900/40 text-[10px] font-mono uppercase tracking-widest text-neutral-500">
                {columns.map((column) => (
                  <th
                    key={`${title}-${column.label}`}
                    className={`px-4 py-3 ${column.align === "right" ? "text-right" : "text-left"}`}
                  >
                    {column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-900">
              {rows.map((row, index) => (
                <tr
                  key={`${title}-${index}`}
                  className="align-top hover:bg-neutral-900/20"
                >
                  {columns.map((column) => (
                    <td
                      key={`${title}-${index}-${column.label}`}
                      className={`px-4 py-3 ${column.align === "right" ? "text-right" : "text-left"}`}
                    >
                      {column.render(row)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function EmptyPanel({ title, detail }: { title: string; detail: string }) {
  return (
    <section className="border border-neutral-800 bg-neutral-950/50 p-6">
      <div className="text-sm font-semibold text-neutral-100">{title}</div>
      <div className="mt-2 max-w-2xl text-sm leading-relaxed text-neutral-400">
        {detail}
      </div>
    </section>
  );
}

function CodeBurnDailyChart({ rows }: { rows: TableRow[] }) {
  if (!rows.length) {
    return (
      <EmptyPanel
        title="Daily Activity"
        detail="No daily rows were captured in this snapshot."
      />
    );
  }

  const recent = [...rows]
    .sort((left, right) => String(left.date).localeCompare(String(right.date)))
    .slice(-14);
  const maxCost = Math.max(...recent.map((row) => toNumber(row.cost)), 0.01);

  return (
    <section className="border border-neutral-800 bg-neutral-950/50 p-5 space-y-4">
      <div>
        <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
          Daily Activity
        </div>
        <div className="mt-1 text-sm text-neutral-400">
          Last {recent.length} daily snapshots in the selected window.
        </div>
      </div>

      <div className="flex items-end gap-2 overflow-x-auto pb-2">
        {recent.map((row) => {
          const cost = toNumber(row.cost);
          return (
            <div
              key={String(row.date)}
              className="flex min-w-[48px] flex-col items-center gap-2"
            >
              <div className="text-[10px] font-mono text-emerald-300">
                {fmtCurrency(cost)}
              </div>
              <div className="flex h-28 items-end">
                <div
                  className="w-7 rounded-t-sm bg-purple-400/70"
                  style={{ height: `${Math.max((cost / maxCost) * 112, 6)}px` }}
                />
              </div>
              <div className="text-[10px] font-mono text-neutral-500">
                {toText(row.date).slice(5)}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function TokscaleDailyChart({ rows }: { rows: TableRow[] }) {
  if (!rows.length) {
    return (
      <EmptyPanel
        title="Daily View"
        detail="No daily Tokscale contributions were captured in this snapshot."
      />
    );
  }

  const daily = [...rows].sort((left, right) =>
    String(left.date).localeCompare(String(right.date))
  );
  const maxCost = Math.max(
    ...daily.map((row) => toNumber(asRecord(row.totals).cost)),
    0.01
  );

  return (
    <section className="border border-neutral-800 bg-neutral-950/50 p-5 space-y-4">
      <div>
        <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
          Daily Spend
        </div>
        <div className="mt-1 text-sm text-neutral-400">
          Native day-level contributions from Tokscale&apos;s graph export.
        </div>
      </div>

      <div className="flex items-end gap-2 overflow-x-auto pb-2">
        {daily.map((row) => {
          const totals = asRecord(row.totals);
          const cost = toNumber(totals.cost);
          const messages = Math.round(toNumber(totals.messages));
          return (
            <div
              key={toText(row.date)}
              className="flex min-w-[54px] flex-col items-center gap-2"
              title={`${toText(row.date)} · ${fmtCurrency(cost, 3)} · ${messages.toLocaleString()} messages`}
            >
              <div className="text-[10px] font-mono text-cyan-300">
                {fmtCurrency(cost, 3)}
              </div>
              <div className="flex h-28 items-end">
                <div
                  className="w-8 rounded-t-sm bg-cyan-400/70"
                  style={{ height: `${Math.max((cost / maxCost) * 112, 6)}px` }}
                />
              </div>
              <div className="text-[10px] font-mono text-neutral-500">
                {toText(row.date).slice(5)}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function CodeBurnPanel({ toolWindow }: { toolWindow: ToolWindow }) {
  const payload = asRecord(toolWindow.latest.payload);
  const overview = asRecord(payload.overview);
  const tokens = asRecord(overview.tokens);
  const models = asRecordArray(payload.models).sort(
    (left, right) => toNumber(right.cost) - toNumber(left.cost)
  );
  const activities = asRecordArray(payload.activities).sort(
    (left, right) => toNumber(right.cost) - toNumber(left.cost)
  );
  const projects = asRecordArray(payload.projects).sort(
    (left, right) => toNumber(right.cost) - toNumber(left.cost)
  );
  const providers = asRecordArray(payload.providerEntries).sort(
    (left, right) => toNumber(right.costUSD) - toNumber(left.costUSD)
  );
  const topSessions = asRecordArray(payload.topSessions).sort(
    (left, right) => toNumber(right.cost) - toNumber(left.cost)
  );
  const tools = asRecordArray(payload.tools).sort(
    (left, right) => toNumber(right.calls) - toNumber(left.calls)
  );
  const shellCommands = asRecordArray(payload.shellCommands).sort(
    (left, right) => toNumber(right.calls) - toNumber(left.calls)
  );
  const mcpServers = asRecordArray(payload.mcpServers).sort(
    (left, right) => toNumber(right.calls) - toNumber(left.calls)
  );
  const maxModelCost = Math.max(
    ...models.map((row) => toNumber(row.cost)),
    0.01
  );

  return (
    <div className="space-y-6">
      <section className="border border-neutral-800 bg-neutral-950/50 p-5">
        <div className="grid gap-4 md:grid-cols-4">
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Spend
            </div>
            <div className="mt-2 text-lg font-semibold text-amber-300">
              {fmtCurrency(overview.cost)}
            </div>
          </div>
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Calls
            </div>
            <div className="mt-2 text-lg font-semibold text-neutral-100">
              {Math.round(toNumber(overview.calls)).toLocaleString()}
            </div>
          </div>
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Sessions
            </div>
            <div className="mt-2 text-lg font-semibold text-neutral-100">
              {Math.round(toNumber(overview.sessions)).toLocaleString()}
            </div>
          </div>
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Cache Hit
            </div>
            <div className="mt-2 text-lg font-semibold text-cyan-300">
              {fmtPercent(overview.cacheHitPercent)}
            </div>
          </div>
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Input
            </div>
            <div className="mt-2 text-lg font-semibold text-emerald-300">
              {fmtTokens(tokens.input)}
            </div>
          </div>
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Output
            </div>
            <div className="mt-2 text-lg font-semibold text-red-300">
              {fmtTokens(tokens.output)}
            </div>
          </div>
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Cache Read
            </div>
            <div className="mt-2 text-lg font-semibold text-cyan-300">
              {fmtTokens(tokens.cacheRead)}
            </div>
          </div>
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Cache Write
            </div>
            <div className="mt-2 text-lg font-semibold text-amber-300">
              {fmtTokens(tokens.cacheWrite)}
            </div>
          </div>
        </div>
      </section>

      <div className="grid gap-4 xl:grid-cols-2">
        <div className="space-y-4">
          <CodeBurnDailyChart rows={asRecordArray(payload.daily)} />

          <CompactTableSection
            title="By Provider"
            rows={providers}
            columns={[
              {
                label: "Provider",
                render: (row) => (
                  <span className="font-semibold text-neutral-100">
                    {toText(row.providerDisplayName || row.provider)}
                  </span>
                ),
              },
              {
                label: "Models",
                align: "right",
                render: (row) => (
                  <span className="font-mono text-neutral-300">
                    {Math.round(toNumber(row.models)).toLocaleString()}
                  </span>
                ),
              },
              {
                label: "Cost",
                align: "right",
                render: (row) => (
                  <span className="font-mono text-amber-300">
                    {fmtCurrency(row.costUSD, 3)}
                  </span>
                ),
              },
            ]}
          />
        </div>

        <CompactTableSection
          title="Models by Cost"
          subtitle="Closest match to the terminal model ranking view."
          rows={models.slice(0, 10)}
          columns={[
            {
              label: "Model",
              render: (row) => (
                <div className="space-y-1">
                  <div className="font-mono text-[11px] text-neutral-100">
                    {toText(row.name)}
                  </div>
                  <div className="text-[10px] text-neutral-500">
                    {toText(row.provider)} / {toText(row.source)}
                  </div>
                </div>
              ),
            },
            {
              label: "Input",
              align: "right",
              render: (row) => (
                <span className="font-mono text-emerald-300">
                  {fmtTokens(row.inputTokens)}
                </span>
              ),
            },
            {
              label: "Output",
              align: "right",
              render: (row) => (
                <span className="font-mono text-red-300">
                  {fmtTokens(row.outputTokens)}
                </span>
              ),
            },
            {
              label: "Cache",
              align: "right",
              render: (row) => (
                <span className="font-mono text-cyan-300">
                  {fmtTokens(row.cacheReadTokens)}
                </span>
              ),
            },
            {
              label: "1-Shot",
              align: "right",
              render: (row) => (
                <span className="font-mono text-neutral-400">
                  {row.oneShotRate == null ? "-" : fmtPercent(row.oneShotRate)}
                </span>
              ),
            },
            {
              label: "Cost",
              align: "right",
              render: (row) => (
                <div className="inline-flex items-center gap-3">
                  <span className="font-mono text-amber-300">
                    {fmtCurrency(row.cost)}
                  </span>
                  <RelativeBar
                    value={toNumber(row.cost)}
                    max={maxModelCost}
                    tone="bg-purple-400/70"
                  />
                </div>
              ),
            },
          ]}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <CompactTableSection
          title="By Activity"
          rows={activities}
          columns={[
            {
              label: "Activity",
              render: (row) => (
                <span className="font-semibold text-neutral-100">
                  {toText(row.category)}
                </span>
              ),
            },
            {
              label: "Turns",
              align: "right",
              render: (row) => (
                <span className="font-mono text-neutral-300">
                  {Math.round(toNumber(row.turns)).toLocaleString()}
                </span>
              ),
            },
            {
              label: "1-Shot",
              align: "right",
              render: (row) => (
                <span className="font-mono text-neutral-400">
                  {row.oneShotRate == null ? "-" : fmtPercent(row.oneShotRate)}
                </span>
              ),
            },
            {
              label: "Cost",
              align: "right",
              render: (row) => (
                <span className="font-mono text-amber-300">
                  {fmtCurrency(row.cost)}
                </span>
              ),
            },
          ]}
        />
        <CompactTableSection
          title="By Project"
          rows={projects}
          columns={[
            {
              label: "Project",
              render: (row) => (
                <div className="space-y-1">
                  <div className="font-semibold text-neutral-100">
                    {toText(row.name)}
                  </div>
                  <div className="text-[10px] font-mono text-neutral-500">
                    {toText(row.path)}
                  </div>
                </div>
              ),
            },
            {
              label: "Sessions",
              align: "right",
              render: (row) => (
                <span className="font-mono text-neutral-300">
                  {Math.round(toNumber(row.sessions)).toLocaleString()}
                </span>
              ),
            },
            {
              label: "Avg",
              align: "right",
              render: (row) => (
                <span className="font-mono text-neutral-400">
                  {fmtCurrency(row.avgCostPerSession, 3)}
                </span>
              ),
            },
            {
              label: "Cost",
              align: "right",
              render: (row) => (
                <span className="font-mono text-amber-300">
                  {fmtCurrency(row.cost)}
                </span>
              ),
            },
          ]}
        />
        <CompactTableSection
          title="Top Sessions"
          rows={topSessions}
          columns={[
            {
              label: "Date",
              render: (row) => (
                <span className="font-mono text-neutral-400">
                  {toText(row.date)}
                </span>
              ),
            },
            {
              label: "Project",
              render: (row) => (
                <span className="text-neutral-100">{toText(row.project)}</span>
              ),
            },
            {
              label: "Calls",
              align: "right",
              render: (row) => (
                <span className="font-mono text-neutral-300">
                  {Math.round(toNumber(row.calls)).toLocaleString()}
                </span>
              ),
            },
            {
              label: "Cost",
              align: "right",
              render: (row) => (
                <span className="font-mono text-amber-300">
                  {fmtCurrency(row.cost)}
                </span>
              ),
            },
          ]}
        />
        <CompactTableSection
          title="Shell Commands"
          rows={shellCommands.slice(0, 12)}
          columns={[
            {
              label: "Command",
              render: (row) => (
                <span className="font-mono text-neutral-100">
                  {toText(row.name)}
                </span>
              ),
            },
            {
              label: "Calls",
              align: "right",
              render: (row) => (
                <span className="font-mono text-neutral-300">
                  {Math.round(toNumber(row.calls)).toLocaleString()}
                </span>
              ),
            },
          ]}
        />
        <CompactTableSection
          title="Core Tools"
          rows={tools.slice(0, 12)}
          columns={[
            {
              label: "Tool",
              render: (row) => (
                <span className="font-mono text-neutral-100">
                  {toText(row.name)}
                </span>
              ),
            },
            {
              label: "Calls",
              align: "right",
              render: (row) => (
                <span className="font-mono text-neutral-300">
                  {Math.round(toNumber(row.calls)).toLocaleString()}
                </span>
              ),
            },
          ]}
        />
        <CompactTableSection
          title="MCP Servers"
          rows={mcpServers.slice(0, 12)}
          emptyLabel="No MCP usage was captured in this CodeBurn snapshot."
          columns={[
            {
              label: "Server",
              render: (row) => (
                <span className="font-mono text-neutral-100">
                  {toText(row.name || row.server || row.id)}
                </span>
              ),
            },
            {
              label: "Calls",
              align: "right",
              render: (row) => (
                <span className="font-mono text-neutral-300">
                  {Math.round(toNumber(row.calls)).toLocaleString()}
                </span>
              ),
            },
          ]}
        />
      </div>
    </div>
  );
}

function CodeBurnOptimizePanel({ toolWindow }: { toolWindow: ToolWindow }) {
  const payload = asRecord(toolWindow.latest.payload);
  const overview = asRecord(payload.overview);
  const recommendations = asRecordArray(payload.recommendations).sort(
    (left, right) =>
      toNumber(right.estimated_usd_saved) - toNumber(left.estimated_usd_saved)
  );

  return (
    <div className="space-y-6">
      <section className="border border-neutral-800 bg-neutral-950/50 p-5">
        <div className="grid gap-4 md:grid-cols-4">
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Setup Grade
            </div>
            <div className="mt-2 text-xl font-bold text-neutral-100">
              {toText(overview.health_grade)}
            </div>
            <div className="mt-1 text-[10px] text-neutral-500">
              Score {Math.round(toNumber(overview.health_score))}/100
            </div>
          </div>
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Waste Detected
            </div>
            <div className="mt-2 text-lg font-semibold text-emerald-300">
              {fmtCurrency(overview.estimated_usd_saved, 3)}
            </div>
            <div className="mt-1 text-[10px] text-neutral-500">
              {fmtTokens(overview.estimated_tokens_saved)}
            </div>
          </div>
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Issues Flagged
            </div>
            <div className="mt-2 text-lg font-semibold text-amber-300">
              {Math.round(toNumber(overview.issue_count)).toLocaleString()}
            </div>
            <div className="mt-1 text-[10px] text-neutral-500">
              from {Math.round(toNumber(overview.sessions)).toLocaleString()}{" "}
              sessions
            </div>
          </div>
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Current Spend
            </div>
            <div className="mt-2 text-lg font-semibold text-neutral-100">
              {fmtCurrency(overview.cost)}
            </div>
            <div className="mt-1 text-[10px] text-neutral-500">
              {Math.round(toNumber(overview.calls)).toLocaleString()} calls
            </div>
          </div>
        </div>
      </section>

      {recommendations.length ? (
        <div className="grid gap-4 xl:grid-cols-2">
          {recommendations.map((row, index) => {
            const severity = String(row.severity || "medium").toLowerCase();
            const severityClasses =
              severity === "high"
                ? "border-red-900/50 bg-red-950/15 text-red-200"
                : severity === "low"
                  ? "border-cyan-900/50 bg-cyan-950/15 text-cyan-200"
                  : "border-amber-900/50 bg-amber-950/15 text-amber-100";
            return (
              <article
                key={`${row.title}-${index}`}
                className={`border p-4 ${severityClasses}`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-sm font-semibold text-neutral-100">
                      {toText(row.title)}
                    </div>
                    <div className="mt-1 text-[10px] font-mono uppercase tracking-widest text-neutral-500">
                      Severity {toText(row.severity)}
                    </div>
                  </div>
                  <div className="text-right">
                    <div className="font-mono text-sm text-emerald-300">
                      {fmtCurrency(row.estimated_usd_saved, 3)}
                    </div>
                    <div className="mt-1 text-[10px] text-neutral-500">
                      {fmtTokens(row.estimated_tokens_saved)} saved
                    </div>
                  </div>
                </div>

                <div className="mt-4 text-sm leading-relaxed text-neutral-300">
                  {toText(row.description)}
                </div>

                {String(row.action || "").trim() ? (
                  <div className="mt-4 border border-neutral-900 bg-black/40 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-neutral-200">
                    {toText(row.action)}
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      ) : (
        <EmptyPanel
          title="Recommendations"
          detail="No recommendations were captured for this snapshot."
        />
      )}
    </div>
  );
}

function TokscaleContributionGraph({
  dailyEntries,
  statsRows,
}: {
  dailyEntries: TableRow[];
  statsRows: TableRow[];
}) {
  // Create a map of date -> cost for quick lookup
  const costByDate = new Map<string, number>();
  dailyEntries.forEach((entry) => {
    const date = toText(entry.date);
    const totals = asRecord(entry.totals);
    const cost = toNumber(totals.cost);
    costByDate.set(date, cost);
  });

  // Find max cost for color normalization
  const maxCost = Math.max(...Array.from(costByDate.values()), 0.01);

  // Parse dates and group by week
  const dateEntries: { date: Date; dateStr: string; cost: number }[] = [];
  const now = new Date();
  const oneYearAgo = new Date(now.getTime() - 365 * 24 * 60 * 60 * 1000);

  for (
    let timestamp = oneYearAgo.getTime();
    timestamp <= now.getTime();
    timestamp += 24 * 60 * 60 * 1000
  ) {
    const d = new Date(timestamp);
    const dateStr = d.toISOString().split("T")[0];
    const cost = costByDate.get(dateStr) || 0;
    dateEntries.push({ date: d, dateStr, cost });
  }

  // Group by week (Sunday to Saturday)
  const weeks: (typeof dateEntries)[] = [];
  let currentWeek: typeof dateEntries = [];

  dateEntries.forEach((entry) => {
    if (entry.date.getDay() === 0 && currentWeek.length > 0) {
      weeks.push(currentWeek);
      currentWeek = [];
    }
    currentWeek.push(entry);
  });
  if (currentWeek.length > 0) {
    weeks.push(currentWeek);
  }

  // Function to get color intensity based on cost
  function getCellColor(cost: number): string {
    if (cost === 0) return "bg-neutral-800/20";
    const intensity = Math.max(0.2, (cost / maxCost) * 1);
    if (intensity > 0.8) return "bg-cyan-600";
    if (intensity > 0.6) return "bg-cyan-500";
    if (intensity > 0.4) return "bg-cyan-400/70";
    if (intensity > 0.2) return "bg-cyan-400/40";
    return "bg-cyan-400/20";
  }

  const dayLabels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  return (
    <section className="border border-neutral-800 bg-neutral-950/50 p-5 space-y-4">
      <div>
        <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
          Contribution Graph
        </div>
        <div className="mt-1 text-sm text-neutral-400">
          Last 52 weeks of activity, intensity reflects daily cost.
        </div>
      </div>

      <div className="overflow-x-auto pb-4">
        <div className="inline-flex gap-0.5">
          {/* Day labels on the left */}
          <div className="flex flex-col justify-start gap-0.5">
            <div className="h-4 w-8" /> {/* spacer for month labels */}
            {dayLabels.map((day, idx) => (
              <div
                key={day}
                className={`h-4 w-8 text-[9px] text-neutral-600 flex items-center justify-end pr-1 ${
                  idx % 2 === 0 ? "" : "invisible"
                }`}
              >
                {idx % 2 === 0 ? day : ""}
              </div>
            ))}
          </div>

          {/* Weeks grid */}
          <div className="flex gap-0.5">
            {weeks.map((week, weekIdx) => (
              <div
                key={`week-${weekIdx}`}
                className="flex flex-col gap-0.5"
                title={`Week of ${week[0]?.dateStr}`}
              >
                {/* Month label - only show first week of month */}
                {weekIdx === 0 ||
                week[0].date.getDate() <= 7 ||
                (weeks[weekIdx - 1] &&
                  weeks[weekIdx - 1][0].date.getMonth() !==
                    week[0].date.getMonth()) ? (
                  <div className="h-4 text-[9px] text-neutral-600 flex items-center px-0.5">
                    {week[0].date.toLocaleDateString("en-US", {
                      month: "short",
                    })}
                  </div>
                ) : (
                  <div className="h-4" />
                )}

                {/* Day cells */}
                {week.map((entry) => (
                  <div
                    key={`${entry.dateStr}`}
                    className={`h-4 w-4 rounded-sm border border-neutral-700 cursor-help transition-opacity hover:opacity-80 ${getCellColor(
                      entry.cost
                    )}`}
                    title={`${entry.dateStr} · ${fmtCurrency(entry.cost, 2)}`}
                  />
                ))}
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Legend */}
      <div className="flex items-center gap-3 text-[10px] text-neutral-500 pt-2 border-t border-neutral-900">
        <span>Less</span>
        <div className="flex gap-0.5">
          <div className="h-3 w-3 rounded-sm bg-cyan-400/20" />
          <div className="h-3 w-3 rounded-sm bg-cyan-400/40" />
          <div className="h-3 w-3 rounded-sm bg-cyan-400/70" />
          <div className="h-3 w-3 rounded-sm bg-cyan-500" />
          <div className="h-3 w-3 rounded-sm bg-cyan-600" />
        </div>
        <span>More</span>
      </div>

      {/* Summary stats */}
      <div className="grid gap-3 md:grid-cols-3 pt-2">
        {statsRows.slice(0, 3).map((row, idx) => (
          <div key={idx} className="border border-neutral-900 bg-black/20 p-3">
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              {toText(row.label)}
            </div>
            <div className="mt-1 text-sm font-semibold text-neutral-100">
              {toText(row.value)}
            </div>
            {Boolean(row.detail) && (
              <div className="mt-0.5 text-[10px] text-neutral-500">
                {toText(row.detail)}
              </div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

function TokscaleUnavailable({
  title,
  detail,
}: {
  title: string;
  detail: string;
}) {
  return (
    <section className="border border-neutral-800 bg-neutral-950/50 p-6">
      <div className="text-sm font-semibold text-neutral-100">{title}</div>
      <div className="mt-2 max-w-2xl text-sm leading-relaxed text-neutral-400">
        {detail}
      </div>
    </section>
  );
}

function TokscalePanel({ toolWindow }: { toolWindow: ToolWindow }) {
  const payload = asRecord(toolWindow.latest.payload);
  const entries = asRecordArray(payload.entries).sort(
    (left, right) => toNumber(right.cost) - toNumber(left.cost)
  );
  const modelEntries = asRecordArray(payload.modelEntries);
  const hourlyEntries = asRecordArray(payload.hourlyEntries).sort(
    (left, right) => String(left.hour).localeCompare(String(right.hour))
  );
  const dailyEntries = asRecordArray(payload.dailyEntries).sort((left, right) =>
    String(left.date).localeCompare(String(right.date))
  );
  const monthlyEntries = asRecordArray(payload.monthlyEntries).sort(
    (left, right) => String(left.month).localeCompare(String(right.month))
  );
  const dailySummary = asRecord(payload.dailySummary);
  const dailyMeta = asRecord(payload.dailyMeta);
  const dateRange = asRecord(dailyMeta.dateRange);
  const modelRows = (modelEntries.length ? modelEntries : entries).sort(
    (left, right) => toNumber(right.cost) - toNumber(left.cost)
  );
  const [activeTab, setActiveTab] = useState<TokscaleTab>("Overview");

  const byAgent = useMemo(() => {
    return Object.values(
      modelRows.reduce<Record<string, TableRow>>((acc, entry) => {
        const client = toText(entry.client);
        const existing = acc[client] ?? {
          client,
          models: 0,
          input: 0,
          output: 0,
          cacheRead: 0,
          cacheWrite: 0,
          reasoning: 0,
          messageCount: 0,
          cost: 0,
          modelSet: new Set<string>(),
        };
        existing.input = toNumber(existing.input) + toNumber(entry.input);
        existing.output = toNumber(existing.output) + toNumber(entry.output);
        existing.cacheRead =
          toNumber(existing.cacheRead) + toNumber(entry.cacheRead);
        existing.cacheWrite =
          toNumber(existing.cacheWrite) + toNumber(entry.cacheWrite);
        existing.reasoning =
          toNumber(existing.reasoning) + toNumber(entry.reasoning);
        existing.messageCount =
          toNumber(existing.messageCount) + toNumber(entry.messageCount);
        existing.cost = toNumber(existing.cost) + toNumber(entry.cost);
        const modelSet = existing.modelSet as Set<string>;
        modelSet.add(toText(entry.model));
        existing.models = modelSet.size;
        acc[client] = existing;
        return acc;
      }, {})
    ).sort((left, right) => toNumber(right.cost) - toNumber(left.cost));
  }, [modelRows]);

  const maxEntryCost = Math.max(
    ...modelRows.map((entry) => toNumber(entry.cost)),
    0.01
  );
  const statsRows: TableRow[] = [
    {
      label: "Grouping",
      value: toText(payload.groupBy),
      detail: `${modelRows.length} entr${modelRows.length === 1 ? "y" : "ies"}`,
    },
    {
      label: "Total Input",
      value: fmtTokens(payload.totalInput),
      detail: `${fmtTokens(payload.totalCacheRead)} cache read`,
    },
    {
      label: "Total Output",
      value: fmtTokens(payload.totalOutput),
      detail: `${fmtTokens(payload.totalCacheWrite)} cache write`,
    },
    {
      label: "Total Cost",
      value: fmtCurrency(payload.totalCost, 3),
      detail: `${Math.round(toNumber(payload.totalMessages)).toLocaleString()} messages`,
    },
    {
      label: "Processing Time",
      value: `${Math.round(toNumber(payload.processingTimeMs))}ms`,
      detail: `${byAgent.length} active client${byAgent.length === 1 ? "" : "s"}`,
    },
    {
      label: "Cache / Input",
      value: fmtPercent(
        toNumber(payload.totalInput) > 0
          ? toNumber(payload.totalCacheRead) / toNumber(payload.totalInput)
          : 0
      ),
      detail: "Based on the persisted snapshot",
    },
    {
      label: "Active Days",
      value: Math.round(toNumber(dailySummary.activeDays)).toLocaleString(),
      detail:
        toText(dateRange.start) !== "-" && toText(dateRange.end) !== "-"
          ? `${toText(dateRange.start)} to ${toText(dateRange.end)}`
          : "From Tokscale graph summary",
    },
    {
      label: "Avg / Day",
      value:
        toNumber(dailySummary.averagePerDay) > 0
          ? fmtCurrency(dailySummary.averagePerDay, 3)
          : "-",
      detail:
        toNumber(dailySummary.maxCostInSingleDay) > 0
          ? `Peak ${fmtCurrency(dailySummary.maxCostInSingleDay, 3)}`
          : "No daily peak reported",
    },
  ];

  return (
    <div className="space-y-6">
      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard
          label="Spend"
          value={fmtCurrency(payload.totalCost, 3)}
          tone="cyan"
        />
        <MetricCard
          label="Input"
          value={fmtTokens(payload.totalInput)}
          detail={`${fmtTokens(payload.totalCacheRead)} cache read`}
          tone="emerald"
        />
        <MetricCard
          label="Output"
          value={fmtTokens(payload.totalOutput)}
          detail={`${fmtTokens(payload.totalCacheWrite)} cache write`}
          tone="neutral"
        />
        <MetricCard
          label="Messages"
          value={Math.round(toNumber(payload.totalMessages)).toLocaleString()}
          detail={`Grouped by ${toText(payload.groupBy)}`}
          tone="neutral"
        />
      </section>

      <div className="flex flex-wrap gap-1 border-b border-neutral-800">
        {TOKSCALE_TABS.map((tab) => (
          <button
            key={tab}
            type="button"
            onClick={() => setActiveTab(tab)}
            className={`px-4 py-2 text-[11px] font-semibold uppercase tracking-wider ${
              activeTab === tab
                ? "-mb-px border-b-2 border-cyan-500 text-cyan-300"
                : "text-neutral-500 hover:text-cyan-200"
            }`}
          >
            {tab}
          </button>
        ))}
      </div>

      {activeTab === "Overview" && (
        <div className="grid gap-4 xl:grid-cols-[1fr,1fr]">
          <CompactTableSection
            title="Top Models"
            subtitle={
              modelEntries.length
                ? "Native tokscale models report."
                : "Fallback to the older grouped entries snapshot."
            }
            rows={modelRows.slice(0, 12)}
            columns={[
              {
                label: "Model",
                render: (row) => (
                  <div className="space-y-1">
                    <div className="font-mono text-[11px] text-neutral-100">
                      {toText(row.model)}
                    </div>
                    <div className="text-[10px] text-neutral-500">
                      {toText(row.provider)} / {toText(row.client)}
                    </div>
                  </div>
                ),
              },
              {
                label: "Total",
                align: "right",
                render: (row) => (
                  <span className="font-mono text-neutral-300">
                    {fmtTokens(
                      toNumber(row.input) +
                        toNumber(row.output) +
                        toNumber(row.cacheRead) +
                        toNumber(row.cacheWrite)
                    )}
                  </span>
                ),
              },
              {
                label: "Cost",
                align: "right",
                render: (row) => (
                  <div className="inline-flex items-center gap-3">
                    <span className="font-mono text-cyan-300">
                      {fmtCurrency(row.cost, 3)}
                    </span>
                    <RelativeBar
                      value={toNumber(row.cost)}
                      max={maxEntryCost}
                    />
                  </div>
                ),
              },
            ]}
          />

          <CompactTableSection
            title="Agents"
            subtitle="Aggregated from the tokscale client field."
            rows={byAgent}
            columns={[
              {
                label: "Agent",
                render: (row) => (
                  <span className="font-mono text-neutral-100">
                    {toText(row.client)}
                  </span>
                ),
              },
              {
                label: "Models",
                align: "right",
                render: (row) => (
                  <span className="font-mono text-neutral-300">
                    {Math.round(toNumber(row.models)).toLocaleString()}
                  </span>
                ),
              },
              {
                label: "Messages",
                align: "right",
                render: (row) => (
                  <span className="font-mono text-neutral-300">
                    {Math.round(toNumber(row.messageCount)).toLocaleString()}
                  </span>
                ),
              },
              {
                label: "Cost",
                align: "right",
                render: (row) => (
                  <span className="font-mono text-cyan-300">
                    {fmtCurrency(row.cost, 3)}
                  </span>
                ),
              },
            ]}
          />
        </div>
      )}

      {activeTab === "Models" && (
        <CompactTableSection
          title="Models"
          subtitle={
            modelEntries.length
              ? "Native tokscale models report captured alongside the overview snapshot."
              : "This stored snapshot predates the bundled Tokscale collector, so Atelier is falling back to the original grouped entries snapshot."
          }
          rows={modelRows}
          columns={[
            {
              label: "Model",
              render: (row) => (
                <div className="space-y-1">
                  <div className="font-mono text-[11px] text-neutral-100">
                    {toText(row.model)}
                  </div>
                  <div className="text-[10px] text-neutral-500">
                    {toText(row.provider)} / {toText(row.client)}
                  </div>
                </div>
              ),
            },
            {
              label: "Input",
              align: "right",
              render: (row) => (
                <span className="font-mono text-emerald-300">
                  {fmtTokens(row.input)}
                </span>
              ),
            },
            {
              label: "Output",
              align: "right",
              render: (row) => (
                <span className="font-mono text-red-300">
                  {fmtTokens(row.output)}
                </span>
              ),
            },
            {
              label: "Cache R",
              align: "right",
              render: (row) => (
                <span className="font-mono text-cyan-300">
                  {fmtTokens(row.cacheRead)}
                </span>
              ),
            },
            {
              label: "Cache W",
              align: "right",
              render: (row) => (
                <span className="font-mono text-amber-300">
                  {fmtTokens(row.cacheWrite)}
                </span>
              ),
            },
            {
              label: "Msgs",
              align: "right",
              render: (row) => (
                <span className="font-mono text-neutral-300">
                  {Math.round(toNumber(row.messageCount)).toLocaleString()}
                </span>
              ),
            },
            {
              label: "Cost",
              align: "right",
              render: (row) => (
                <span className="font-mono text-cyan-300">
                  {fmtCurrency(row.cost, 3)}
                </span>
              ),
            },
          ]}
        />
      )}

      {activeTab === "Daily" &&
        (dailyEntries.length ? (
          <div className="space-y-4">
            <TokscaleDailyChart rows={dailyEntries} />
            <CompactTableSection
              title="Daily Contributions"
              subtitle="Native Tokscale graph export, normalized for day-level analytics."
              rows={dailyEntries}
              columns={[
                {
                  label: "Date",
                  render: (row) => (
                    <span className="font-mono text-neutral-100">
                      {toText(row.date)}
                    </span>
                  ),
                },
                {
                  label: "Clients",
                  render: (row) => {
                    const clients = asRecordArray(row.clients);
                    return (
                      <span className="text-[11px] text-neutral-400">
                        {clients.length
                          ? clients
                              .map((client) => toText(asRecord(client).client))
                              .join(", ")
                          : "-"}
                      </span>
                    );
                  },
                },
                {
                  label: "Input",
                  align: "right",
                  render: (row) => (
                    <span className="font-mono text-emerald-300">
                      {fmtTokens(asRecord(row.tokenBreakdown).input)}
                    </span>
                  ),
                },
                {
                  label: "Output",
                  align: "right",
                  render: (row) => (
                    <span className="font-mono text-red-300">
                      {fmtTokens(asRecord(row.tokenBreakdown).output)}
                    </span>
                  ),
                },
                {
                  label: "Messages",
                  align: "right",
                  render: (row) => (
                    <span className="font-mono text-neutral-300">
                      {Math.round(
                        toNumber(asRecord(row.totals).messages)
                      ).toLocaleString()}
                    </span>
                  ),
                },
                {
                  label: "Cost",
                  align: "right",
                  render: (row) => (
                    <span className="font-mono text-cyan-300">
                      {fmtCurrency(asRecord(row.totals).cost, 3)}
                    </span>
                  ),
                },
              ]}
            />
          </div>
        ) : (
          <TokscaleUnavailable
            title="Daily View Missing From This Snapshot"
            detail="This stored Tokscale snapshot predates the bundled collector. Refresh Tokscale after this change to capture day-level graph data."
          />
        ))}

      {activeTab === "Hourly" &&
        (hourlyEntries.length ? (
          <CompactTableSection
            title="Hourly Activity"
            subtitle="Native Tokscale hourly report."
            rows={hourlyEntries}
            columns={[
              {
                label: "Hour",
                render: (row) => (
                  <span className="font-mono text-neutral-100">
                    {toText(row.hour)}
                  </span>
                ),
              },
              {
                label: "Clients",
                render: (row) => (
                  <span className="text-[11px] text-neutral-400">
                    {asStringArray(row.clients).join(", ") || "-"}
                  </span>
                ),
              },
              {
                label: "Models",
                render: (row) => (
                  <span className="text-[11px] text-neutral-400">
                    {asStringArray(row.models).join(", ") || "-"}
                  </span>
                ),
              },
              {
                label: "Messages",
                align: "right",
                render: (row) => (
                  <span className="font-mono text-neutral-300">
                    {Math.round(toNumber(row.messageCount)).toLocaleString()}
                  </span>
                ),
              },
              {
                label: "Tokens",
                align: "right",
                render: (row) => (
                  <span className="font-mono text-neutral-300">
                    {fmtTokens(
                      toNumber(row.input) +
                        toNumber(row.output) +
                        toNumber(row.cacheRead) +
                        toNumber(row.cacheWrite)
                    )}
                  </span>
                ),
              },
              {
                label: "Cost",
                align: "right",
                render: (row) => (
                  <span className="font-mono text-cyan-300">
                    {fmtCurrency(row.cost, 3)}
                  </span>
                ),
              },
            ]}
          />
        ) : (
          <TokscaleUnavailable
            title="Hourly View Missing From This Snapshot"
            detail="This stored Tokscale snapshot predates the bundled collector. Refresh Tokscale after this change to capture the native hourly report."
          />
        ))}

      {activeTab === "Stats" && (
        <div className="space-y-6">
          {dailyEntries.length ? (
            <TokscaleContributionGraph
              dailyEntries={dailyEntries}
              statsRows={statsRows}
            />
          ) : (
            <TokscaleUnavailable
              title="Contribution Graph Missing From This Snapshot"
              detail="This stored Tokscale snapshot predates the bundled collector. Refresh Tokscale after this change to capture day-level contribution data."
            />
          )}

          {monthlyEntries.length ? (
            <CompactTableSection
              title="Monthly Rollup"
              subtitle="Native Tokscale monthly report."
              rows={monthlyEntries}
              columns={[
                {
                  label: "Month",
                  render: (row) => (
                    <span className="font-mono text-neutral-100">
                      {toText(row.month)}
                    </span>
                  ),
                },
                {
                  label: "Models",
                  render: (row) => (
                    <span className="text-[11px] text-neutral-400">
                      {asStringArray(row.models).join(", ") || "-"}
                    </span>
                  ),
                },
                {
                  label: "Input",
                  align: "right",
                  render: (row) => (
                    <span className="font-mono text-emerald-300">
                      {fmtTokens(row.input)}
                    </span>
                  ),
                },
                {
                  label: "Output",
                  align: "right",
                  render: (row) => (
                    <span className="font-mono text-red-300">
                      {fmtTokens(row.output)}
                    </span>
                  ),
                },
                {
                  label: "Messages",
                  align: "right",
                  render: (row) => (
                    <span className="font-mono text-neutral-300">
                      {Math.round(toNumber(row.messageCount)).toLocaleString()}
                    </span>
                  ),
                },
                {
                  label: "Cost",
                  align: "right",
                  render: (row) => (
                    <span className="font-mono text-cyan-300">
                      {fmtCurrency(row.cost, 3)}
                    </span>
                  ),
                },
              ]}
            />
          ) : null}
        </div>
      )}

      {activeTab === "Agents" && (
        <CompactTableSection
          title="Agents"
          subtitle="Aggregated from tokscale's persisted client field."
          rows={byAgent}
          columns={[
            {
              label: "Agent",
              render: (row) => (
                <span className="font-mono text-neutral-100">
                  {toText(row.client)}
                </span>
              ),
            },
            {
              label: "Models",
              align: "right",
              render: (row) => (
                <span className="font-mono text-neutral-300">
                  {Math.round(toNumber(row.models)).toLocaleString()}
                </span>
              ),
            },
            {
              label: "Input",
              align: "right",
              render: (row) => (
                <span className="font-mono text-emerald-300">
                  {fmtTokens(row.input)}
                </span>
              ),
            },
            {
              label: "Output",
              align: "right",
              render: (row) => (
                <span className="font-mono text-red-300">
                  {fmtTokens(row.output)}
                </span>
              ),
            },
            {
              label: "Messages",
              align: "right",
              render: (row) => (
                <span className="font-mono text-neutral-300">
                  {Math.round(toNumber(row.messageCount)).toLocaleString()}
                </span>
              ),
            },
            {
              label: "Cost",
              align: "right",
              render: (row) => (
                <span className="font-mono text-cyan-300">
                  {fmtCurrency(row.cost, 3)}
                </span>
              ),
            },
          ]}
        />
      )}
    </div>
  );
}

function CcusagePanel({ toolWindow }: { toolWindow: ToolWindow }) {
  const payload = asRecord(toolWindow.latest.payload);
  const totals = asRecord(payload.totals);
  const daily = asRecordArray(payload.daily).sort((left, right) =>
    String(left.date).localeCompare(String(right.date))
  );
  const models = asRecordArray(payload.modelEntries);
  const sessions = asRecordArray(payload.sessions);
  const maxModelCost = Math.max(
    ...models.map((row) => toNumber(row.cost)),
    0.01
  );
  const maxDayCost = Math.max(...daily.map((row) => toNumber(row.totalCost)), 0.01);

  return (
    <div className="space-y-6">
      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard
          label="Total Cost"
          value={fmtCurrency(totals.totalCost, 2)}
          tone="emerald"
        />
        <MetricCard
          label="Input"
          value={fmtTokens(totals.inputTokens)}
          detail={`${fmtTokens(totals.cacheReadTokens)} cache read`}
          tone="emerald"
        />
        <MetricCard
          label="Output"
          value={fmtTokens(totals.outputTokens)}
          detail={`${fmtTokens(totals.cacheCreationTokens)} cache write`}
          tone="neutral"
        />
        <MetricCard
          label="Total Tokens"
          value={fmtTokens(totals.totalTokens)}
          detail={`${daily.length} day${daily.length === 1 ? "" : "s"} · ${sessions.length} session${sessions.length === 1 ? "" : "s"}`}
          tone="neutral"
        />
      </section>

      {daily.length > 0 && (
        <section className="border border-neutral-800 bg-neutral-950/50 p-5 space-y-4">
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Daily Spend (ccusage native)
            </div>
            <div className="mt-1 text-sm text-neutral-400">
              Per-day totals straight from ccusage's daily view.
            </div>
          </div>
          <div className="flex items-end gap-2 overflow-x-auto pb-2">
            {daily.map((row) => {
              const cost = toNumber(row.totalCost);
              return (
                <div
                  key={String(row.date)}
                  className="flex min-w-[54px] flex-col items-center gap-2"
                  title={`${toText(row.date)} · ${fmtCurrency(cost, 2)} · ${fmtTokens(row.totalTokens)} tokens`}
                >
                  <div className="text-[10px] font-mono text-emerald-300">
                    {fmtCurrency(cost, 2)}
                  </div>
                  <div className="flex h-28 items-end">
                    <div
                      className="w-8 rounded-t-sm bg-emerald-400/70"
                      style={{
                        height: `${Math.max((cost / maxDayCost) * 112, 6)}px`,
                      }}
                    />
                  </div>
                  <div className="text-[10px] font-mono text-neutral-500">
                    {toText(row.date).slice(5)}
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        <CompactTableSection
          title="Models by Cost"
          subtitle="Ranked across the selected window. Compare against Atelier's own Claude rows to spot dedup differences."
          rows={models}
          columns={[
            {
              label: "Model",
              render: (row) => (
                <span className="font-mono text-[11px] text-neutral-100">
                  {toText(row.model)}
                </span>
              ),
            },
            {
              label: "Input",
              align: "right",
              render: (row) => (
                <span className="font-mono text-emerald-300">
                  {fmtTokens(row.inputTokens)}
                </span>
              ),
            },
            {
              label: "Output",
              align: "right",
              render: (row) => (
                <span className="font-mono text-red-300">
                  {fmtTokens(row.outputTokens)}
                </span>
              ),
            },
            {
              label: "Cache R",
              align: "right",
              render: (row) => (
                <span className="font-mono text-cyan-300">
                  {fmtTokens(row.cacheReadTokens)}
                </span>
              ),
            },
            {
              label: "Cache W",
              align: "right",
              render: (row) => (
                <span className="font-mono text-amber-300">
                  {fmtTokens(row.cacheCreationTokens)}
                </span>
              ),
            },
            {
              label: "Cost",
              align: "right",
              render: (row) => (
                <div className="inline-flex items-center gap-3">
                  <span className="font-mono text-emerald-300">
                    {fmtCurrency(row.cost, 2)}
                  </span>
                  <RelativeBar
                    value={toNumber(row.cost)}
                    max={maxModelCost}
                    tone="bg-emerald-400/70"
                  />
                </div>
              ),
            },
          ]}
        />

        <CompactTableSection
          title="Top Sessions"
          subtitle="ccusage's per-session aggregation. Useful for spotting outlier conversations."
          rows={sessions
            .slice()
            .sort(
              (left, right) =>
                toNumber(right.totalCost) - toNumber(left.totalCost)
            )
            .slice(0, 12)}
          emptyLabel="No per-session detail in this ccusage snapshot."
          columns={[
            {
              label: "Session",
              render: (row) => (
                <span className="font-mono text-[10px] text-neutral-300">
                  {toText(row.sessionId).slice(0, 24)}
                </span>
              ),
            },
            {
              label: "Last Activity",
              render: (row) => (
                <span className="font-mono text-[10px] text-neutral-400">
                  {toText(row.lastActivity)}
                </span>
              ),
            },
            {
              label: "Tokens",
              align: "right",
              render: (row) => (
                <span className="font-mono text-neutral-300">
                  {fmtTokens(row.totalTokens)}
                </span>
              ),
            },
            {
              label: "Cost",
              align: "right",
              render: (row) => (
                <span className="font-mono text-emerald-300">
                  {fmtCurrency(row.totalCost, 2)}
                </span>
              ),
            },
          ]}
        />
      </div>
    </div>
  );
}

function GenericToolPanel({ toolWindow }: { toolWindow: ToolWindow }) {
  const payload = asRecord(toolWindow.latest.payload);
  const keys = Object.keys(payload).sort();
  return (
    <div className="space-y-6">
      <section className="border border-neutral-800 bg-neutral-950/50 p-5">
        <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
          Payload Keys
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          {keys.length ? (
            keys.map((key) => (
              <span
                key={key}
                className="border border-neutral-800 bg-neutral-900/60 px-2 py-1 font-mono text-[10px] text-neutral-300"
              >
                {key}
              </span>
            ))
          ) : (
            <span className="text-sm italic text-neutral-500">
              No structured payload keys were captured.
            </span>
          )}
        </div>
      </section>
    </div>
  );
}

function ExternalHistory({ runs }: { runs: ExternalAnalyticsRun[] }) {
  return (
    <CompactTableSection
      title="Snapshot History"
      subtitle="Recent stored snapshots for the active tool."
      rows={runs.slice(0, 12).map((run) => run as unknown as TableRow)}
      columns={[
        {
          label: "Period",
          render: (row) => (
            <span className="font-mono text-neutral-300">
              {titleCaseKey(toText(row.period))}
            </span>
          ),
        },
        {
          label: "Collected",
          render: (row) => (
            <span className="font-mono text-[11px] text-neutral-400">
              {fmtTimestamp(toText(row.collected_at))}
            </span>
          ),
        },
        {
          label: "Status",
          render: (row) => {
            const ok = Boolean(row.ok);
            return (
              <span
                className={`font-mono text-[10px] uppercase tracking-widest ${ok ? "text-emerald-300" : "text-red-300"}`}
              >
                {ok ? "ok" : `error ${toText(row.returncode)}`}
              </span>
            );
          },
        },
        {
          label: "Command",
          render: (row) => (
            <span className="font-mono text-[11px] text-neutral-500 break-all">
              {toText(row.command_display)}
            </span>
          ),
        },
      ]}
    />
  );
}

export default function External() {
  const [externalData, setExternalData] =
    useState<ExternalAnalyticsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const { days } = useTimeRange();
  const [activeTool, setActiveTool] = useState("");
  const selectedWindowDays = days;

  useEffect(() => {
    const externalLimit = Math.min(
      EXTERNAL_MAX_LIMIT,
      Math.max(180, days * 12)
    );
    setLoading(true);
    setErr(null);
    api
      .externalAnalytics(days, undefined, externalLimit)
      .then((payload) => {
        setExternalData(payload);
      })
      .catch((error) => {
        setErr(String(error));
        setExternalData(null);
      })
      .finally(() => setLoading(false));
  }, [days]);

  const toolWindows = useMemo(
    () => buildToolWindows(externalData?.runs ?? [], selectedWindowDays),
    [externalData, selectedWindowDays]
  );

  useEffect(() => {
    if (!toolWindows.length) return;
    if (!toolWindows.some((toolWindow) => toolWindow.tool === activeTool)) {
      setActiveTool(toolWindows[0].tool);
    }
  }, [activeTool, toolWindows]);

  const activeToolWindow =
    toolWindows.find((toolWindow) => toolWindow.tool === activeTool) ??
    toolWindows[0] ??
    null;

  return (
    <div className="min-h-screen max-w-7xl space-y-6 bg-black p-6 font-sans text-neutral-200 mx-auto">
      {loading ? (
        <div className="p-6 text-sm italic text-neutral-500 animate-pulse">
          Loading external analyzer snapshots...
        </div>
      ) : err ? (
        <div className="border border-red-900/50 bg-red-950/20 p-5 text-sm text-red-200">
          {err}
        </div>
      ) : !externalData || toolWindows.length === 0 ? (
        <EmptyPanel
          title="No External Snapshots"
          detail="Atelier has not stored any external analyzer reports for this window yet. Run `atelier external-report --tool all --period month` or let servicectl collect them, then reload this page."
        />
      ) : (
        <>
          <div className="flex flex-col gap-3 border-b border-neutral-800 pb-2 md:flex-row md:items-center md:justify-between">
            <div className="flex flex-wrap gap-2">
              {toolWindows.map((toolWindow) => (
                <button
                  key={toolWindow.tool}
                  type="button"
                  onClick={() => setActiveTool(toolWindow.tool)}
                  title={toolDescription(toolWindow.tool)}
                  className={`border px-3 py-2 text-xs font-semibold uppercase tracking-wider transition ${toolTabClasses(
                    toolWindow.tool,
                    toolWindow.tool === activeToolWindow?.tool
                  )}`}
                >
                  {displayToolName(toolWindow.tool)}
                </button>
              ))}
            </div>
          </div>

          {activeToolWindow?.tool === "codeburn" && (
            <CodeBurnPanel
              key={activeToolWindow.tool}
              toolWindow={activeToolWindow}
            />
          )}
          {activeToolWindow?.tool === "codeburn:optimize" && (
            <CodeBurnOptimizePanel
              key={activeToolWindow.tool}
              toolWindow={activeToolWindow}
            />
          )}
          {activeToolWindow?.tool === "tokscale" && (
            <TokscalePanel
              key={activeToolWindow.tool}
              toolWindow={activeToolWindow}
            />
          )}
          {activeToolWindow?.tool === "ccusage" && (
            <CcusagePanel
              key={activeToolWindow.tool}
              toolWindow={activeToolWindow}
            />
          )}
          {activeToolWindow &&
            !["codeburn", "codeburn:optimize", "tokscale", "ccusage"].includes(
              activeToolWindow.tool
            ) && (
              <GenericToolPanel
                key={activeToolWindow.tool}
                toolWindow={activeToolWindow}
              />
            )}

          {activeToolWindow ? (
            <ExternalHistory runs={activeToolWindow.allRuns} />
          ) : null}
        </>
      )}
    </div>
  );
}
