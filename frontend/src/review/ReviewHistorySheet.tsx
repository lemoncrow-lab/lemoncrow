import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bot, CircleDot, GitCompareArrows, UserRound, X } from "lucide-react";

import "./reviewUi.css";
import { trapModalTab } from "./focusTrap";
import {
  fetchReviewActivity,
  fetchRevisions,
  historicalReviewHref,
  sourceCompareHref,
} from "./reviewApi";
import type { ReviewActivityEvent, RevisionInfo } from "./types";

type HistoryTab = "activity" | "revisions";

interface ReviewHistorySheetProps {
  reviewId: string;
  selectedRevisionNumber: number;
  historical: boolean;
  initialTab?: HistoryTab;
  onClose(): void;
}

function actorPresentation(event: ReviewActivityEvent): { label: string; tone: string; icon: "human" | "agent" | "system" } {
  if (event.actor_type === "human") {
    return {
      label: event.actor_id || "Human reviewer",
      tone: "border-emerald-900/70 bg-emerald-950/15 text-emerald-300",
      icon: "human",
    };
  }
  if (event.actor_type === "agent") {
    return {
      label: event.actor_id || "Agent",
      tone: "border-violet-900/70 bg-violet-950/15 text-violet-300",
      icon: "agent",
    };
  }
  return {
    label: event.actor_id || "LemonCrow",
    tone: "border-neutral-800 bg-neutral-900/50 text-neutral-400",
    icon: "system",
  };
}

function eventReason(event: ReviewActivityEvent): string {
  const reason = event.detail.reason;
  return typeof reason === "string" ? reason : "";
}

function revisionSource(row: RevisionInfo): string {
  return row.ref || `rr/${row.id}`;
}

