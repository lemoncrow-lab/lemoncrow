import { useEffect, useState } from "react";
import LeverBar from "../components/LeverBar";
import SavingsTimeChart from "../components/SavingsTimeChart";
import type {
  SavingsProofSession,
  SavingsSummaryV2,
  SavingsVerificationSummary,
} from "../api";
import { api } from "../api";

const usdFmt = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 4,
});

const fmt = new Intl.NumberFormat();

function toTitle(label: string): string {
  return label
    .split(/[_\s:-]+/)
    .filter(Boolean)
    .map((part) => part[0]?.toUpperCase() + part.slice(1))
    .join(" ");
}

function truncate(value: string, max = 240): string {
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function statusTone(status: string): string {
  if (status === "success") return "text-emerald-300 border-emerald-500/40";
  if (status === "failed") return "text-red-300 border-red-500/40";
  return "text-amber-300 border-amber-500/40";
}

function displayToolName(toolName: string): string {
  return toolName === "unattributed" ? "Capture gap" : toolName;
}

function Sparkline({ values }: { values: number[] }) {
  if (values.length === 0) return null;
  const width = 240;
  const height = 56;
  const maxVal = Math.max(1, ...values);
  const points = values
    .map((value, i) => {
      const x = (i * width) / Math.max(1, values.length - 1);
      const y = height - (value / maxVal) * height;
      return `${x},${y}`;
    })
    .join(" ");
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="w-full max-w-[240px]"
      aria-label="reduction sparkline"
    >
      <polyline fill="none" stroke="#06b6d4" strokeWidth="3" points={points} />
    </svg>
  );
}

function EmptyState() {
  return (
    <div className="border border-neutral-800 bg-neutral-950/70 p-6 text-neutral-300">
      <h2 className="font-mono text-lg text-neutral-100 mb-2">
        No savings telemetry yet
      </h2>
      <p className="text-sm text-neutral-400">
        Run any task with{" "}
        <code className="bg-neutral-900 px-1">atelier-mcp</code> enabled to
        start collecting savings telemetry.
      </p>
    </div>
  );
}

