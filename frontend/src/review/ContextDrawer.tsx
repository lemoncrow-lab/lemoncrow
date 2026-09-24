import { useEffect, useMemo, useRef, useState } from "react";
import { Pin, PinOff, X } from "lucide-react";

import { OrphanedComments } from "./AnnotationCard";
import {
  ArtifactCard,
  Discussion,
  Impact,
  PanelCard,
  Provenance,
  Verification,
  emptyProvenance,
} from "./ContextSections";
import { annotationSource, partition } from "./annotationModel";
import { targetDisplayLabel } from "./readerModel";
import { fetchTargetHistory } from "./reviewApi";
import type {
  Annotation,
  DegradedNote,
  FileDetail,
  ImpactSite,
  ReviewEvidence,
  ReviewMarkEvent,
  ReviewTarget,
} from "./types";

export type ContextDrawerTab = "impact" | "checks" | "evidence" | "author" | "discussion" | "history";

interface ContextDrawerProps {
  reviewId: string;
  target: ReviewTarget;
  detail: FileDetail | null;
  degraded: DegradedNote[];
  annotations: Annotation[];
  evidence: ReviewEvidence[];
  tab: ContextDrawerTab;
  pinned: boolean;
  uploading?: boolean;
  readOnly?: boolean;
  onTab(tab: ContextDrawerTab): void;
  onClose(): void;
  onTogglePin(): void;
  onOpenImpact(site: ImpactSite): void;
  onUploadEvidence(files: File[]): void;
  onAddPreview(url: string, title: string): void;
}

const PRIMARY_TABS: ContextDrawerTab[] = ["impact", "discussion", "checks"];
const DETAIL_TABS: ContextDrawerTab[] = ["evidence", "author", "history"];

function tabLabel(tab: ContextDrawerTab): string {
  if (tab === "discussion") return "Comments";
  if (tab === "author") return "Author & provenance";
  return tab[0].toUpperCase() + tab.slice(1);
}

function checkWeight(status: string): number {
  if (status === "FAIL") return 0;
  if (status === "UNKNOWN" || status === "NOT_RUN") return 1;
  if (status === "PASS") return 2;
  return 3;
}

/**
 * File-scoped checks before review-wide checks (spec §14.4).
 *
 * Strictly the second key, never the first: the same section orders by outcome
 * first, so a review-wide FAIL still sits above a file-scoped PASS. This only
 * decides between two checks that ended the same way, and there the one that
 * actually ran against this file is the one that says something about it — a
 * review-wide PASS is context, not proof about this target.
 *
 * A row with no scope recorded is treated as review-wide, matching how
 * `Verification` labels it.
 */
function scopeWeight(scope: "review" | "file" | undefined): number {
  return scope === "file" ? 0 : 1;
}

