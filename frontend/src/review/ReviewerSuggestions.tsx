import { useEffect, useState } from "react";
import { AlertTriangle, UserRound, UsersRound } from "lucide-react";

import { Button as UiButton } from "../components/ui/button";
import {
  degradedSignals,
  exclusionSummary,
  fetchReviewerSuggestions,
  type ReviewerSuggestion,
  type ReviewerSuggestionSet,
} from "./reviewerSuggestions";

export interface ReviewerSuggestionListProps {
  data: ReviewerSuggestionSet | null;
  loading?: boolean;
  error?: string;
  /** Present only where the viewer may request reviewers (hosted, with review:manage). */
  onRequest?(suggestion: ReviewerSuggestion): void;
  busyKey?: string;
}

function SuggestionRow({ suggestion, onRequest, busy }: { suggestion: ReviewerSuggestion; onRequest?(suggestion: ReviewerSuggestion): void; busy: boolean }) {
  const requested = suggestion.status === "requested";
  return (
    <li className="flex gap-2.5 py-2.5" data-testid="reviewer-suggestion">
      <div className="mt-0.5 shrink-0 text-muted-foreground">
        {suggestion.kind === "team" ? <UsersRound size={12} aria-label="Team" /> : <UserRound size={12} aria-label="Person" />}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate text-caption font-medium text-neutral-200" title={suggestion.key}>{suggestion.label}</span>
          {suggestion.kind === "team" && <span className="text-caption text-muted-foreground">team</span>}
          <span className={`text-caption ${requested ? "text-sky-400" : "text-muted-foreground"}`}>{requested ? "Requested" : "Suggested"}</span>
          {suggestion.access === "unknown" && <span className="text-caption text-muted-foreground">access checked on request</span>}
        </div>
        <ul className="mt-1 space-y-0.5 text-caption leading-4 text-muted-foreground">
          {suggestion.reasons.map((reason) => (
            <li key={reason.signal} title={reason.sources.join("; ")}>{reason.detail}</li>
          ))}
        </ul>
        {suggestion.critical_paths.length > 0 && (
          <div className="mt-0.5 truncate text-caption text-amber-300/80" title={suggestion.critical_paths.join(", ")}>
            Critical paths · {suggestion.critical_paths.join(", ")}
          </div>
        )}
      </div>
      {onRequest && !requested && (
        <UiButton
          type="button"
          variant="ghost"
          size="compact"
          disabled={busy}
          onClick={() => onRequest(suggestion)}
          aria-label={`Request review from ${suggestion.label}`}
        >
          {busy ? "Requesting…" : "Request"}
        </UiButton>
      )}
    </li>
  );
}

/** Explained, advisory reviewer suggestions; never requests anything by itself. */
export function ReviewerSuggestionList({ data, loading = false, error = "", onRequest, busyKey = "" }: ReviewerSuggestionListProps) {
  const excluded = data ? exclusionSummary(data.excluded) : "";
  const degraded = data ? degradedSignals(data.signals) : [];
  return (
    <div className="mt-3" aria-labelledby="reviewer-suggestions-title">
      <div id="reviewer-suggestions-title" className="text-caption font-semibold text-neutral-300">Suggested reviewers</div>
      <div className="mb-1 text-caption leading-4 text-muted-foreground">
        Advisory · nothing is requested until someone asks.
        {data?.automatic_routing ? " Organization routing may request rule reviewers automatically." : ""}
      </div>
      {loading && !data && <div role="status" className="py-2 text-caption text-muted-foreground">Loading suggestions…</div>}
      {/* Advisory: a load failure is shown in place but never competes with readiness alerts. */}
      {error && <div className="py-2 text-caption text-rose-300">{error}</div>}
      {data && data.head_changes.map((change) => (
        <div key={change.path} className="mb-1 flex gap-2 text-caption leading-4 text-amber-200/80">
          <AlertTriangle size={11} className="mt-0.5 shrink-0" />
          <span>{change.note}</span>
        </div>
      ))}
      {data && data.suggestions.length === 0 && (
        <div className="py-2 text-caption text-muted-foreground">No reviewer suggestions for this Snapshot.</div>
      )}
      {data && data.suggestions.length > 0 && (
        <ul className="divide-y divide-neutral-900 border-y border-neutral-900">
          {data.suggestions.map((suggestion) => (
            <SuggestionRow key={suggestion.key} suggestion={suggestion} onRequest={onRequest} busy={busyKey === suggestion.key} />
          ))}
        </ul>
      )}
      {(excluded || degraded.length > 0 || (data?.truncated ?? 0) > 0) && (
        <div className="mt-1 space-y-0.5 text-caption leading-4 text-muted-foreground">
          {excluded && <div>{excluded}</div>}
          {(data?.truncated ?? 0) > 0 && <div>{data?.truncated} more not shown</div>}
          {degraded.map((line) => <div key={line}>{line}</div>)}
        </div>
      )}
    </div>
  );
}

/** Local Reader: loads the Snapshot's suggestions when Review Status opens. */
export default function ReviewerSuggestionsPanel({ reviewId, revisionId }: { reviewId: string; revisionId: string }) {
  const [data, setData] = useState<ReviewerSuggestionSet | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    setData(null);
    setError("");
    async function load() {
      try {
        const next = await fetchReviewerSuggestions(reviewId, revisionId);
        if (live) setData(next);
      } catch (reason: unknown) {
        if (live) setError(`Could not load reviewer suggestions: ${reason instanceof Error ? reason.message : String(reason)}`);
      }
    }
    if (reviewId) void load();
    return () => { live = false; };
  }, [reviewId, revisionId]);
  return <ReviewerSuggestionList data={data} loading={!data && !error} error={error} />;
}