export default function Savings() {
  const [data, setData] = useState<SavingsSummaryV2 | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ledgerByRun, setLedgerByRun] = useState<Record<string, any>>({});
  const [ledgerLoading, setLedgerLoading] = useState<Record<string, boolean>>(
    {}
  );
  const [expandedRuns, setExpandedRuns] = useState<Record<string, boolean>>({});

  useEffect(() => {
    api
      .savingsSummary(14)
      .then(setData)
      .catch((e) => setErr(String(e)));
  }, []);

  if (err) return <div className="text-red-400">Error: {err}</div>;
  if (!data) return <div className="text-neutral-500">Loading…</div>;

  const latestBenchmark = data.latest_benchmark ?? null;
  const toolAggregates = data.tool_aggregates ?? [];
  const sessionProof = data.session_proof ?? [];
  const coverageGaps = data.coverage_gaps ?? [];
  const verification = data.verification ?? null;
  const hasData =
    data.total_naive_tokens > 0 ||
    latestBenchmark !== null ||
    sessionProof.length > 0 ||
    coverageGaps.length > 0;
  const sortedLevers = Object.entries(data.per_lever)
    .sort((a, b) => b[1] - a[1])
    .map(([label, value]) => ({ label, value }));
  const sparkValues = data.by_day.map((d) => {
    if (d.naive <= 0) return 0;
    return Math.max(0, Math.round((1 - d.actual / d.naive) * 100));
  });
  const maxLever = sortedLevers[0]?.value ?? 0;
  const topSources = data.top_sources ?? [];
  const trackedToolCalls = data.tracked_tool_calls ?? 0;
  const headlineLabel =
    verification?.headline_kind === "estimated_tool_compression"
      ? "Estimated Tool Compression"
      : verification?.headline_kind === "tracked_proof_reduction"
        ? "Tracked Proof Reduction"
        : "Token Reduction";
  const loadLedger = (runId: string) => {
    const nextExpanded = !expandedRuns[runId];
    setExpandedRuns((prev) => ({ ...prev, [runId]: nextExpanded }));
    if (!nextExpanded || ledgerByRun[runId] || ledgerLoading[runId]) {
      return;
    }
    setLedgerLoading((prev) => ({ ...prev, [runId]: true }));
    api
      .ledger(runId)
      .then((ledger) => {
        setLedgerByRun((prev) => ({ ...prev, [runId]: ledger }));
      })
      .catch((error) => {
        setLedgerByRun((prev) => ({
          ...prev,
          [runId]: { error: String(error) },
        }));
      })
      .finally(() => {
        setLedgerLoading((prev) => ({ ...prev, [runId]: false }));
      });
  };

  return (
    <div className="space-y-8">
      <section className="border border-cyan-900/60 bg-gradient-to-r from-cyan-950/60 to-neutral-950 p-6">
        <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-6">
          <div>
            <div className="text-[11px] font-mono uppercase tracking-[0.22em] text-cyan-300/80">
              {headlineLabel}
            </div>
            <div className="text-6xl md:text-7xl font-semibold leading-none text-cyan-200 mt-2">
              {data.reduction_pct.toFixed(1)}%
            </div>
            <p className="text-sm text-neutral-400 mt-3">
              {fmt.format(data.total_naive_tokens)} naive tool-output tokens vs{" "}
              {fmt.format(data.total_actual_tokens)} compacted tool-output
              tokens from {fmt.format(trackedToolCalls)} tracked tool turns over
              the last {data.window_days} days.
            </p>
            {verification?.headline_explanation && (
              <p className="max-w-3xl text-xs text-neutral-500 mt-3 leading-relaxed">
                {verification.headline_explanation}
              </p>
            )}
          </div>
          <div className="w-full md:w-auto">
            <Sparkline values={sparkValues} />
            <p className="font-mono text-[10px] text-neutral-500 uppercase tracking-wider mt-2">
              Daily reduction trend
            </p>
          </div>
        </div>
      </section>

      <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="border border-emerald-900/60 bg-emerald-950/30 p-4">
          <div className="text-[10px] font-mono uppercase tracking-widest text-emerald-400/70 mb-1">
            Cost Saved
          </div>
          <div className="text-2xl font-semibold text-emerald-300">
            {usdFmt.format(data.saved_usd ?? 0)}
          </div>
          {(data.saved_pct ?? 0) > 0 && (
            <div className="text-xs text-emerald-400/60 mt-1">
              {(data.saved_pct ?? 0).toFixed(1)}% vs baseline
            </div>
          )}
        </div>
        <div className="border border-neutral-800 bg-neutral-950/50 p-4">
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-400/70 mb-1">
            Actual Cost
          </div>
          <div className="text-2xl font-semibold text-neutral-200">
            {usdFmt.format(data.actually_cost_usd ?? 0)}
          </div>
          <div className="text-xs text-neutral-500 mt-1">
            {data.cost_basis === "context_budget"
              ? `tracked from context-budget rows (${usdFmt.format(data.tracked_actual_cost_usd ?? 0)} actual / ${usdFmt.format(data.tracked_baseline_cost_usd ?? 0)} baseline)`
              : `live estimate ${usdFmt.format(data.live_saved_usd ?? 0)}`}
          </div>
        </div>
        <div className="border border-neutral-800 bg-neutral-950/50 p-4">
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-400/70 mb-1">
            Calls Saved
          </div>
          <div className="text-2xl font-semibold text-neutral-200">
            {fmt.format(data.live_calls_saved ?? 0)}
          </div>
          <div className="text-xs text-neutral-500 mt-1">
            {(data.total_calls ?? 0) > 0
              ? `${fmt.format(data.total_calls ?? 0)} LLM calls tracked`
              : trackedToolCalls > 0
                ? `${fmt.format(trackedToolCalls)} tool turns tracked`
                : "0 tracked calls yet"}
          </div>
        </div>
        <div className="border border-neutral-800 bg-neutral-950/50 p-4">
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-400/70 mb-1">
            Actual Tool Tokens
          </div>
          <div className="text-2xl font-semibold text-neutral-200">
            {fmt.format(data.total_actual_tokens)}
          </div>
          <div className="text-xs text-neutral-500 mt-1">
            persisted compacted tool-output tokens
          </div>
        </div>
      </section>

      {(data.unpriced_cache_write_tokens ?? 0) > 0 && (
        <section className="border border-amber-900/50 bg-amber-950/20 p-4 text-xs text-neutral-300">
          <span className="font-mono uppercase tracking-widest text-amber-300 mr-2">
            Cost Note
          </span>
          {fmt.format(data.unpriced_cache_write_tokens ?? 0)} cache-write tokens
          were tracked in the proof data but are not priced by the current model
          pricing table yet.
        </section>
      )}

      {verification && verification.tracked_row_count > 0 && (
        <ManualVerificationPanel verification={verification} />
      )}

      {!hasData ? (
        <EmptyState />
      ) : (
        <>
          <section className="border border-neutral-800 bg-neutral-950/70 p-5">
            <h2 className="text-xs uppercase tracking-widest font-mono text-neutral-400 mb-4">
              Per-lever savings
            </h2>
            <div className="space-y-4">
              {sortedLevers.map((lever) => (
                <LeverBar
                  key={lever.label}
                  label={lever.label}
                  value={lever.value}
                  maxValue={maxLever}
                />
              ))}
            </div>
          </section>

          <SavingsTimeChart data={data.by_day} />

          {latestBenchmark && (
            <section className="border border-cyan-900/50 bg-cyan-950/20 p-5">
              <h2 className="text-xs uppercase tracking-widest font-mono text-cyan-300 mb-4">
                Latest paired benchmark
              </h2>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
                <div>
                  <div className="text-[10px] uppercase tracking-widest text-neutral-500">
                    Token reduction
                  </div>
                  <div className="text-2xl font-semibold text-cyan-200">
                    {latestBenchmark.reduction_pct.toFixed(1)}%
                  </div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-widest text-neutral-500">
                    Cost saved
                  </div>
                  <div className="text-2xl font-semibold text-emerald-300">
                    {usdFmt.format(latestBenchmark.cost_saved_usd)}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-widest text-neutral-500">
                    Tasks
                  </div>
                  <div className="text-2xl font-semibold text-neutral-200">
                    {fmt.format(latestBenchmark.n_prompts)}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-widest text-neutral-500">
                    Success
                  </div>
                  <div className="text-2xl font-semibold text-neutral-200">
                    {(latestBenchmark.atelier_success_rate * 100).toFixed(0)}%
                  </div>
                </div>
              </div>
              <p className="mt-3 text-xs text-neutral-500">
                Real paired command run: baseline{" "}
                {fmt.format(latestBenchmark.total_tokens_baseline)} tokens vs
                Atelier-enabled{" "}
                {fmt.format(latestBenchmark.total_tokens_atelier)} tokens.
              </p>
            </section>
          )}

          {topSources.length > 0 && (
            <section className="border border-neutral-800 bg-neutral-950/70 p-5">
              <h2 className="text-xs uppercase tracking-widest font-mono text-neutral-400 mb-4">
                Top savings sources
              </h2>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="text-[10px] uppercase tracking-widest text-neutral-500">
                    <tr>
                      <th className="pb-2 pr-4">Lever</th>
                      <th className="pb-2 pr-4">Tool</th>
                      <th className="pb-2 pr-4 text-right">Calls</th>
                      <th className="pb-2 pr-4 text-right">Tokens</th>
                      <th className="pb-2 text-right">Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {topSources.map((source) => (
                      <tr
                        key={`${source.lever}:${source.tool_name}`}
                        className="border-t border-neutral-900 text-neutral-300"
                      >
                        <td className="py-2 pr-4 font-semibold text-cyan-200">
                          {toTitle(source.lever)}
                        </td>
                        <td className="py-2 pr-4 text-neutral-400">
                          {source.tool_name}
                        </td>
                        <td className="py-2 pr-4 text-right">
                          {fmt.format(source.calls_saved)}
                        </td>
                        <td className="py-2 pr-4 text-right">
                          {fmt.format(source.tokens_saved)}
                        </td>
                        <td className="py-2 text-right text-emerald-300">
                          {usdFmt.format(source.cost_saved_usd)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="mt-3 text-xs text-neutral-500">
                These rows use an equivalent-call estimator: search, edit, and
                SQL tools count the built-in calls they replace, then apply live
                token constants to estimate avoided cost.
              </p>
            </section>
          )}

          {toolAggregates.length > 0 && (
            <section className="border border-neutral-800 bg-neutral-950/70 p-5">
              <h2 className="text-xs uppercase tracking-widest font-mono text-neutral-400 mb-4">
                Per-tool cost proof
              </h2>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="text-[10px] uppercase tracking-widest text-neutral-500">
                    <tr>
                      <th className="pb-2 pr-4">Tool</th>
                      <th className="pb-2 pr-4">Lever</th>
                      <th className="pb-2 pr-4 text-right">Turns</th>
                      <th className="pb-2 pr-4 text-right">Sessions</th>
                      <th className="pb-2 pr-4 text-right">Actual Cost</th>
                      <th className="pb-2 pr-4 text-right">Baseline</th>
                      <th className="pb-2 pr-4 text-right">Saved</th>
                      <th className="pb-2 text-right">Saved Tokens</th>
                    </tr>
                  </thead>
                  <tbody>
                    {toolAggregates.map((tool) => (
                      <tr
                        key={`${tool.tool_name}:${tool.lever}`}
                        className="border-t border-neutral-900 text-neutral-300"
                      >
                        <td className="py-2 pr-4 font-semibold text-cyan-200">
                          {displayToolName(tool.tool_name)}
                        </td>
                        <td className="py-2 pr-4 text-neutral-500">
                          {toTitle(tool.lever)}
                        </td>
                        <td className="py-2 pr-4 text-right">
                          {fmt.format(tool.turns)}
                        </td>
                        <td className="py-2 pr-4 text-right">
                          {fmt.format(tool.session_count)}
                        </td>
                        <td className="py-2 pr-4 text-right text-neutral-200">
                          {usdFmt.format(tool.actual_cost_usd)}
                        </td>
                        <td className="py-2 pr-4 text-right text-neutral-400">
                          {usdFmt.format(tool.baseline_cost_usd)}
                        </td>
                        <td className="py-2 pr-4 text-right text-emerald-300">
                          {usdFmt.format(tool.saved_cost_usd)}
                        </td>
                        <td className="py-2 text-right">
                          {fmt.format(tool.saved_tokens)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="mt-3 text-xs text-neutral-500 leading-relaxed">
                These rows are built from persisted proof items. Actual and
                baseline cost reflect the tracked tokens on each tool turn.
                Baseline here is the naive tool-output baseline, not audited
                provider billing; live-call overlays remain visible separately
                for avoided round trips.
              </p>
            </section>
          )}

          {sessionProof.length > 0 && (
            <section className="space-y-4">
              <div>
                <h2 className="text-xs uppercase tracking-widest font-mono text-neutral-400 mb-2">
                  Session proof
                </h2>
                <p className="text-xs text-neutral-500">
                  Each run below links saved tokens and cost back to concrete
                  tool turns. Expand a run to inspect the stored ledger,
                  imported trace conversation, and command/tool evidence.
                </p>
              </div>
              {sessionProof.map((session) => (
                <SessionProofCard
                  key={session.run_id}
                  session={session}
                  expanded={Boolean(expandedRuns[session.run_id])}
                  loading={Boolean(ledgerLoading[session.run_id])}
                  ledger={ledgerByRun[session.run_id]}
                  onToggle={() => loadLedger(session.run_id)}
                />
              ))}
            </section>
          )}

          {coverageGaps.length > 0 && (
            <section className="border border-red-900/40 bg-red-950/10 p-5">
              <h2 className="text-xs uppercase tracking-widest font-mono text-red-300 mb-4">
                Coverage gaps
              </h2>
              <div className="space-y-3">
                {coverageGaps.map((gap) => (
                  <div
                    key={gap.run_id}
                    className="border border-red-900/20 bg-neutral-950/40 p-4"
                  >
                    <div className="flex flex-wrap items-center gap-2 mb-2 text-[10px] font-mono uppercase tracking-widest">
                      <span className="px-2 py-0.5 border border-red-500/40 text-red-300">
                        {gap.agent}
                      </span>
                      {gap.trace_confidence && (
                        <span className="px-2 py-0.5 border border-neutral-700 text-neutral-400">
                          {gap.trace_confidence}
                        </span>
                      )}
                      <span className="text-neutral-500">{gap.run_id}</span>
                    </div>
                    <div className="text-sm text-neutral-200">{gap.task}</div>
                    <p className="mt-2 text-xs text-neutral-400 leading-relaxed">
                      {gap.reason}
                    </p>
                  </div>
                ))}
              </div>
            </section>
          )}
        </>
      )}

      <section className="border border-neutral-800 bg-neutral-950/60 p-5">
        <h2 className="text-xs uppercase tracking-widest font-mono text-neutral-400 mb-2">
          Why this matters
        </h2>
        <p className="text-sm text-neutral-300 leading-relaxed">
          This view breaks savings down by lever so regressions are visible
          immediately, not hidden in a single aggregate metric. See the
          <a
            className="text-cyan-300 hover:text-cyan-200 ml-1"
            href="/docs/architecture/IMPLEMENTATION_PLAN_V2.md"
            target="_blank"
            rel="noreferrer noopener"
          >
            V2 implementation plan
          </a>{" "}
          for the methodology.
        </p>
      </section>
    </div>
  );
}

function ManualVerificationPanel({
  verification,
}: {
  verification: SavingsVerificationSummary;
}) {
  const dominantRun = verification.dominant_run ?? null;
  const dominantItem = verification.dominant_item ?? null;
  const compactOutputRows = verification.compact_output_row_count ?? 0;
  const compactOutputSavedTokens =
    verification.compact_output_saved_tokens ?? 0;

  return (
    <section className="border border-amber-900/40 bg-amber-950/10 p-5 space-y-4">
      <div className="flex flex-col xl:flex-row xl:items-start xl:justify-between gap-4">
        <div>
          <h2 className="text-xs uppercase tracking-widest font-mono text-amber-300 mb-2">
            Manual verification
          </h2>
          <p className="text-sm text-neutral-300 leading-relaxed max-w-3xl">
            This section shows the raw proof coverage behind the headline so you
            can audit whether the aggregate is broad-based or dominated by a
            small number of tool turns.
          </p>
        </div>
        {verification.warning && (
          <div className="max-w-xl border border-amber-700/40 bg-neutral-950/50 p-3 text-xs text-amber-200 leading-relaxed">
            {verification.warning}
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
        <Metric
          label="Proof Rows"
          value={fmt.format(verification.tracked_row_count)}
        />
        <Metric
          label="Tracked Runs"
          value={fmt.format(verification.tracked_run_count)}
        />
        <Metric
          label="Trace Linked"
          value={fmt.format(verification.trace_linked_run_count)}
        />
        <Metric
          label="Ledger Backed"
          value={fmt.format(verification.ledger_backed_run_count)}
        />
        <Metric
          label="Live Events"
          value={fmt.format(verification.live_event_count)}
        />
        <Metric
          label="Coverage Gaps"
          value={fmt.format(verification.coverage_gap_count)}
        />
      </div>

      {compactOutputRows > 0 && (
        <div className="border border-neutral-800 bg-neutral-950/50 p-4 space-y-2">
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
            Excluded Compact Output Rows
          </div>
          <div className="text-lg font-semibold text-cyan-200">
            {fmt.format(compactOutputRows)} row
            {compactOutputRows === 1 ? "" : "s"}
          </div>
          <div className="text-xs text-neutral-400 leading-relaxed">
            {fmt.format(compactOutputSavedTokens)} saved tokens remain visible
            in session proof, but are excluded from the top-line headline
            because they represent tool-output compaction rather than audited
            run-level savings.
          </div>
        </div>
      )}

      <div className="grid xl:grid-cols-2 gap-3">
        <div className="border border-neutral-800 bg-neutral-950/50 p-4 space-y-2">
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
            Dominant Run
          </div>
          {dominantRun ? (
            <>
              <div className="text-lg font-semibold text-cyan-200">
                {verification.dominant_run_share_pct.toFixed(1)}% of saved
                tokens
              </div>
              <div className="text-xs text-neutral-400 font-mono break-all">
                {dominantRun.run_id}
              </div>
              <div className="text-sm text-neutral-300">
                {dominantRun.agent ?? "unknown"}
              </div>
              <p className="text-xs text-neutral-500 leading-relaxed">
                {truncate(dominantRun.task ?? "No trace task attached.", 180)}
              </p>
              <div className="text-xs text-neutral-400">
                {fmt.format(dominantRun.saved_tokens)} saved tokens
              </div>
            </>
          ) : (
            <div className="text-sm text-neutral-500">
              No tracked run data yet.
            </div>
          )}
        </div>

        <div className="border border-neutral-800 bg-neutral-950/50 p-4 space-y-2">
          <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
            Largest Proof Row
          </div>
          {dominantItem ? (
            <>
              <div className="text-lg font-semibold text-cyan-200">
                {verification.dominant_item_share_pct.toFixed(1)}% of saved
                tokens
              </div>
              <div className="text-sm text-neutral-300">
                {dominantItem.tool_name} · {toTitle(dominantItem.lever)} · turn{" "}
                {dominantItem.turn_index}
              </div>
              <div className="text-xs text-neutral-400 font-mono break-all">
                {dominantItem.run_id}
              </div>
              <div className="text-xs text-neutral-400 leading-relaxed">
                {fmt.format(dominantItem.saved_tokens)} saved tokens from{" "}
                {fmt.format(dominantItem.naive_tokens)} naive vs{" "}
                {fmt.format(dominantItem.actual_tokens)} compacted tokens.
              </div>
            </>
          ) : (
            <div className="text-sm text-neutral-500">
              No tracked proof rows yet.
            </div>
          )}
        </div>
      </div>

      <p className="text-xs text-neutral-500 font-mono break-all">
        Data root: {verification.data_root}
      </p>
    </section>
  );
}

function SessionProofCard({
  session,
  expanded,
  loading,
  ledger,
  onToggle,
}: {
  session: SavingsProofSession;
  expanded: boolean;
  loading: boolean;
  ledger: any;
  onToggle: () => void;
}) {
  const conversations = Array.isArray(ledger?.conversations)
    ? ledger.conversations
    : [];
  const toolsCalled = Array.isArray(ledger?.tools_called)
    ? ledger.tools_called
    : [];
  const commandsRun = Array.isArray(ledger?.commands_run)
    ? ledger.commands_run
    : [];
  const captureSources = session.capture_sources ?? [];
  const missingSurfaces = session.missing_surfaces ?? [];
  const hasEvidence =
    toolsCalled.length > 0 ||
    commandsRun.length > 0 ||
    conversations.length > 0;
  const missingEvidenceMessage =
    session.trace_id || session.has_ledger
      ? "This run has saved token rows, but the stored ledger/trace did not preserve tool-call, command, or conversation detail for this slice."
      : "This run only has persisted context-budget rows. No live ledger or imported trace survived, so detailed proof beyond the token rows cannot be reconstructed.";

  return (
    <article className="border border-neutral-800 bg-neutral-950/70 p-5 space-y-4">
      <div className="flex flex-col xl:flex-row xl:items-start xl:justify-between gap-4">
        <div className="space-y-2 min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-[10px] font-mono uppercase tracking-widest">
            <span
              className={`px-2 py-0.5 border ${statusTone(session.status)}`}
            >
              {session.status}
            </span>
            <span className="px-2 py-0.5 border border-neutral-700 text-neutral-400">
              {session.agent}
            </span>
            {session.trace_confidence && (
              <span className="px-2 py-0.5 border border-neutral-700 text-neutral-500">
                {session.trace_confidence}
              </span>
            )}
            {captureSources.map((source) => (
              <span
                key={source}
                className="px-2 py-0.5 border border-cyan-900/50 text-cyan-300"
              >
                {source}
              </span>
            ))}
            {missingSurfaces.map((surface) => (
              <span
                key={surface}
                className="px-2 py-0.5 border border-amber-900/50 text-amber-300"
              >
                missing:{surface}
              </span>
            ))}
          </div>
          <div className="text-sm text-neutral-100 break-words">
            {session.task}
          </div>
          <div className="text-[10px] font-mono text-neutral-500 break-all">
            {session.run_id}
          </div>
          {session.note && (
            <p className="text-xs text-amber-300/90 leading-relaxed">
              {session.note}
            </p>
          )}
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs min-w-[280px]">
          <Metric
            label="Saved Cost"
            value={usdFmt.format(session.saved_cost_usd)}
          />
          <Metric
            label="Saved Tokens"
            value={fmt.format(session.saved_tokens)}
          />
          <Metric
            label="Tool Turns"
            value={fmt.format(session.tracked_tool_calls)}
          />
          <Metric
            label="Live Calls Saved"
            value={fmt.format(session.live_calls_saved)}
          />
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-[10px] uppercase tracking-widest text-neutral-500">
            <tr>
              <th className="pb-2 pr-4">Turn</th>
              <th className="pb-2 pr-4">Tool</th>
              <th className="pb-2 pr-4">Lever</th>
              <th className="pb-2 pr-4 text-right">Actual</th>
              <th className="pb-2 pr-4 text-right">Baseline</th>
              <th className="pb-2 pr-4 text-right">Saved</th>
              <th className="pb-2 text-right">Saved Cost</th>
            </tr>
          </thead>
          <tbody>
            {session.items.map((item) => (
              <tr
                key={`${item.run_id}:${item.turn_index}:${item.tool_name}`}
                className="border-t border-neutral-900 text-neutral-300"
              >
                <td className="py-2 pr-4 font-mono text-neutral-500">
                  {item.turn_index}
                </td>
                <td className="py-2 pr-4 text-cyan-200 font-semibold">
                  {displayToolName(item.tool_name)}
                </td>
                <td className="py-2 pr-4 text-neutral-500">
                  {toTitle(item.lever)}
                </td>
                <td className="py-2 pr-4 text-right">
                  {fmt.format(item.actual_tokens)}
                </td>
                <td className="py-2 pr-4 text-right text-neutral-400">
                  {fmt.format(item.naive_tokens)}
                </td>
                <td className="py-2 pr-4 text-right">
                  {fmt.format(item.saved_tokens)}
                </td>
                <td className="py-2 text-right text-emerald-300">
                  {usdFmt.format(item.saved_cost_usd)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={onToggle}
          className="text-[11px] px-2.5 py-1 border border-neutral-700 text-neutral-300 hover:text-cyan-300 hover:border-cyan-500/50 transition"
        >
          {expanded ? "Hide evidence details" : "Inspect evidence details"}
        </button>
        {session.has_ledger && (
          <span className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
            stored ledger available
          </span>
        )}
      </div>

      {expanded && (
        <div className="border border-neutral-900 bg-neutral-950/60 p-4 space-y-4">
          {loading && (
            <div className="text-xs text-neutral-500">
              Loading ledger proof…
            </div>
          )}
          {!loading && ledger?.error && (
            <div className="text-xs text-red-400">{ledger.error}</div>
          )}
          {!loading && !ledger?.error && (
            <>
              {!hasEvidence ? (
                <div className="border border-amber-900/40 bg-amber-950/10 p-4 space-y-2 text-xs">
                  <div className="text-[10px] font-mono uppercase tracking-widest text-amber-300">
                    Capture gap
                  </div>
                  <div className="text-neutral-200">
                    No detailed tool-call, command, or conversation proof was
                    stored for this run.
                  </div>
                  <p className="text-neutral-400 leading-relaxed">
                    {missingEvidenceMessage}
                  </p>
                  {session.note && (
                    <p className="text-amber-200 leading-relaxed">
                      {session.note}
                    </p>
                  )}
                </div>
              ) : (
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 text-xs">
                  <div>
                    <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500 mb-2">
                      Tool calls
                    </div>
                    <div className="space-y-2">
                      {toolsCalled.length === 0 ? (
                        <div className="text-neutral-500">
                          No tool-call detail captured for this run.
                        </div>
                      ) : (
                        toolsCalled
                          .slice(0, 8)
                          .map((tool: any, index: number) => (
                            <div
                              key={`${tool.name}:${index}`}
                              className="border border-neutral-900 p-2"
                            >
                              <div className="text-cyan-200 font-semibold">
                                {tool.name}
                              </div>
                              <div className="text-neutral-500 font-mono">
                                x{tool.count ?? 1}
                              </div>
                              {tool.result_summary && (
                                <div className="mt-1 text-neutral-400 leading-relaxed">
                                  {truncate(String(tool.result_summary), 120)}
                                </div>
                              )}
                            </div>
                          ))
                      )}
                    </div>
                  </div>

                  <div>
                    <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500 mb-2">
                      Commands
                    </div>
                    <div className="space-y-2">
                      {commandsRun.length === 0 ? (
                        <div className="text-neutral-500">
                          No command detail captured for this run.
                        </div>
                      ) : (
                        commandsRun
                          .slice(0, 8)
                          .map((command: any, index: number) => {
                            const commandText =
                              typeof command === "string"
                                ? command
                                : String(command.command ?? "");
                            return (
                              <div
                                key={`${commandText}:${index}`}
                                className="border border-neutral-900 p-2"
                              >
                                <div className="font-mono text-neutral-300 break-all">
                                  {truncate(commandText, 140)}
                                </div>
                              </div>
                            );
                          })
                      )}
                    </div>
                  </div>

                  <div>
                    <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500 mb-2">
                      Conversation / LLM response
                    </div>
                    <div className="space-y-2">
                      {conversations.length === 0 ? (
                        <div className="text-neutral-500">
                          No conversation transcript was attached to this run.
                        </div>
                      ) : (
                        conversations
                          .slice(0, 6)
                          .map((entry: any, index: number) => (
                            <div
                              key={`${entry.kind}:${index}`}
                              className="border border-neutral-900 p-2"
                            >
                              <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">
                                {entry.kind}
                              </div>
                              <div className="mt-1 text-neutral-200 leading-relaxed">
                                {truncate(String(entry.summary ?? ""), 120)}
                              </div>
                              {entry.content && (
                                <div className="mt-1 text-neutral-500 leading-relaxed whitespace-pre-wrap break-words">
                                  {truncate(String(entry.content), 180)}
                                </div>
                              )}
                            </div>
                          ))
                      )}
                    </div>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </article>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="border border-neutral-900 bg-neutral-950/40 p-3">
      <div className="text-[10px] font-mono uppercase tracking-widest text-neutral-500 mb-1">
        {label}
      </div>
      <div className="text-sm font-semibold text-neutral-200">{value}</div>
    </div>
  );
}
