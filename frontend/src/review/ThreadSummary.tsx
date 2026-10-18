import { useEffect, useRef, useState } from "react";
import { LoaderCircle, Sparkles } from "lucide-react";

import { Button as UiButton } from "../components/ui/button";
import { annotationIdentityLabel } from "./annotationModel";
import { fetchThreadSummary, postThreadSummary, threadSummariesAvailable } from "./reviewApi";
import type { Annotation, ThreadSummaryStatement, ThreadSummaryView } from "./types";

const REF_PREFIX = "annotation:";

/**
 * DS0: a thread's AI summary, opened on request. Derived presentation only:
 * every statement links the messages it came from, and the thread's state is
 * shown from the comments themselves, never from the summary. Opening reads
 * (no model call); only Summarize/Refresh asks for one.
 */
export function ThreadSummary({
  reviewId,
  threadId,
  messages,
}: {
  reviewId: string;
  threadId: string;
  /** The root comment, then its replies, in thread order. */
  messages: Annotation[];
}) {
  const [minimum, setMinimum] = useState<number | null>(null);
  const [view, setView] = useState<ThreadSummaryView | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const opened = useRef(false);
  const signature = messages.map((item) => `${item.id}:${item.updated_at}:${item.body.length}`).join("|");

  useEffect(() => {
    let live = true;
    void threadSummariesAvailable(reviewId).then((value) => {
      if (live) setMinimum(value.available ? value.min_messages : null);
    });
    return () => {
      live = false;
    };
  }, [reviewId]);

  function load() {
    setLoading(true);
    setError("");
    fetchThreadSummary(reviewId, threadId)
      .then((read) => setView(read.summary))
      .catch((exc: unknown) => setError(exc instanceof Error ? exc.message : "the summary could not be read"))
      .finally(() => setLoading(false));
  }

  // An open summary re-reads when the thread changes, so staleness shows at once.
  useEffect(() => {
    if (opened.current) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  if (minimum === null || messages.length < minimum) return null;

  function summarize() {
    setRunning(true);
    setError("");
    postThreadSummary(reviewId, threadId)
      .then((run) => {
        setView(run.summary);
        if (run.result.status !== "ready" && run.result.status !== "reused") {
          setError(run.result.reason || `the summary was not made (${run.result.status})`);
        }
      })
      .catch((exc: unknown) => setError(exc instanceof Error ? exc.message : "the summary was not made"))
      .finally(() => setRunning(false));
  }

  const summary = view?.summary ?? null;
  const positions = new Map(messages.map((item, index) => [`${REF_PREFIX}${item.id}`, { index, item }]));

  function refs(statement: ThreadSummaryStatement) {
    return (
      <span className="ml-1 inline-flex flex-wrap gap-1">
        {statement.message_refs.map((ref) => {
          const found = positions.get(ref);
          return (
            <span
              key={ref}
              className="rounded border border-border/70 px-1 font-mono text-caption text-muted-foreground"
              title={found ? `Message ${found.index + 1} · ${annotationIdentityLabel(found.item)}` : ref}
              data-testid="thread-summary-ref"
            >
              #{found ? found.index + 1 : "?"}
            </span>
          );
        })}
      </span>
    );
  }

  return (
    <details
      className="review-thread-evidence"
      data-testid="thread-summary"
      onToggle={(event) => {
        if ((event.currentTarget as HTMLDetailsElement).open && !opened.current) {
          opened.current = true;
          load();
        }
      }}
    >
      <summary className="cursor-pointer">
        <Sparkles size={12} aria-hidden="true" className="mr-1 inline" />
        Thread summary · AI
      </summary>
      <div className="space-y-1 pt-1 text-caption">
        {loading && <LoaderCircle size={13} className="animate-spin" aria-label="Loading summary" />}
        {view && (
          <div className="text-muted-foreground" data-testid="thread-summary-state">
            Thread {view.thread.state} · {view.thread.message_count} messages · state is from the comments, not the summary
          </div>
        )}
        {summary && (
          <>
            <div className="text-muted-foreground">
              AI summary, derived from the messages; not a decision
              {summary.model ? ` · ${summary.driver} ${summary.model}` : summary.driver ? ` · ${summary.driver}` : ""}
            </div>
            {!summary.current && (
              <div className="w-fit rounded-md border border-amber-900/55 bg-amber-950/15 px-2 py-0.5 text-amber-300" data-testid="thread-summary-stale">
                Out of date: the thread changed after this summary
              </div>
            )}
            <div data-testid="thread-summary-topic">
              {summary.topic.text}
              {refs(summary.topic)}
            </div>
            {summary.open_asks.length > 0 && (
              <div>
                <div className="font-medium">Open asks in the conversation</div>
                <ul className="pl-4">
                  {summary.open_asks.map((item, index) => (
                    <li key={`ask-${index}`}>
                      {item.text}
                      {refs(item)}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {summary.points.length > 0 && (
              <div>
                <div className="font-medium">Points made</div>
                <ul className="pl-4">
                  {summary.points.map((item, index) => (
                    <li key={`point-${index}`}>
                      {item.text}
                      {refs(item)}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
        {view && !summary && view.last_attempt?.status === "failed" && !error && (
          <div className="text-amber-300">Last attempt failed: {view.last_attempt.error}</div>
        )}
        {error && (
          <div className="text-amber-300" data-testid="thread-summary-error">
            {error}
          </div>
        )}
        {view && (!summary || !summary.current) && (
          <UiButton variant="outline" size="sm" disabled={running} onClick={summarize}>
            {running ? "Summarizing…" : summary ? "Refresh summary" : "Summarize thread"}
          </UiButton>
        )}
      </div>
    </details>
  );
}
