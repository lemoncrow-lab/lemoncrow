import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Check, CheckCircle2, ChevronDown, CornerUpRight, MessageSquareText, RefreshCw, X } from "lucide-react";

import "./reviewUi.css";
import { trapModalTab } from "./focusTrap";
import { annotationSource } from "./annotationModel";
import type { Annotation, FeedbackDeliveryCapability, FeedbackStatus } from "./types";

type Filter = "open" | "rereview" | "resolved" | "orphaned" | "all";
type SourceFilter = "all" | "human" | "ai" | "lemoncrow";

interface ReviewCommentsSheetProps {
  annotations: Annotation[];
  feedbackStatus?: FeedbackStatus;
  feedbackDelivery?: FeedbackDeliveryCapability | null;
  onClose(): void;
  onJump(annotation: Annotation): void;
}

const FILTERS: { id: Filter; label: string }[] = [
  { id: "open", label: "Open" },
  { id: "rereview", label: "Re-review" },
  { id: "resolved", label: "Resolved" },
  { id: "orphaned", label: "Orphaned" },
  { id: "all", label: "All" },
];

const SOURCE_FILTERS: { id: SourceFilter; label: string }[] = [
  { id: "all", label: "All sources" },
  { id: "human", label: "Human" },
  { id: "ai", label: "AI" },
  { id: "lemoncrow", label: "LemonCrow" },
];

function sourceGroup(annotation: Annotation): Exclude<SourceFilter, "all"> {
  const source = annotationSource(annotation);
  if (source === "lemoncrow") return "lemoncrow";
  if (source === "ai_review" || source === "author") return "ai";
  return "human";
}

function sourceLabel(annotation: Annotation): string {
  const source = annotationSource(annotation);
  if (source === "lemoncrow") return "LemonCrow";
  if (source === "ai_review") return "AI review";
  if (source === "author") return "AI author";
  return "Human";
}

