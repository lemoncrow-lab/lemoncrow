import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api, type SessionReport } from "../api";
import { MetricCard, SectionHeader } from "../components/WorkbenchUI";

function fmtUsd(v: number) {
  return `$${v.toFixed(2)}`;
}

function fmtTok(n: number) {
  return n.toLocaleString();
}

function fmtDate(s: string | null) {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString();
  } catch {
    return s;
  }
}

function fmtDuration(secs: number) {
  if (secs < 60) return `${Math.round(secs)}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${Math.round(secs % 60)}s`;
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
}

export default function SessionDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [report, setReport] = useState<SessionReport | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    setReport(null);
    setErr(null);
    api
      .sessionReport(id)
      .then(setReport)
      .catch((e) => setErr(String(e)));
  }, [id]);

  if (err) {
    return (
      <div className="space-y-4">
        <button
          type="button"
          className="text-xs text-neutral-500 hover:text-neutral-300"
          onClick={() => navigate("/sessions")}
        >
          ← Back to Sessions
        </button>
        <div className="border border-red-800 bg-red-950/30 p-4 text-sm text-red-300">{err}</div>
      </div>
    );
  }

  if (!report) {
    return (
      <div className="border border-neutral-800 p-8 text-center text-sm text-neutral-500">
        Loading session report…
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <button
          type="button"
          className="text-xs text-neutral-500 hover:text-neutral-300"
          onClick={() => navigate("/sessions")}
        >
          ← Back to Sessions
        </button>
      </div>

      <SectionHeader
        title={`Session ${report.session_id.slice(0, 16)}…`}
        description={`${fmtDate(report.started_at)} · ${fmtDuration(report.duration_seconds)} · ${report.vendor}`}
      />

      {/* Cost breakdown */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricCard label="Total cost" value={fmtUsd(report.total_cost_usd)} tone="amber" />
        <MetricCard
          label="Atelier savings"
          value={fmtUsd(report.total_atelier_savings_usd)}
          tone="emerald"
        />
        <MetricCard label="Turns" value={String(report.total_turns)} tone="violet" />
        <MetricCard label="Tool calls" value={String(report.tool_call_count)} tone="neutral" />
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricCard
          label="Input cost"
          value={fmtUsd(report.input_token_cost_usd)}
          tone="neutral"
        />
        <MetricCard
          label="Output cost"
          value={fmtUsd(report.output_token_cost_usd)}
          tone="neutral"
        />
        <MetricCard
          label="Cache write cost"
          value={fmtUsd(report.cache_write_cost_usd)}
          tone="neutral"
        />
        <MetricCard
          label="Cache read cost"
          value={fmtUsd(report.cache_read_cost_usd)}
          tone="neutral"
        />
      </div>

      {/* Token counts */}
      <section className="border border-neutral-800 p-4">
        <h2 className="mb-3 text-xs font-bold uppercase tracking-widest text-neutral-500">
          Token Usage
        </h2>
        <div className="grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
          {[
            ["Input", report.input_tokens],
            ["Output", report.output_tokens],
            ["Cache write", report.cache_write_tokens],
            ["Cache read", report.cache_read_tokens],
          ].map(([label, val]) => (
            <div key={String(label)} className="flex justify-between border-b border-neutral-800 pb-1">
              <span className="text-neutral-500">{label}</span>
              <span className="font-semibold text-neutral-200">{fmtTok(val as number)}</span>
            </div>
          ))}
        </div>
      </section>

      {/* Atelier savings detail */}
      <section className="border border-neutral-800 p-4">
        <h2 className="mb-3 text-xs font-bold uppercase tracking-widest text-neutral-500">
          Atelier Savings
        </h2>
        <div className="grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
          {[
            ["Routing downtiered turns", report.routing_downtiered_turns],
            ["Routing savings", fmtUsd(report.routing_savings_usd)],
            ["Compact events", report.compact_events],
            ["Compact savings (est.)", fmtUsd(report.compact_savings_estimate_usd)],
          ].map(([label, val]) => (
            <div key={String(label)} className="flex justify-between border-b border-neutral-800 pb-1">
              <span className="text-neutral-500">{label}</span>
              <span className="font-semibold text-green-400">{String(val)}</span>
            </div>
          ))}
        </div>
      </section>

      {/* Models used */}
      {Object.keys(report.models_used).length > 0 && (
        <section className="border border-neutral-800 p-4">
          <h2 className="mb-3 text-xs font-bold uppercase tracking-widest text-neutral-500">
            Models Used
          </h2>
          <div className="flex flex-wrap gap-2">
            {Object.entries(report.models_used).map(([model, count]) => (
              <span
                key={model}
                className="border border-neutral-700 bg-neutral-900/40 px-2 py-1 text-xs"
              >
                <span className="text-purple-400">{model}</span>
                <span className="ml-1 text-neutral-500">×{count}</span>
              </span>
            ))}
          </div>
        </section>
      )}

      {/* Top tools by cost */}
      {report.top_tools_by_cost.length > 0 && (
        <section className="border border-neutral-800 p-4">
          <h2 className="mb-3 text-xs font-bold uppercase tracking-widest text-neutral-500">
            Top Tools by Cost
          </h2>
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-neutral-800 text-neutral-500">
                <th className="py-1 pr-4">Tool</th>
                <th className="py-1 pr-4 text-right">Calls</th>
                <th className="py-1 text-right">Cost</th>
              </tr>
            </thead>
            <tbody>
              {report.top_tools_by_cost.map((t) => (
                <tr key={t.tool} className="border-b border-neutral-800/40">
                  <td className="py-1 pr-4 font-mono text-neutral-300">{t.tool}</td>
                  <td className="py-1 pr-4 text-right text-neutral-400">{t.calls}</td>
                  <td className="py-1 text-right text-amber-300">{fmtUsd(t.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}
