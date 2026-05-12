import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  type OptimizationAutoOptimization,
  type OptimizationImpactValidation,
  type OptimizationModelRoutingSimulation,
  type OptimizationQualitySummary,
  type OptimizationRecommendation,
  type OptimizationRereadTelemetry,
  type OptimizationsSummary,
} from "../api";
import {
  MetricCard,
  PageHero,
  SectionHeader,
  cx,
} from "../components/WorkbenchUI";

const usdFmt = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function fmtTokens(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(1)}B`;
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return value.toLocaleString();
}

function fmtUsd(value: number): string {
  if (value === 0) return "$0.00";
  if (Math.abs(value) < 0.01) return `$${value.toFixed(4)}`;
  return usdFmt.format(value);
}

function fmtPct(value: number, digits = 1): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}%`;
}

function fmtRatio(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function fmtRange(from: string | null, to: string | null): string {
  if (!from || !to) return "No dated traces";
  return `${new Date(from).toLocaleDateString()} to ${new Date(to).toLocaleDateString()}`;
}

function Pill({
  label,
  tone = "neutral",
}: {
  label: string;
  tone?: "neutral" | "emerald" | "amber" | "red" | "cyan" | "violet";
}) {
  const toneClass: Record<string, string> = {
    neutral: "border-neutral-800 bg-neutral-900/50 text-neutral-300",
    emerald: "border-emerald-900/50 bg-emerald-950/20 text-emerald-300",
    amber: "border-amber-900/50 bg-amber-950/20 text-amber-300",
    red: "border-red-900/50 bg-red-950/20 text-red-300",
    cyan: "border-cyan-900/50 bg-cyan-950/20 text-cyan-300",
    violet: "border-violet-900/50 bg-violet-950/20 text-violet-300",
  };

  return (
    <span
      className={cx(
        "inline-flex items-center border px-2 py-0.5 text-[10px] font-mono uppercase tracking-widest",
        toneClass[tone]
      )}
    >
      {label}
    </span>
  );
}

function gradeTone(grade: string): "emerald" | "cyan" | "amber" | "red" {
  if (grade === "S" || grade === "A") return "emerald";
  if (grade === "B") return "cyan";
  if (grade === "C" || grade === "D") return "amber";
  return "red";
}

function signalTone(score: number): string {
  if (score >= 80) return "bg-emerald-400";
  if (score >= 65) return "bg-cyan-400";
  if (score >= 50) return "bg-amber-400";
  return "bg-red-400";
}

function signalWidthClass(score: number): string {
  if (score >= 95) return "w-[95%]";
  if (score >= 90) return "w-[90%]";
  if (score >= 85) return "w-[85%]";
  if (score >= 80) return "w-[80%]";
  if (score >= 75) return "w-[75%]";
  if (score >= 70) return "w-[70%]";
  if (score >= 65) return "w-[65%]";
  if (score >= 60) return "w-[60%]";
  if (score >= 55) return "w-[55%]";
  if (score >= 50) return "w-[50%]";
  if (score >= 45) return "w-[45%]";
  if (score >= 40) return "w-[40%]";
  if (score >= 35) return "w-[35%]";
  if (score >= 30) return "w-[30%]";
  if (score >= 25) return "w-[25%]";
  if (score >= 20) return "w-[20%]";
  if (score >= 15) return "w-[15%]";
  if (score >= 10) return "w-[10%]";
  return "w-[6%]";
}

function verdictTone(verdict: string): "emerald" | "amber" | "red" | "cyan" {
  if (verdict === "improved") return "emerald";
  if (verdict === "regressed") return "red";
  if (verdict === "mixed") return "amber";
  return "cyan";
}

function deltaTone(
  value: number,
  lowerIsBetter: boolean
): "emerald" | "red" | "neutral" {
  if (value === 0) return "neutral";
  const improved = lowerIsBetter ? value < 0 : value > 0;
  return improved ? "emerald" : "red";
}

function QualityScorePanel({
  summary,
}: {
  summary: OptimizationQualitySummary;
}) {
  return (
    <section className="border border-neutral-800 bg-neutral-950/60">
      <div className="border-b border-neutral-800 px-5 py-4">
        <SectionHeader
          eyebrow="Quality"
          title="Recent Trace Quality Score"
          description="This stays because it is derived from recent traces, not from a static checklist. It tells you whether session behavior is degrading even when savings still look healthy."
        />
      </div>

      <div className="grid gap-4 p-5 xl:grid-cols-[0.78fr_1.22fr]">
        <div className="space-y-4">
          <div className="border border-neutral-800 bg-black/20 p-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
                  Quality grade
                </div>
                <div className="mt-2 text-3xl font-semibold text-neutral-100">
                  {summary.score}/100
                </div>
              </div>
              <Pill label={summary.grade} tone={gradeTone(summary.grade)} />
            </div>
            <div className="mt-3 text-sm text-neutral-400 leading-relaxed">
              {summary.trace_count.toLocaleString()} traces scored in the active
              window.
            </div>
            <div className="mt-2 text-xs text-neutral-500">
              Dominant model: {summary.dominant_model || "unknown"}
              {summary.dominant_context_window_tokens > 0
                ? ` • ${fmtTokens(summary.dominant_context_window_tokens)} window`
                : ""}
            </div>
          </div>

          <div className="border border-neutral-800 bg-black/20 p-4">
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Recommendations
            </div>
            <div className="mt-3 space-y-2 text-sm text-neutral-300">
              {summary.recommendations.map((item) => (
                <div key={item} className="leading-relaxed">
                  {item}
                </div>
              ))}
            </div>
          </div>

          {summary.risk_flags.length > 0 && (
            <div className="border border-amber-900/40 bg-amber-950/10 p-4">
              <div className="text-[10px] font-mono uppercase tracking-widest text-amber-300">
                Risk flags
              </div>
              <div className="mt-3 space-y-2 text-sm text-amber-100/90">
                {summary.risk_flags.map((item) => (
                  <div key={item} className="leading-relaxed">
                    {item}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="space-y-3">
          {summary.signals.length === 0 ? (
            <div className="border border-neutral-800 bg-black/20 p-4 text-sm text-neutral-500">
              No signal data was available for this window.
            </div>
          ) : (
            summary.signals.map((signal) => (
              <article
                key={signal.id}
                className="border border-neutral-800 bg-black/20 p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-sm font-semibold text-neutral-100">
                      {signal.title}
                    </div>
                    <div className="mt-1 text-[10px] font-mono uppercase tracking-widest text-neutral-500">
                      Weight {signal.weight_pct}%
                    </div>
                  </div>
                  <div className="text-sm font-mono text-neutral-300">
                    {signal.score}
                  </div>
                </div>
                <div className="mt-3 h-2 overflow-hidden rounded-full bg-neutral-900">
                  <div
                    className={cx(
                      "h-full rounded-full",
                      signalTone(signal.score),
                      signalWidthClass(signal.score)
                    )}
                  />
                </div>
                <p className="mt-3 text-sm text-neutral-400 leading-relaxed">
                  {signal.detail}
                </p>
              </article>
            ))
          )}
        </div>
      </div>
    </section>
  );
}

function ImpactValidationPanel({
  validation,
}: {
  validation: OptimizationImpactValidation;
}) {
  const tokenTone = deltaTone(validation.deltas.tokens_pct, true);
  const costTone = deltaTone(validation.deltas.cost_pct, true);
  const cacheTone = deltaTone(validation.deltas.cache_leverage_pct, false);

  return (
    <section className="border border-neutral-800 bg-neutral-950/60">
      <div className="border-b border-neutral-800 px-5 py-4">
        <SectionHeader
          eyebrow="Impact Validation"
          title="Before/After Proof"
          description="The split is automatic and chronological. It compares earlier traces in the window against later ones so the tab can show whether token load, spend, and cache leverage actually moved."
        />
      </div>

      <div className="space-y-4 p-5">
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm text-neutral-400">
            Strategy:{" "}
            <span className="text-neutral-200">
              {validation.strategy.replaceAll("_", " ")}
            </span>
          </div>
          <Pill
            label={validation.verdict}
            tone={verdictTone(validation.verdict)}
          />
        </div>

        <div className="grid gap-4 md:grid-cols-3">
          <article className="border border-neutral-800 bg-black/20 p-4">
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Avg tokens per trace
            </div>
            <div className="mt-3 text-sm text-neutral-300">
              {fmtTokens(validation.before.avg_tokens)}
              <span className="mx-2 text-neutral-600">→</span>
              {fmtTokens(validation.after.avg_tokens)}
            </div>
            <div className="mt-2">
              <Pill
                label={fmtPct(validation.deltas.tokens_pct)}
                tone={tokenTone}
              />
            </div>
          </article>

          <article className="border border-neutral-800 bg-black/20 p-4">
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Avg cost per trace
            </div>
            <div className="mt-3 text-sm text-neutral-300">
              {fmtUsd(validation.before.avg_cost_usd)}
              <span className="mx-2 text-neutral-600">→</span>
              {fmtUsd(validation.after.avg_cost_usd)}
            </div>
            <div className="mt-2">
              <Pill
                label={fmtPct(validation.deltas.cost_pct)}
                tone={costTone}
              />
            </div>
          </article>

          <article className="border border-neutral-800 bg-black/20 p-4">
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Cache leverage
            </div>
            <div className="mt-3 text-sm text-neutral-300">
              {fmtRatio(validation.before.avg_cache_leverage)}
              <span className="mx-2 text-neutral-600">→</span>
              {fmtRatio(validation.after.avg_cache_leverage)}
            </div>
            <div className="mt-2">
              <Pill
                label={fmtPct(validation.deltas.cache_leverage_pct)}
                tone={cacheTone}
              />
            </div>
          </article>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <article className="border border-neutral-800 bg-black/20 p-4">
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Before window
            </div>
            <div className="mt-3 text-sm text-neutral-300">
              {validation.before.trace_count.toLocaleString()} traces
            </div>
            <div className="mt-1 text-xs text-neutral-500">
              {fmtRange(validation.before.from, validation.before.to)}
            </div>
            <div className="mt-3 text-xs text-neutral-500">
              Avg tracked tool savings:{" "}
              {fmtTokens(validation.before.avg_saved_tokens)}
            </div>
          </article>

          <article className="border border-neutral-800 bg-black/20 p-4">
            <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              After window
            </div>
            <div className="mt-3 text-sm text-neutral-300">
              {validation.after.trace_count.toLocaleString()} traces
            </div>
            <div className="mt-1 text-xs text-neutral-500">
              {fmtRange(validation.after.from, validation.after.to)}
            </div>
            <div className="mt-3 text-xs text-neutral-500">
              Avg tracked tool savings:{" "}
              {fmtTokens(validation.after.avg_saved_tokens)}
            </div>
          </article>
        </div>

        <div className="border border-neutral-800 bg-black/20 p-4">
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
            What changed
          </div>
          <div className="mt-3 space-y-2 text-sm text-neutral-300">
            {validation.notes.map((item) => (
              <div key={item} className="leading-relaxed">
                {item}
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function AutoOptimizationGrid({
  rows,
}: {
  rows: OptimizationAutoOptimization[];
}) {
  return (
    <section className="space-y-4">
      <SectionHeader
        eyebrow="Auto Optimization"
        title="Observed Automation At Work"
        description="Only levers that saved tokens in the current window are shown here. Static capability catalogs were removed."
      />
      {rows.length === 0 ? (
        <section className="border border-neutral-800 bg-neutral-950/60 p-5 text-sm text-neutral-500">
          No observed optimization levers were recorded in this window.
        </section>
      ) : (
        <div className="grid gap-4 xl:grid-cols-2">
          {rows.map((row) => (
            <article
              key={row.id}
              className="border border-neutral-800 bg-neutral-950/60 p-4"
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-sm font-semibold text-neutral-100">
                    {row.title}
                  </div>
                  <div className="mt-1 text-[10px] font-mono uppercase tracking-widest text-neutral-500">
                    {row.id.replaceAll("_", " ")}
                  </div>
                </div>
                <Pill label={fmtTokens(row.tokens_saved)} tone="emerald" />
              </div>

              <div className="mt-4 grid grid-cols-2 gap-3">
                <MetricCard
                  label="Cost saved"
                  value={fmtUsd(row.cost_saved_usd)}
                  tone="amber"
                />
                <MetricCard
                  label="Calls saved"
                  value={row.calls_saved.toLocaleString()}
                  detail={`${row.session_count.toLocaleString()} sessions`}
                  tone="cyan"
                />
              </div>

              <div className="mt-4 text-[11px] uppercase tracking-widest text-neutral-500">
                Tools observed
              </div>
              <div className="mt-2 flex flex-wrap gap-2 text-xs text-neutral-400">
                {row.tools.length > 0 ? (
                  row.tools.map((tool) => (
                    <Pill key={tool} label={tool} tone="neutral" />
                  ))
                ) : (
                  <span>Telemetry came from aggregated savings history.</span>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function RereadTelemetryPanel({
  telemetry,
}: {
  telemetry: OptimizationRereadTelemetry;
}) {
  return (
    <section className="border border-neutral-800 bg-neutral-950/60">
      <div className="border-b border-neutral-800 px-5 py-4">
        <SectionHeader
          eyebrow="Reread Telemetry"
          title="Delta Read And Structure Map Savings"
          description="Repeated file work is now measured explicitly. Outline-first reads land as structure-map savings; narrow follow-up reads land as delta-read savings."
        />
      </div>

      <div className="space-y-4 p-5">
        <div className="grid gap-4 md:grid-cols-2">
          <MetricCard
            label="Reread savings"
            value={fmtTokens(telemetry.total_tokens_saved)}
            detail={`${telemetry.event_count.toLocaleString()} measured rereads`}
            tone="emerald"
          />
          <MetricCard
            label="Cost saved"
            value={fmtUsd(telemetry.total_cost_saved_usd)}
            detail={`${telemetry.top_paths.length.toLocaleString()} active files`}
            tone="cyan"
          />
        </div>

        <div className="grid gap-4 xl:grid-cols-2">
          <div className="space-y-3">
            {telemetry.kinds.length === 0 ? (
              <div className="border border-neutral-800 bg-black/20 p-4 text-sm text-neutral-500">
                No structure-map or delta-read savings were recorded in this
                window.
              </div>
            ) : (
              telemetry.kinds.map((kind) => (
                <article
                  key={kind.id}
                  className="border border-neutral-800 bg-black/20 p-4"
                >
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-semibold text-neutral-100">
                        {kind.title}
                      </div>
                      <div className="mt-1 text-[10px] font-mono uppercase tracking-widest text-neutral-500">
                        {kind.event_count.toLocaleString()} events •{" "}
                        {kind.path_count.toLocaleString()} files
                      </div>
                    </div>
                    <Pill label={fmtTokens(kind.tokens_saved)} tone="emerald" />
                  </div>
                  <div className="mt-3 text-xs text-neutral-500">
                    Cost saved: {fmtUsd(kind.cost_saved_usd)}
                    {kind.last_seen_at
                      ? ` • Last seen ${new Date(kind.last_seen_at).toLocaleString()}`
                      : ""}
                  </div>
                </article>
              ))
            )}
          </div>

          <div className="border border-neutral-800 bg-black/20">
            <div className="border-b border-neutral-800 px-4 py-3 text-[10px] font-mono uppercase tracking-widest text-neutral-500">
              Top files
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs border-collapse">
                <thead>
                  <tr className="border-b border-neutral-800 bg-neutral-900/50 text-[10px] uppercase tracking-widest text-neutral-500 font-mono">
                    <th className="px-4 py-3">Path</th>
                    <th className="px-4 py-3 text-right">Events</th>
                    <th className="px-4 py-3 text-right">Saved</th>
                    <th className="px-4 py-3">Kinds</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-neutral-900">
                  {telemetry.top_paths.length === 0 ? (
                    <tr>
                      <td
                        colSpan={4}
                        className="px-4 py-8 text-center text-neutral-600 italic"
                      >
                        No reread-heavy files were recorded.
                      </td>
                    </tr>
                  ) : (
                    telemetry.top_paths.map((row) => (
                      <tr
                        key={row.path}
                        className="hover:bg-neutral-900/40 align-top"
                      >
                        <td className="px-4 py-3 font-mono text-neutral-300">
                          {row.path}
                        </td>
                        <td className="px-4 py-3 text-right text-neutral-400">
                          {row.event_count.toLocaleString()}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-cyan-300">
                          {fmtTokens(row.tokens_saved)}
                        </td>
                        <td className="px-4 py-3 text-neutral-500">
                          {row.kinds.join(" • ")}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function RoutingSimulationPanel({
  simulation,
}: {
  simulation: OptimizationModelRoutingSimulation;
}) {
  return (
    <section className="border border-neutral-800 bg-neutral-950/60">
      <div className="border-b border-neutral-800 px-5 py-4">
        <SectionHeader
          eyebrow="Routing Simulation"
          title="Routine Trace Downshift"
          description="This is a conservative simulation, not a policy change. It estimates what obviously routine traces would have cost on a cheaper tier."
        />
      </div>

      <div className="space-y-4 p-5">
        <div className="grid gap-4 md:grid-cols-3">
          <MetricCard
            label="Candidates"
            value={simulation.candidate_count.toLocaleString()}
            detail={fmtTokens(simulation.total_tokens_rerouted)}
            tone="cyan"
          />
          <MetricCard
            label="Current spend"
            value={fmtUsd(simulation.current_cost_usd)}
            detail="Across candidate traces"
            tone="neutral"
          />
          <MetricCard
            label="Estimated savings"
            value={fmtUsd(simulation.estimated_cost_saved_usd)}
            detail={fmtUsd(simulation.simulated_cost_usd)}
            tone="emerald"
          />
        </div>

        <div className="border border-neutral-800 bg-black/20 p-4 text-sm text-neutral-400 leading-relaxed">
          {simulation.heuristic}
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs border-collapse">
            <thead>
              <tr className="border-b border-neutral-800 bg-neutral-900/50 text-[10px] uppercase tracking-widest text-neutral-500 font-mono">
                <th className="px-4 py-3">Trace</th>
                <th className="px-4 py-3">Current → Target</th>
                <th className="px-4 py-3 text-right">Tokens</th>
                <th className="px-4 py-3 text-right">Cost delta</th>
                <th className="px-4 py-3">Why routine</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-900">
              {simulation.candidates.length === 0 ? (
                <tr>
                  <td
                    colSpan={5}
                    className="px-4 py-8 text-center text-neutral-600 italic"
                  >
                    No conservative routing candidates were detected in this
                    window.
                  </td>
                </tr>
              ) : (
                simulation.candidates.map((row) => (
                  <tr
                    key={row.trace_id}
                    className="hover:bg-neutral-900/40 align-top"
                  >
                    <td className="px-4 py-3">
                      <div className="font-mono text-neutral-200">
                        {row.trace_id}
                      </div>
                      <div className="mt-1 text-neutral-500">{row.task}</div>
                    </td>
                    <td className="px-4 py-3 text-neutral-400">
                      {row.current_model}
                      <span className="mx-2 text-neutral-600">→</span>
                      {row.target_model}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-neutral-300">
                      {fmtTokens(row.total_tokens)}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="font-mono text-emerald-300">
                        {fmtUsd(row.estimated_cost_saved_usd)}
                      </div>
                      <div className="mt-1 text-[11px] text-neutral-500">
                        {fmtUsd(row.current_cost_usd)} →{" "}
                        {fmtUsd(row.simulated_cost_usd)}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-neutral-500 leading-relaxed">
                      {row.reason}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function Recommendations({ rows }: { rows: OptimizationRecommendation[] }) {
  if (rows.length === 0) {
    return (
      <section className="border border-neutral-800 bg-neutral-950/60 p-5 text-sm text-neutral-400">
        No high-confidence optimization opportunities were detected in this
        trace window.
      </section>
    );
  }

  return (
    <section className="space-y-4">
      <SectionHeader
        eyebrow="Recommendations"
        title="Current Session Optimization Opportunities"
        description="These still come from the recent trace window, not from static rules. They represent real sessions that look expensive or low-yield right now."
      />
      <div className="grid gap-4 xl:grid-cols-3">
        {rows.map((row) => (
          <article
            key={row.id}
            className={cx(
              "border p-4",
              row.severity === "high"
                ? "border-amber-900/50 bg-amber-950/15"
                : "border-neutral-800 bg-neutral-950/60"
            )}
          >
            <div className="flex items-center justify-between gap-2">
              <div className="text-sm font-semibold text-neutral-100">
                {row.title}
              </div>
              <Pill
                label={row.severity}
                tone={row.severity === "high" ? "amber" : "cyan"}
              />
            </div>
            <div className="mt-4 grid grid-cols-2 gap-3">
              <MetricCard
                label="Sessions"
                value={row.session_count.toString()}
                tone="neutral"
              />
              <MetricCard
                label="Est. Savings"
                value={fmtUsd(row.estimated_usd_saved)}
                detail={fmtTokens(row.estimated_tokens_saved)}
                tone="amber"
              />
            </div>
            <div className="mt-4 text-[11px] uppercase tracking-widest text-neutral-500">
              Suggested action
            </div>
            <p className="mt-1 text-sm text-neutral-300 leading-relaxed">
              {row.action}
            </p>
            <div className="mt-4 space-y-2">
              {row.sessions.slice(0, 4).map((session) => (
                <div
                  key={session.trace_id}
                  className="border border-neutral-800/80 bg-black/20 p-3 text-xs"
                >
                  <div className="font-mono text-neutral-200">
                    {session.trace_id}
                  </div>
                  <div className="mt-1 text-neutral-400">
                    {session.project || session.host || "session"}
                    {typeof session.cost_usd === "number"
                      ? ` • ${fmtUsd(session.cost_usd)}`
                      : ""}
                    {typeof session.input_output_ratio === "number"
                      ? ` • ${session.input_output_ratio.toFixed(1)}:1 in/out`
                      : ""}
                    {typeof session.multiple === "number"
                      ? ` • ${session.multiple.toFixed(1)}x peer avg`
                      : ""}
                  </div>
                  {session.tools && session.tools.length > 0 && (
                    <div className="mt-1 text-neutral-500">
                      Tools: {session.tools.join(", ")}
                    </div>
                  )}
                  {session.reason && (
                    <div className="mt-1 text-neutral-500">
                      {session.reason}
                    </div>
                  )}
                  <Link
                    to={`/runs?trace=${encodeURIComponent(session.trace_id)}`}
                    className="mt-3 inline-flex border border-neutral-700 px-2 py-1 text-[10px] font-mono uppercase tracking-widest text-neutral-300 transition hover:border-amber-500/50 hover:text-amber-300"
                  >
                    Open in Runs
                  </Link>
                </div>
              ))}
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function CodeBurnOptimizations({
  external,
}: {
  external: OptimizationsSummary["external_optimizations"];
}) {
  if (!external || !external.payload) return null;

  const { overview, recommendations } = external.payload;

  return (
    <section className="space-y-4">
      <SectionHeader
        eyebrow="External Insights"
        title="CodeBurn Efficiency Report"
        description="These recommendations are generated by CodeBurn's post-hoc efficiency analyzer. They complement Atelier's trace-based heuristics with historical waste detection."
      />

      <div className="grid gap-4 md:grid-cols-4">
        <MetricCard
          label="Sessions analyzed"
          value={overview.sessions?.toLocaleString() || "0"}
          detail={`${overview.calls?.toLocaleString() || "0"} calls`}
          tone="neutral"
        />
        <MetricCard
          label="Waste detected"
          value={fmtUsd(overview.estimated_usd_saved || 0)}
          detail={fmtTokens(overview.estimated_tokens_saved || 0)}
          tone="red"
        />
        <MetricCard
          label="Health Grade"
          value={overview.health_grade || "Unknown"}
          detail={`Score: ${overview.health_score || 0}/100`}
          tone={gradeTone(overview.health_grade)}
        />
        <MetricCard
          label="Report Age"
          value={new Date(external.collected_at).toLocaleDateString()}
          detail={`Source: ${external.source}`}
          tone="cyan"
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        {recommendations.map((row, idx) => (
          <article
            key={idx}
            className={cx(
              "border p-4",
              row.severity === "high"
                ? "border-red-900/50 bg-red-950/15"
                : "border-neutral-800 bg-neutral-950/60"
            )}
          >
            <div className="flex items-center justify-between gap-2">
              <div className="text-sm font-semibold text-neutral-100">
                {row.title}
              </div>
              <Pill
                label={row.severity}
                tone={row.severity === "high" ? "red" : "amber"}
              />
            </div>

            <div className="mt-3 text-sm text-neutral-400 leading-relaxed">
              {row.description}
            </div>

            <div className="mt-4 flex items-center gap-4">
              <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
                Est. Savings:{" "}
                <span className="text-emerald-400">
                  {fmtTokens(row.estimated_tokens_saved)}
                </span>{" "}
                /{" "}
                <span className="text-amber-400">
                  {fmtUsd(row.estimated_usd_saved)}
                </span>
              </div>
            </div>

            {row.action && (
              <>
                <div className="mt-4 text-[10px] font-mono uppercase tracking-widest text-neutral-500">
                  Action required
                </div>
                <div className="mt-2 border border-neutral-800 bg-black/40 p-3 font-mono text-[11px] text-neutral-300 whitespace-pre-wrap leading-relaxed">
                  {row.action}
                </div>
              </>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}

export default function Optimizations() {
  const [summary, setSummary] = useState<OptimizationsSummary | null>(null);
  const [days, setDays] = useState(14);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    api
      .optimizationsSummary(days)
      .then((payload) => {
        setSummary(payload);
        setErr(null);
      })
      .catch((error) => setErr(String(error)))
      .finally(() => setLoading(false));
  }, [days]);

  if (err) return <div className="text-red-400 p-6">Error: {err}</div>;
  if (!summary || loading)
    return (
      <div className="p-6 text-neutral-500 italic">Loading optimizations…</div>
    );

  return (
    <div className="space-y-8">
      <PageHero
        eyebrow="Optimizations"
        title="Measured Optimization Impact"
        description="This view now keeps only observed telemetry and trace-derived comparisons. Static host coverage tables, rule dumps, gap catalogs, and raw guidance blocks were removed."
        tone="amber"
      >
        <div className="border border-neutral-800 bg-neutral-950/60 p-4">
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
            Window
          </div>
          <div className="mt-3 flex items-center gap-3">
            <input
              type="number"
              value={days}
              min={1}
              max={30}
              aria-label="Optimization summary window in days"
              title="Optimization summary window in days"
              onChange={(e) =>
                setDays(Math.max(1, parseInt(e.target.value, 10) || 14))
              }
              className="w-20 border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-200 focus:border-amber-500 focus:outline-none"
            />
            <div className="text-xs text-neutral-500">
              Generated {new Date(summary.generated_at).toLocaleString()}
            </div>
          </div>
        </div>
      </PageHero>

      <section className="grid gap-4 md:grid-cols-4">
        <MetricCard
          label="Auto savings tracked"
          value={fmtUsd(summary.savings.saved_usd ?? 0)}
          detail={fmtTokens(
            (summary.savings.total_naive_tokens ?? 0) -
              (summary.savings.total_actual_tokens ?? 0)
          )}
          tone="emerald"
        />
        <MetricCard
          label="Impact verdict"
          value={summary.impact_validation.verdict.replaceAll("_", " ")}
          detail={`${fmtPct(summary.impact_validation.deltas.tokens_pct)} tokens • ${fmtPct(summary.impact_validation.deltas.cost_pct)} cost`}
          tone="amber"
        />
        <MetricCard
          label="Reread savings"
          value={fmtTokens(summary.reread_telemetry.total_tokens_saved)}
          detail={`${summary.reread_telemetry.event_count.toLocaleString()} measured rereads`}
          tone="cyan"
        />
        <MetricCard
          label="Routine reroute"
          value={fmtUsd(
            summary.model_routing_simulation.estimated_cost_saved_usd
          )}
          detail={`${summary.model_routing_simulation.candidate_count.toLocaleString()} candidates`}
          tone="violet"
        />
      </section>

      <section className="grid gap-4 xl:grid-cols-[1.08fr_0.92fr]">
        <QualityScorePanel summary={summary.quality_score} />
        <ImpactValidationPanel validation={summary.impact_validation} />
      </section>

      <section className="grid gap-4 xl:grid-cols-[0.98fr_1.02fr]">
        <AutoOptimizationGrid rows={summary.auto_optimizations} />
        <RereadTelemetryPanel telemetry={summary.reread_telemetry} />
      </section>

      <RoutingSimulationPanel simulation={summary.model_routing_simulation} />

      <CodeBurnOptimizations external={summary.external_optimizations} />

      <Recommendations rows={summary.recommendations.recommendations} />
    </div>
  );
}
