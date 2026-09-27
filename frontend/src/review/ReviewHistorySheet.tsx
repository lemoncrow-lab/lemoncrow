import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bot, CircleDot, GitCompareArrows, UserRound, X } from "lucide-react";

import "./reviewUi.css";
import { trapModalTab } from "./focusTrap";
import {
  fetchReviewActivity,
  fetchRevisions,
  historicalReviewHref,
  reviewCompareHref,
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
  const [compareBase, setCompareBase] = useState<number | null>(null);
  const [compareTarget, setCompareTarget] = useState<number | null>(null);
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
  const defaultCompareBase = rows.find((row) => row.is_last_reviewed)?.revision_number
    ?? rows.find((row) => row.revision_number !== latestRevision)?.revision_number
    ?? latestRevision;
  const activeCompareBase = compareBase ?? defaultCompareBase;
  const activeCompareTarget = compareTarget ?? latestRevision;
  const advancedCompareHref = activeCompareBase !== activeCompareTarget
    ? reviewCompareHref(reviewId, activeCompareBase, activeCompareTarget)
    : "";
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
              Viewing Snapshot {selectedRevisionNumber}{latestRevision !== selectedRevisionNumber ? ` · latest Snapshot ${latestRevision}` : ""}
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
                {item === "revisions" ? "Snapshots" : "Activity"}
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
                      {revisionNumber !== undefined && <span className="font-mono text-[10px] text-sky-400/80">Snapshot {revisionNumber}</span>}
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
            <div className="py-10 text-center text-[11px] text-neutral-600">No persisted Snapshots.</div>
          )}
          {!loading && !error && tab === "revisions" && (
            <div className="space-y-1.5">
              {rows.length > 1 && (
                <details className="mb-3 rounded-md border border-neutral-800/80 bg-neutral-900/20">
                  <summary className="cursor-pointer list-none px-2.5 py-2 text-[10px] font-medium text-neutral-500 hover:text-neutral-200 [&::-webkit-details-marker]:hidden">
                    Advanced comparison
                  </summary>
                  <div className="flex flex-wrap items-end gap-2 border-t border-neutral-800/70 px-2.5 py-2.5">
                    <label className="min-w-[130px] flex-1 text-[9px] uppercase tracking-[0.08em] text-neutral-600">
                      From
                      <select
                        aria-label="Compare from Snapshot"
                        value={activeCompareBase}
                        onChange={(event) => setCompareBase(Number(event.target.value))}
                        className="review-field mt-1 h-8 w-full py-0 text-[10px] normal-case tracking-normal"
                      >
                        {rows.map((row) => (
                          <option key={row.id} value={row.revision_number}>Snapshot {row.revision_number}</option>
                        ))}
                      </select>
                    </label>
                    <label className="min-w-[130px] flex-1 text-[9px] uppercase tracking-[0.08em] text-neutral-600">
                      To
                      <select
                        aria-label="Compare to Snapshot"
                        value={activeCompareTarget}
                        onChange={(event) => setCompareTarget(Number(event.target.value))}
                        className="review-field mt-1 h-8 w-full py-0 text-[10px] normal-case tracking-normal"
                      >
                        {rows.map((row) => (
                          <option key={row.id} value={row.revision_number}>Snapshot {row.revision_number}</option>
                        ))}
                      </select>
                    </label>
                    {advancedCompareHref ? (
                      <a
                        href={advancedCompareHref}
                        className="review-toolbar-button-primary inline-flex h-8 items-center gap-1.5 px-3 text-[10px]"
                      >
                        <GitCompareArrows size={11} aria-hidden="true" />
                        Compare
                      </a>
                    ) : (
                      <span className="inline-flex h-8 items-center px-3 text-[10px] text-neutral-700">Choose two Snapshots</span>
                    )}
                  </div>
                </details>
              )}
              {rows.map((row) => {
                const selected = row.revision_number === selectedRevisionNumber;
                const isLatest = row.is_latest ?? row.revision_number === latestRevision;
                const href = historicalReviewHref(reviewId, row.revision_number);
                const compareHref = latest && !isLatest
                  ? reviewCompareHref(reviewId, row.revision_number, latest.revision_number)
                  : "";
                return (
                  <div key={row.id} className={`rounded-md border px-2.5 py-2.5 transition-colors ${selected ? "border-neutral-800 bg-neutral-900/45" : "border-transparent hover:border-neutral-800/80 hover:bg-neutral-900/30"}`}>
                    <div className="flex min-w-0 items-start gap-2">
                      <a href={href} className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-[11px] font-medium text-neutral-200">Snapshot {row.revision_number}</span>
                          {isLatest && <span className="text-[10px] text-sky-400">Latest</span>}
                          {row.is_last_reviewed && <span className="text-[10px] text-emerald-400/80">Last reviewed</span>}
                          {row.is_last_commented && <span className="text-[10px] text-violet-400/80">Last commented</span>}
                          {selected && <span className="text-[10px] text-neutral-500">Viewing</span>}
                          {row.dirty && <span className="text-[10px] text-amber-500/80">working tree</span>}
                        </div>
                        <div className="mt-1 flex flex-wrap gap-x-3 text-[10px] text-neutral-600">
                          <span>{row.created_at ? new Date(row.created_at).toLocaleString() : "Captured Snapshot"}</span>
                          {row.base_moved && row.semantic_changed === false && (
                            <span className="text-sky-400/80">Base moved · review areas unchanged</span>
                          )}
                          {row.review_area_count != null && (
                            <span className="font-mono">
                              {row.previous_revision_number == null
                                ? `${row.review_area_count} initial review area${row.review_area_count === 1 ? "" : "s"}`
                                : row.review_area_delta
                                  ? `Δ +${row.review_area_delta.added} ~${row.review_area_delta.changed} −${row.review_area_delta.removed} review areas`
                                  : `${row.review_area_count} review area${row.review_area_count === 1 ? "" : "s"}`}
                            </span>
                          )}
                          {row.head_sha
                            ? <span className="font-mono">SCM {row.head_sha.slice(0, 10)}</span>
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
