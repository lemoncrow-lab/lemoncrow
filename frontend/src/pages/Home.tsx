import { useEffect, useMemo, useState } from "react";
import { Activity, AlertTriangle, ArrowRight, ClipboardCheck, Play } from "lucide-react";
import { Link } from "react-router-dom";

import {
  api,
  type InsightsWindow,
  type SavingsSummaryV2,
  type Trace,
} from "../api";
import { Card, Chip, PageFrame } from "../components/WorkbenchUI";
import {
  fetchLocalIntegrations,
  type LocalIntegrations,
} from "../control/controlApi";
import { fmtPct, fmtRelativeTime, fmtTok, fmtUsd } from "../lib/format";
import {
  fetchReviewDirectory,
  reviewHref,
} from "../review/reviewApi";
import type { ReviewDirectoryRow } from "../review/types";

function isActiveRun(trace: Trace): boolean {
  const status = trace.status.toLowerCase();
  return Boolean(trace._live) || status === "running" || status === "partial";
}

function isFailedRun(trace: Trace): boolean {
  const status = trace.status.toLowerCase();
  return status === "failed" || status === "error";
}

function runLabel(trace: Trace): string {
  return trace.task?.trim() || trace.output_summary?.trim() || trace.session_id || trace.id;
}

function runHref(trace: Trace): string {
  return `/runs/${encodeURIComponent(trace.session_id || trace.id)}`;
}

