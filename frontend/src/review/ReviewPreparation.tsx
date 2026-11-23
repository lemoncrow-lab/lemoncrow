import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Check, ChevronDown, ChevronUp, Circle, LoaderCircle, X } from "lucide-react";

import { Button } from "../components/ui/button";
import { authenticatedFetch } from "./reviewApi";

import { useReviewPreparation, type PreparationStep } from "./reviewPreparationContext";

/** A single progress request that takes longer than this is abandoned and retried. */
const POLL_TIMEOUT_MS = 15_000;
/** A step that runs longer than this offers a way out instead of an endless spinner. */
export const PREPARATION_STALL_MS = 25_000;

interface Preparation {
  id: string;
  state: "running" | "ready" | "failed";
  detail: string;
  review_url: string;
  steps: PreparationStep[];
}

/** Renders immediately; source capture and Reader loading run behind this surface. */
export default function ReviewPreparation() {
  const location = useLocation();
  const navigate = useNavigate();
  const reader = useReviewPreparation()?.reader;
  const readerPending = Boolean(reader?.pending);
  const preparationId = /^\/reviews\/preparing\/([a-f0-9]{32})$/.exec(location.pathname)?.[1];
  const jobId = preparationId ?? new URLSearchParams(location.search).get("startup") ?? "";
  const [job, setJob] = useState<Preparation | null>(null);
  const [error, setError] = useState("");
  const [expired, setExpired] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    if (!/^[a-f0-9]{32}$/.test(jobId)) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const startedAt = Date.now();
    setJob(null);
    setError("");
    setExpired(false);
    setDismissed(false);
    async function poll() {
      let delay = 850;
      // One unanswered request must not freeze progress: give each poll a deadline and let the retry path take over.
      const deadline = new AbortController();
      const abortDeadline = () => deadline.abort();
      controller.signal.addEventListener("abort", abortDeadline, { once: true });
      const timeout = setTimeout(abortDeadline, POLL_TIMEOUT_MS);
      try {
        const response = await authenticatedFetch(
          `/api/review-preparations/${jobId}`, { signal: deadline.signal, cache: "no-store" },
        );
        if (!response.ok) {
          if (response.status === 404) {
            if (!disposed) { setError("This preparation has expired."); setExpired(true); }
            return;
          }
          throw new Error("Cannot load preparation progress. Reconnecting…");
        }
        const next = await response.json() as Preparation;
        if (disposed) return;
        setJob(next);
        setError("");
        if (next.state !== "running") return;
      } catch (cause) {
        if (disposed) return;
        setError(cause instanceof Error && cause.name !== "AbortError" ? cause.message : "Cannot load preparation progress. Reconnecting…");
        if (Date.now() - startedAt > 600_000) return;
        delay = 2000;
      } finally {
        clearTimeout(timeout);
        controller.signal.removeEventListener("abort", abortDeadline);
      }
      if (!disposed) timer = setTimeout(poll, delay);
    }
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [jobId, retry]);

  useEffect(() => {
    if (!preparationId || !job?.review_url) return;
    const target = new URL(job.review_url, window.location.origin);
    if (target.origin !== window.location.origin || !target.pathname.startsWith("/r/")) return;
    if (job.state !== "ready") target.searchParams.set("startup", jobId);
    navigate(target.pathname + target.search + target.hash, { replace: true });
  }, [preparationId, job?.review_url, job?.state, jobId, navigate]);

  useEffect(() => {
    if (preparationId || readerPending || job?.state !== "ready" || !jobId) return;
    // The Reader also updates its view through native history. Preserve the
    // latest browser query when it belongs to this route.
    const browser = window.location.pathname === location.pathname ? window.location : location;
    const params = new URLSearchParams(browser.search);
    params.delete("startup");
    const search = params.toString();
    navigate(location.pathname + (search ? `?${search}` : "") + browser.hash, { replace: true });
  }, [preparationId, readerPending, job?.state, jobId, location.pathname, location.search, location.hash, navigate]);

  const hasJob = /^[a-f0-9]{32}$/.test(jobId);
  // A /reviews/preparing/<id> address whose id cannot be a preparation: say so instead of a blank page.
  if (!preparationId && /^\/reviews\/preparing\/[^/]*\/?$/.test(location.pathname)) return <PreparationNotFound />;
  if ((!hasJob && !readerPending) || dismissed || (!preparationId && job?.state === "ready" && !readerPending)) return null;
  const steps = [
    ...(hasJob ? job?.steps.filter((step) => step.state !== "skipped") ?? [] : []),
    ...(reader?.steps.filter((step) => step.state !== "skipped"
      && !(hasJob && step.key === "reader_source" && step.state === "done")) ?? []),
  ];
  return <PreparationProgress
    fullPage={Boolean(preparationId) || readerPending}
    steps={steps}
    failed={hasJob && job?.state === "failed"}
    detail={hasJob ? job?.detail : undefined}
    error={hasJob ? error : undefined}
    expired={hasJob && expired}
    starting={!job && !reader}
    onReconnect={() => setRetry((value) => value + 1)}
    onDismiss={() => setDismissed(true)}
    onOpenCode={reader?.onOpenCode}
  />;
}

/** `/reviews/preparing/<not an id>`: a full page with one way out. */
function PreparationNotFound() {
  return (
    <section aria-label="Review preparation" data-testid="preparation-not-found" className="mx-auto flex min-h-screen max-w-xl flex-col justify-center gap-3 px-4 py-8 text-foreground">
      <h1 className="text-title font-semibold">This preparation link isn't valid</h1>
      <p className="text-body text-muted-foreground">Check the link, or run <code className="rounded bg-muted px-1 font-mono text-meta">lc review</code> in your terminal to start a new review.</p>
      <div><Button asChild size="sm"><Link to="/reviews">All reviews</Link></Button></div>
    </section>
  );
}

