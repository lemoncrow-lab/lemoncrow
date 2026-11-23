import { Button as UiButton } from "../components/ui/button";
import { useEffect, useState } from "react";
import { useTimeRange } from "../lib/TimeRangeContext";
import LeverBar from "../components/LeverBar";
import SavingsTimeChart from "../components/SavingsTimeChart";
import { EmptyState } from "../components/WorkbenchUI";
import { fmtPct, fmtTok, fmtUsd } from "../lib/format";
import { toolLabel } from "../lib/labels";
import { REDUCTION_HINT, REDUCTION_LABEL, savingsHeadline } from "../lib/savings";
import { ServerUnreachable, describeError, isUnreachable } from "../components/ServerUnreachable";
import type {
  SavingsProofSession,
  SavingsSummaryV2,
  SavingsVerificationSummary,
} from "../api";
import { api } from "../api";

// TODO: drop this extension once frontend/src/api.ts's SavingsSummaryV2 lands
// read_saved_usd / carry_tokens / total_saved_usd from the backend batch
// landing concurrently (see savings-display consistency plan).

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
  return toolLabel(toolName);
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

export default function Savings() {
  const [data, setData] = useState<SavingsSummaryV2 | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);
  const [ledgerByRun, setLedgerByRun] = useState<Record<string, any>>({});
  const [ledgerLoading, setLedgerLoading] = useState<Record<string, boolean>>(
    {}
  );
  const [expandedRuns, setExpandedRuns] = useState<Record<string, boolean>>({});
  const { days } = useTimeRange();

  useEffect(() => {
    setErr(null);
    api
      .savingsSummary(days)
      .then(setData)
      .catch((e) => setErr(e ?? new Error("Savings couldn't be loaded.")));
  }, [days, attempt]);

  if (err) {
    if (isUnreachable(err)) return <ServerUnreachable onRetry={() => setAttempt((n) => n + 1)} />;
    return <div role="alert" className="p-4 text-body leading-6 text-muted-foreground">{describeError(err)}</div>;
  }
  if (!data) return <div className="text-muted-foreground">Loading…</div>;

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
  // The headline is the shared definition (lib/savings.ts): the same Saved and Cost reduction as Home and Usage.
  const headline = savingsHeadline(data);
  const headlineLabel = REDUCTION_LABEL;
  const totalSavedUsd = headline.savedUsd;
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
    <div className="space-y-4">
      <section className="rounded-lg border border-border/70 bg-background/60 p-4">
        <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
          <div>
            <div className="text-label font-mono uppercase tracking-tag text-cyan-300" title={REDUCTION_HINT}>
              {headlineLabel}
            </div>
            <div className="mt-1 text-h2 font-semibold leading-none text-foreground">
              {fmtPct(headline.reductionPct)}
            </div>
            <p className="text-heading text-muted-foreground mt-3">
              {fmtUsd(totalSavedUsd)} saved of{" "}
              {fmtUsd(headline.baselineUsd)} would-have-cost over the
              last {data.window_days} days —{" "}
              {fmtTok(
                data.cost_basis === "session_ledger"
                  ? (data.ledger_tokens_saved ?? 0)
                  : data.total_naive_tokens
              )}{" "}
              tokens kept out of the model across{" "}
              {fmtTok(
                data.cost_basis === "session_ledger"
                  ? (data.ledger_calls_saved ?? 0)
                  : (data.live_calls_saved ?? 0)
              )}{" "}
              avoided calls.
            </p>
            <p className="max-w-3xl text-body text-muted-foreground mt-3 leading-relaxed">
              {data.cost_basis === "context_budget"
                ? "Worked out from each tool output LemonCrow recorded: its token cost with LemonCrow against what it would have cost without. The per-session totals below are the ones the status line and the savings command show, so you can compare them."
                : "The savings recorded for each session: the same figure the status line and the savings command show."}
            </p>
          </div>
          <div className="w-full md:w-auto">
            <Sparkline values={sparkValues} />
            <p className="font-mono text-caption text-muted-foreground uppercase tracking-wider mt-2">
              Daily reduction trend
            </p>
          </div>
        </div>
      </section>

      {/* At most three cards a row: the sheet is narrow, and six columns wrapped each line to one or two words. */}
      <section className="grid grid-cols-2 md:grid-cols-3 gap-3">
        <div className="rounded-lg border border-border/70 border-l-2 border-l-emerald-600/50 bg-background/60 p-4">
          <div className="text-caption font-mono uppercase tracking-widest text-emerald-300 mb-1">
            Tool-output savings
          </div>
          <div className="text-h3 font-semibold text-emerald-300">
            {fmtUsd(data.saved_usd ?? 0)}
          </div>
          <div className="text-body text-muted-foreground mt-1">
            part of the {fmtUsd(totalSavedUsd)} saved
          </div>
          {(data.routing_saved_usd ?? 0) > 0 && (
            <div className="text-body text-muted-foreground mt-1">
              plus {fmtUsd(data.routing_saved_usd ?? 0)} from model routing, counted separately
            </div>
          )}
          {(data.ops_saved_usd ?? 0) > 0 && (
            <div className="text-body text-muted-foreground mt-1">
              internal ops: {fmtUsd(data.ops_saved_usd ?? 0)}
            </div>
          )}
        </div>
        <div className="rounded-lg border border-border/70 bg-background/60 p-4">
          <div className="text-caption font-mono uppercase tracking-widest text-muted-foreground/70 mb-1">
            Actual Cost
          </div>
          <div className="text-h3 font-semibold text-foreground">
            {fmtUsd(data.actually_cost_usd ?? 0)}
          </div>
          <div className="text-body text-muted-foreground mt-1">
            {data.cost_basis === "session_ledger"
              ? `realized session spend (${fmtUsd(data.saved_usd ?? 0)} saved)`
              : data.cost_basis === "context_budget"
                ? `${fmtUsd(data.tracked_actual_cost_usd ?? 0)} with LemonCrow against ${fmtUsd(data.tracked_baseline_cost_usd ?? 0)} without, on the recorded tool outputs`
                : `live estimate ${fmtUsd(data.live_saved_usd ?? 0)}`}
          </div>
        </div>
        <div className="rounded-lg border border-border/70 bg-background/60 p-4">
          <div className="text-caption font-mono uppercase tracking-widest text-muted-foreground/70 mb-1">
            Calls Saved
          </div>
          <div className="text-h3 font-semibold text-foreground">
            {fmtTok(
              data.cost_basis === "session_ledger"
                ? (data.ledger_calls_saved ?? 0)
                : (data.live_calls_saved ?? 0)
            )}
          </div>
          <div className="text-body text-muted-foreground mt-1">
            {(data.total_calls ?? 0) > 0
              ? `${fmtTok(data.total_calls ?? 0)} LLM calls tracked`
              : trackedToolCalls > 0
                ? `${fmtTok(trackedToolCalls)} tool turns tracked`
                : "0 tracked calls yet"}
          </div>
        </div>
        <div className="rounded-lg border border-border/70 bg-background/60 p-4">
          <div className="text-caption font-mono uppercase tracking-widest text-muted-foreground/70 mb-1">
            Actual Tool Tokens
          </div>
          <div className="text-h3 font-semibold text-foreground">
            {fmtTok(data.total_actual_tokens)}
          </div>
          <div className="text-body text-muted-foreground mt-1">
            tool-output tokens the model read after LemonCrow shortened them
          </div>
        </div>
        <div className="rounded-lg border border-border/70 border-l-2 border-l-emerald-600/50 bg-background/60 p-4">
          <div className="text-caption font-mono uppercase tracking-widest text-emerald-300 mb-1">
            Read Savings
          </div>
          <div className="text-h3 font-semibold text-emerald-300">
            {fmtUsd(data.read_saved_usd ?? 0)}
          </div>
          <div className="text-body text-muted-foreground mt-1">
            cache-read and retrieval cost avoided
          </div>
        </div>
        <div className="rounded-lg border border-border/70 bg-muted/60 p-4">
          <div className="mb-1 text-caption font-mono uppercase tracking-widest text-muted-foreground/70">
            Context Carry
          </div>
          <div className="text-h3 font-semibold text-foreground">
            {fmtUsd(data.carry_usd ?? 0)}
          </div>
          <div className="text-body text-muted-foreground mt-1">
            {fmtTok(data.carry_tokens ?? 0)} tokens carried
          </div>
        </div>
      </section>

      {(data.unpriced_cache_write_tokens ?? 0) > 0 && (
        <section className="rounded-lg border border-border/70 border-l-2 border-l-amber-600/50 bg-background/60 p-4 text-body text-subtle">
          <span className="font-mono uppercase tracking-widest text-amber-300 mr-2">
            Cost Note
          </span>
          {fmtTok(data.unpriced_cache_write_tokens ?? 0)} cache-write tokens
          were tracked in the proof data but are not priced by the current model
          pricing table yet.
        </section>
      )}

      {verification && verification.tracked_row_count > 0 && (
        <ManualVerificationPanel verification={verification} />
      )}

      {!hasData ? (
        <EmptyState
          title="No savings telemetry yet"
          description={
            <>
              Run any task with{" "}
              <code className="bg-muted px-1">lc mcp</code> enabled to
              start collecting savings telemetry.
            </>
          }
        />
      ) : (
        <>
          <section className="rounded-lg border border-border/70 bg-background/60 p-4">
            <h2 className="text-body uppercase tracking-widest text-muted-foreground mb-4">
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

          <SavingsTimeChart data={data.by_day} windowDays={data.window_days} />

          {latestBenchmark && (
            <section className="rounded-lg border border-border/70 border-l-2 border-l-cyan-600/45 bg-background/60 p-4">
              <h2 className="text-body uppercase tracking-widest text-cyan-300 mb-4">
                Latest paired benchmark
              </h2>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-heading">
                <div>
                  <div className="text-caption uppercase tracking-widest text-muted-foreground">
                    Token reduction
                  </div>
                  <div className="text-h3 font-semibold text-cyan-200">
                    {fmtPct(latestBenchmark.reduction_pct)}
                  </div>
                </div>
                <div>
                  <div className="text-caption uppercase tracking-widest text-muted-foreground">
                    Cost saved
                  </div>
                  <div className="text-h3 font-semibold text-emerald-300">
                    {fmtUsd(latestBenchmark.cost_saved_usd)}
                  </div>
                </div>
                <div>
                  <div className="text-caption uppercase tracking-widest text-muted-foreground">
                    Tasks
                  </div>
                  <div className="text-h3 font-semibold text-foreground">
                    {fmtTok(latestBenchmark.n_prompts)}
                  </div>
                </div>
                <div>
                  <div className="text-caption uppercase tracking-widest text-muted-foreground">
                    Success
                  </div>
                  <div className="text-h3 font-semibold text-foreground">
                    {fmtPct(latestBenchmark.lemoncrow_success_rate * 100, 0)}
                  </div>
                </div>
              </div>
              <p className="mt-3 text-body text-muted-foreground">
                Real paired command run: baseline{" "}
                {fmtTok(latestBenchmark.total_tokens_baseline)} tokens vs
                LemonCrow-enabled {fmtTok(latestBenchmark.total_tokens_lemoncrow)}{" "}
                tokens.
              </p>
            </section>
          )}

          {topSources.length > 0 && (
            <section className="rounded-lg border border-border/70 bg-background/60 p-4">
              <h2 className="text-body uppercase tracking-widest text-muted-foreground mb-4">
                Top savings sources
              </h2>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-body">
                  <thead className="text-caption uppercase tracking-widest text-muted-foreground">
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
                        className="border-t border-border text-subtle"
                      >
                        <td className="py-2 pr-4 font-semibold text-cyan-200">
                          {toTitle(source.lever)}
                        </td>
                        <td className="py-2 pr-4 text-muted-foreground">
                          {source.tool_name}
                        </td>
                        <td className="py-2 pr-4 text-right">
                          {fmtTok(source.calls_saved)}
                        </td>
                        <td className="py-2 pr-4 text-right">
                          {fmtTok(source.tokens_saved)}
                        </td>
                        <td className="py-2 text-right text-emerald-300">
                          {fmtUsd(source.cost_saved_usd)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="mt-3 text-body text-muted-foreground">
                These rows use an equivalent-call estimator: search, edit, and
                SQL tools count the built-in calls they replace, then apply live
                token constants to estimate avoided cost.
              </p>
            </section>
          )}

          {toolAggregates.length > 0 && (
            <section className="rounded-lg border border-border/70 bg-background/60 p-4">
              <h2 className="text-body uppercase tracking-widest text-muted-foreground mb-4">
                Per-tool cost proof
              </h2>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-body">
                  <thead className="text-caption uppercase tracking-widest text-muted-foreground">
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
                        className="border-t border-border text-subtle"
                      >
                        <td className="py-2 pr-4 font-semibold text-cyan-200">
                          {displayToolName(tool.tool_name)}
                        </td>
                        <td className="py-2 pr-4 text-muted-foreground">
                          {toTitle(tool.lever)}
                        </td>
                        <td className="py-2 pr-4 text-right">
                          {fmtTok(tool.turns)}
                        </td>
                        <td className="py-2 pr-4 text-right">
                          {fmtTok(tool.session_count)}
                        </td>
                        <td className="py-2 pr-4 text-right text-foreground">
                          {fmtUsd(tool.actual_cost_usd)}
                        </td>
                        <td className="py-2 pr-4 text-right text-muted-foreground">
                          {fmtUsd(tool.baseline_cost_usd)}
                        </td>
                        <td className="py-2 pr-4 text-right text-emerald-300">
                          {fmtUsd(tool.saved_cost_usd)}
                        </td>
                        <td className="py-2 text-right">
                          {fmtTok(tool.saved_tokens)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="mt-3 text-body text-muted-foreground leading-relaxed">
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
                <h2 className="text-body uppercase tracking-widest text-muted-foreground mb-2">
                  Session proof
                </h2>
                <p className="text-body text-muted-foreground">
                  Each session below links saved tokens and cost back to
                  concrete tool turns. Expand a session to inspect the stored
                  ledger, imported trace conversation, and command/tool
                  evidence.
                </p>
              </div>
              {sessionProof.map((session) => (
                <SessionProofCard
                  key={session.session_id}
                  session={session}
                  expanded={Boolean(expandedRuns[session.session_id])}
                  loading={Boolean(ledgerLoading[session.session_id])}
                  ledger={ledgerByRun[session.session_id]}
                  onToggle={() => loadLedger(session.session_id)}
                />
              ))}
            </section>
          )}

          {coverageGaps.length > 0 && (
            <section className="rounded-lg border border-border/70 border-l-2 border-l-red-600/45 bg-background/60 p-4">
              <h2 className="text-body uppercase tracking-widest text-red-300 mb-4">
                Coverage gaps
              </h2>
              <div className="space-y-3">
                {coverageGaps.map((gap) => (
                  <div
                    key={gap.session_id}
                    className="rounded-md border border-border/70 border-l-2 border-l-red-600/45 bg-background/60 p-4"
                  >
                    <div className="flex flex-wrap items-center gap-2 mb-2 text-caption font-mono uppercase tracking-widest">
                      <span className="px-2 py-0.5 border border-red-500/40 text-red-300">
                        {gap.agent}
                      </span>
                      {gap.trace_confidence && (
                        <span className="px-2 py-0.5 border border-border text-muted-foreground">
                          {gap.trace_confidence}
                        </span>
                      )}
                      <span className="text-muted-foreground">{gap.session_id}</span>
                    </div>
                    <div className="text-heading text-foreground">{gap.task}</div>
                    <p className="mt-2 text-body text-muted-foreground leading-relaxed">
                      {gap.reason}
                    </p>
                  </div>
                ))}
              </div>
            </section>
          )}
        </>
      )}
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
    <section className="rounded-lg border border-border/70 border-l-2 border-l-amber-600/45 bg-background/60 p-4 space-y-4">
      <div className="flex flex-col gap-4">
        <div>
          <h2 className="text-body uppercase tracking-widest text-muted-foreground mb-2">
            How this figure is worked out
          </h2>
          <p className="text-heading text-subtle leading-relaxed max-w-3xl">
            {fmtTok(verification.tracked_row_count)} recorded tool outputs across{" "}
            {fmtTok(verification.tracked_run_count)} sessions. Open the details
            to check whether a few of them account for most of the savings.
          </p>
        </div>
        {verification.warning && (
          <div className="max-w-3xl rounded-lg border border-border/70 border-l-2 border-l-amber-600/50 bg-background/60 p-3 text-body text-subtle leading-relaxed">
            {verification.warning}
          </div>
        )}
      </div>

      <details className="group">
        <summary className="cursor-pointer text-body font-medium text-foreground">
          Details for auditing the total
        </summary>
        <div className="mt-4 space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        <Metric
          label="Recorded tool outputs"
          value={fmtTok(verification.tracked_row_count)}
        />
        <Metric
          label="Sessions counted"
          value={fmtTok(verification.tracked_run_count)}
        />
        <Metric
          label="Sessions with a trace"
          value={fmtTok(verification.trace_linked_run_count)}
        />
        <Metric
          label="Sessions with saved totals"
          value={fmtTok(verification.ledger_backed_run_count)}
        />
        <Metric
          label="Live events"
          value={fmtTok(verification.live_event_count)}
        />
        <Metric
          label="Sessions missing data"
          value={fmtTok(verification.coverage_gap_count)}
        />
      </div>

      {compactOutputRows > 0 && (
        <div className="rounded-lg border border-border/70 bg-background/60 p-4 space-y-2">
          <div className="text-caption font-mono uppercase tracking-widest text-muted-foreground">
            Left out of the headline
          </div>
          <div className="text-lead font-semibold text-cyan-200">
            {fmtTok(compactOutputRows)} row
            {compactOutputRows === 1 ? "" : "s"}
          </div>
          <div className="text-body text-muted-foreground leading-relaxed">
            {fmtTok(compactOutputSavedTokens)} tokens saved by shortening tool
            output still appear in each session's details, but the headline
            leaves them out: they come from shortening tool output, not from
            the audited session totals.
          </div>
        </div>
      )}

      <div className="grid xl:grid-cols-2 gap-3">
        <div className="rounded-lg border border-border/70 bg-background/60 p-4 space-y-2">
          <div className="text-caption font-mono uppercase tracking-widest text-muted-foreground">
            Dominant Session
          </div>
          {dominantRun ? (
            <>
              <div className="text-lead font-semibold text-cyan-200">
                {fmtPct(verification.dominant_run_share_pct)} of saved tokens
              </div>
              <div className="text-body text-muted-foreground font-mono break-all">
                {dominantRun.session_id}
              </div>

              <div className="text-heading text-subtle">
                {dominantRun.agent ?? "unknown"}
              </div>
              <p className="text-body text-muted-foreground leading-relaxed">
                {truncate(dominantRun.task ?? "No trace task attached.", 180)}
              </p>
              <div className="text-body text-muted-foreground">
                {fmtTok(dominantRun.saved_tokens)} saved tokens
              </div>
            </>
          ) : (
            <div className="text-heading text-muted-foreground">
              No tracked session data yet.
            </div>
          )}
        </div>

        <div className="rounded-lg border border-border/70 bg-background/60 p-4 space-y-2">
          <div className="text-caption font-mono uppercase tracking-widest text-muted-foreground">
            Largest Proof Row
          </div>
          {dominantItem ? (
            <>
              <div className="text-lead font-semibold text-cyan-200">
                {fmtPct(verification.dominant_item_share_pct)} of saved tokens
              </div>
              <div className="text-heading text-subtle">
                {dominantItem.tool_name} · {toTitle(dominantItem.lever)} · turn{" "}
                {dominantItem.turn_index}
              </div>
              <div className="text-body text-muted-foreground font-mono break-all">
                {dominantItem.session_id}
              </div>

              <div className="text-body text-muted-foreground leading-relaxed">
                {fmtTok(dominantItem.saved_tokens)} saved tokens from{" "}
                {fmtTok(dominantItem.naive_tokens)} naive vs{" "}
                {fmtTok(dominantItem.actual_tokens)} compacted tokens.
              </div>
            </>
          ) : (
            <div className="text-heading text-muted-foreground">
              No tracked proof rows yet.
            </div>
          )}
        </div>
      </div>

      <p className="text-body text-muted-foreground font-mono break-all">
        Data root: {verification.data_root}
      </p>
        </div>
      </details>
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
      ? "This session has saved token rows, but the stored ledger/trace did not preserve tool-call, command, or conversation detail for this slice."
      : "This session only has persisted context-budget rows. No live ledger or imported trace survived, so detailed proof beyond the token rows cannot be reconstructed.";

  return (
    <article className="rounded-lg border border-border/70 bg-background/60 p-4 space-y-4">
      <div className="flex flex-col xl:flex-row xl:items-start xl:justify-between gap-4">
        <div className="space-y-2 min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-caption font-mono uppercase tracking-widest">
            {session.status && (
              <span
                className={`px-2 py-0.5 border ${statusTone(session.status)}`}
              >
                {session.status}
              </span>
            )}
            <span className="px-2 py-0.5 border border-border text-muted-foreground">
              {session.agent}
            </span>
            {session.trace_confidence && (
              <span className="px-2 py-0.5 border border-border text-muted-foreground">
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
          <div className="text-heading text-foreground break-words">
            {session.task}
          </div>
          <div className="text-caption font-mono text-muted-foreground break-all">
            Session: {session.session_id}
          </div>
          {session.note && (
            <p className="text-body text-amber-300 leading-relaxed">
              {session.note}
            </p>
          )}
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-body min-w-[280px]">
          <Metric label="Saved Cost" value={fmtUsd(session.saved_cost_usd)} />
          <Metric label="Saved Tokens" value={fmtTok(session.saved_tokens)} />
          <Metric
            label="Tool Turns"
            value={fmtTok(session.tracked_tool_calls ?? 0)}
          />
          <Metric
            label="Live Calls Saved"
            value={fmtTok(session.live_calls_saved ?? 0)}
          />
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-left text-body">
          <thead className="text-caption uppercase tracking-widest text-muted-foreground">
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
                key={`${item.session_id}:${item.turn_index}:${item.tool_name}`}
                className="border-t border-border text-subtle"
              >
                <td className="py-2 pr-4 font-mono text-muted-foreground">
                  {item.turn_index}
                </td>
                <td className="py-2 pr-4 text-cyan-200 font-semibold">
                  {displayToolName(item.tool_name)}
                </td>
                <td className="py-2 pr-4 text-muted-foreground">
                  {toTitle(item.lever)}
                </td>
                <td className="py-2 pr-4 text-right">
                  {fmtTok(item.actual_tokens)}
                </td>
                <td className="py-2 pr-4 text-right text-muted-foreground">
                  {fmtTok(item.naive_tokens)}
                </td>
                <td className="py-2 pr-4 text-right">
                  {fmtTok(item.saved_tokens)}
                </td>
                <td className="py-2 text-right text-emerald-300">
                  {fmtUsd(item.saved_cost_usd)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <UiButton
          type="button"
          onClick={onToggle}
          size="xs"
          variant="outline"
        >
          {expanded ? "Hide evidence details" : "Inspect evidence details"}
        </UiButton>
        {session.has_ledger && (
          <span className="text-caption font-mono uppercase tracking-widest text-muted-foreground">
            stored ledger available
          </span>
        )}
      </div>

      {expanded && (
        <div className="rounded-lg border border-border/70 bg-background/60 p-4 space-y-4">
          {loading && (
            <div className="text-body text-muted-foreground">
              Loading ledger proof…
            </div>
          )}
          {!loading && ledger?.error && (
            <div className="text-body text-red-300">{ledger.error}</div>
          )}
          {!loading && !ledger?.error && (
            <>
              {!hasEvidence ? (
                <div className="rounded-lg border border-border/70 border-l-2 border-l-amber-600/50 bg-background/60 p-4 space-y-2 text-body">
                  <div className="text-caption font-mono uppercase tracking-widest text-amber-300">
                    Capture gap
                  </div>
                  <div className="text-foreground">
                    No detailed tool-call, command, or conversation proof was
                    stored for this session.
                  </div>
                  <p className="text-muted-foreground leading-relaxed">
                    {missingEvidenceMessage}
                  </p>
                  {session.note && (
                    <p className="text-amber-200 leading-relaxed">
                      {session.note}
                    </p>
                  )}
                </div>
              ) : (
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 text-body">
                  <div>
                    <div className="text-caption font-mono uppercase tracking-widest text-muted-foreground mb-2">
                      Tool calls
                    </div>
                    <div className="space-y-2">
                      {toolsCalled.length === 0 ? (
                        <div className="text-muted-foreground">
                          No tool-call detail captured for this session.
                        </div>
                      ) : (
                        toolsCalled
                          .slice(0, 8)
                          .map((tool: any, index: number) => (
                            <div
                              key={`${tool.name}:${index}`}
                              className="rounded-md border border-border/70 p-2"
                            >
                              <div className="text-cyan-200 font-semibold">
                                {tool.name}
                              </div>
                              <div className="text-muted-foreground font-mono">
                                x{tool.count ?? 1}
                              </div>
                              {tool.result_summary && (
                                <div className="mt-1 text-muted-foreground leading-relaxed">
                                  {truncate(String(tool.result_summary), 120)}
                                </div>
                              )}
                            </div>
                          ))
                      )}
                    </div>
                  </div>

                  <div>
                    <div className="text-caption font-mono uppercase tracking-widest text-muted-foreground mb-2">
                      Commands
                    </div>
                    <div className="space-y-2">
                      {commandsRun.length === 0 ? (
                        <div className="text-muted-foreground">
                          No command detail captured for this session.
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
                                className="rounded-md border border-border/70 p-2"
                              >
                                <div className="font-mono text-subtle break-all">
                                  {truncate(commandText, 140)}
                                </div>
                              </div>
                            );
                          })
                      )}
                    </div>
                  </div>

                  <div>
                    <div className="text-caption font-mono uppercase tracking-widest text-muted-foreground mb-2">
                      Conversation / LLM response
                    </div>
                    <div className="space-y-2">
                      {conversations.length === 0 ? (
                        <div className="text-muted-foreground">
                          No conversation transcript was attached to this
                          session.
                        </div>
                      ) : (
                        conversations
                          .slice(0, 6)
                          .map((entry: any, index: number) => (
                            <div
                              key={`${entry.kind}:${index}`}
                              className="rounded-md border border-border/70 p-2"
                            >
                              <div className="text-caption font-mono uppercase tracking-widest text-muted-foreground">
                                {entry.kind}
                              </div>
                              <div className="mt-1 text-foreground leading-relaxed">
                                {truncate(String(entry.summary ?? ""), 120)}
                              </div>
                              {entry.content && (
                                <div className="mt-1 text-muted-foreground leading-relaxed whitespace-pre-wrap break-words">
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
    <div className="rounded-md border border-border/70 bg-background/60 p-3">
      <div className="text-caption font-mono uppercase tracking-widest text-muted-foreground mb-1">
        {label}
      </div>
      <div className="text-heading font-semibold text-foreground">{value}</div>
    </div>
  );
}
