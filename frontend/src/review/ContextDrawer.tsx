import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { useEffect, useMemo, useRef, useState } from "react";
import { Code2, Pin, PinOff, X } from "lucide-react";

import type { CodeTarget } from "../lib/codeLinks";
import { useResolvedProductCapabilities } from "../lib/RuntimeCapabilities";

import { OrphanedComments } from "./AnnotationCard";
import ScreenshotEvidenceCompare from "./ScreenshotEvidenceCompare";
import ContextDevelopment, { fileDevelopmentCount } from "./ContextDevelopment";
import ReviewCodePeek from "./ReviewCodePeek";
import ReviewContextFiles from "./ReviewContextFiles";
import ArtifactAnalysesPanel from "./ArtifactAnalysesPanel";
import CheckRecipesPanel from "./CheckRecipesPanel";
import InteractivePreviewPanel from "./InteractivePreview";
import RunPlanPanel from "./RunPlanPanel";
import RuntimeObservationPanel from "./RuntimeObservationPanel";
import StaticFindingsPanel from "./StaticFindingsPanel";
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
  ReviewDevelopment,
  ReviewJournal,
  ReviewMarkEvent,
  ReviewTarget,
} from "./types";

export type ContextDrawerTab = "context" | "checks" | "discussion" | "activity";

interface ContextDrawerProps {
  reviewId: string;
  revisionNumber?: number;
  historical?: boolean;
  codeRoot?: string;
  target: ReviewTarget;
  detail: FileDetail | null;
  degraded: DegradedNote[];
  annotations: Annotation[];
  evidence: ReviewEvidence[];
  journal?: ReviewJournal | null;
  /** Change-level Development for this Snapshot; supersedes the legacy journal when it has stream evidence. */
  development?: ReviewDevelopment | null;
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
  onOpenDevelopment?(): void;
  /** Opens a location in Code (the working tree); absent without a trusted local checkout. */
  onOpenInCode?(target: CodeTarget): void;
  /** Goes to a Code Peek location inside this Review, onto the backtrail. */
  onJumpToCode?(target: CodeTarget): void;
  /** Resolve/reopen a comment, including one that lost its anchor. */
  onSetAnnotationState?(id: string, state: "open" | "resolved"): void;
  /** `projection.triage.hidden_finding_ids`: findings the finding threshold folds (imported ones by ref). */
  hiddenFindingIds?: readonly string[];
}

const ALL_TABS: ContextDrawerTab[] = ["context", "checks", "discussion", "activity"];