/** One progress surface for capture, saved state, and the first useful Reader view. */
export function PreparationProgress({
  fullPage, steps, failed = false, detail, error, expired = false, starting = false, onReconnect, onDismiss, onOpenCode,
}: {
  fullPage: boolean;
  steps: PreparationStep[];
  failed?: boolean;
  detail?: string;
  error?: string;
  /** The server no longer knows this preparation: nothing will open, so no progress line and no Reconnect. */
  expired?: boolean;
  starting?: boolean;
  onReconnect?: () => void;
  onDismiss?: () => void;
  onOpenCode?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const current = steps.find((step) => step.state === "running");
  const waitingOn = failed ? "" : current?.key ?? (starting ? "starting" : "");
  // When the step being waited on started, measured here so a stalled request is caught whatever the step reports.
  const [waitStart, setWaitStart] = useState({ key: waitingOn, at: Date.now() });
  if (waitStart.key !== waitingOn) setWaitStart({ key: waitingOn, at: Date.now() });
  useEffect(() => {
    if (!waitingOn) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [waitingOn]);
  const stalled = Boolean(waitingOn) && waitStart.key === waitingOn && now - waitStart.at > PREPARATION_STALL_MS;
  const completed = steps.filter((step) => step.state === "done").length;
  const label = failed ? "Review preparation stopped" : "Preparing your review";
  const showSteps = fullPage || expanded;
  const progress = steps.length ? Math.round(completed / steps.length * 100) : 0;

  return (
    <section
      aria-label="Review preparation"
      data-testid={fullPage ? "review-loading-screen" : undefined}
      className={fullPage
        ? "mx-auto flex min-h-screen max-w-xl flex-col justify-center px-4 py-8 text-foreground"
        : "fixed bottom-4 right-4 z-indicator w-[calc(100%_-_2rem)] max-w-sm rounded-lg border border-border bg-background p-3 text-foreground"}
    >
      <div className="flex items-center justify-between gap-3">
        <h1 className={fullPage ? "text-title font-semibold" : "text-body font-semibold"}>{label}</h1>
        {!fullPage && (
          <div className="flex gap-1">
            <Button variant="ghost" size="icon" aria-label={expanded ? "Hide preparation steps" : "Show preparation steps"}
              aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
              {expanded ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
            </Button>
            {failed && onDismiss && <Button variant="ghost" size="icon" aria-label="Dismiss preparation progress"
              onClick={onDismiss}><X size={14} /></Button>}
          </div>
        )}
      </div>
      <p className="mt-2 text-body text-muted-foreground" role="status">
        {error || (failed ? detail : current?.label) || "Starting preparation…"}
      </p>
      <div className="mt-4 h-1 overflow-hidden rounded-full bg-muted" role="progressbar"
        aria-label="Review preparation steps" aria-valuemin={0} aria-valuemax={100}
        aria-valuenow={starting ? undefined : progress}
        aria-valuetext={starting ? "Starting preparation" : `${completed} of ${steps.length} steps complete`}>
        <div className="h-full bg-foreground transition-[width] duration-200 motion-reduce:transition-none"
          style={{ width: `${progress}%` }} />
      </div>
      {expired ? null : <p className="mt-2 text-caption tabular-nums text-muted-foreground">
        {starting ? "The review opens as soon as the source revision is ready." : `${completed} of ${steps.length} steps complete`}
      </p>}
      {showSteps && steps.length > 0 && (
        <ol className="mt-5 space-y-3 text-body">
          {steps.map((step) => {
            const elapsed = step.elapsed ?? (step.started_at ? now / 1000 - step.started_at : null);
            return (
              <li key={step.key} className="flex items-start gap-2">
                <span className="mt-0.5 shrink-0" aria-hidden="true">
                  {step.state === "done" ? <Check size={14} className="text-success-foreground" />
                    : step.state === "running" ? <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" />
                      : step.state === "failed" ? <X size={14} /> : <Circle size={14} className="text-muted-foreground" />}
                </span>
                <span className={step.state === "pending" ? "min-w-0 flex-1 text-muted-foreground" : "min-w-0 flex-1"}>
                  {step.label}<span className="sr-only"> · {step.state}</span>
                </span>
                {elapsed !== null && <span className="shrink-0 text-caption tabular-nums text-muted-foreground">
                  {Math.max(0, elapsed).toFixed(1)}s
                </span>}
              </li>
            );
          })}
        </ol>
      )}
      {onOpenCode && (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button size="sm" onClick={onOpenCode}>Open code</Button>
          <p className="text-caption text-muted-foreground">The source is ready. Brief preparation continues in the background.</p>
        </div>
      )}
      {(failed || error) && (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          {error && !expired && onReconnect && <Button size="sm" onClick={onReconnect}>Reconnect</Button>}
          <Button asChild size="sm"><Link to="/reviews">Open reviews</Link></Button>
          {failed || expired ? <p className="text-caption text-muted-foreground">Run <code className="rounded bg-muted px-1 font-mono">lc review</code> again to retry preparation.</p> : null}
        </div>
      )}
      {stalled && !error && (
        <div className="mt-4 space-y-2" data-testid="preparation-stalled">
          <p className="text-caption text-muted-foreground">This is taking longer than usual. The review keeps preparing if you leave.</p>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" onClick={() => window.location.reload()}>Reload</Button>
            <Button asChild size="sm" variant="outline"><Link to="/reviews">Open reviews</Link></Button>
          </div>
        </div>
      )}
    </section>
  );
}