export default function Home() {
  const [insights, setInsights] = useState<InsightsWindow | null>(null);
  const [reviews, setReviews] = useState<ReviewDirectoryRow[]>([]);
  const [runs, setRuns] = useState<Trace[]>([]);
  const [integrations, setIntegrations] = useState<LocalIntegrations | null>(null);
  const [savings, setSavings] = useState<SavingsSummaryV2 | null>(null);
  const [savingsUnavailable, setSavingsUnavailable] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([
      api.insightsWindow("7d"),
      fetchReviewDirectory({ status: "all", query: "", cursor: "", limit: 12 }),
      api.traces(20, 0),
      fetchLocalIntegrations(),
      api.savingsSummary(7),
    ]).then(([insightsResult, reviewResult, runResult, integrationResult, savingsResult]) => {
      if (cancelled) return;
      if (insightsResult.status === "fulfilled") setInsights(insightsResult.value);
      if (reviewResult.status === "fulfilled") setReviews(reviewResult.value.reviews);
      if (runResult.status === "fulfilled") setRuns(runResult.value.items);
      if (integrationResult.status === "fulfilled") setIntegrations(integrationResult.value);
      if (savingsResult.status === "fulfilled") {
        setSavings(savingsResult.value);
        setSavingsUnavailable(false);
      } else {
        setSavingsUnavailable(true);
      }
      const failures = [insightsResult, reviewResult, runResult, integrationResult, savingsResult].filter(
        (result) => result.status === "rejected",
      );
      setError(failures.length === 5 ? "Home data is unavailable." : "");
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const activeRuns = useMemo(() => runs.filter(isActiveRun).slice(0, 5), [runs]);
  const failedRuns = useMemo(() => runs.filter(isFailedRun).slice(0, 4), [runs]);
  const recentRuns = useMemo(
    () => runs.filter((run) => !isActiveRun(run)).slice(0, 6),
    [runs],
  );
  const openReviews = useMemo(
    () => reviews.filter((review) => review.status === "open"),
    [reviews],
  );
  const recentReviews = reviews.slice(0, 6);
  const integrationIssue =
    integrations !== null &&
    (!integrations.dispatcher_available ||
      !integrations.hosts.some((host) => host.configured));
  const attentionCount = openReviews.length + failedRuns.length + (integrationIssue ? 1 : 0);
  const totalSavedUsd = savings
    ? savings.total_saved_usd ?? (savings.saved_usd ?? 0) + (savings.carry_usd ?? 0)
    : 0;
  const savedPct = savings
    ? savings.cost_basis === "session_ledger"
      ? (savings.saved_pct ?? 0)
      : savings.reduction_pct
    : 0;
  const actualCostUsd = savings
    ? savings.actually_cost_usd ?? savings.tracked_actual_cost_usd ?? 0
    : 0;
  const baselineCostUsd = savings
    ? savings.would_have_cost_usd ?? savings.tracked_baseline_cost_usd ?? 0
    : 0;
  const savedTokens = savings
    ? savings.cost_basis === "session_ledger" && savings.ledger_tokens_saved !== undefined
      ? savings.ledger_tokens_saved
      : Math.max(0, savings.total_naive_tokens - savings.total_actual_tokens)
    : 0;

  return (
    <PageFrame className="space-y-5 text-neutral-200">
      {error && (
        <div className="border border-rose-900/50 bg-rose-950/15 px-3 py-2 text-xs text-rose-300">
          {error}
        </div>
      )}

      <section
        aria-label="Savings summary"
        className="flex flex-wrap items-stretch overflow-hidden rounded-sm border border-neutral-800 bg-neutral-900/20"
      >
        <div className="flex min-w-[150px] flex-1 items-center gap-3 border-b border-neutral-800 px-3 py-2.5 sm:border-b-0 sm:border-r">
          <div className="min-w-0">
            <div className="text-[9px] font-semibold uppercase tracking-widest text-neutral-600">
              Last 7 days
            </div>
            <div className="mt-0.5 text-[10px] text-neutral-500">
              {insights ? `${insights.session_count} sessions` : "Current usage"}
            </div>
          </div>
        </div>
        {savingsUnavailable ? (
          <div className="flex min-h-12 flex-[4] items-center px-3 text-xs text-amber-300">
            Savings data is unavailable. Usage details may still be available.
          </div>
        ) : savings ? (
          <>
            <div className="min-w-[130px] flex-1 border-r border-neutral-800 px-3 py-2.5">
              <div className="text-[9px] font-semibold uppercase tracking-widest text-neutral-600">Saved</div>
              <div className="mt-0.5 text-sm font-semibold text-emerald-400">{fmtUsd(totalSavedUsd)}</div>
            </div>
            <div className="min-w-[130px] flex-1 border-r border-neutral-800 px-3 py-2.5">
              <div className="text-[9px] font-semibold uppercase tracking-widest text-neutral-600">Reduction</div>
              <div className="mt-0.5 text-sm font-semibold text-neutral-200">{fmtPct(savedPct)}</div>
            </div>
            <div className="min-w-[170px] flex-1 border-r border-neutral-800 px-3 py-2.5">
              <div className="text-[9px] font-semibold uppercase tracking-widest text-neutral-600">Cost</div>
              <div className="mt-0.5 text-sm font-semibold text-neutral-200">
                {fmtUsd(actualCostUsd)} <span className="font-normal text-neutral-600">/ {fmtUsd(baselineCostUsd)} baseline</span>
              </div>
            </div>
            <div className="min-w-[140px] flex-1 px-3 py-2.5">
              <div className="text-[9px] font-semibold uppercase tracking-widest text-neutral-600">Tokens saved</div>
              <div className="mt-0.5 text-sm font-semibold text-neutral-200">{fmtTok(savedTokens)}</div>
            </div>
          </>
        ) : (
          <div className="flex min-h-12 flex-[4] items-center px-3 text-xs text-neutral-600">
            Loading savings…
          </div>
        )}
        <Link
          to="/usage?inspect=savings"
          className="flex min-h-12 items-center gap-1 border-l border-neutral-800 px-3 text-[10px] font-semibold uppercase tracking-wider text-neutral-500 hover:bg-neutral-900/60 hover:text-neutral-200"
        >
          Details <ArrowRight size={12} />
        </Link>
      </section>

      <section className="space-y-2">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h1 className="text-sm font-semibold text-neutral-100">Needs attention</h1>
            <p className="mt-0.5 text-[11px] text-neutral-500">
              Reviews and runs that need a human decision.
            </p>
          </div>
          <Chip tone={attentionCount > 0 ? "amber" : "neutral"}>
            {attentionCount} items
          </Chip>
        </div>

        {attentionCount === 0 ? (
          <div className="border border-neutral-800 bg-neutral-900/20 px-4 py-4 text-xs text-neutral-500">
            Nothing needs attention right now.
          </div>
        ) : (
          <div className="grid gap-3 xl:grid-cols-2">
            <Card className="overflow-hidden p-0">
              <div className="flex items-center gap-2 border-b border-neutral-800 px-3 py-2 text-[10px] font-semibold uppercase tracking-widest text-neutral-500">
                <ClipboardCheck size={12} /> Reviews
              </div>
              {openReviews.length === 0 ? (
                <div className="px-3 py-4 text-xs text-neutral-600">No open reviews.</div>
              ) : (
                openReviews.slice(0, 5).map((review) => (
                  <a
                    key={`${review.repo_id}:${review.id}`}
                    href={reviewHref(review.ref || review.id)}
                    className="flex min-h-11 items-center gap-3 border-b border-neutral-900 px-3 py-2 last:border-0 hover:bg-neutral-900/55"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-xs font-medium text-neutral-200">
                        {review.title || "Untitled review"}
                      </div>
                      <div className="mt-0.5 truncate text-[10px] text-neutral-600">
                        {review.repo_id} · revision {review.revision_number}
                      </div>
                    </div>
                    <ArrowRight size={13} className="shrink-0 text-neutral-600" />
                  </a>
                ))
              )}
            </Card>

            <Card className="overflow-hidden p-0">
              <div className="flex items-center gap-2 border-b border-neutral-800 px-3 py-2 text-[10px] font-semibold uppercase tracking-widest text-neutral-500">
                <AlertTriangle size={12} /> Failed runs
              </div>
              {failedRuns.length === 0 ? (
                <div className="px-3 py-4 text-xs text-neutral-600">No failed runs.</div>
              ) : (
                failedRuns.map((run) => (
                  <Link
                    key={run.id}
                    to={runHref(run)}
                    className="flex min-h-11 items-center gap-3 border-b border-neutral-900 px-3 py-2 last:border-0 hover:bg-neutral-900/55"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-xs font-medium text-neutral-200">
                        {runLabel(run)}
                      </div>
                      <div className="mt-0.5 truncate text-[10px] text-neutral-600">
                        {run.host || run.agent || "agent"} · {fmtRelativeTime(run.created_at)}
                      </div>
                    </div>
                    <span className="text-[10px] uppercase text-rose-300">{run.status}</span>
                  </Link>
                ))
              )}
            </Card>

            {integrationIssue && (
              <Card className="overflow-hidden p-0">
                <div className="flex items-center gap-2 border-b border-neutral-800 px-3 py-2 text-[10px] font-semibold uppercase tracking-widest text-neutral-500">
                  <AlertTriangle size={12} /> Setup
                </div>
                <Link
                  to="/settings/integrations"
                  className="flex min-h-16 items-center gap-3 px-3 py-3 hover:bg-neutral-900/55"
                >
                  <div className="min-w-0 flex-1">
                    <div className="text-xs font-medium text-neutral-200">
                      {!integrations?.dispatcher_available
                        ? "Agent connection is unavailable"
                        : "Connect a coding agent"}
                    </div>
                    <div className="mt-1 text-[10px] leading-4 text-neutral-600">
                      Open Integrations to repair or finish the coding-host setup.
                    </div>
                  </div>
                  <ArrowRight size={13} className="shrink-0 text-neutral-600" />
                </Link>
              </Card>
            )}
          </div>
        )}
      </section>

      <section className="space-y-2">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold text-neutral-100">Active now</h2>
            <p className="mt-0.5 text-[11px] text-neutral-500">
              Coding work LemonCrow can currently see.
            </p>
          </div>
          <Link to="/runs" className="text-[10px] font-semibold uppercase tracking-wider text-neutral-500 hover:text-neutral-200">
            All runs →
          </Link>
        </div>
        {activeRuns.length === 0 ? (
          <div className="border border-neutral-800 bg-neutral-900/20 px-4 py-4 text-xs text-neutral-500">
            No active runs.
          </div>
        ) : (
          <div className="grid gap-2 lg:grid-cols-2 xl:grid-cols-3">
            {activeRuns.map((run) => (
              <Link key={run.id} to={runHref(run)} className="block">
                <Card className="h-full p-3 transition hover:border-neutral-700 hover:bg-neutral-900/50">
                  <div className="flex items-start gap-2">
                    <Play size={12} className="mt-0.5 shrink-0 text-emerald-400" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-xs font-medium text-neutral-200">
                        {runLabel(run)}
                      </div>
                      <div className="mt-1 text-[10px] text-neutral-600">
                        {run.host || run.agent || "agent"}
                        {run.model ? ` · ${run.model}` : ""}
                      </div>
                    </div>
                    <span className="h-2 w-2 shrink-0 rounded-full bg-emerald-400" />
                  </div>
                </Card>
              </Link>
            ))}
          </div>
        )}
      </section>

      <section className="grid gap-4 xl:grid-cols-2">
        <Card className="overflow-hidden p-0">
          <div className="flex items-center justify-between border-b border-neutral-800 px-3 py-2">
            <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-widest text-neutral-500">
              <ClipboardCheck size={12} /> Recent reviews
            </div>
            <Link to="/reviews" className="text-[10px] text-neutral-600 hover:text-neutral-300">View all</Link>
          </div>
          {recentReviews.length === 0 ? (
            <div className="px-3 py-4 text-xs text-neutral-600">No recent reviews.</div>
          ) : (
            recentReviews.map((review) => (
              <a
                key={`${review.repo_id}:${review.id}`}
                href={reviewHref(review.ref || review.id)}
                className="flex items-center gap-3 border-b border-neutral-900 px-3 py-2 last:border-0 hover:bg-neutral-900/55"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs text-neutral-300">{review.title || review.source_ref}</div>
                  <div className="mt-0.5 text-[10px] text-neutral-600">{review.repo_id}</div>
                </div>
                <span className="text-[10px] capitalize text-neutral-500">{review.status === "open" ? "needs review" : review.status}</span>
              </a>
            ))
          )}
        </Card>

        <Card className="overflow-hidden p-0">
          <div className="flex items-center justify-between border-b border-neutral-800 px-3 py-2">
            <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-widest text-neutral-500">
              <Activity size={12} /> Recent runs
            </div>
            <Link to="/runs" className="text-[10px] text-neutral-600 hover:text-neutral-300">View all</Link>
          </div>
          {recentRuns.length === 0 ? (
            <div className="px-3 py-4 text-xs text-neutral-600">No recent runs.</div>
          ) : (
            recentRuns.map((run) => (
              <Link
                key={run.id}
                to={runHref(run)}
                className="flex items-center gap-3 border-b border-neutral-900 px-3 py-2 last:border-0 hover:bg-neutral-900/55"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs text-neutral-300">{runLabel(run)}</div>
                  <div className="mt-0.5 text-[10px] text-neutral-600">
                    {run.host || run.agent || "agent"} · {fmtRelativeTime(run.created_at)}
                  </div>
                </div>
                <span className="text-[10px] capitalize text-neutral-500">{run.status}</span>
              </Link>
            ))
          )}
        </Card>
      </section>

      <section className="flex flex-wrap items-center gap-x-5 gap-y-2 border-t border-neutral-900 pt-3 text-[10px] text-neutral-500">
        <span className="font-semibold uppercase tracking-wider text-neutral-600">Last 7 days</span>
        <span>{insights ? `${insights.session_count} sessions` : "… sessions"}</span>
        <span>{insights ? `${fmtUsd(insights.total_cost_usd)} spent` : "… spent"}</span>
        <Link to="/usage" className="ml-auto font-semibold text-neutral-500 hover:text-neutral-200">
          Usage →
        </Link>
      </section>
    </PageFrame>
  );
}