export default function ContextDrawer({
  reviewId,
  target,
  detail,
  degraded,
  annotations,
  evidence,
  tab,
  pinned,
  uploading = false,
  readOnly = false,
  onTab,
  onClose,
  onTogglePin,
  onOpenImpact,
  onUploadEvidence,
  onAddPreview,
}: ContextDrawerProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [previewUrl, setPreviewUrl] = useState("");
  const [history, setHistory] = useState<ReviewMarkEvent[]>([]);
  const [historyError, setHistoryError] = useState("");
  /**
   * The file's comments split the way spec §15.6 requires them to be read: the
   * ones that still point somewhere, and the ones that lost their anchor and
   * can only be listed. `Discussion` has no anchor filter of its own, so handing
   * it everything would mix an orphan into the anchored list under nothing but
   * its stale `L41` and the bare word "orphaned" — a guessed line presented as
   * a location, with the rung that failed to re-find it nowhere on screen.
   *
   * `fileAnnotations` below stays the whole set: both halves are on the
   * discussion tab, so the tab's count and the author/human slices are counted
   * over everything, in the order the server sent it.
   */
  const { anchored: anchoredComments, orphaned: orphanedComments } = useMemo(
    () => partition(annotations, target.path),
    [annotations, target.path],
  );
  const fileAnnotations = useMemo(
    () => annotations.filter((item) => item.path === target.path && !item.parent_id && item.state !== "obsolete"),
    [annotations, target.path],
  );
  const relevantEvidence = useMemo(
    () => evidence.filter((item) => !item.path || item.path === target.path),
    [evidence, target.path],
  );
  const currentEvidence = relevantEvidence.filter((item) => item.status === "current");
  const staleEvidence = relevantEvidence.filter((item) => item.status === "stale");
  const verification = [...(detail?.evidence ?? [])].sort(
    (left, right) =>
      checkWeight(left.status) - checkWeight(right.status) ||
      scopeWeight(left.scope) - scopeWeight(right.scope) ||
      left.name.localeCompare(right.name),
  );
  const authorNotes = fileAnnotations.filter((item) => annotationSource(item) === "author");
  const humanNotes = fileAnnotations.filter((item) => annotationSource(item) === "human");
  const failedChecks = verification.filter((row) => row.status === "FAIL").length;
  const unresolvedChecks = verification.filter((row) => row.status === "UNKNOWN" || row.status === "NOT_RUN").length;
  const passedChecks = verification.filter((row) => row.status === "PASS").length;
  const meaningfulReasons = target.reasons.filter((reason) => !/^\+\d+\s+-\d+$/.test(reason.trim()));
  const symbols = detail?.symbols ?? [];
  const targetLabel = targetDisplayLabel(target);

  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  /**
   * A preview URL is typed for the target on screen, and nothing remounts this
   * drawer when the reviewer steps to the next one — `ReviewReader` mounts it
   * without a key, so a pinned drawer keeps its state across `j`/`k`. Left
   * behind, a half-typed URL would be filed against whatever file happens to be
   * showing when Add is finally clicked: not a discarded draft but a piece of
   * evidence attached to the wrong file, with nothing on screen saying so.
   */
  useEffect(() => {
    setPreviewUrl("");
    setDragging(false);
  }, [target.path]);

  useEffect(() => {
    if (tab !== "history" || !reviewId || !target.unit_key) return;
    let cancelled = false;
    void fetchTargetHistory(reviewId, target.unit_key)
      .then((result) => {
        if (cancelled) return;
        setHistory(result.events);
        setHistoryError("");
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setHistory([]);
        setHistoryError(String(reason instanceof Error ? reason.message : reason));
      });
    return () => { cancelled = true; };
  }, [reviewId, tab, target.unit_key]);

  return (
    <aside className="flex h-full min-h-0 w-full flex-col bg-neutral-950" aria-label="Review context" role="complementary">
      <div className="shrink-0 border-b border-neutral-800 px-3 pt-2">
        <div className="flex items-start gap-2 pb-2">
          <div className="min-w-0 flex-1">
            <div className="truncate font-mono text-[11px] font-medium text-neutral-200" title={targetLabel}>{targetLabel}</div>
            <div className="mt-0.5 truncate font-mono text-[10px] text-neutral-600" title={target.path}>{target.path}</div>
          </div>
          <button
            type="button"
            onClick={onTogglePin}
            aria-pressed={pinned}
            aria-label={pinned ? "Unpin" : "Pin"}
            title={pinned ? "Unpin context drawer" : "Pin context drawer"}
            className={`review-icon-button h-7 w-7 ${pinned ? "text-sky-300" : ""}`}
          >
            {pinned ? <PinOff size={12} /> : <Pin size={12} />}
          </button>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close review context" className="review-icon-button h-7 w-7">
            <X size={13} />
          </button>
        </div>
        <div className="flex items-end gap-3 overflow-x-auto" role="tablist" aria-label="Review context sections">
          {PRIMARY_TABS.map((item) => {
            const count = item === "discussion"
              ? fileAnnotations.length
              : item === "checks"
                ? verification.filter((row) => row.status !== "PASS").length
                : (detail?.impact ?? []).length;
            return (
              <button
                key={item}
                id={`review-context-tab-${item}`}
                type="button"
                role="tab"
                aria-selected={tab === item}
                aria-controls="review-context-panel"
                onClick={() => onTab(item)}
                className={`shrink-0 border-b-2 pb-2 text-[10px] ${tab === item ? "border-sky-500 text-neutral-100" : "border-transparent text-neutral-500 hover:text-neutral-50"}`}
              >
                {tabLabel(item)}
                {count > 0 && <span className="ml-1 font-mono text-[10px] text-neutral-600">{count}</span>}
              </button>
            );
          })}
          {DETAIL_TABS.includes(tab) && (
            <button
              id={`review-context-tab-${tab}`}
              type="button"
              role="tab"
              aria-selected="true"
              aria-controls="review-context-panel"
              className="shrink-0 border-b-2 border-sky-500 pb-2 text-[10px] text-neutral-100"
            >
              {tabLabel(tab)}
            </button>
          )}
          <details data-review-dropdown className="relative shrink-0 pb-1.5">
            <summary
              aria-label="More review context"
              className="cursor-pointer list-none rounded px-1.5 py-0.5 text-[10px] text-neutral-600 hover:bg-neutral-900 hover:text-neutral-200 [&::-webkit-details-marker]:hidden"
            >
              Details
            </summary>
            <div className="review-menu absolute left-0 top-full z-50 mt-1 w-48">
              {DETAIL_TABS.map((item) => {
                const count = item === "evidence"
                  ? currentEvidence.length
                  : item === "history"
                    ? history.length
                    : authorNotes.length;
                return (
                  <button
                    key={item}
                    type="button"
                    onClick={(event) => {
                      onTab(item);
                      event.currentTarget.closest("details")?.removeAttribute("open");
                    }}
                    className="review-menu-item"
                  >
                    <span>{tabLabel(item)}</span>
                    {count > 0 && <span className="ml-auto font-mono text-[10px] text-neutral-600">{count}</span>}
                  </button>
                );
              })}
            </div>
          </details>
        </div>
      </div>

      <div
        id="review-context-panel"
        role="tabpanel"
        aria-labelledby={`review-context-tab-${tab}`}
        className="min-h-0 flex-1 overflow-y-auto px-3 py-3"
      >
        {tab === "impact" && (
          <div className="space-y-4">
            {meaningfulReasons.length > 0 && (
              <section>
                <div className="review-kicker mb-1.5">Why review this</div>
                <div className="space-y-1 text-[10px] leading-4 text-neutral-300">
                  {meaningfulReasons.map((reason) => <div key={reason}>{reason}</div>)}
                </div>
              </section>
            )}
            <section className="border-t border-neutral-900 pt-3 first:border-t-0 first:pt-0">
              <div className="review-kicker mb-2">Affected code</div>
              <Impact sites={detail?.impact ?? []} onOpen={onOpenImpact} />
            </section>
            {symbols.length > 0 && (
              <details className="border-t border-neutral-900 pt-3">
                <summary className="cursor-pointer text-[10px] text-neutral-500 hover:text-neutral-50">Definitions in this file · {symbols.length}</summary>
                <div className="mt-2 space-y-1.5 border-l border-neutral-800 pl-2">
                  {symbols.slice(0, 12).map((symbol) => (
                    <div key={`${symbol.qualified_name}:${symbol.start_line}`} className="flex items-baseline gap-2 text-[10px]">
                      <span className="min-w-0 flex-1 truncate font-mono text-neutral-300">{symbol.qualified_name || symbol.symbol_name}</span>
                      <span className="text-neutral-600">{symbol.change}</span>
                      <span className="font-mono text-neutral-600">{symbol.caller_count >= 0 ? `${symbol.caller_count} callers` : "? callers"}</span>
                    </div>
                  ))}
                  {symbols.length > 12 && <div className="text-[10px] text-neutral-600">+{symbols.length - 12} more</div>}
                </div>
              </details>
            )}
            {degraded.length > 0 && (
              <details className="border-t border-neutral-900 pt-3">
                <summary className="cursor-pointer text-[10px] text-amber-300/80">Analysis limitations · {degraded.length}</summary>
                <div className="mt-2 space-y-2 border-l border-neutral-800 pl-2">
                  {degraded.map((item) => <div key={item.name} className="text-[10px] leading-relaxed text-neutral-500"><span className="font-mono text-neutral-400">{item.name}</span><div>{item.note}</div></div>)}
                </div>
              </details>
            )}
          </div>
        )}

        {tab === "checks" && (
          <div className="space-y-3">
            {verification.length > 0 && (
              <div className="flex flex-wrap gap-2 text-[10px]">
                {failedChecks > 0 && <span className="text-rose-300">{failedChecks} failed</span>}
                {unresolvedChecks > 0 && <span className="text-amber-300">{unresolvedChecks} unresolved</span>}
                {passedChecks > 0 && <span className="text-emerald-300/70">{passedChecks} passed</span>}
              </div>
            )}
            <Verification rows={verification} />
            <div className="text-[10px] leading-relaxed text-neutral-600">
              Review-wide passes are context, not proof that this target is correct.
            </div>
          </div>
        )}

        {tab === "evidence" && (
          <div className="space-y-3">
            <PanelCard title="Current revision" action={<span className="font-mono text-[10px] text-neutral-600">{currentEvidence.length}</span>}>
              {currentEvidence.length === 0 ? (
                <div className="text-[10px] text-neutral-600">No current screenshots, video, traces, or previews attached.</div>
              ) : (
                <div className="space-y-2">{currentEvidence.map((artifact) => <ArtifactCard key={artifact.id} artifact={artifact} />)}</div>
              )}
            </PanelCard>
            {staleEvidence.length > 0 && (
              <details className="border-t border-neutral-800 pt-2">
                <summary className="cursor-pointer text-[10px] uppercase tracking-wider text-neutral-600">Previous revisions · {staleEvidence.length}</summary>
                <div className="mt-2 space-y-2">{staleEvidence.map((artifact) => <ArtifactCard key={artifact.id} artifact={artifact} />)}</div>
              </details>
            )}
            {readOnly ? (
              <div className="border border-neutral-800 px-2.5 py-2 text-[10px] text-neutral-500">Evidence is read-only until this review is restored.</div>
            ) : (
              <details className="border border-neutral-800 bg-neutral-900/20">
                <summary className="cursor-pointer px-2.5 py-2 text-[10px] uppercase tracking-wider text-neutral-500">Add evidence</summary>
                <div className="space-y-2 border-t border-neutral-800 p-2.5">
                  <label
                    onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
                    onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
                    onDragLeave={() => setDragging(false)}
                    onDrop={(event) => {
                      event.preventDefault();
                      setDragging(false);
                      const files = Array.from(event.dataTransfer.files);
                      if (files.length > 0) onUploadEvidence(files);
                    }}
                    className={`block cursor-pointer border border-dashed p-3 text-center focus-within:border-brand-400/35 ${dragging ? "border-sky-600 bg-sky-950/20" : "border-neutral-700"}`}
                  >
                    <input
                      ref={fileInputRef}
                      type="file"
                      multiple
                      aria-label="Attach evidence files"
                      className="sr-only"
                      accept="image/*,video/*,.zip,.pdf,.html,.txt"
                      onChange={(event) => {
                        const files = Array.from(event.target.files ?? []);
                        if (files.length > 0) onUploadEvidence(files);
                        event.currentTarget.value = "";
                      }}
                    />
                    <div className="text-[10px] text-neutral-300">{uploading ? "Adding evidence…" : "Drop screenshot, video, trace.zip, or document"}</div>
                  </label>
                  {/*
                    Dropping a file and clicking the drop zone are both pointer
                    gestures, and the file input behind the zone is display:none
                    and therefore not in the tab order, so without this button a
                    keyboard reviewer has no way to attach evidence at all.
                  */}
                  <button
                    type="button"
                    onClick={() => fileInputRef.current?.click()}
                    className="border border-neutral-700 px-2 py-1 text-[10px] text-neutral-400 hover:text-neutral-50"
                  >
                    Choose files
                  </button>
                  <div className="flex gap-1.5">
                    <input value={previewUrl} onChange={(event) => setPreviewUrl(event.target.value)} placeholder="http://localhost:3000/..." className="min-w-0 flex-1 border border-neutral-800 bg-neutral-950 px-2 py-1 text-[10px] text-neutral-300 outline-none focus:border-neutral-600" />
                    <button
                      type="button"
                      disabled={!previewUrl.trim()}
                      onClick={() => {
                        onAddPreview(previewUrl.trim(), "Live preview");
                        setPreviewUrl("");
                      }}
                      className="border border-neutral-700 px-2 text-[10px] text-neutral-400 disabled:opacity-40"
                    >
                      Add
                    </button>
                  </div>
                </div>
              </details>
            )}
          </div>
        )}

        {tab === "author" && (
          <div className="space-y-4">
            <section>
              <div className="review-kicker mb-2">Author rationale</div>
              {authorNotes.length === 0 ? (
                <div className="text-[10px] text-neutral-600">No exact author rationale is attached to this file.</div>
              ) : (
                <div className="space-y-2">
                  {authorNotes.map((note) => (
                    <div key={note.id} className="border-l border-amber-900/70 pl-2 text-[10px] leading-relaxed text-neutral-300">
                      {note.title && <div className="font-medium text-neutral-200">{note.title}</div>}
                      <div className="whitespace-pre-wrap">{note.body}</div>
                    </div>
                  ))}
                </div>
              )}
            </section>
            <section className="border-t border-neutral-900 pt-3">
              <div className="review-kicker mb-2">Authored by</div>
              <Provenance info={detail?.provenance ?? emptyProvenance()} />
            </section>
          </div>
        )}

        {tab === "discussion" && (
          <div className="space-y-3">
            {/*
              The badge hangs off the card, so it counts the card. The orphans
              are a sibling block outside it; counting them here made the header
              read "Discussion 2" over a single comment.
            */}
            <PanelCard title="Comments" action={<span className="font-mono text-[10px] text-neutral-600">{anchoredComments.length}</span>}>
              {anchoredComments.length === 0 && orphanedComments.length > 0 ? (
                <div className="text-[10px] text-neutral-600">Every comment on this file lost its anchor; they are listed below.</div>
              ) : (
                <Discussion annotations={anchoredComments} path={target.path} />
              )}
            </PanelCard>
            {/*
              The dedicated unresolved area spec §15.6 requires. `OrphanedComments`
              renders itself to nothing when the list is empty, and it is the only
              surface that names the rung that failed and the reason it failed, so
              the last known line reads as history rather than as a location.
            */}
            <OrphanedComments comments={orphanedComments} />
            {humanNotes.length === 0 && <div className="text-[10px] leading-relaxed text-neutral-600">Line comments remain inline in the diff. Human judgment remains distinct from AUTHOR, LEMONCROW, and AI REVIEW claims.</div>}
          </div>
        )}

        {tab === "history" && (
          <div className="space-y-3">
            <PanelCard title="Judgment history" action={<span className="font-mono text-[10px] text-neutral-600">{history.length}</span>}>
              {historyError ? (
                <div className="text-[10px] text-rose-300">{historyError}</div>
              ) : history.length === 0 ? (
                <div className="text-[10px] text-neutral-600">No recorded judgment transitions for this target yet.</div>
              ) : (
                <div className="space-y-2">
                  {[...history].reverse().map((event) => (
                    <div key={event.id} className="border-l border-neutral-800 pl-2 text-[10px]">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={event.actor_type === "human" ? "text-emerald-300" : event.actor_type === "agent" ? "text-violet-300" : "text-neutral-400"}>
                          {event.reviewer_id || (event.actor_type === "agent" ? "agent" : "LemonCrow")}
                        </span>
                        <span className="font-mono text-neutral-300">{event.from_state || "none"} → {event.to_state || "removed"}</span>
                        <span className="text-neutral-600">{event.event_kind}</span>
                      </div>
                      <div className="mt-0.5 text-[10px] text-neutral-600">{event.created_at ? new Date(event.created_at).toLocaleString() : ""}</div>
                      {event.reason && <div className="mt-1 leading-relaxed text-neutral-500">{event.reason}</div>}
                      {event.previous_unit_key && event.previous_unit_key !== event.unit_key && (
                        <div className="mt-1 font-mono text-[10px] text-neutral-600">moved from {event.previous_unit_key}</div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </PanelCard>
          </div>
        )}
      </div>
    </aside>
  );
}
