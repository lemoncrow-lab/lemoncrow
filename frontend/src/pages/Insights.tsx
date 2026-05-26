import { useEffect, useState } from "react";
import { Sparkles } from "lucide-react";
import { api, type InsightsWindow, type InsightsSessionSummary } from "../api";
import {
  Alert,
  Card,
  EmptyState,
  MetricCard,
} from "../components/WorkbenchUI";
import { useTimeRange } from "../lib/TimeRangeContext";

function fmtUsd(v: number) {
  return `$${v.toFixed(2)}`;
}


function PctBar({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="h-1.5 flex-1 bg-neutral-800">
      <div className="h-full bg-violet-600" style={{ width: `${pct}%` }} />
    </div>
  );
}

export default function Insights() {
  const { range } = useTimeRange();
  const [data, setData] = useState<InsightsWindow | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    setErr(null);
    api
      .insightsWindow(range)
      .then(setData)
      .catch((e) => setErr(String(e)));
  }, [range]);

  const isEmpty = data !== null && data.session_count === 0;

  // Convert Record<string, number> to sorted pairs
  const toolEntries = data
    ? Object.entries(data.cost_by_tool)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 8)
    : [];
  const vendorEntries = data
    ? Object.entries(data.cost_by_vendor).sort((a, b) => b[1] - a[1])
    : [];

  return (
    <div className="space-y-6">
      {err && <Alert tone="danger" description={err} />}

      {data === null && !err && (
        <EmptyState title="Loading insights…" className="p-6" />
      )}

      {isEmpty && (
        <EmptyState
          icon={<Sparkles size={32} />}
          title="No insights yet"
          description="Insights appear after Atelier captures session data."
        />
      )}

      {data !== null && !isEmpty && (
        <>
          {/* Summary metrics */}
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <MetricCard
              label="Sessions"
              value={String(data.session_count)}
              tone="violet"
            />
            <MetricCard
              label="Total cost"
              value={fmtUsd(data.total_cost_usd)}
              tone="amber"
            />
            <MetricCard
              label="Total savings"
              value={fmtUsd(data.total_atelier_savings_usd)}
              tone="emerald"
            />
            <MetricCard
              label="Avg session cost"
              value={
                data.session_count > 0
                  ? fmtUsd(data.total_cost_usd / data.session_count)
                  : "—"
              }
              tone="neutral"
            />
          </div>

          {/* Top sessions */}
          {data.top_sessions.length > 0 && (
            <Card className="p-4">
              <h2 className="mb-3 text-xs font-bold uppercase tracking-widest text-neutral-500">
                Top Cost Sessions
              </h2>
              <div className="space-y-2">
                {data.top_sessions.map((s: InsightsSessionSummary) => {
                  const maxCost = data.top_sessions[0]?.cost_usd ?? 1;
                  return (
                    <div key={s.session_id} className="flex flex-col gap-0.5">
                      <div className="flex justify-between text-xs">
                        <span className="font-mono text-violet-400/80">
                          {s.session_id.slice(0, 16)}…
                        </span>
                        <span className="text-amber-300">
                          {fmtUsd(s.cost_usd)}
                        </span>
                      </div>
                      <div className="flex items-center gap-2">
                        <PctBar value={s.cost_usd} max={maxCost} />
                      </div>
                    </div>
                  );
                })}
              </div>
            </Card>
          )}

          {/* Cost by tool */}
          {toolEntries.length > 0 && (
            <Card className="p-4">
              <h2 className="mb-3 text-xs font-bold uppercase tracking-widest text-neutral-500">
                Cost by Tool
              </h2>
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-neutral-800 text-neutral-500">
                    <th className="py-1 pr-4">Tool</th>
                    <th className="py-1 text-right">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {toolEntries.map(([tool, cost]) => (
                    <tr key={tool} className="border-b border-neutral-800/40">
                      <td className="py-1 pr-4 font-mono text-neutral-300">
                        {tool}
                      </td>
                      <td className="py-1 text-right text-amber-300">
                        {fmtUsd(cost)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}

          {/* Cost by vendor */}
          {vendorEntries.length > 0 && (
            <Card className="p-4">
              <h2 className="mb-3 text-xs font-bold uppercase tracking-widest text-neutral-500">
                Cost by Vendor
              </h2>
              <div className="space-y-2">
                {vendorEntries.map(([vendor, cost]) => {
                  const maxCost = vendorEntries[0]?.[1] ?? 1;
                  return (
                    <div key={vendor} className="flex flex-col gap-0.5">
                      <div className="flex justify-between text-xs">
                        <span className="text-neutral-300">{vendor}</span>
                        <span className="text-amber-300">{fmtUsd(cost)}</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <PctBar value={cost} max={maxCost} />
                      </div>
                    </div>
                  );
                })}
              </div>
            </Card>
          )}

          {/* Opportunities */}
          {data.opportunities.length > 0 && (
            <Card tone="amber" className="p-4">
              <h2 className="mb-2 text-xs font-bold uppercase tracking-widest text-amber-500">
                Optimization Opportunities
              </h2>
              <ul className="space-y-2">
                {data.opportunities.map((opp) => (
                  <li key={opp.kind} className="text-xs">
                    <div className="flex justify-between">
                      <span className="font-semibold text-neutral-200">
                        {opp.kind}
                      </span>
                      <span className="text-emerald-400">
                        {fmtUsd(opp.estimated_savings_usd)}
                      </span>
                    </div>
                    <p className="mt-0.5 text-neutral-500">{opp.message}</p>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