export default function ReviewHistorySheet({
  reviewId,
  selectedRevisionNumber,
  initialTab = "revisions",
  onClose,
}: ReviewHistorySheetProps) {
  const [rows, setRows] = useState<RevisionInfo[]>([]);
  const [events, setEvents] = useState<ReviewActivityEvent[]>([]);
  const [tab, setTab] = useState<HistoryTab>(initialTab);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const closeRef = useRef<HTMLButtonElement>(null);

  const loadHistory = useCallback(async () => {
    setLoading(true);
    try {
      const [revisionResult, activityResult] = await Promise.all([fetchRevisions(reviewId), fetchReviewActivity(reviewId)]);
      setRows([...revisionResult.revisions].sort((a, b) => b.revision_number - a.revision_number));
      setEvents([...activityResult.events].reverse());
      setError("");
    } catch (reason: unknown) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setLoading(false);
    }
  }, [reviewId]);

  useEffect(() => {
    closeRef.current?.focus();
    void loadHistory();
  }, [loadHistory]);

  const latest = rows[0] ?? null;
  const latestRevision = latest?.revision_number ?? 0;
  const revisionById = useMemo(() => new Map(rows.map((row) => [row.id, row.revision_number])), [rows]);

  return (
    <div
      className="absolute inset-0 z-[76] flex justify-end bg-black/45"
      role="dialog"
      aria-modal="true"
      aria-labelledby="review-history-title"
      onKeyDown={(event) => {
        if (trapModalTab(event)) return;
        if (event.key === "Escape") {
          event.stopPropagation();
          onClose();
        }
      }}
    >
      <section className="review-sheet flex h-full w-[min(560px,100%)] flex-col border-y-0 border-r-0">
        <div className="flex items-center gap-3 border-b border-neutral-800/80 px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 id="review-history-title" className="text-[13px] font-semibold text-neutral-100">History</h2>
            <div className="mt-0.5 text-[10px] text-neutral-500">
              Viewing rev {selectedRevisionNumber}{latestRevision > 0 && latestRevision !== selectedRevisionNumber ? ` · latest rev ${latestRevision}` : ""}
            </div>
          </div>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close review history" className="review-icon-button">
            <X size={14} />
          </button>
        </div>

        <div className="flex shrink-0 border-b border-neutral-800/70 px-4 py-2">
          <div className="review-segment" role="tablist" aria-label="Review history sections">
            {(["activity", "revisions"] as const).map((item) => (
              <button
                key={item}
                type="button"
                role="tab"
                aria-selected={tab === item}
                onClick={() => setTab(item)}
                className={`review-segment-button inline-flex items-center gap-1.5 capitalize ${tab === item ? "review-segment-button-active" : ""}`}
              >
                {item}
                <span className="font-mono text-[10px] text-neutral-600">{item === "activity" ? events.length : rows.length}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
          {loading && rows.length === 0 && events.length === 0 && !error && (
            <div className="py-8 text-center text-[10px] text-neutral-600" role="status">Loading history…</div>
          )}
          {error && (
            <div className="flex items-start gap-3 border-y border-rose-900/45 bg-rose-950/10 px-3 py-2.5">
              <div className="min-w-0 flex-1">
                <div className="text-[10px] font-medium text-rose-300">Could not load history</div>
                <details className="mt-1 text-[10px] text-neutral-600"><summary className="cursor-pointer">Error details</summary><div className="mt-1 font-mono text-rose-300/75">{error}</div></details>
              </div>
              <button type="button" disabled={loading} onClick={() => void loadHistory()} className="review-toolbar-button h-7">Retry</button>
            </div>
          )}

          {!loading && !error && tab === "activity" && events.length === 0 && (
            <div className="py-10 text-center text-[11px] text-neutral-600">No recorded review activity.</div>
          )}
          {!loading && !error && tab === "activity" && (
            <div className="space-y-0">
              {events.map((event) => {
                const actor = actorPresentation(event);
                const revisionNumber = revisionById.get(event.revision_id);
                const reason = eventReason(event);
                return (
                  <div key={event.id} className="relative border-l border-neutral-800/70 py-3.5 pl-5">
                    <span className="absolute -left-[5px] top-[18px] h-2.5 w-2.5 rounded-full border border-neutral-700 bg-neutral-950" />
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] ${actor.tone}`}>
                        {actor.icon === "human" ? <UserRound size={10} /> : actor.icon === "agent" ? <Bot size={10} /> : <CircleDot size={10} />}
                        {actor.label}
                      </span>
                      {revisionNumber && <span className="font-mono text-[10px] text-sky-400/80">rev {revisionNumber}</span>}
                      <span className="text-[10px] text-neutral-600">{event.created_at ? new Date(event.created_at).toLocaleString() : ""}</span>
                    </div>
                    <div className="mt-1 text-[11px] leading-relaxed text-neutral-200">{event.summary || "Review activity"}</div>
                    {reason && <div className="mt-1 text-[10px] leading-relaxed text-neutral-500">{reason}</div>}
                    <details className="mt-1.5 text-[10px] text-neutral-600">
                      <summary className="w-fit cursor-pointer select-none hover:text-neutral-50">Technical details</summary>
                      <div className="mt-1 flex flex-wrap gap-x-2 border-l border-neutral-800 pl-2 font-mono">
                        <span>{event.kind}</span>
                        {event.subject_id && <span className="truncate">{event.subject_id}</span>}
                      </div>
                    </details>
                  </div>
                );
              })}
            </div>
          )}

          {!loading && !error && tab === "revisions" && rows.length === 0 && (
            <div className="py-10 text-center text-[11px] text-neutral-600">No persisted revisions.</div>
          )}
          {!loading && !error && tab === "revisions" && (
            <div className="space-y-1.5">
              {rows.map((row) => {
                const selected = row.revision_number === selectedRevisionNumber;
                const isLatest = row.revision_number === latestRevision;
                const href = historicalReviewHref(row.ref || row.id);
                const compareHref = latest && !isLatest
                  ? sourceCompareHref(
                      revisionSource(row),
                      revisionSource(latest),
                      reviewId.startsWith("r/") ? reviewId : `r/${reviewId}`,
                    )
                  : "";
                return (
                  <div key={row.id} className={`rounded-md border px-2.5 py-2.5 transition-colors ${selected ? "border-neutral-800 bg-neutral-900/45" : "border-transparent hover:border-neutral-800/80 hover:bg-neutral-900/30"}`}>
                    <div className="flex min-w-0 items-start gap-2">
                      <a href={href} className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-[11px] font-medium text-neutral-200">rev {row.revision_number}</span>
                          {isLatest && <span className="text-[10px] text-sky-400">Latest</span>}
                          {selected && <span className="text-[10px] text-neutral-500">Viewing</span>}
                          {row.dirty && <span className="text-[10px] text-amber-500/80">working tree</span>}
                        </div>
                        <div className="mt-1 flex flex-wrap gap-x-3 text-[10px] text-neutral-600">
                          <span>{row.created_at ? new Date(row.created_at).toLocaleString() : "Captured revision"}</span>
                          {row.head_sha
                            ? <span className="font-mono">{row.head_sha.slice(0, 10)}</span>
                            : row.dirty
                              ? <span>local snapshot</span>
                              : null}
                        </div>
                      </a>
                      {compareHref && (
                        <a href={compareHref} className="review-compact-action inline-flex h-7 shrink-0 items-center gap-1 px-2 text-[10px] text-sky-300/80">
                          <GitCompareArrows size={11} aria-hidden="true" /> Compare to latest
                        </a>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
