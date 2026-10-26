import { useEffect, useMemo, useState } from "react";
import { Activity, AlertTriangle, ArrowRight, ClipboardCheck, PiggyBank, Play } from "lucide-react";
import { Link } from "react-router-dom";

import {
  api,
  type InsightsWindow,
  type SavingsSummaryV2,
  type TraceSummary,
} from "../api";
import { Badge, Chip, PageFrame } from "../components/WorkbenchUI";
import { Button } from "../components/ui/button";
import { EmptyState } from "../components/ui/empty-state";
import { Hunk } from "../components/ui/hunk";
import {
  fetchLocalIntegrations,
  type LocalIntegrations,
} from "../control/controlApi";
import { fmtPct, fmtRelativeTime, fmtTok, fmtUsd } from "../lib/format";
import { useProductCapabilities, useRuntimeCapabilities } from "../lib/RuntimeCapabilities";
import {
  fetchReviewDirectory,
  reviewHref,
} from "../review/reviewApi";
import type { ReviewDirectoryRow } from "../review/types";

function sessionCount(count: number): string {
  return `${count} ${count === 1 ? "session" : "sessions"}`;
}

function isActiveRun(trace: TraceSummary): boolean {
  const status = trace.status.toLowerCase();
  return Boolean(trace._live) || status === "running" || status === "partial";
}

function isFailedRun(trace: TraceSummary): boolean {
  const status = trace.status.toLowerCase();
  return status === "failed" || status === "error";
}

function runLabel(trace: TraceSummary): string {
  return trace.task?.trim() || trace.output_summary?.trim() || trace.session_id || trace.id;
}

function runHref(trace: TraceSummary): string {
  return `/activity/${encodeURIComponent(trace.session_id || trace.id)}`;
}

/** Where a Review lives in human terms; the opaque repository id only as a last resort. */
export function reviewPlace(review: ReviewDirectoryRow): string {
  const project = review.project || review.directory || "";
  const worktree = review.worktree && review.worktree !== project ? review.worktree : "";
  return [project, worktree, review.branch].filter(Boolean).join(" · ") || review.repo_id;
}

