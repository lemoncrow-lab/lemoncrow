import { Select, SelectItem } from "../components/ui/select";
import { Button as UiButton } from "../components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetHeader, SheetTitle } from "../components/ui/sheet";
import { ToggleGroup } from "../components/ui/toggle-group";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Bot, CircleDot, GitCompareArrows, UserRound, X } from "lucide-react";

import "./reviewUi.css";
import { useRuntimeCapabilities } from "../lib/RuntimeCapabilities";
import {
  fetchFixLines,
  fetchReviewActivity,
  fetchRevisions,
  historicalReviewHref,
  reviewCompareHref,
  sourceCompareHref,
} from "./reviewApi";
import ReviewFixLines from "./ReviewFixLines";
import { isMutableReview, shortBase } from "./ReviewBaseIdentity";
import type { FixLine, ReviewActivityEvent, RevisionInfo } from "./types";

type HistoryTab = "activity" | "revisions";

interface ReviewHistorySheetProps {
  reviewId: string;
  selectedRevisionNumber: number;
  historical: boolean;
  /** Shared Review identity, not a second base authority. */
  rangeMode?: string;
  reviewBaseSha?: string | null;
  initialTab?: HistoryTab;
  canFix?: boolean;
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
    tone: "border-border bg-muted/50 text-neutral-400",
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
  rangeMode = "",
  reviewBaseSha,
  initialTab = "revisions",
  canFix = false,
  onClose,
}: ReviewHistorySheetProps) {
  const { localMachine } = useRuntimeCapabilities();
  const [rows, setRows] = useState<RevisionInfo[]>([]);
  const [events, setEvents] = useState<ReviewActivityEvent[]>([]);
  const [fixLines, setFixLines] = useState<FixLine[]>([]);
  const [tab, setTab] = useState<HistoryTab>(initialTab);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [compareBase, setCompareBase] = useState<number | null>(null);
  const [compareTarget, setCompareTarget] = useState<number | null>(null);

  const loadHistory = useCallback(async () => {
    setLoading(true);
    try {
      const [revisionResult, activityResult, fixResult] = await Promise.all([
        fetchRevisions(reviewId),
        fetchReviewActivity(reviewId),
        localMachine
          ? fetchFixLines(reviewId).catch(() => ({ fix_lines: [], fix_of: null }))
          : Promise.resolve({ fix_lines: [], fix_of: null }),
      ]);
      setRows([...revisionResult.revisions].sort((a, b) => b.revision_number - a.revision_number));
      setEvents([...activityResult.events].reverse());
      setFixLines(fixResult.fix_lines);
      setError("");
    } catch (reason: unknown) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setLoading(false);
    }
  }, [localMachine, reviewId]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  // Snapshot history is append-only: ordinary captures remain inspectable; feedback changes emphasis, not visibility.
  const visibleRows = rows;
  const latest = visibleRows[0] ?? null;
  const latestRevision = latest?.revision_number ?? 0;
  const latestSaved = visibleRows.find((row) => row.capture_kind !== "draft" || row.is_published_snapshot) ?? null;
  const runningDraft = latest?.capture_kind === "draft" && !latest.is_published_snapshot ? latest : null;
  const selectedRow = rows.find((row) => row.revision_number === selectedRevisionNumber);
  const rowById = useMemo(() => new Map(rows.map((row) => [row.id, row])), [rows]);
  const visibleEvents = events.filter((event) => {
    const row = rowById.get(event.revision_id);
    return event.kind !== "revision.recorded" || row?.capture_kind !== "draft" || row.is_published_snapshot || row.has_review_feedback;
  });
  const defaultCompareBase = visibleRows.find((row) => (row.has_review_feedback || row.is_last_commented) && row.revision_number < latestRevision)?.revision_number
    ?? latestRevision;
  const activeCompareBase = compareBase ?? defaultCompareBase;
  const activeCompareTarget = compareTarget ?? latestRevision;
  const advancedCompareHref = activeCompareBase !== activeCompareTarget
    ? reviewCompareHref(reviewId, activeCompareBase, activeCompareTarget)
    : "";
  const revisionById = useMemo(() => new Map(rows.map((row) => [row.id, row.revision_number])), [rows]);
  const fixLineBySnapshot = useMemo(() => {
    const byId = new Map<string, FixLine>();
    for (const line of fixLines) {
      const current = byId.get(line.snapshot_revision_id);
      const isOpen = line.status === "fixing" || line.status === "applied_with_conflicts";
      if (!current || isOpen) byId.set(line.snapshot_revision_id, line);
    }
    return byId;
  }, [fixLines]);

  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent className="w-[min(560px,100%)]">
        <SheetHeader className="items-center">
          <div className="min-w-0 flex-1">
            <SheetTitle>History</SheetTitle>
            <div className="mt-0.5 text-caption text-muted-foreground">
              Viewing {selectedRow?.capture_kind === "draft" && !selectedRow.is_published_snapshot ? "working changes" : selectedRow ? `Snapshot ${selectedRow.revision_number}` : `Snapshot ${selectedRevisionNumber}`}{latestSaved && latestSaved.revision_number !== selectedRevisionNumber ? ` · newest saved Snapshot ${latestSaved.revision_number}` : ""}
            </div>
          </div>
          <SheetClose asChild>
            <UiButton variant="ghost" size="icon" type="button" aria-label="Close review history">
              <X size={14} />
            </UiButton>
          </SheetClose>
        </SheetHeader>

        <div className="flex shrink-0 border-b border-border/70 px-4 py-2">
          <ToggleGroup
            variant="segment"
            aria-label="Review history sections"
            itemClassName="inline-flex items-center gap-1.5 capitalize"
            value={tab}
            onChange={(value) => setTab(value as HistoryTab)}
            options={(["activity", "revisions"] as const).map((item) => ({
              value: item,
              label: (
                <>
                  {item === "revisions" ? "Snapshots" : "Activity"}
                  <span className="font-mono text-caption text-muted-foreground" aria-hidden="true">{item === "activity" ? visibleEvents.length : visibleRows.length}</span>
                </>
              ),
            }))}
          />
        </div>
        {!loading && !error && runningDraft && (
          <div className="mx-4 mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-border bg-muted/30 px-3 py-2.5" role="status">
            <div className="min-w-0 flex-1">
              <div className="text-label font-medium text-foreground">Running draft</div>
              <div className="mt-0.5 text-caption text-muted-foreground">
                {latestSaved
                  ? `Working changes since Snapshot ${latestSaved.revision_number}. Read-only until you create a Snapshot.`
                  : "Working changes. No saved Snapshot yet."}
              </div>
            </div>
            <UiButton asChild variant="ghost" size="compact">
              <a href={historicalReviewHref(reviewId, runningDraft.revision_number)}>Open draft</a>
            </UiButton>
          </div>
        )}
        <div className="px-4 py-1.5 text-caption text-muted-foreground">
          {tab === "revisions" ? "Saved Snapshots from Review captures and commits. Open one to inspect it." : "Review actions and decisions in time order."}
          {tab === "revisions" && isMutableReview(rangeMode) && (
            <div className="mt-1" data-testid="history-review-base">
              {reviewBaseSha?.trim()
                ? <span title={`Recorded Review base ${reviewBaseSha}`}>Review base <span className="font-mono text-neutral-300">{shortBase(reviewBaseSha.trim())}</span> · each Snapshot may use a later integration base</span>
                : <span className="font-medium text-neutral-300">Base not frozen · captures may compare against a different HEAD</span>}
            </div>
          )}
        </div>
        <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
          {loading && rows.length === 0 && events.length === 0 && !error && (
            <div className="py-8 text-center text-caption text-muted-foreground" role="status">Loading history…</div>
          )}
          {error && (
            <div className="flex items-start gap-3 border-y border-rose-900/45 bg-rose-950/10 px-3 py-2.5">
              <div className="min-w-0 flex-1">
                <div className="text-caption font-medium text-rose-300">Could not load history</div>
                <details className="mt-1 text-caption text-muted-foreground"><summary className="cursor-pointer">Error details</summary><div className="mt-1 font-mono text-rose-300/75">{error}</div></details>
              </div>
              <UiButton size="xs" type="button" disabled={loading} onClick={() => void loadHistory()}>Retry</UiButton>
            </div>
          )}

          {!loading && !error && tab === "activity" && visibleEvents.length === 0 && (
            <div className="py-10 text-center text-label text-muted-foreground">No recorded review activity.</div>
          )}
          {!loading && !error && tab === "activity" && (
            <div className="space-y-0">
              {visibleEvents.map((event) => {
                const actor = actorPresentation(event);
                const revisionNumber = revisionById.get(event.revision_id);
                const reason = eventReason(event);
                return (
                  <div key={event.id} className="relative border-l border-border/70 py-3.5 pl-5">
                    <span className="absolute -left-[5px] top-[18px] h-2.5 w-2.5 rounded-full border border-neutral-700 bg-background" />
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-caption ${actor.tone}`}>
                        {actor.icon === "human" ? <UserRound size={10} /> : actor.icon === "agent" ? <Bot size={10} /> : <CircleDot size={10} />}
                        {actor.label}
                      </span>
                      {revisionNumber !== undefined && <span className="font-mono text-caption text-sky-400/80">Snapshot {revisionNumber}</span>}
                      <span className="text-caption text-muted-foreground">{event.created_at ? new Date(event.created_at).toLocaleString() : ""}</span>
                    </div>
                    <div className="mt-1 text-label leading-relaxed text-neutral-200">{event.summary || "Review activity"}</div>
                    {reason && <div className="mt-1 text-caption leading-relaxed text-muted-foreground">{reason}</div>}
                    <details className="mt-1.5 text-caption text-muted-foreground">
                      <summary className="w-fit cursor-pointer select-none hover:text-neutral-50">Technical details</summary>
                      <div className="mt-1 flex flex-wrap gap-x-2 border-l border-border pl-2 font-mono">
                        <span>{event.kind}</span>
                        {event.subject_id && <span className="truncate">{event.subject_id}</span>}
                      </div>
                    </details>
                  </div>
                );
              })}
            </div>
          )}

          {!loading && !error && tab === "revisions" && visibleRows.length === 0 && (
            <div className="py-10 text-center text-label text-muted-foreground">No saved Snapshots yet. Run lc review or refresh the Review to save one.</div>
          )}
          {!loading && !error && tab === "revisions" && (
            <div className="space-y-1.5">
              <UiButton asChild variant="ghost" size="compact" className="mb-2">
                <a href={sourceCompareHref(
                  `git/${latest?.base_sha || "HEAD"}`,
                  selectedRow?.ref || latest?.ref || `r/${reviewId}`,
                  `r/${reviewId}`,
                )}>Compare Git commits and Snapshots</a>
              </UiButton>
              {visibleRows.length > 1 && (
                <details className="mb-3 rounded-md border border-border/80 bg-muted/20">
                  <summary className="cursor-pointer list-none px-2.5 py-2 text-caption font-medium text-muted-foreground hover:text-neutral-200 [&::-webkit-details-marker]:hidden">
                    Advanced comparison
                  </summary>
                  <div className="flex flex-wrap items-end gap-2 border-t border-border/70 px-2.5 py-2.5">
                    <label className="min-w-[130px] flex-1 text-micro uppercase tracking-[0.08em] text-muted-foreground">
                      From
                      <Select
                        aria-label="Compare from Snapshot"
                        value={activeCompareBase}
                        onValueChange={(value) => setCompareBase(Number(value))}
                        uiSize="xs"
                        className="mt-1 w-full py-0 normal-case tracking-normal"
                      >
                        {visibleRows.map((row) => (
                          <SelectItem key={row.id} value={row.revision_number}>Snapshot {row.revision_number}</SelectItem>
                        ))}
                      </Select>
                    </label>
                    <label className="min-w-[130px] flex-1 text-micro uppercase tracking-[0.08em] text-muted-foreground">
                      To
                      <Select
                        aria-label="Compare to Snapshot"
                        value={activeCompareTarget}
                        onValueChange={(value) => setCompareTarget(Number(value))}
                        uiSize="xs"
                        className="mt-1 w-full py-0 normal-case tracking-normal"
                      >
                        {visibleRows.map((row) => (
                          <SelectItem key={row.id} value={row.revision_number}>Snapshot {row.revision_number}</SelectItem>
                        ))}
                      </Select>
                    </label>
                    {advancedCompareHref ? (
                      <UiButton asChild variant="default" className="px-3 text-caption">
                        <a href={advancedCompareHref}>
                          <GitCompareArrows size={11} aria-hidden="true" />
                          Compare
                        </a>
                      </UiButton>
                    ) : (
                      <span className="inline-flex h-8 items-center px-3 text-caption text-muted-foreground">Choose two Snapshots</span>
                    )}
                  </div>
                </details>
              )}
              {visibleRows.map((row) => {
                const selected = row.revision_number === selectedRevisionNumber;
                const isLatest = row.id === latest?.id;
                const hasFeedback = row.has_review_feedback || row.is_last_commented;
                const captureLabel = `${row.capture_kind === "draft" && !row.is_published_snapshot ? "Draft capture" : "Snapshot"} ${row.revision_number}`;
                const href = historicalReviewHref(reviewId, row.revision_number);
                const compareHref = latest && !isLatest
                  ? reviewCompareHref(reviewId, row.revision_number, latest.revision_number)
                  : "";
                return (
                  <div key={row.id} className={`rounded-md border px-2.5 py-2.5 transition-colors ${selected ? "border-border bg-muted/45" : hasFeedback ? "border-border bg-muted/20" : "border-transparent hover:bg-muted/30"}`}>
                    <div className="flex min-w-0 items-start gap-2">
                      <a href={href} className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-label font-medium text-neutral-200">{captureLabel}</span>
                          {row.title && <span className="max-w-[26rem] truncate text-caption text-neutral-300">{row.title}</span>}
                          {row.capture_kind === "commit" && <span className="text-caption text-muted-foreground">Commit {row.commit_number ?? ""}{row.commit_sha ? ` · ${row.commit_sha.slice(0, 8)}` : ""}</span>}
                          {row.id === latestSaved?.id && <span className="text-caption text-sky-400">Newest saved</span>}
                          {isLatest && runningDraft && <span className="text-caption text-muted-foreground">Running draft</span>}
                          {row.is_last_reviewed && <span className="text-caption text-emerald-400/80">Last reviewed</span>}
                          {hasFeedback && <span className="text-caption text-violet-400/80">{row.is_last_commented ? "Latest feedback" : "Review feedback"}</span>}
                          {row.capture_kind === "commit" && row.commit_state === "superseded" && (
                            <span className="text-caption text-amber-400/80">Rewritten</span>
                          )}
                          {selected && <span className="text-caption text-muted-foreground">Viewing</span>}
                          {row.dirty && <span className="text-caption text-amber-500/80">working tree at capture</span>}
                        </div>
                        <div className="mt-1 flex flex-wrap gap-x-3 text-caption text-muted-foreground">
                          <span>{row.created_at ? new Date(row.created_at).toLocaleString() : "Captured Snapshot"}</span>
                          {row.base_moved && row.semantic_changed === false && (
                            <span className="text-sky-400/80">Base moved · review areas unchanged</span>
                          )}
                          {row.base_moved && row.semantic_changed === true && (
                            <span className="text-muted-foreground">Base moved · review areas also changed</span>
                          )}
                          {isMutableReview(rangeMode) && row.base_sha && (
                            <span className="font-mono" title={`Effective Snapshot base ${row.base_sha}`}>
                              Snapshot base {shortBase(row.base_sha)}
                            </span>
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
                            ? <span className="font-mono">Provider {row.head_sha.slice(0, 10)}</span>
                            : row.dirty
                              ? <span>working changes</span>
                              : null}
                        </div>
                      </a>
                      <div className="flex shrink-0 flex-wrap justify-end gap-1">
                        <UiButton asChild variant="ghost" size="compact">
                          <a href={href} aria-label={`Open ${captureLabel}`}>Open</a>
                        </UiButton>
                        {compareHref && (
                          <UiButton asChild variant="ghost" size="compact" className="text-sky-300/80">
                            <a href={compareHref}>
                              <GitCompareArrows size={11} aria-hidden="true" /> Compare to latest
                            </a>
                          </UiButton>
                        )}
                      </div>
                    </div>
                    <ReviewFixLines
                      reviewId={reviewId}
                      revisionId={row.id}
                      snapshotNumber={row.revision_number}
                      line={fixLineBySnapshot.get(row.id) ?? null}
                      canFix={canFix && localMachine}
                      onChanged={() => void loadHistory()}
                    />
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