function tabLabel(tab: ContextDrawerTab): string {
  if (tab === "context") return "Context";
  if (tab === "checks") return "Checks";
  if (tab === "discussion") return "Discussion";
  return "Activity";
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
  revisionNumber = 0,
  historical = false,
  codeRoot = "",
  target,
  detail,
  degraded,
  annotations,
  evidence,
  journal = null,
  development = null,
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
  onOpenDevelopment,
  onOpenInCode,
  onJumpToCode,
  onSetAnnotationState,
  hiddenFindingIds,
}: ContextDrawerProps) {
  // Code Peek is a Runtime consumer: no entry point in a Review-only install,
  // nor while product discovery is still pending.
  const runtimeCode = useResolvedProductCapabilities()?.features["runtime.code"] === true;
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
  const screenshotPairs = useMemo(() => {
    const activeRevisionId = detail?.revision_id ?? "";
    const activeRevisionNumber = evidence.find((item) => item.revision_id === activeRevisionId)?.revision_number ?? null;
    const isScreenshot = (item: ReviewEvidence) =>
      (item.kind === "screenshot" || item.kind === "image")
      && item.mime_type.startsWith("image/")
      && Boolean(item.content_url);
    const key = (item: ReviewEvidence) => `${item.path}\u0000${item.title}`;
    const previous = new Map<string, ReviewEvidence[]>();
    for (const item of staleEvidence) {
      if (!isScreenshot(item) || !item.title) continue;
      const rows = previous.get(key(item)) ?? [];
      rows.push(item);
      previous.set(key(item), rows);
    }
    for (const rows of previous.values()) {
      rows.sort((left, right) =>
        (right.revision_number ?? -1) - (left.revision_number ?? -1)
        || right.created_at.localeCompare(left.created_at));
    }
    return currentEvidence.flatMap((after) => {
      if (!isScreenshot(after) || !after.title) return [];
      // Evidence status already uses reviewed-code identity, not row identity.
      // A carried artifact from an equivalent source generation is therefore
      // valid current proof for this Snapshot even when captured on an older
      // Snapshot row. The comparison labels the images only Before/After; it
      // does not name the revision either one was captured on.
      const before = (previous.get(key(after)) ?? []).find((candidate) => {
        if (candidate.revision_id === after.revision_id) return false;
        if (activeRevisionNumber != null && candidate.revision_number != null) {
          return candidate.revision_number < activeRevisionNumber;
        }
        if (after.revision_number == null || candidate.revision_number == null) return true;
        return candidate.revision_number < after.revision_number;
      });
      return before ? [{ before, after }] : [];
    });
  }, [currentEvidence, detail?.revision_id, staleEvidence]);
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
  const agentEntries = (journal?.entries ?? []).filter((item) => {
    if (!item.path) return true;
    const normalized = item.path.replace(/\\/g, "/").replace(/^\.\//, "");
    return normalized === target.path || normalized.endsWith(`/${target.path}`);
  });
  const agentPlan = agentEntries.filter((item) => item.kind === "plan");
  const agentDecisions = agentEntries.filter((item) => item.kind === "fact" || item.kind === "decision");
  const agentOpen = agentEntries.filter((item) => item.kind === "question" || item.kind === "blocker");
  const agentActivity = agentEntries.filter((item) => !["plan", "fact", "decision", "question", "blocker"].includes(item.kind));
  // A frozen legacy journal stays authoritative for the Snapshots it was
  // captured with; otherwise the Development stream owns this tab.
  const fileDevelopment =
    development && development.snapshot.source !== "legacy_journal" && (development.snapshot.available || !journal?.available)
      ? development
      : null;
  const agentSignalCount = fileDevelopment ? fileDevelopmentCount(fileDevelopment, target.path) : agentEntries.length;

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
    if (tab !== "activity" || !reviewId || !target.unit_key) return;
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

  const tabCount = (item: ContextDrawerTab): number => {
    if (item === "context") return (detail?.impact ?? []).length + authorNotes.length;
    if (item === "checks") return verification.filter((row) => row.status !== "PASS").length + currentEvidence.length;
    if (item === "discussion") return fileAnnotations.length;
    return agentSignalCount + history.length;
  };

  return (
    <aside className="flex h-full min-h-0 w-full flex-col bg-background" aria-label="Review context" role="complementary">
      <Tabs value={tab} onValueChange={(value) => onTab(value as ContextDrawerTab)} className="contents">
      <div className="shrink-0 border-b border-border px-2.5 pt-1.5">
        <div className="flex items-start gap-1.5 pb-1.5">
          <div className="min-w-0 flex-1">
            <div className="truncate font-mono text-label font-medium text-neutral-200" title={targetLabel}>{targetLabel}</div>
            <div className="mt-0.5 truncate font-mono text-caption text-muted-foreground" title={target.path}>{target.path}</div>
          </div>
          {onOpenInCode && detail?.status !== "deleted" && (
            <UiButton
              type="button"
              variant="ghost"
              size="icon"
              onClick={() => onOpenInCode({ path: target.path, line: target.start_line, endLine: target.end_line })}
              aria-label="Open in Code"
              tooltip="Open the working tree's current file in Code (current index)"
              className="h-7 w-7"
            >
              <Code2 size={12} />
            </UiButton>
          )}
          <UiButton
            type="button"
            variant="ghost"
            size="icon"
            onClick={onTogglePin}
            aria-pressed={pinned}
            aria-label={pinned ? "Unpin" : "Pin"}
            tooltip={pinned ? "Unpin context drawer" : "Pin context drawer"}
            className={`h-7 w-7 ${pinned ? "text-sky-300" : ""}`}
          >
            {pinned ? <PinOff size={12} /> : <Pin size={12} />}
          </UiButton>
          <UiButton ref={closeRef} type="button" variant="ghost" size="icon" onClick={onClose} aria-label="Close review context" className="h-7 w-7">
            <X size={13} />
          </UiButton>
        </div>
        <div className="flex min-w-0 items-end gap-1">
          <TabsList variant="underline" aria-label="Review context homes" className="flex min-w-0 flex-1 overflow-x-auto">
            {ALL_TABS.map((item) => {
              const count = tabCount(item);
              return (
                <TabsTrigger key={item} value={item} variant="underline" className="gap-1 data-[state=active]:border-sky-500">
                  {tabLabel(item)}
                  {count > 0 && <span className="font-mono text-caption leading-none text-muted-foreground">{count}</span>}
                </TabsTrigger>
              );
            })}
          </TabsList>
        </div>
      </div>

      <TabsContent value={tab} className="min-h-0 flex-1 overflow-y-auto px-2.5 py-2">
        {tab === "context" && (
          <div className="space-y-4">
            {meaningfulReasons.length > 0 && (
              <section>
                <div className="review-kicker mb-1.5">Why review this</div>
                <div className="space-y-1 text-caption leading-4 text-neutral-300">
                  {meaningfulReasons.map((reason) => <div key={reason}>{reason}</div>)}
                </div>
              </section>
            )}
            <section className="border-t border-neutral-900 pt-3 first:border-t-0 first:pt-0">
              <div className="review-kicker mb-2">Affected code</div>
              <Impact sites={detail?.impact ?? []} onOpen={onOpenImpact} />
            </section>
            {runtimeCode && codeRoot && target.symbol && (
              <ReviewCodePeek
                projectRoot={codeRoot}
                path={target.path}
                symbol={target.symbol}
                line={target.start_line}
                onJump={onJumpToCode}
                onOpenInCode={onOpenInCode}
              />
            )}
            <ReviewContextFiles
              reviewId={reviewId}
              revisionNumber={revisionNumber}
              historical={historical}
              onOpenInCode={onOpenInCode}
            />
            {symbols.length > 0 && (
              <details className="border-t border-neutral-900 pt-3">
                <summary className="cursor-pointer text-caption text-muted-foreground hover:text-neutral-50">Definitions in this file · {symbols.length}</summary>
                <div className="mt-2 space-y-1.5 border-l border-border pl-2">
                  {symbols.slice(0, 12).map((symbol) => (
                    <div key={`${symbol.qualified_name}:${symbol.start_line}`} className="flex items-baseline gap-2 text-caption">
                      <span className="min-w-0 flex-1 truncate font-mono text-neutral-300">{symbol.qualified_name || symbol.symbol_name}</span>
                      <span className="text-muted-foreground">{symbol.change}</span>
                      <span className="font-mono text-muted-foreground">{symbol.caller_count >= 0 ? `${symbol.caller_count} callers` : "? callers"}</span>
                      {onOpenInCode && symbol.start_line > 0 && detail?.status !== "deleted" && (
                        <UiButton
                          type="button"
                          variant="ghost"
                          size="icon"
                          onClick={() => onOpenInCode({ path: target.path, line: symbol.start_line, endLine: symbol.end_line })}
                          aria-label={`Open ${symbol.qualified_name || symbol.symbol_name} in Code`}
                          tooltip="Open in Code (current index)"
                          className="h-5 w-5 shrink-0 self-center"
                        >
                          <Code2 size={11} />
                        </UiButton>
                      )}
                    </div>
                  ))}
                  {symbols.length > 12 && <div className="text-caption text-muted-foreground">+{symbols.length - 12} more</div>}
                </div>
              </details>
            )}
            {degraded.length > 0 && (
              <details className="border-t border-neutral-900 pt-3">
                <summary className="cursor-pointer text-caption text-amber-300/80">Analysis limitations · {degraded.length}</summary>
                <div className="mt-2 space-y-2 border-l border-border pl-2">
                  {degraded.map((item) => <div key={item.name} className="text-caption leading-relaxed text-muted-foreground"><span className="font-mono text-neutral-400">{item.name}</span><div>{item.note}</div></div>)}
                </div>
              </details>
            )}
          </div>
        )}

        {tab === "activity" && fileDevelopment && (
          <ContextDevelopment
            reviewId={reviewId}
            development={fileDevelopment}
            path={target.path}
            onOpenDevelopment={onOpenDevelopment}
          />
        )}

        {tab === "activity" && !fileDevelopment && (
          <div className="space-y-4">
            {!journal?.available ? (
              <div className="text-caption leading-relaxed text-muted-foreground">
                {journal?.reason || "No exact author-session journal is attached to this Review yet."}
              </div>
            ) : (
              <>
                <section>
                  <div className="flex items-center gap-2">
                    <div className="review-kicker">Live author session</div>
                    <span className="font-mono text-micro text-muted-foreground">{journal.host || "agent"}</span>
                  </div>
                  {journal.session_id && (
                    <div className="mt-1 truncate font-mono text-micro text-muted-foreground" title={journal.session_id}>
                      {journal.session_id}
                    </div>
                  )}
                  {agentEntries.length === 0 && (
                    <div className="mt-2 text-caption text-muted-foreground">{journal.reason || "No review-relevant author activity is recorded yet."}</div>
                  )}
                </section>

                {agentPlan.length > 0 && (
                  <section className="border-t border-neutral-900 pt-3">
                    <div className="review-kicker mb-2">Plan</div>
                    <div className="space-y-2">
                      {agentPlan.map((item) => (
                        <div key={item.id} className="border-l border-sky-900/60 pl-2 text-caption leading-relaxed text-neutral-300">
                          <div>{item.summary}</div>
                          {item.detail && <div className="mt-1 whitespace-pre-wrap text-muted-foreground">{item.detail}</div>}
                        </div>
                      ))}
                    </div>
                  </section>
                )}

                {agentDecisions.length > 0 && (
                  <section className="border-t border-neutral-900 pt-3">
                    <div className="review-kicker mb-2">Decisions & facts</div>
                    <div className="space-y-1.5 text-caption leading-relaxed">
                      {agentDecisions.map((item) => (
                        <div key={item.id} className={`border-l pl-2 ${item.kind === "fact" ? "border-emerald-900/60 text-neutral-300" : "border-violet-900/60 text-neutral-400"}`}>
                          {item.summary}
                          {item.detail && <div className="mt-1 text-muted-foreground">{item.detail}</div>}
                        </div>
                      ))}
                    </div>
                  </section>
                )}

                {agentOpen.length > 0 && (
                  <section className="border-t border-neutral-900 pt-3">
                    <div className="review-kicker mb-2">Still open</div>
                    <div className="space-y-1.5 text-caption leading-relaxed">
                      {agentOpen.map((item) => (
                        <div key={item.id} className={item.kind === "blocker" ? "text-rose-300/90" : "text-amber-300/80"}>
                          {item.summary}
                        </div>
                      ))}
                    </div>
                  </section>
                )}

                <section className="border-t border-neutral-900 pt-3">
                  <div className="review-kicker mb-2">Relevant activity</div>
                  {agentActivity.length === 0 ? (
                    <div className="text-caption text-muted-foreground">No recorded agent activity is scoped to this file yet.</div>
                  ) : (
                    <div className="space-y-2">
                      {[...agentActivity].reverse().slice(0, 24).map((item) => (
                        <div key={item.id} className="border-l border-border pl-2 text-caption">
                          <div className="flex min-w-0 items-baseline gap-2">
                            <span className="shrink-0 font-mono text-muted-foreground">{item.kind}</span>
                            <span className="min-w-0 flex-1 truncate text-neutral-300" title={item.summary}>{item.summary}</span>
                            {item.status && (
                              <span className={item.status === "pass" ? "text-emerald-300" : item.status === "fail" ? "text-rose-300" : "text-muted-foreground"}>{item.status}</span>
                            )}
                          </div>
                          {item.detail && <div className="mt-1 max-h-28 overflow-auto whitespace-pre-wrap break-words font-mono text-micro leading-4 text-muted-foreground">{item.detail}</div>}
                          {item.at && <div className="mt-0.5 text-micro text-muted-foreground">{new Date(item.at).toLocaleTimeString()}</div>}
                        </div>
                      ))}
                    </div>
                  )}
                </section>

                <div className="border-t border-neutral-900 pt-2 text-micro leading-relaxed text-muted-foreground">
                  Review shows observable plans, decisions, tool/test evidence, messages, and edit activity. Hidden chain-of-thought is not captured.
                </div>
              </>
            )}
          </div>
        )}

        {tab === "checks" && (
          <div className="space-y-3">
            {verification.length > 0 && (
              <div className="flex flex-wrap gap-2 text-caption">
                {failedChecks > 0 && <span className="text-rose-300">{failedChecks} failed</span>}
                {unresolvedChecks > 0 && <span className="text-amber-300">{unresolvedChecks} unresolved</span>}
                {passedChecks > 0 && <span className="text-emerald-300/70">{passedChecks} passed</span>}
              </div>
            )}
            <Verification rows={verification} />
            <StaticFindingsPanel
              reviewId={reviewId}
              revisionId={detail?.revision_id ?? ""}
              path={target.path}
              readOnly={readOnly || historical}
              hiddenFindingIds={hiddenFindingIds}
            />
            <RunPlanPanel reviewId={reviewId} revisionId={detail?.revision_id ?? ""} readOnly={readOnly || historical} />
            <CheckRecipesPanel reviewId={reviewId} revisionId={detail?.revision_id ?? ""} readOnly={readOnly || historical} />
            <RuntimeObservationPanel reviewId={reviewId} revisionId={detail?.revision_id ?? ""} readOnly={readOnly || historical} />
            <ArtifactAnalysesPanel reviewId={reviewId} revisionId={detail?.revision_id ?? ""} readOnly={readOnly || historical} />
            {!historical && (
              <InteractivePreviewPanel reviewId={reviewId} revisionId={detail?.revision_id ?? ""} readOnly={readOnly} openedFrom="checks_rail" />
            )}
            <div className="text-caption leading-relaxed text-muted-foreground">
              Review-wide passes are context, not proof that this review area is correct.
            </div>
          </div>
        )}

        {tab === "checks" && (
          <div className="space-y-3">
            {screenshotPairs.map(({ before, after }) => (
              <ScreenshotEvidenceCompare key={`${before.id}:${after.id}`} before={before} after={after} />
            ))}
            <PanelCard title="Current Snapshot" action={<span className="font-mono text-caption text-muted-foreground">{currentEvidence.length}</span>}>
              {currentEvidence.length === 0 ? (
                <div className="text-caption text-muted-foreground">No current screenshots, video, traces, or previews attached.</div>
              ) : (
                <div className="space-y-2">{currentEvidence.map((artifact) => <ArtifactCard key={artifact.id} artifact={artifact} />)}</div>
              )}
            </PanelCard>
            {staleEvidence.length > 0 && (
              <details className="border-t border-border pt-2">
                <summary className="cursor-pointer text-caption uppercase tracking-wider text-muted-foreground">Previous Snapshots · {staleEvidence.length}</summary>
                <div className="mt-2 space-y-2">{staleEvidence.map((artifact) => <ArtifactCard key={artifact.id} artifact={artifact} />)}</div>
              </details>
            )}
            {readOnly ? (
              <div className="border border-border px-2.5 py-2 text-caption text-muted-foreground">Evidence is read-only until this review is restored.</div>
            ) : (
              <details className="border border-border bg-muted/20">
                <summary className="cursor-pointer px-2.5 py-2 text-caption uppercase tracking-wider text-muted-foreground">Add evidence</summary>
                <div className="space-y-2 border-t border-border p-2.5">
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
                    <UiInput unstyled
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
                    <div className="text-caption text-neutral-300">{uploading ? "Adding evidence…" : "Drop screenshot, video, trace.zip, or document"}</div>
                  </label>
                  {/*
                    Dropping a file and clicking the drop zone are both pointer
                    gestures, and the file input behind the zone is display:none
                    and therefore not in the tab order, so without this button a
                    keyboard reviewer has no way to attach evidence at all.
                  */}
                  <UiButton
                    type="button"
                    variant="outline"
                    size="xs"
                    onClick={() => fileInputRef.current?.click()}
                  >
                    Choose files
                  </UiButton>
                  <div className="flex gap-1.5">
                    <UiInput uiSize="xs" value={previewUrl} onChange={(event) => setPreviewUrl(event.target.value)} placeholder="http://localhost:3000/..." className="min-w-0 flex-1" />
                    <UiButton
                      type="button"
                      variant="outline"
                      size="xs"
                      disabled={!previewUrl.trim()}
                      onClick={() => {
                        onAddPreview(previewUrl.trim(), "Live preview");
                        setPreviewUrl("");
                      }}
                    >
                      Add
                    </UiButton>
                  </div>
                </div>
              </details>
            )}
          </div>
        )}

        {tab === "context" && (
          <div className="space-y-4">
            <section>
              <div className="review-kicker mb-2">Author rationale</div>
              {authorNotes.length === 0 ? (
                <div className="text-caption text-muted-foreground">No exact author rationale is attached to this file.</div>
              ) : (
                <div className="space-y-2">
                  {authorNotes.map((note) => (
                    <div key={note.id} className="border-l border-amber-900/70 pl-2 text-caption leading-relaxed text-neutral-300">
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
            <PanelCard title="Comments" action={<span className="font-mono text-caption text-muted-foreground">{anchoredComments.length}</span>}>
              {anchoredComments.length === 0 && orphanedComments.length > 0 ? (
                <div className="text-caption text-muted-foreground">Every comment on this file lost its anchor; they are listed below.</div>
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
            <OrphanedComments comments={orphanedComments} onSetState={readOnly ? undefined : onSetAnnotationState} />
            {humanNotes.length === 0 && <div className="text-caption leading-relaxed text-muted-foreground">Line comments remain inline in the diff. Human judgment remains distinct from AUTHOR, LEMONCROW, and AI REVIEW claims.</div>}
          </div>
        )}

        {tab === "activity" && (
          <div className="space-y-3">
            <PanelCard title="Judgment history" action={<span className="font-mono text-caption text-muted-foreground">{history.length}</span>}>
              {historyError ? (
                <div className="text-caption text-rose-300">{historyError}</div>
              ) : history.length === 0 ? (
                <div className="text-caption text-muted-foreground">No recorded judgment transitions for this review area yet.</div>
              ) : (
                <div className="space-y-2">
                  {[...history].reverse().map((event) => (
                    <div key={event.id} className="border-l border-border pl-2 text-caption">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={event.actor_type === "human" ? "text-emerald-300" : event.actor_type === "agent" ? "text-violet-300" : "text-neutral-400"}>
                          {event.reviewer_id || (event.actor_type === "agent" ? "agent" : "LemonCrow")}
                        </span>
                        <span className="font-mono text-neutral-300">{event.from_state || "none"} → {event.to_state || "removed"}</span>
                        <span className="text-muted-foreground">{event.event_kind}</span>
                      </div>
                      <div className="mt-0.5 text-caption text-muted-foreground">{event.created_at ? new Date(event.created_at).toLocaleString() : ""}</div>
                      {event.reason && <div className="mt-1 leading-relaxed text-muted-foreground">{event.reason}</div>}
                      {event.previous_unit_key && event.previous_unit_key !== event.unit_key && (
                        <div className="mt-1 font-mono text-caption text-muted-foreground">moved from {event.previous_unit_key}</div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </PanelCard>
          </div>
        )}
      </TabsContent>
      </Tabs>
    </aside>
  );
}