export default function Home() {
  const productCapabilities = useProductCapabilities();
  const runtimeCapabilities = useRuntimeCapabilities();
  const reviewAvailable = productCapabilities.products.review.available;
  const runtimeAvailable = productCapabilities.products.runtime.available;
  const [insights, setInsights] = useState<InsightsWindow | null>(null);
  const [reviews, setReviews] = useState<ReviewDirectoryRow[]>([]);
  const [runs, setRuns] = useState<TraceSummary[]>([]);
  const [integrations, setIntegrations] = useState<LocalIntegrations | null>(null);
  const [savings, setSavings] = useState<SavingsSummaryV2 | null>(null);
  const [savingsUnavailable, setSavingsUnavailable] = useState(false);
  const [runsUnavailable, setRunsUnavailable] = useState(false);
  const [reviewsUnavailable, setReviewsUnavailable] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    const tasks: Array<Promise<void>> = [];

    setError("");
    if (reviewAvailable) {
      tasks.push(
        // View mode pages lightweight rows; the legacy list opens every Review it returns.
        fetchReviewDirectory({ view: "all", status: "all", query: "", cursor: "", limit: 12 })
          .then((result) => {
            if (cancelled) return;
            setReviews(result.reviews);
            setReviewsUnavailable(false);
          })
          .catch((error) => {
            if (!cancelled) setReviewsUnavailable(true);
            throw error;
          }),
      );
    } else {
      setReviews([]);
      setReviewsUnavailable(false);
    }

    if (runtimeAvailable) {
      tasks.push(
        api.insightsWindow("7d").then((result) => {
          if (!cancelled) setInsights(result);
        }),
        api.traces(20, 0)
          .then((result) => {
            if (cancelled) return;
            setRuns(result.items);
            setRunsUnavailable(false);
          })
          .catch((error) => {
            if (!cancelled) setRunsUnavailable(true);
            throw error;
          }),
        api.savingsSummary(7)
          .then((result) => {
            if (cancelled) return;
            setSavings(result);
            setSavingsUnavailable(false);
          })
          .catch((error) => {
            if (!cancelled) setSavingsUnavailable(true);
            throw error;
          }),
      );
      if (runtimeCapabilities.localIntegrations) {
        tasks.push(
          fetchLocalIntegrations().then((result) => {
            if (!cancelled) setIntegrations(result);
          }),
        );
      } else {
        setIntegrations(null);
      }
    } else {
      setInsights(null);
      setRuns([]);
      setIntegrations(null);
      setSavings(null);
      setRunsUnavailable(false);
      setSavingsUnavailable(false);
    }

    Promise.allSettled(tasks).then((results) => {
      if (cancelled || results.length === 0) return;
      if (results.every((result) => result.status === "rejected")) setError("Home data is unavailable.");
    });

    return () => {
      cancelled = true;
    };
  }, [reviewAvailable, runtimeAvailable, runtimeCapabilities.localIntegrations]);

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
    runtimeAvailable &&
    integrations !== null &&
    (!integrations.dispatcher_available ||
      !integrations.hosts.some(
        (host) => host.support !== "coming_soon" && host.configured,
      ));
  const attentionCount =
    (reviewAvailable ? openReviews.length : 0) +
    (runtimeAvailable ? failedRuns.length + (integrationIssue ? 1 : 0) : 0);
  const attentionIncomplete =
    (reviewAvailable && reviewsUnavailable) || (runtimeAvailable && runsUnavailable);
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

  // A measure with no evidence is an empty state that says what will appear and where to go, never a "No data" tile.
  const savingsMissing = savings?.savings_available === false;
  const spendMissing = savings?.spend_available === false;
  const savingsEmptyEverywhere = runtimeAvailable && !savingsUnavailable && savings !== null && savingsMissing && spendMissing;
  const tileClass = (minWidth: string, divider: boolean) =>
    `${minWidth} flex flex-1 flex-col justify-center ${divider ? "border-r border-border " : ""}px-3 py-2.5`;
  const savedTile = (
    <div className={tileClass("min-w-[130px]", true)}>
      <div className="font-mono text-tag uppercase text-muted-foreground">Saved</div>
      <div className="mt-0.5 text-heading font-semibold"><span className="text-ok">{fmtUsd(totalSavedUsd)}</span></div>
    </div>
  );
  const reductionTile = (
    <div className={tileClass("min-w-[130px]", true)}>
      <div className="font-mono text-tag uppercase text-muted-foreground">Reduction</div>
      <div className="mt-0.5 text-heading font-semibold text-foreground">{fmtPct(savedPct)}</div>
    </div>
  );
  const costTile = (
    <div className={tileClass("min-w-[170px]", true)}>
      <div className="font-mono text-tag uppercase text-muted-foreground">Cost</div>
      <div className="mt-0.5 text-heading font-semibold text-foreground">
        {(savings?.spend?.unpriced_rows ?? 0) > 0 ? "≥ " : ""}{fmtUsd(actualCostUsd)}
        {!savingsMissing && <span className="font-normal text-muted-foreground"> / {fmtUsd(baselineCostUsd)} baseline</span>}
      </div>
    </div>
  );
  const tokensTile = (divider: boolean) => (
    <div className={tileClass("min-w-[140px]", divider)}>
      <div className="font-mono text-tag uppercase text-muted-foreground">Tokens saved</div>
      <div className="mt-0.5 text-heading font-semibold text-foreground">{fmtTok(savedTokens)}</div>
    </div>
  );

  return (
    <PageFrame className="space-y-5 text-foreground">
      {error && (
        <div className="rounded-lg border border-p0-line bg-p0-bg px-3 py-2 text-body text-p0">
          {error}
        </div>
      )}

      {runtimeAvailable && (
      <section
        aria-label="Savings summary"
        className={savingsEmptyEverywhere ? undefined : "hunk flex flex-wrap items-stretch"}
      >
        {savingsEmptyEverywhere ? (
          <EmptyState
            label={insights ? `Last 7 days · ${sessionCount(insights.session_count)}` : "Last 7 days"}
            icon={<PiggyBank aria-hidden="true" />}
            title="No usage or savings recorded yet"
            description="Saved, reduction, cost and tokens saved appear here once a connected coding agent records them."
            action={
              <Button asChild variant="outline">
                <Link to="/settings/integrations">{integrationIssue ? "Connect a coding agent" : "Open Integrations"}</Link>
              </Button>
            }
          />
        ) : (
          <>
        <div className="flex min-w-[150px] flex-1 items-center gap-3 border-b border-border px-3 py-2.5 sm:border-b-0 sm:border-r">
          <div className="min-w-0">
            <div className="font-mono text-tag uppercase text-muted-foreground">
              Last 7 days
            </div>
            <div className="mt-0.5 text-caption text-muted-foreground">
              {insights ? sessionCount(insights.session_count) : "Current usage"}
            </div>
          </div>
        </div>
        {savingsUnavailable ? (
          <div className="flex min-h-12 flex-[4] items-center px-3 text-body text-p1">
            Savings data is unavailable. Usage details may still be available.
          </div>
        ) : savings ? (
          <>
            {!savingsMissing && !spendMissing && (
              <>
                {savedTile}
                {reductionTile}
                {costTile}
                {tokensTile(false)}
              </>
            )}
            {savingsMissing && (
              <>
                {costTile}
                <EmptyState
                  frame={false}
                  density="compact"
                  className="min-w-[260px] flex-[3]"
                  title="No savings measured yet"
                  description={
                    <>
                      Saved, reduction and tokens saved appear once a task runs with{" "}
                      <code className="rounded-sm bg-muted px-1 font-mono text-code">lc mcp</code> enabled.
                    </>
                  }
                  action={
                    <Button asChild variant="outline" size="xs">
                      <Link to="/usage?inspect=savings">How savings are measured</Link>
                    </Button>
                  }
                />
              </>
            )}
            {spendMissing && (
              <>
                {savedTile}
                {tokensTile(true)}
                <EmptyState
                  frame={false}
                  density="compact"
                  className="min-w-[260px] flex-[3]"
                  title="No priced usage yet"
                  description="Cost and reduction appear once a session records usage for a model with a known price."
                  action={
                    <Button asChild variant="outline" size="xs">
                      <Link to="/usage">Open Usage</Link>
                    </Button>
                  }
                />
              </>
            )}
          </>
        ) : (
          <div className="flex min-h-12 flex-[4] items-center px-3 text-body text-muted-foreground">
            Loading savings…
          </div>
        )}
        <Link
          to="/usage?inspect=savings"
          className="flex min-h-12 items-center gap-1 border-l border-border px-3 font-mono text-label uppercase tracking-tag text-muted-foreground transition-colors duration-fast hover:bg-accent hover:text-foreground"
        >
          Details <ArrowRight size={14} />
        </Link>
          </>
        )}
      </section>
      )}

      {(reviewAvailable || runtimeAvailable) && (
      <section className="space-y-2">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h1 className="text-heading font-semibold text-foreground">Needs attention</h1>
            <p className="mt-0.5 text-label text-muted-foreground">
              {reviewAvailable && runtimeAvailable
                ? "Reviews and agent sessions that need a human decision."
                : reviewAvailable
                  ? "Reviews that need a human decision."
                  : "Agent sessions that need attention."}
            </p>
          </div>
          <Chip tone={attentionCount > 0 ? "warning" : "neutral"}>
            {attentionCount} {attentionIncomplete ? "known items" : "items"}
          </Chip>
        </div>

        {attentionCount === 0 ? (
          <div className="rounded-lg border border-border bg-card px-4 py-4 text-body text-muted-foreground">
            {attentionIncomplete ? "Attention data is incomplete because a source is unavailable." : "Nothing needs attention right now."}
          </div>
        ) : (
          <div className="grid gap-3 xl:grid-cols-2">
            {reviewAvailable && (
            <Hunk>
              <div className="hunk-head justify-start">
                <ClipboardCheck size={14} /> Reviews
              </div>
              {reviewsUnavailable ? (
                <div className="px-3 py-4 text-body text-muted-foreground">Review data is unavailable.</div>
              ) : openReviews.length === 0 ? (
                <div className="px-3 py-4 text-body text-muted-foreground">No open reviews.</div>
              ) : (
                openReviews.slice(0, 5).map((review) => (
                  <a
                    key={`${review.repo_id}:${review.id}`}
                    href={reviewHref(review.ref || review.id)}
                    className="flex min-h-11 items-center gap-3 border-b border-border px-3 py-2 last:border-0 transition-colors duration-fast hover:bg-accent/40"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-body font-medium text-foreground">
                        {review.title || "Untitled review"}
                      </div>
                      <div className="mt-0.5 truncate text-caption text-muted-foreground">
                        {reviewPlace(review)} · Snapshot {review.revision_number}
                      </div>
                    </div>
                    <ArrowRight size={14} className="shrink-0 text-muted-foreground" />
                  </a>
                ))
              )}
            </Hunk>
            )}

            {runtimeAvailable && (
            <Hunk>
              <div className="hunk-head justify-start">
                <AlertTriangle size={14} /> Failed runs
              </div>
              {runsUnavailable ? (
                <div className="px-3 py-4 text-body text-muted-foreground">Run history is unavailable.</div>
              ) : failedRuns.length === 0 ? (
                <div className="px-3 py-4 text-body text-muted-foreground">No failed runs.</div>
              ) : (
                failedRuns.map((run) => (
                  <Link
                    key={run.id}
                    to={runHref(run)}
                    className="flex min-h-11 items-center gap-3 border-b border-border px-3 py-2 last:border-0 transition-colors duration-fast hover:bg-accent/40"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-body font-medium text-foreground">
                        {runLabel(run)}
                      </div>
                      <div className="mt-0.5 truncate text-caption text-muted-foreground">
                        {run.host || run.agent || "agent"} · {fmtRelativeTime(run.created_at)}
                      </div>
                    </div>
                    <Badge tone="p0">{run.status}</Badge>
                  </Link>
                ))
              )}
            </Hunk>
            )}

            {integrationIssue && (
              <Hunk>
                <div className="hunk-head justify-start">
                  <AlertTriangle size={14} /> Setup
                </div>
                <Link
                  to="/settings/integrations"
                  className="flex min-h-16 items-center gap-3 px-3 py-3 transition-colors duration-fast hover:bg-accent/40"
                >
                  <div className="min-w-0 flex-1">
                    <div className="text-body font-medium text-foreground">
                      {!integrations?.dispatcher_available
                        ? "Agent connection is unavailable"
                        : "Connect a coding agent"}
                    </div>
                    <div className="mt-1 text-caption leading-4 text-muted-foreground">
                      Open Integrations to repair or finish the coding-host setup.
                    </div>
                  </div>
                  <ArrowRight size={14} className="shrink-0 text-muted-foreground" />
                </Link>
              </Hunk>
            )}
          </div>
        )}
      </section>
      )}

      {runtimeAvailable && (
      <section className="space-y-2">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-heading font-semibold text-foreground">Active now</h2>
            <p className="mt-0.5 text-label text-muted-foreground">
              Coding work LemonCrow can currently see.
            </p>
          </div>
          <Link to="/activity" className="font-mono text-label uppercase tracking-tag text-muted-foreground transition-colors duration-fast hover:text-foreground">
            All activity →
          </Link>
        </div>
        {runsUnavailable ? (
          <div className="rounded-lg border border-border bg-card px-4 py-4 text-body text-muted-foreground">Coding-agent activity is unavailable.</div>
        ) : activeRuns.length === 0 ? (
          <div className="rounded-lg border border-border bg-card px-4 py-4 text-body text-muted-foreground">
            No active runs.
          </div>
        ) : (
          <div className="grid gap-2 lg:grid-cols-2 xl:grid-cols-3">
            {activeRuns.map((run) => (
              <Link key={run.id} to={runHref(run)} className="block">
                <Hunk className="h-full p-3 transition-colors duration-fast hover:border-input">
                  <div className="flex items-start gap-2">
                    <Play size={14} className="mt-0.5 shrink-0 text-ok" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-body font-medium text-foreground">
                        {runLabel(run)}
                      </div>
                      <div className="mt-1 text-caption text-muted-foreground">
                        {run.host || run.agent || "agent"}
                        {run.model ? ` · ${run.model}` : ""}
                      </div>
                    </div>
                    <span className="h-2 w-2 shrink-0 rounded-full bg-ok-solid" />
                  </div>
                </Hunk>
              </Link>
            ))}
          </div>
        )}
      </section>
      )}

      {(reviewAvailable || runtimeAvailable) && (
      <section className="grid gap-4 xl:grid-cols-2">
        {reviewAvailable && (
        <Hunk>
          <div className="hunk-head">
            <div className="flex items-center gap-2">
              <ClipboardCheck size={14} /> Recent reviews
            </div>
            <Link to="/reviews" className="text-meta text-muted-foreground transition-colors duration-fast hover:text-foreground">View all</Link>
          </div>
          {reviewsUnavailable ? (
            <div className="px-3 py-4 text-body text-muted-foreground">Review data is unavailable.</div>
          ) : recentReviews.length === 0 ? (
            <div className="px-3 py-4 text-body text-muted-foreground">No recent reviews.</div>
          ) : (
            recentReviews.map((review) => (
              <a
                key={`${review.repo_id}:${review.id}`}
                href={reviewHref(review.ref || review.id)}
                className="flex items-center gap-3 border-b border-border px-3 py-2 last:border-0 transition-colors duration-fast hover:bg-accent/40"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-body text-subtle">{review.title || review.source_ref}</div>
                  <div className="mt-0.5 text-caption text-muted-foreground">{review.repo_id}</div>
                </div>
                <span className="text-caption capitalize text-muted-foreground">{review.status === "open" ? "needs review" : review.status}</span>
              </a>
            ))
          )}
        </Hunk>
        )}

        {runtimeAvailable && (
        <Hunk>
          <div className="hunk-head">
            <div className="flex items-center gap-2">
              <Activity size={14} /> Recent sessions
            </div>
            <Link to="/activity" className="text-meta text-muted-foreground transition-colors duration-fast hover:text-foreground">View all</Link>
          </div>
          {runsUnavailable ? (
            <div className="px-3 py-4 text-body text-muted-foreground">Session history is unavailable.</div>
          ) : recentRuns.length === 0 ? (
            <div className="px-3 py-4 text-body text-muted-foreground">No recent sessions.</div>
          ) : (
            recentRuns.map((run) => (
              <Link
                key={run.id}
                to={runHref(run)}
                className="flex items-center gap-3 border-b border-border px-3 py-2 last:border-0 transition-colors duration-fast hover:bg-accent/40"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-body text-subtle">{runLabel(run)}</div>
                  <div className="mt-0.5 text-caption text-muted-foreground">
                    {run.host || run.agent || "agent"} · {fmtRelativeTime(run.created_at)}
                  </div>
                </div>
                <span className="text-caption capitalize text-muted-foreground">{run.status}</span>
              </Link>
            ))
          )}
        </Hunk>
        )}
      </section>
      )}

    </PageFrame>
  );
}
