import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { InsightsSessionSummary, InsightsWindow } from "../api";
import { api } from "../api";
import { Card, SectionHeader } from "../components/WorkbenchUI";
import { HealthStrip } from "../components/HealthStrip";
import { fmtUsd } from "../lib/format";
import { useTimeRange } from "../lib/TimeRangeContext";

type OverviewTone = "amber" | "cyan" | "emerald" | "violet";

interface SnapshotChipData {
  label: string;
  value: string;
  detail: string;
  tone: OverviewTone;
}

const TONE_STYLES: Record<OverviewTone, { chip: string; value: string }> = {
  amber: {
    chip: "border-amber-900/40 bg-amber-950/20",
    value: "text-amber-200",
  },
  cyan: { chip: "border-cyan-900/40 bg-cyan-950/20", value: "text-cyan-100" },
  emerald: {
    chip: "border-emerald-900/40 bg-emerald-950/20",
    value: "text-emerald-100",
  },
  violet: {
    chip: "border-violet-900/40 bg-violet-950/20",
    value: "text-violet-100",
  },
};

function SnapshotChip({ label, value, detail, tone }: SnapshotChipData) {
  const palette = TONE_STYLES[tone];
  return (
    <div className={`border px-3 py-3 ${palette.chip}`}>
      <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-400">
        {label}
      </div>
      <div className={`mt-2 text-xl font-semibold ${palette.value}`}>
        {value}
      </div>
      <div className="mt-1 text-xs leading-relaxed text-neutral-400">
        {detail}
      </div>
    </div>
  );
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

export default function Overview() {
  const [insights, setInsights] = useState<InsightsWindow | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { range } = useTimeRange();

  useEffect(() => {
    let active = true;
    setInsights(null);
    setErr(null);
    api
      .insightsWindow(range)
      .then((data) => {
        if (active) setInsights(data);
      })
      .catch((reason) => {
        if (active) setErr(errorMessage(reason));
      });
    return () => {
      active = false;
    };
  }, [range]);

  const wouldHaveCost = insights
    ? insights.total_cost_usd + insights.total_atelier_savings_usd
    : 0;
  const reductionPct =
    insights && wouldHaveCost > 0
      ? (insights.total_atelier_savings_usd / wouldHaveCost) * 100
      : 0;

  const snapshotChips: SnapshotChipData[] = [
    {
      label: "Sessions",
      value: insights ? String(insights.session_count) : "…",
      detail: `Last ${range}`,
      tone: "cyan",
    },
    {
      label: "Cost",
      value: insights ? fmtUsd(insights.total_cost_usd) : "…",
      detail: `${range} window`,
      tone: "amber",
    },
    {
      label: "Saved",
      value: insights ? fmtUsd(insights.total_atelier_savings_usd) : "…",
      detail: "Atelier-attributed",
      tone: "emerald",
    },
    {
      label: "Reduction",
      value: insights ? `${reductionPct.toFixed(1)}%` : "…",
      detail: "vs. would-have-cost",
      tone: "violet",
    },
  ];

  return (
    <div className="space-y-6">
      {err && <div className="text-sm text-red-300">{err}</div>}

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {snapshotChips.map((chip) => (
          <SnapshotChip key={chip.label} {...chip} />
        ))}
      </section>

      <section className="space-y-3">
        <SectionHeader
          eyebrow="Freshness"
          title="Daemon & host status"
          description="Live daemon health plus per-host adapter status."
        />
        <HealthStrip compact />
      </section>

      {insights && insights.session_count > 0 && (
        <div className="grid gap-4 xl:grid-cols-3">
          {insights.top_sessions.length > 0 && (
            <Card className="p-4">
              <h3 className="mb-3 text-[10px] font-mono font-bold uppercase tracking-widest text-neutral-400">
                Top Cost Sessions
              </h3>
              <div className="space-y-2">
                {insights.top_sessions.map((s: InsightsSessionSummary) => {
                  const maxCost = insights.top_sessions[0]?.cost_usd ?? 1;
                  return (
                    <Link
                      key={s.session_id}
                      to={`/sessions/${s.session_id}`}
                      className="block flex-col gap-0.5 hover:opacity-80"
                    >
                      <div className="flex justify-between text-xs">
                        <span className="font-mono text-violet-300">
                          {s.session_id.slice(0, 14)}…
                        </span>
                        <span className="text-amber-300">
                          {fmtUsd(s.cost_usd)}
                        </span>
                      </div>
                      <div className="h-1 w-full bg-neutral-800">
                        <div
                          className="h-full bg-violet-600"
                          style={{
                            width: `${
                              maxCost > 0
                                ? Math.min(100, (s.cost_usd / maxCost) * 100)
                                : 0
                            }%`,
                          }}
                        />
                      </div>
                    </Link>
                  );
                })}
              </div>
            </Card>
          )}

          {Object.keys(insights.cost_by_vendor).length > 0 && (
            <Card className="p-4">
              <h3 className="mb-3 text-[10px] font-mono font-bold uppercase tracking-widest text-neutral-400">
                Cost by Vendor
              </h3>
              <div className="space-y-2">
                {Object.entries(insights.cost_by_vendor)
                  .sort((a, b) => b[1] - a[1])
                  .map(([vendor, cost]) => {
                    const maxCost = Math.max(
                      ...Object.values(insights.cost_by_vendor)
                    );
                    return (
                      <div key={vendor} className="flex flex-col gap-0.5">
                        <div className="flex justify-between text-xs">
                          <span className="text-neutral-300">{vendor}</span>
                          <span className="text-amber-300">{fmtUsd(cost)}</span>
                        </div>
                        <div className="h-1 w-full bg-neutral-800">
                          <div
                            className="h-full bg-amber-600"
                            style={{
                              width: `${
                                maxCost > 0
                                  ? Math.min(100, (cost / maxCost) * 100)
                                  : 0
                              }%`,
                            }}
                          />
                        </div>
                      </div>
                    );
                  })}
              </div>
            </Card>
          )}

          {insights.opportunities.length > 0 && (
            <Card tone="amber" className="p-4">
              <div className="mb-2 flex items-center justify-between gap-2">
                <h3 className="text-[10px] font-mono font-bold uppercase tracking-widest text-amber-300">
                  Optimization Opportunities
                </h3>
                <Link
                  to="/costs/advisor"
                  className="text-[10px] font-mono uppercase tracking-widest text-amber-300 hover:text-amber-200"
                >
                  Advisor →
                </Link>
              </div>
              <ul className="space-y-2">
                {insights.opportunities.map((opp) => (
                  <li key={opp.kind} className="text-xs">
                    <div className="flex justify-between">
                      <span className="font-semibold text-neutral-200">
                        {opp.kind}
                      </span>
                      <span className="text-emerald-300">
                        {fmtUsd(opp.estimated_savings_usd)}
                      </span>
                    </div>
                    <p className="mt-0.5 text-neutral-400">{opp.message}</p>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>
      )}

      {insights && insights.session_count === 0 && !err && (
        <div className="flex flex-col items-center justify-center py-20 text-center">
          <p className="text-sm text-neutral-400">No activity data yet.</p>
          <p className="mt-2 text-xs text-neutral-400">
            Start using Atelier with your AI agent to see sessions, costs, and
            savings here.
          </p>
        </div>
      )}
    </div>
  );
}
