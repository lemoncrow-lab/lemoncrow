import { useEffect, useState } from "react";
import type { OverviewStats } from "../api";
import { api } from "../api";
import { MetricCard } from "../components/WorkbenchUI";
import { getTelemetryConfig, type TelemetryConfig } from "../lib/insightsApi";

const fmt = new Intl.NumberFormat();
const usd = (n: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 4,
  }).format(n);

export default function Overview() {
  const [stats, setStats] = useState<OverviewStats | null>(null);
  const [config, setConfig] = useState<TelemetryConfig | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .overview()
      .then(setStats)
      .catch((e) => setErr(String(e)));

    getTelemetryConfig()
      .then(setConfig)
      .catch(() => undefined);
  }, []);

  return (
    <div className="space-y-6">
      {err && <div className="text-sm text-red-400">Error: {err}</div>}

      {config?.dev_mode && (
        <div className="border border-amber-500/20 bg-amber-500/5 p-4 text-xs text-amber-200/80">
          <span className="font-bold text-amber-500 mr-2">DEV MODE ACTIVE</span>
          Active reasoning features (Knowledge retrieval, Plan linting, Rubric
          verification) are enabled.
        </div>
      )}

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          label="Runs"
          value={stats ? fmt.format(stats.total_traces) : "…"}
          tone="cyan"
        />
        {config?.dev_mode && (
          <div className="relative">
            <MetricCard
              label="Reason blocks"
              value={stats ? fmt.format(stats.total_blocks) : "…"}
              tone="amber"
            />
            <span className="absolute top-2 right-2 text-[8px] font-bold text-amber-500/60">
              DEV
            </span>
          </div>
        )}
        {config?.dev_mode && (
          <div className="relative">
            <MetricCard
              label="Rubrics"
              value={stats ? fmt.format(stats.total_rubrics) : "…"}
              tone="emerald"
            />
            <span className="absolute top-2 right-2 text-[8px] font-bold text-amber-500/60">
              DEV
            </span>
          </div>
        )}
        <MetricCard
          label="Saved cost"
          value={stats ? usd(stats.estimated_saved_cost_usd) : "…"}
          tone="cyan"
        />
        <div
          className="border border-neutral-800 bg-neutral-900/40 p-4 cursor-pointer hover:bg-neutral-800/60 transition"
          onClick={() => (window.location.href = "/analytics")}
        >
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
            Analytics
          </div>
          <div className="mt-2 text-2xl font-semibold text-cyan-400">
            Tool Usage →
          </div>
          <div className="mt-2 text-xs text-neutral-500">
            Deep dive into granular agent activity.
          </div>
        </div>
      </section>

      <section className="grid gap-4 md:grid-cols-2">
        <MetricCard
          label="Failure clusters"
          value={stats ? fmt.format(stats.total_clusters) : "…"}
          tone="violet"
        />
        <MetricCard
          label="Compression"
          value={stats ? stats.average_compression_ratio.toFixed(3) : "…"}
          tone="neutral"
        />
      </section>

      <section className="grid gap-4 md:grid-cols-2">
        <MetricCard
          label="Estimated total cost"
          value={stats ? usd(stats.estimated_total_cost_usd) : "…"}
          tone="neutral"
        />
        <MetricCard
          label="Raw tokens"
          value={stats ? fmt.format(stats.total_raw_tokens_estimate) : "…"}
          tone="neutral"
        />
      </section>
    </div>
  );
}