export default function ReviewCommentsSheet({
  annotations,
  feedbackStatus,
  feedbackDelivery,
  onClose,
  onJump,
}: ReviewCommentsSheetProps) {
  const [filter, setFilter] = useState<Filter>("open");
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>("all");
  const closeRef = useRef<HTMLButtonElement>(null);
  const sourceMenuRef = useRef<HTMLDetailsElement>(null);
  useEffect(() => closeRef.current?.focus(), []);

  const roots = useMemo(() => annotations.filter((item) => !item.parent_id), [annotations]);
  const replies = useMemo(() => {
    const counts = new Map<string, number>();
    for (const item of annotations) {
      if (item.parent_id) counts.set(item.parent_id, (counts.get(item.parent_id) ?? 0) + 1);
    }
    return counts;
  }, [annotations]);
  const filterCounts = useMemo<Record<Filter, number>>(() => ({
    all: roots.length,
    open: roots.filter((item) => item.state === "open").length,
    rereview: roots.filter((item) => item.author_response === "addressed" && item.state !== "resolved" && item.state !== "obsolete").length,
    resolved: roots.filter((item) => item.state === "resolved").length,
    orphaned: roots.filter((item) => item.state === "orphaned" || !item.anchored).length,
  }), [roots]);
  const destination =
    feedbackDelivery?.label
    || feedbackDelivery?.host
    || (feedbackDelivery?.supported ? "coding agent" : "author");
  const handoffParts = feedbackStatus ? [
    feedbackStatus.unpublished > 0 ? `${feedbackStatus.unpublished} not sent` : "",
    feedbackStatus.in_flight > 0 ? `${feedbackStatus.in_flight} delivery pending` : "",
    feedbackStatus.published > 0 ? `${feedbackStatus.published} sent to ${destination}` : "",
    feedbackStatus.addressed > 0 ? `${feedbackStatus.addressed} ready for re-review` : "",
  ].filter(Boolean) : [];
  const statusVisible = roots.filter((item) => {
    if (filter === "all") return true;
    if (filter === "rereview") return item.author_response === "addressed" && item.state !== "resolved" && item.state !== "obsolete";
    if (filter === "orphaned") return item.state === "orphaned" || !item.anchored;
    return item.state === filter;
  });
  const sourceCounts = useMemo<Record<SourceFilter, number>>(() => ({
    all: statusVisible.length,
    human: statusVisible.filter((item) => sourceGroup(item) === "human").length,
    ai: statusVisible.filter((item) => sourceGroup(item) === "ai").length,
    lemoncrow: statusVisible.filter((item) => sourceGroup(item) === "lemoncrow").length,
  }), [statusVisible]);
  const visible = sourceFilter === "all"
    ? statusVisible
    : statusVisible.filter((item) => sourceGroup(item) === sourceFilter);

  return (
    <div
      className="review-sheet-backdrop absolute z-[75] justify-end"
      role="dialog"
      aria-modal="true"
      aria-labelledby="review-comments-title"
      onKeyDown={(event) => {
        if (trapModalTab(event)) return;
        if (event.key === "Escape") {
          event.stopPropagation();
          onClose();
        }
      }}
    >
      <section className="review-sheet review-comments-sheet flex h-full w-[min(600px,100%)] flex-col border-y-0 border-r-0">
        <div className="flex items-start gap-3 border-b border-neutral-800/80 px-5 py-5">
          <div className="min-w-0 flex-1">
            <h2 id="review-comments-title" className="text-lg font-semibold tracking-tight text-neutral-100">Comments</h2>
            <div className="mt-1 text-[12px] text-neutral-400">
              {handoffParts.length > 0
                ? handoffParts.join(" · ")
                : `${filterCounts.open} open${filterCounts.rereview > 0 ? ` · ${filterCounts.rereview} need re-review` : ""}`}
            </div>
            {feedbackStatus && feedbackStatus.open_total > 0 && (
              <details className="mt-3 text-[12px] leading-5 text-neutral-400">
                <summary className="cursor-pointer">How feedback works</summary>
                <p className="mt-2">
                {feedbackDelivery?.supported
                  ? `Flow: send to ${destination} → agent fixes and verifies → marks addressed → you re-review and resolve.`
                  : "Flow: copy/export feedback to the author → re-review their response → resolve only after you verify the fix."}
                </p>
              </details>
            )}
          </div>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close review comments" className="review-icon-button">
            <X size={14} />
          </button>
        </div>

        <div className="review-comments-filters flex items-center gap-2">
          <div className="min-w-0 flex-1 overflow-x-auto">
            <div className="review-segment w-max" role="tablist" aria-label="Comment status filters">
            {FILTERS.map((item) => (
              <button
                key={item.id}
                type="button"
                role="tab"
                aria-label={item.label}
                aria-selected={filter === item.id}
                tabIndex={filter === item.id ? 0 : -1}
                onKeyDown={(event) => {
                  const index = FILTERS.findIndex((option) => option.id === item.id);
                  const next = event.key === "ArrowRight" ? (index + 1) % FILTERS.length
                    : event.key === "ArrowLeft" ? (index + FILTERS.length - 1) % FILTERS.length
                      : event.key === "Home" ? 0 : event.key === "End" ? FILTERS.length - 1 : -1;
                  if (next < 0) return;
                  event.preventDefault();
                  setFilter(FILTERS[next].id);
                  (event.currentTarget.parentElement?.children[next] as HTMLButtonElement | undefined)?.focus();
                }}
                onClick={() => setFilter(item.id)}
                className={`review-segment-button inline-flex items-center gap-1.5 ${filter === item.id ? "review-segment-button-active" : ""}`}
              >
                {item.label}
                <span className="font-mono text-[10px] text-neutral-600">{filterCounts[item.id]}</span>
              </button>
            ))}
            </div>
          </div>
          <div className="sticky right-0 ml-auto shrink-0 bg-neutral-950 pl-2">
            <details ref={sourceMenuRef} data-review-dropdown className="group relative">
              <summary
                aria-label={`Comment source: ${SOURCE_FILTERS.find((item) => item.id === sourceFilter)?.label ?? "All sources"}`}
                className="flex h-7 min-w-[132px] cursor-pointer list-none items-center gap-2 rounded-md border border-neutral-800 bg-neutral-900/70 px-2.5 text-[10px] font-medium text-neutral-300 shadow-sm outline-none transition-colors hover:border-neutral-700 hover:bg-neutral-900 focus-visible:border-violet-700 [&::-webkit-details-marker]:hidden"
              >
                <span className="min-w-0 flex-1 truncate text-left">
                  {SOURCE_FILTERS.find((item) => item.id === sourceFilter)?.label ?? "All sources"}
                </span>
                <span className="font-mono text-[9px] text-neutral-600">{sourceCounts[sourceFilter]}</span>
                <ChevronDown size={11} className="shrink-0 text-neutral-600 transition-transform group-open:rotate-180" aria-hidden="true" />
              </summary>
              <div
                role="listbox"
                aria-label="Comment source options"
                className="absolute right-0 top-full z-50 mt-1 w-44 overflow-hidden rounded-md border border-neutral-800 bg-neutral-950 p-1 shadow-2xl shadow-black/45"
              >
                {SOURCE_FILTERS.map((item) => {
                  const selected = sourceFilter === item.id;
                  return (
                    <button
                      key={item.id}
                      type="button"
                      role="option"
                      aria-selected={selected}
                      onClick={() => {
                        setSourceFilter(item.id);
                        sourceMenuRef.current?.removeAttribute("open");
                      }}
                      className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[10px] transition-colors ${selected ? "bg-violet-500/10 text-violet-200" : "text-neutral-400 hover:bg-neutral-900 hover:text-neutral-200"}`}
                    >
                      <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center">
                        {selected && <Check size={11} aria-hidden="true" />}
                      </span>
                      <span className="min-w-0 flex-1 truncate">{item.label}</span>
                      <span className="font-mono text-[9px] text-neutral-600">{sourceCounts[item.id]}</span>
                    </button>
                  );
                })}
              </div>
            </details>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-auto px-5 py-2">
          {visible.length === 0 && <div className="review-comments-empty"><MessageSquareText size={24} strokeWidth={1.5} aria-hidden="true" /><p>No matching comments.</p></div>}
          <div className="divide-y divide-neutral-800/70">
            {visible.map((item) => {
              const canJump = item.anchored && item.state !== "orphaned" && Boolean(item.path);
              const orphaned = item.state === "orphaned" || !item.anchored;
              const reReview = item.author_response === "addressed" && item.state !== "resolved" && item.state !== "obsolete";
              const replyCount = replies.get(item.id) ?? 0;
              const StateIcon = orphaned ? AlertTriangle : reReview ? RefreshCw : item.state === "resolved" ? CheckCircle2 : MessageSquareText;
              const stateTone = orphaned
                ? "border-amber-900/50 bg-amber-950/15 text-amber-300"
                : reReview
                  ? "border-sky-900/55 bg-sky-950/15 text-sky-300"
                  : item.state === "resolved"
                    ? "border-emerald-900/45 bg-emerald-950/10 text-emerald-400"
                    : "border-neutral-800 bg-neutral-900/35 text-neutral-400";
              return (
                <article key={item.id} className="review-comment-list-item py-5">
                  <div className="flex flex-wrap items-center gap-2 text-[12px]">
                    <span className="font-medium text-neutral-200">{item.created_by || item.source_id || "unknown"}</span>
                    <span className="rounded-full border border-neutral-800 bg-neutral-900/50 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.08em] text-neutral-500">
                      {sourceLabel(item)}
                    </span>
                    {item.kind !== "comment" && <span className="capitalize text-neutral-500">{item.kind.replace("_", " ")}</span>}
                    {item.origin_revision_number != null && (
                      <span
                        className="font-mono text-[10px] text-neutral-500"
                        title={`Created on immutable Snapshot ${item.origin_revision_number}`}
                      >
                        {item.relation_to_revision === "inherited" ? "from " : ""}Snapshot {item.origin_revision_number}
                      </span>
                    )}
                    {orphaned || reReview || item.state === "resolved" ? (
                      <span className={`inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 font-medium ${stateTone}`}>
                        <StateIcon size={9} aria-hidden="true" />
                        {orphaned ? "orphaned" : reReview ? "re-review" : "resolved"}
                      </span>
                    ) : null}
                    {replyCount > 0 && (
                      <span className="ml-auto inline-flex items-center gap-1 text-neutral-600">
                        <CornerUpRight size={10} aria-hidden="true" />
                        {replyCount} repl{replyCount === 1 ? "y" : "ies"}
                      </span>
                    )}
                  </div>
                  {item.title && <h3 className="mt-3 text-[14px] font-semibold text-neutral-100">{item.title}</h3>}
                  <div className="review-thread-body mt-2">{item.body}</div>
                  {reReview && (
                    <div className="mt-2 rounded-md border border-sky-900/35 bg-sky-950/10 px-2.5 py-1.5 text-[10px] text-sky-300/85">
                      {feedbackDelivery?.supported ? "Coding agent marked this addressed." : "Author says addressed."}
                      {item.author_addressed_revision_number != null ? ` Addressed in Snapshot ${item.author_addressed_revision_number}.` : ""}
                      {" "}This stays open until you re-review and resolve it.
                    </div>
                  )}
                  <div className="mt-3 flex min-w-0 flex-wrap items-center gap-2 text-[11px]">
                    <span className="min-w-0 flex-1 truncate font-mono text-neutral-400" title={item.path}>
                      {item.path || "No current path"}{item.start_line > 0
                        ? `:${item.start_line}${item.end_line > item.start_line ? `-${item.end_line}` : ""}`
                        : ""}
                    </span>
                    {canJump ? (
                      <button type="button" onClick={() => onJump(item)} className="review-compact-action text-sky-400 hover:text-sky-300">
                        Show in review
                      </button>
                    ) : (
                      <span className="text-amber-500/80">Not anchored in current Snapshot</span>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        </div>
      </section>
    </div>
  );
}
