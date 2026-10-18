import { Button as UiButton } from "../components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu";
import { Sheet, SheetClose, SheetContent, SheetHeader, SheetTitle } from "../components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, CornerUpRight, MessageSquareText, RefreshCw, X } from "lucide-react";

import "./reviewUi.css";
import { annotationIdentityColor, annotationIdentityLabel, annotationProducerDetail, annotationProducerTitle, annotationSource, isUnanchoredComment } from "./annotationModel";
import { BelowThresholdList } from "./findingThreshold";
import { handoffDestinationLabel, handoffToCodingAgent } from "./reviewPrimaryAction";
import type { Annotation, FeedbackDeliveryCapability, FeedbackStatus } from "./types";

export type CommentsFilter = "open" | "rereview" | "resolved" | "orphaned" | "all";
type Filter = CommentsFilter;
type SourceFilter = "all" | "human" | "ai" | "lemoncrow" | `agent:${string}`;

interface ReviewCommentsSheetProps {
  annotations: Annotation[];
  feedbackStatus?: FeedbackStatus;
  feedbackDelivery?: FeedbackDeliveryCapability | null;
  onClose(): void;
  onJump(annotation: Annotation): void;
  /**
   * The tab to open on. Without one the sheet opens on Open, or on Re-review
   * when every comment awaiting the reviewer is an addressed one Open cannot
   * list (its lines were rewritten, so it is orphaned rather than open).
   */
  initialFilter?: CommentsFilter;
  /** Resolve/reopen an unanchored comment. Absent when judgments are read-only. */
  onSetAnnotationState?(id: string, state: "open" | "resolved"): void;
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

function awaitsRereview(annotation: Annotation): boolean {
  return annotation.author_response === "addressed" && annotation.state !== "resolved" && annotation.state !== "obsolete";
}

function matchesSource(annotation: Annotation, filter: SourceFilter): boolean {
  if (filter === "all") return true;
  if (filter.startsWith("agent:")) {
    return sourceGroup(annotation) === "ai" && `agent:${annotation.agent_role || "unknown"}` === filter;
  }
  return sourceGroup(annotation) === filter;
}

export default function ReviewCommentsSheet({
  annotations,
  feedbackStatus,
  feedbackDelivery,
  onClose,
  onJump,
  initialFilter,
  onSetAnnotationState,
}: ReviewCommentsSheetProps) {
  const [filter, setFilter] = useState<Filter>(() => {
    if (initialFilter) return initialFilter;
    const rootItems = annotations.filter((item) => !item.parent_id);
    return !rootItems.some((item) => item.state === "open") && rootItems.some(awaitsRereview) ? "rereview" : "open";
  });
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>("all");
  const closeRef = useRef<HTMLButtonElement>(null);
  const sourceFilters = useMemo(() => [
    ...SOURCE_FILTERS,
    ...[...new Set(annotations.filter((item) => sourceGroup(item) === "ai").map((item) => item.agent_role || "unknown"))]
      .sort().map((role) => ({ id: `agent:${role}` as SourceFilter, label: `Agent(${role})` })),
  ], [annotations]);

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
    rereview: roots.filter(awaitsRereview).length,
    resolved: roots.filter((item) => item.state === "resolved").length,
    orphaned: roots.filter(isUnanchoredComment).length,
  }), [roots]);
  const destination = handoffDestinationLabel(feedbackDelivery);
  const toAgent = handoffToCodingAgent(feedbackDelivery);
  const handoffParts = feedbackStatus ? [
    feedbackStatus.unpublished > 0 ? `${feedbackStatus.unpublished} not sent` : "",
    feedbackStatus.in_flight > 0 ? `${feedbackStatus.in_flight} delivery pending` : "",
    feedbackStatus.published > 0 ? `${feedbackStatus.published} sent to ${destination}` : "",
    feedbackStatus.addressed > 0 ? `${feedbackStatus.addressed} ready for re-review` : "",
  ].filter(Boolean) : [];
  const statusVisible = roots.filter((item) => {
    if (filter === "all") return true;
    if (filter === "rereview") return awaitsRereview(item);
    if (filter === "orphaned") return isUnanchoredComment(item);
    return item.state === filter;
  });
  const sourceCounts = Object.fromEntries(sourceFilters.map((item) => [
    item.id, statusVisible.filter((annotation) => matchesSource(annotation, item.id)).length,
  ]));
  const visible = statusVisible.filter((item) => matchesSource(item, sourceFilter));

  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent
        className="w-[min(600px,100%)]"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          closeRef.current?.focus();
        }}
      >
        <SheetHeader className="items-start px-5 py-5">
          <div className="min-w-0 flex-1">
            <SheetTitle className="text-lg tracking-tight">Comments</SheetTitle>
            <div className="mt-1 text-body text-neutral-400">
              {handoffParts.length > 0
                ? handoffParts.join(" · ")
                : `${filterCounts.open} open${filterCounts.rereview > 0 ? ` · ${filterCounts.rereview} need re-review` : ""}`}
            </div>
            {feedbackStatus && feedbackStatus.open_total > 0 && (
              <details className="mt-3 text-body leading-5 text-neutral-400">
                <summary className="cursor-pointer">How feedback works</summary>
                <p className="mt-2">
                {toAgent
                  ? `Flow: send to ${destination} → agent fixes and verifies → marks addressed → you re-review and resolve.`
                  : "Flow: copy/export feedback to the author → re-review their response → resolve only after you verify the fix."}
                </p>
              </details>
            )}
          </div>
          <SheetClose asChild>
            <UiButton ref={closeRef} variant="ghost" size="icon" type="button" aria-label="Close review comments">
              <X size={14} />
            </UiButton>
          </SheetClose>
        </SheetHeader>

        <Tabs value={filter} onValueChange={(value) => setFilter(value as Filter)} className="contents">
        <div className="review-comments-filters flex items-center gap-2">
          <div className="min-w-0 flex-1 overflow-x-auto">
            <TabsList aria-label="Comment status filters" className="flex gap-1 overflow-visible border-0 bg-transparent">
            {FILTERS.map((item) => (
              <TabsTrigger key={item.id} value={item.id} aria-label={item.label} className="rounded-md">
                {item.label}
                <span className="font-mono text-caption tabular-nums text-muted-foreground">{filterCounts[item.id]}</span>
              </TabsTrigger>
            ))}
            </TabsList>
          </div>
          <div className="sticky right-0 ml-auto shrink-0 bg-background pl-2">
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <UiButton
                  size="xs"
                  aria-label={`Comment source: ${sourceFilters.find((item) => item.id === sourceFilter)?.label ?? "All sources"}`}
                  className="min-w-[132px] justify-between gap-2 bg-muted/70 font-medium shadow-sm"
                >
                  <span className="min-w-0 flex-1 truncate text-left">
                    {sourceFilters.find((item) => item.id === sourceFilter)?.label ?? "All sources"}
                  </span>
                  <span className="font-mono text-micro text-muted-foreground">{sourceCounts[sourceFilter]}</span>
                  <ChevronDown size={11} className="shrink-0 text-muted-foreground" aria-hidden="true" />
                </UiButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" aria-label="Comment source options" className="w-44">
                <DropdownMenuRadioGroup value={sourceFilter} onValueChange={(value) => setSourceFilter(value as SourceFilter)}>
                  {sourceFilters.map((item) => (
                    <DropdownMenuRadioItem key={item.id} value={item.id}>
                      <span className="min-w-0 flex-1 truncate">{item.label}</span>
                      <span className="ml-auto font-mono text-micro text-muted-foreground">{sourceCounts[item.id]}</span>
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>

        <TabsContent value={filter} className="min-h-0 flex-1 overflow-auto px-5 py-2">
          {visible.length === 0 && <div className="review-comments-empty"><MessageSquareText size={24} strokeWidth={1.5} aria-hidden="true" /><p>No matching comments.</p></div>}
          <div className="divide-y divide-neutral-800/70">
            <BelowThresholdList items={visible} render={(item) => {
              const canJump = item.anchored && item.state !== "orphaned" && Boolean(item.path);
              const orphaned = isUnanchoredComment(item);
              const reReview = awaitsRereview(item);
              const replyCount = replies.get(item.id) ?? 0;
              const StateIcon = orphaned ? AlertTriangle : reReview ? RefreshCw : item.state === "resolved" ? CheckCircle2 : MessageSquareText;
              const stateTone = orphaned
                ? "border-amber-900/50 bg-amber-950/15 text-amber-300"
                : reReview
                  ? "border-sky-900/55 bg-sky-950/15 text-sky-300"
                  : item.state === "resolved"
                    ? "border-emerald-900/45 bg-emerald-950/10 text-emerald-400"
                    : "border-border bg-muted/35 text-neutral-400";
              return (
                <article key={item.id} className="review-comment-list-item py-5">
                  <div className="flex flex-wrap items-center gap-2 text-body">
                    <span className="font-medium text-neutral-200" title={annotationProducerTitle(item)}>{annotationProducerDetail(item) || "unknown"}</span>
                    <span className={`rounded-full border border-border bg-muted/50 px-1.5 py-0.5 text-micro font-semibold tracking-[0.08em] ${annotationIdentityColor(item)}`}>
                      {annotationIdentityLabel(item)}
                    </span>
                    {item.kind !== "comment" && <span className="capitalize text-muted-foreground">{item.kind.replace("_", " ")}</span>}
                    {item.origin_revision_number != null && (
                      <span
                        className="font-mono text-caption text-muted-foreground"
                        title={`Created on Snapshot ${item.origin_revision_number}`}
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
                      <span className="ml-auto inline-flex items-center gap-1 text-muted-foreground">
                        <CornerUpRight size={10} aria-hidden="true" />
                        {replyCount} repl{replyCount === 1 ? "y" : "ies"}
                      </span>
                    )}
                  </div>
                  {item.title && <h3 className="mt-3 text-title font-semibold text-foreground">{item.title}</h3>}
                  <div className="review-thread-body mt-2">{item.body}</div>
                  {item.rule_refs && item.rule_refs.length > 0 && (
                    <details className="mt-2 text-caption text-muted-foreground">
                      <summary className="cursor-pointer">Review rules · {item.rule_refs.length}</summary>
                      <div className="mt-1 space-y-0.5 pl-2 font-mono text-micro">
                        {item.rule_refs.map((ref) => <div key={ref} className="break-all">{ref}</div>)}
                      </div>
                    </details>
                  )}
                  {reReview && (
                    <div className="mt-2 rounded-md border border-sky-900/35 bg-sky-950/10 px-2.5 py-1.5 text-caption text-sky-300/85">
                      {/* Who claimed it is a fact of the record (the exact agent session
                          that marked it), not of whether the current Snapshot can be
                          sent to an agent, which changes as comments are resolved. */}
                      {item.author_response_source_id || toAgent ? "Coding agent marked this addressed." : "Author says addressed."}
                      {item.author_addressed_revision_number != null ? ` Addressed in Snapshot ${item.author_addressed_revision_number}.` : ""}
                      {" "}This stays open until you re-review and resolve it.
                    </div>
                  )}
                  <div className="mt-3 flex min-w-0 flex-wrap items-center gap-2 text-label">
                    <span className="min-w-0 flex-1 truncate font-mono text-neutral-400" title={item.path}>
                      {item.comment_scope === "change" ? (item.revision_id ? "Change discussion" : "Draft discussion") : item.path || "No current path"}{item.start_line > 0
                        ? `:${item.start_line}${item.end_line > item.start_line ? `-${item.end_line}` : ""}`
                        : ""}
                    </span>
                    {canJump ? (
                      <UiButton variant="ghost" size="compact" type="button" onClick={() => onJump(item)} className="text-sky-400 hover:text-sky-300">
                        Show in review
                      </UiButton>
                    ) : (
                      item.comment_scope !== "change" && <span className="text-amber-500/80">Not anchored in the current Snapshot</span>
                    )}
                    {orphaned && onSetAnnotationState && item.state !== "obsolete" && (
                      <UiButton
                        variant="ghost"
                        size="compact"
                        type="button"
                        aria-label={`${item.state === "resolved" ? "Reopen" : "Resolve"} unanchored comment`}
                        onClick={() => onSetAnnotationState(item.id, item.state === "resolved" ? "open" : "resolved")}
                      >
                        {item.state === "resolved" ? "Reopen" : "Resolve"}
                      </UiButton>
                    )}
                  </div>
                </article>
              );
            }} />
          </div>
        </TabsContent>
        </Tabs>
      </SheetContent>
    </Sheet>
  );
}
