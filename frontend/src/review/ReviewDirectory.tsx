import { Select, SelectItem } from "../components/ui/select";
import { Badge } from "../components/ui/badge";
import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogBody, DialogFooter } from "../components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "../components/ui/dropdown-menu";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Archive, ArchiveRestore, CheckCircle2, CircleDot, Copy, Ellipsis, Pencil, Search, Terminal, Trash2 } from "lucide-react";
import "./reviewUi.css";
import { PageFrame } from "../components/WorkbenchUI";
import {
  adoptBootstrapFragment,
  createInboxFilter,
  deleteInboxFilter,
  fetchInboxFilters,
  fetchReviewDirectory,
  deleteReview,
  finishReview,
  reviewHref,
  type ReviewDirectoryQuery,
  updateReviewTitle,
} from "./reviewApi";
import { hasInboxSignals, InboxRowSignals, InboxViewControls, SavedInboxFilterControls } from "./ReviewInboxViews";
import { reviewEnvironmentDescription, reviewEnvironmentLabel, useReviewEnvironment } from "./reviewEnvironment";
import {
  archiveConfirmationRequired,
  confirmationFor,
  deleteIdentityMatches,
  type ArchiveConfirmation,
  type ArchiveHazards,
} from "./automationApi";
import type {
  InboxView,
  InboxWorkflowState,
  ReviewDirectoryPage,
  ReviewDirectoryRow,
  ReviewDirectoryStatus,
  SavedInboxFilter,
} from "./types";

function statusGlyph(status: ReviewDirectoryRow["status"]) {
  if (status === "archived") return <Archive size={12} className="text-muted-foreground" />;
  if (status === "finished") return <CheckCircle2 size={12} className="text-emerald-400" />;
  return <CircleDot size={12} className="text-sky-400" />;
}

function reviewSourceLabel(row: ReviewDirectoryRow): string {
  if (row.range_mode === "working_tree") return "Working tree";
  if (row.range_mode === "staged") return "Staged changes";
  if (row.range_mode === "commit_range") {
    const source = String(row.source_ref || "");
    // External review identities (PRs/changes) are useful human context. Raw
    // local SHA ranges are diagnostics and do not belong in the scan path.
    if (source && !source.includes("..") && !source.startsWith("client:")) return source;
    return "Commit range";
  }
  const source = String(row.source_ref || "");
  return source && !source.startsWith("client:") ? source : "Local source";
}

/** An open Review whose feedback is with its fixer: nothing for the reviewer to do yet. */
function waitingOnFixer(row: ReviewDirectoryRow): boolean {
  return row.status === "open"
    && (row.primary_bucket === "waiting_on_agent" || row.primary_bucket === "waiting_on_author");
}

function reviewRowAction(row: ReviewDirectoryRow): { label: string; tone: string } {
  if (row.status === "finished") return { label: "Finished", tone: "text-emerald-400" };
  if (row.status === "archived") return { label: "Archived", tone: "text-muted-foreground" };
  if (row.primary_bucket === "returned_to_me") {
    const returned = row.returned_thread_count ?? 0;
    return { label: returned > 0 ? `Returned · re-review ${returned}` : "Returned · re-review", tone: "text-sky-300" };
  }
  if (row.primary_bucket === "delivery_needs_inspection") {
    return { label: "Delivery needs inspection", tone: "text-amber-300" };
  }
  if (waitingOnFixer(row)) {
    return { label: `Waiting on ${row.handoff_label || "author"}`, tone: "text-muted-foreground" };
  }
  const progress = row.progress;
  if (!progress || progress.target_count === 0) return { label: "Needs review", tone: "text-neutral-400" };
  if (progress.changed_since_review > 0) {
    return { label: `${progress.changed_since_review} changed`, tone: "text-sky-300" };
  }
  if (progress.unreviewed + progress.unknown > 0) {
    const pending = progress.unreviewed + progress.unknown;
    return { label: `${pending} left`, tone: "text-amber-300" };
  }
  if (progress.needs_changes > 0) {
    return { label: "Awaiting changes", tone: "text-rose-300" };
  }
  return { label: "Ready to finish", tone: "text-emerald-300" };
}

function ReviewRow({
  row,
  onRename,
  onArchive,
  onDelete,
}: {
  row: ReviewDirectoryRow;
  onRename: (row: ReviewDirectoryRow, title: string) => Promise<void>;
  onArchive: (row: ReviewDirectoryRow, confirmation?: ArchiveConfirmation) => Promise<void>;
  onDelete: (row: ReviewDirectoryRow, confirmIdentity: string) => Promise<void>;
}) {
  const cancelDeleteRef = useRef<HTMLButtonElement>(null);
  const cancelDiscardRef = useRef<HTMLButtonElement>(null);
  const action = reviewRowAction(row);
  const archived = row.status === "archived";
  const [archiving, setArchiving] = useState(false);
  const [archiveError, setArchiveError] = useState("");
  // A discard that would drop pending feedback or withdraw an armed landing is
  // confirmed here, naming each one and what happens to it.
  const [discard, setDiscard] = useState<ArchiveHazards | null>(null);
  const [acknowledged, setAcknowledged] = useState<ReadonlySet<string>>(new Set());
  const toggleArchive = async (confirmation?: ArchiveConfirmation) => {
    setArchiving(true);
    setArchiveError("");
    try {
      await onArchive(row, confirmation);
      setDiscard(null);
    } catch (reason) {
      const hazards = archiveConfirmationRequired(reason);
      if (hazards) {
        setDiscard(hazards);
        setAcknowledged(new Set());
      } else {
        setArchiveError(String(reason instanceof Error ? reason.message : reason));
      }
      setArchiving(false);
    }
  };
  const allAcknowledged = discard !== null && discard.hazards.every((item) => acknowledged.has(item.id));
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [deleteIdentity, setDeleteIdentity] = useState("");
  const deleteConfirmed = deleteIdentityMatches(deleteIdentity, row.id, row.title || "");
  const remove = async () => {
    if (!archived || deleting || !deleteConfirmed) return;
    setDeleting(true);
    setDeleteError("");
    try {
      await onDelete(row, deleteIdentity.trim());
    } catch (reason) {
      setDeleteError(String(reason instanceof Error ? reason.message : reason));
      setDeleting(false);
    }
  };
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [renameError, setRenameError] = useState("");
  const hasWorkspaceIdentity = Boolean(
    row.directory || row.project || row.worktree || row.branch,
  );
  const saveTitle = async (title: string) => {
    setSaving(true);
    setRenameError("");
    try {
      await onRename(row, title);
      setEditing(false);
    } catch (reason) {
      setRenameError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setSaving(false);
    }
  };
  return (
    <div
      className="review-directory-grid group relative min-h-[56px] items-center border-t border-neutral-900/90 px-3 text-label transition-colors hover:bg-muted/55"
    >
      {!editing && <a
        href={reviewHref(row.ref || row.id)}
        data-review-link
        title={`${row.worktree_root || row.repo_id} · ${row.id}`}
        aria-label={`Open ${row.title || "Untitled review"}`}
        className="absolute inset-0 rounded-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring"
      />}
      <div className="pointer-events-none relative min-w-0 pr-4">
        <div className="flex min-w-0 items-center gap-2">
          {statusGlyph(row.status)}
          {editing ? (
            <form className="pointer-events-auto relative z-10 flex min-w-0 flex-1 flex-wrap items-center gap-1" onSubmit={(event) => { event.preventDefault(); void saveTitle(draft.trim()); }}>
              <UiInput
                autoFocus
                value={draft}
                maxLength={120}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => { if (event.key === "Escape") setEditing(false); }}
                aria-label="Custom review title"
                placeholder={row.auto_title || row.title}
                className="h-7 min-w-0 flex-1 text-label"
              />
              <UiButton size="xs" type="submit" disabled={saving}>Save</UiButton>
              <UiButton size="xs" type="button" variant="ghost" disabled={saving} onClick={() => setEditing(false)}>Cancel</UiButton>
              {row.title_is_custom && <UiButton size="xs" type="button" variant="ghost" disabled={saving} onClick={() => void saveTitle("")}>Use auto</UiButton>}
            </form>
          ) : (
            <>
              <span className="min-w-0 truncate font-medium text-neutral-200 group-hover:text-neutral-50">{row.title || "Untitled review"}</span>
              <UiButton
                type="button"
                variant="ghost"
                size="icon"
                className="pointer-events-auto relative z-10 ml-auto h-6 w-6 shrink-0"
                aria-label={`Rename ${row.title || "review"}`}
                tooltip="Edit review title"
                onClick={() => { setDraft(row.title_is_custom ? row.title : ""); setRenameError(""); setEditing(true); }}
              >
                <Pencil size={12} aria-hidden="true" />
              </UiButton>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <UiButton
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="pointer-events-auto relative z-10 h-6 w-6 shrink-0"
                    aria-label={`More actions for ${row.title || "review"}`}
                    tooltip="More review actions"
                    disabled={archiving || deleting}
                  >
                    <Ellipsis size={13} aria-hidden="true" />
                  </UiButton>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-48">
                  <DropdownMenuItem
                    onSelect={() => void toggleArchive()}
                    disabled={archiving || deleting}
                    className="gap-2"
                  >
                    {archived ? <ArchiveRestore size={12} aria-hidden="true" /> : <Archive size={12} aria-hidden="true" />}
                    {archived ? `Restore ${row.title || "review"}` : `Archive ${row.title || "review"}`}
                  </DropdownMenuItem>
                  {archived && (
                    <>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem
                        onSelect={() => { setDeleteError(""); setDeleteIdentity(""); setDeleteOpen(true); }}
                        disabled={archiving || deleting}
                        className="gap-2 text-destructive focus:text-destructive"
                      >
                        <Trash2 size={12} aria-hidden="true" />
                        {`Delete ${row.title || "review"}`}
                      </DropdownMenuItem>
                    </>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
              {archived && (
                <Dialog open={deleteOpen} onOpenChange={(open) => { if (!deleting) setDeleteOpen(open); }}>
                  <DialogContent
                    onOpenAutoFocus={(event) => {
                      event.preventDefault();
                      cancelDeleteRef.current?.focus();
                    }}
                  >
                    <DialogHeader><DialogTitle>Delete archived review?</DialogTitle></DialogHeader>
                    <DialogBody>
                      <DialogDescription>
                        Permanently delete “{row.title || "Untitled review"}” and its review history, comments, and saved snapshots. This cannot be undone and is recorded in the deletion audit. Your source files are kept.
                      </DialogDescription>
                      <label className="mt-3 grid gap-1 text-caption text-muted-foreground">
                        <span>Type the review id <span className="font-mono text-neutral-300">{row.id}</span> or its exact title to confirm</span>
                        <UiInput
                          value={deleteIdentity}
                          onChange={(event) => setDeleteIdentity(event.target.value)}
                          aria-label="Confirm the review to delete"
                          autoComplete="off"
                          className="h-8 text-label"
                        />
                      </label>
                      {deleteError && <div role="alert" className="mt-3 text-body text-destructive">{deleteError}</div>}
                    </DialogBody>
                    <DialogFooter>
                      <UiButton ref={cancelDeleteRef} disabled={deleting} onClick={() => setDeleteOpen(false)}>Cancel</UiButton>
                      <UiButton variant="destructive" disabled={deleting || !deleteConfirmed} onClick={() => void remove()}>
                        {deleting ? "Deleting…" : "Delete review"}
                      </UiButton>
                    </DialogFooter>
                  </DialogContent>
                </Dialog>
              )}
            </>
          )}
        </div>
        {discard && (
          <Dialog open onOpenChange={(open) => { if (!open && !archiving) setDiscard(null); }}>
            <DialogContent
              onOpenAutoFocus={(event) => {
                event.preventDefault();
                cancelDiscardRef.current?.focus();
              }}
            >
              <DialogHeader><DialogTitle>Discard this review?</DialogTitle></DialogHeader>
              <DialogBody>
                <DialogDescription>
                  Discarding “{row.title || "Untitled review"}” affects {discard.hazards.map((item) => item.label.toLowerCase()).join(" and ")}. Confirm each one.
                </DialogDescription>
                <ul className="mt-3 space-y-2" aria-label="What discarding affects">
                  {discard.hazards.map((item) => (
                    <li key={item.id} className="rounded-md border border-amber-900/50 bg-amber-950/10 px-3 py-2">
                      <div className="flex items-start gap-2 text-caption">
                        <span className="min-w-0 flex-1">
                          <span className="block font-medium text-amber-200">{item.label}: {item.detail}</span>
                          <span className="block text-muted-foreground">{item.outcome}</span>
                        </span>
                        <UiButton
                          type="button"
                          size="xs"
                          variant={acknowledged.has(item.id) ? "default" : "outline"}
                          className="shrink-0"
                          aria-pressed={acknowledged.has(item.id)}
                          aria-label={`Confirm ${item.label.toLowerCase()}`}
                          disabled={archiving}
                          onClick={() => setAcknowledged((current) => {
                            const next = new Set(current);
                            if (next.has(item.id)) next.delete(item.id);
                            else next.add(item.id);
                            return next;
                          })}
                        >
                          {acknowledged.has(item.id) ? "Confirmed" : "Confirm"}
                        </UiButton>
                      </div>
                    </li>
                  ))}
                </ul>
                <p className="mt-3 text-caption text-muted-foreground">{discard.retention}</p>
              </DialogBody>
              <DialogFooter>
                <UiButton ref={cancelDiscardRef} disabled={archiving} onClick={() => setDiscard(null)}>Keep review</UiButton>
                <UiButton
                  variant="destructive"
                  disabled={archiving || !allAcknowledged}
                  onClick={() => void toggleArchive(confirmationFor(discard))}
                >
                  {archiving ? "Discarding…" : `Discard with ${discard.hazards.map((item) => item.label.toLowerCase()).join(" and ")}`}
                </UiButton>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        )}
        {renameError && <div role="alert" className="pointer-events-auto relative z-10 pl-5 text-rose-400">{renameError}</div>}
        {archiveError && <div role="alert" className="pointer-events-auto relative z-10 pl-5 text-rose-400">{archiveError}</div>}
        {archived && row.owed_cancellation && (
          <div className="pointer-events-auto relative z-10 pl-5 text-caption text-amber-300" title={row.owed_cancellation.error || row.owed_cancellation.detail}>
            {row.owed_cancellation.provider || "Provider"} cancellation owed · Merge when ready is withdrawn once {row.owed_cancellation.provider || "the provider"} confirms
          </div>
        )}
        <div className="mt-0.5 flex min-w-0 items-center gap-2 pl-5 text-caption text-muted-foreground">
          <span className="truncate" title={row.title_is_custom ? row.auto_title : undefined}>{row.title_is_custom ? `Auto: ${row.auto_title || reviewSourceLabel(row)}` : reviewSourceLabel(row)}</span>
          {row.progress && row.progress.target_count > 0 && (
            <>
              <span className="shrink-0">· {row.progress.reviewed}/{row.progress.target_count} reviewed</span>
              {row.progress.changed_since_review > 0 && (
                <span className="shrink-0 text-sky-400">· {row.progress.changed_since_review} changed</span>
              )}
              {row.progress.needs_changes > 0 && (
                <span className="shrink-0 text-rose-400">· {row.progress.needs_changes} need changes</span>
              )}
            </>
          )}
        </div>
      </div>
      <div
        className="review-grid-secondary pointer-events-none relative min-w-0 pr-4 text-caption text-muted-foreground"
        title={row.worktree_root || row.directory || row.repo_id}
      >
        {hasWorkspaceIdentity ? (
          <>
            <div className="flex min-w-0 items-center gap-1.5 whitespace-nowrap">
              {row.project && (
                <>
                  <span className="text-muted-foreground">project</span>
                  <span className="max-w-28 truncate font-medium text-neutral-400">{row.project}</span>
                </>
              )}
              {row.worktree && (
                <>
                  {row.project && <span className="text-muted-foreground">·</span>}
                  <span className="text-muted-foreground">worktree</span>
                  <span className="max-w-32 truncate font-medium text-neutral-400">{row.worktree}</span>
                </>
              )}
              {row.branch && (
                <>
                  {(row.project || row.worktree) && <span className="text-muted-foreground">·</span>}
                  <span className="text-muted-foreground">branch</span>
                  <span className="max-w-40 truncate font-medium text-neutral-400">{row.branch}</span>
                </>
              )}
            </div>
            {row.directory && (
              <div className="mt-0.5 flex min-w-0 items-center gap-1.5 whitespace-nowrap text-muted-foreground">
                <span>dir</span>
                <span className="truncate font-mono text-muted-foreground">{row.directory}</span>
              </div>
            )}
          </>
        ) : (
          <span className="truncate text-muted-foreground">Workspace unavailable</span>
        )}
      </div>
      <div className={`pointer-events-none relative min-w-0 text-caption font-medium ${action.tone}`}>
        {action.label}
        {hasInboxSignals(row) && <InboxRowSignals signals={row} />}
      </div>
      <div className="review-grid-secondary pointer-events-none relative text-right text-caption text-muted-foreground">
        {row.updated_at
          ? new Date(row.updated_at).toLocaleString([], {
              month: "short",
              day: "numeric",
              hour: "2-digit",
              minute: "2-digit",
            })
          : ""}
      </div>
    </div>
  );
}

export default function ReviewDirectory() {
  const environment = useReviewEnvironment("local");
  const [status, setStatus] = useState<ReviewDirectoryStatus>("open");
  // P9-INBOX: the view, workflow state and saved filter select the server's view mode.
  const [view, setView] = useState<InboxView>("all");
  const [workflow, setWorkflow] = useState<InboxWorkflowState | "">("");
  const [filterId, setFilterId] = useState("");
  const [filters, setFilters] = useState<SavedInboxFilter[]>([]);
  const [counts, setCounts] = useState<ReviewDirectoryPage["counts"]>(undefined);
  const [total, setTotal] = useState<number | null>(null);
  const [commandCopied, setCommandCopied] = useState(false);
  const [searchDraft, setSearchDraft] = useState("");
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState<ReviewDirectoryRow[]>([]);
  const [nextCursor, setNextCursor] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const generationRef = useRef(0);
  const directoryRef = useRef<HTMLDivElement>(null);
  const focusAfterDeleteRef = useRef(false);

  useEffect(() => {
    if (!focusAfterDeleteRef.current) return;
    focusAfterDeleteRef.current = false;
    const directory = directoryRef.current;
    (directory?.querySelector<HTMLAnchorElement>("a[data-review-link]") ?? directory)?.focus();
  }, [rows]);

  useEffect(() => {
    adoptBootstrapFragment();
  }, []);

  const reloadFilters = useCallback(async () => {
    const listed = await fetchInboxFilters();
    setFilters(listed.filters);
    return listed.filters;
  }, []);

  useEffect(() => {
    // Saved filters are optional chrome: a failed list leaves the Reviews list working.
    reloadFilters().catch(() => setFilters([]));
  }, [reloadFilters]);

  const applyFilter = (id: string) => {
    // A saved filter defines the selection; the controls go neutral so it applies as saved.
    setFilterId(id);
    if (id) {
      setView("all");
      setStatus("all");
      setWorkflow("");
      setQuery("");
      setSearchDraft("");
    }
  };

  const saveFilter = async (name: string) => {
    const created = await createInboxFilter(name, {
      view,
      status,
      workflow_states: workflow ? [workflow] : [],
      query,
    });
    await reloadFilters();
    applyFilter(created.filter.id);
  };

  const removeFilter = async (id: string) => {
    await deleteInboxFilter(id);
    await reloadFilters();
    setFilterId("");
  };

  const loadPage = useCallback(
    async (cursor: string, append: boolean, generation: number) => {
      if (append) setLoadingMore(true);
      else setLoading(true);
      const request: ReviewDirectoryQuery = { status, query, cursor, limit: 50, view, workflow, filter: filterId };
      // The full Inbox scores every local Review before paging. Its workflow,
      // attention and readiness are enrichment, not prerequisites for showing
      // a review link. On initial unfiltered load, show the inexpensive legacy
      // page first, then replace it with the authoritative view-mode page.
      const previewEligible = !append && !cursor && status === "open" && view === "all"
        && !query && !workflow && !filterId;
      let previewReady = false;
      if (previewEligible) {
        try {
          const preview = await fetchReviewDirectory({ ...request, view: undefined, summary: true });
          if (generationRef.current !== generation) return;
          setRows(preview.reviews);
          setError("");
          setLoading(false);
          previewReady = true;
        } catch {
          // A missing/failed legacy endpoint cannot prevent the full Inbox.
        }
      }
      try {
        const page: ReviewDirectoryPage = await fetchReviewDirectory(request);
        if (generationRef.current !== generation) return;
        setNextCursor(page.next_cursor);
        setCounts(page.counts);
        setTotal(typeof page.total === "number" ? page.total : null);
        setError("");
        setRows((current) =>
          append
            ? [
                ...current,
                ...page.reviews.filter(
                  (row) =>
                    !current.some(
                      (item) => item.repo_id === row.repo_id && item.id === row.id,
                    ),
                ),
              ]
            : page.reviews,
        );
      } catch (reason) {
        if (generationRef.current !== generation) return;
        setError(String(reason instanceof Error ? reason.message : reason));
        if (!append && !previewReady) setRows([]);
      } finally {
        if (generationRef.current === generation) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [filterId, query, status, view, workflow],
  );

  useEffect(() => {
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    setRows([]);
    setNextCursor("");
    void loadPage("", false, generation);
  }, [loadPage]);

  const rename = async (row: ReviewDirectoryRow, title: string) => {
    const updated = await updateReviewTitle(row.repo_id, row.id, title);
    setRows((current) => current.map((item) => item.repo_id === row.repo_id && item.id === row.id
      ? { ...item, ...updated }
      : item));
  };

  const archive = async (row: ReviewDirectoryRow, confirmation?: ArchiveConfirmation) => {
    const next = row.status === "archived" ? "open" : "archived";
    await finishReview(row.id, next, next === "archived" ? row.revision_id : "", next === "archived" ? confirmation : undefined);
    setRows((current) =>
      status === "all"
        ? current.map((item) => (item.repo_id === row.repo_id && item.id === row.id ? { ...item, status: next } : item))
        : current.filter((item) => !(item.repo_id === row.repo_id && item.id === row.id)),
    );
  };

  const remove = async (row: ReviewDirectoryRow, confirmIdentity: string) => {
    if (row.status !== "archived") return;
    await deleteReview(row.id, confirmIdentity);
    focusAfterDeleteRef.current = true;
    setRows((current) => current.filter((item) => !(item.repo_id === row.repo_id && item.id === row.id)));
  };

  const search = (event: FormEvent) => {
    event.preventDefault();
    setQuery(searchDraft.trim());
  };

  return (
    <div ref={directoryRef} role="region" aria-label="Reviews" tabIndex={-1} className="min-h-full bg-background text-neutral-200" data-testid="review-directory">
      <PageFrame className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <form
            onSubmit={search}
            className="review-field flex h-8 min-w-[260px] max-w-[720px] flex-1 items-center overflow-hidden p-0"
          >
            <Search size={13} className="ml-3 text-muted-foreground" />
            <UiInput unstyled
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
              placeholder="Search reviews…"
              aria-label="Search reviews"
              className="min-w-0 flex-1 border-0 bg-transparent px-2.5 py-2 text-label text-neutral-200 outline-none placeholder:text-muted-foreground"
            />
            <UiButton unstyled
              type="submit"
              className="h-full border-y-0 border-r-0 border-l border-neutral-700/80 bg-transparent px-4 text-caption font-semibold text-neutral-400 hover:bg-muted hover:text-neutral-50"
            >
              Search
            </UiButton>
          </form>
          <InboxViewControls
            view={view}
            onView={setView}
            workflow={workflow}
            onWorkflow={setWorkflow}
            counts={counts ?? {}}
            teamsSupported={false}
            advancedActive={status !== "open" || Boolean(filterId)}
            advanced={(
              <>
                <Select
                  aria-label="Review status"
                  value={status}
                  onValueChange={(value) => setStatus(value as ReviewDirectoryStatus)}
                  uiSize="xs"
                  className="w-full text-caption"
                >
                  <SelectItem value="open">Needs review</SelectItem>
                  <SelectItem value="all">All reviews</SelectItem>
                  <SelectItem value="finished">Finished</SelectItem>
                  <SelectItem value="archived">Archived</SelectItem>
                </Select>
                <SavedInboxFilterControls filters={filters} activeId={filterId} onApply={applyFilter}
                  onSave={saveFilter} onDelete={removeFilter} />
              </>
            )}
          />
          <Badge tone="secondary" className="shrink-0" title={reviewEnvironmentDescription(environment)}>{reviewEnvironmentLabel(environment.mode)}</Badge>
          <Badge tone="secondary" className="shrink-0">{total ?? rows.length} review{(total ?? rows.length) === 1 ? "" : "s"}</Badge>
        </div>

        {loading && rows.length === 0 && (
          <div
            className="border-y border-neutral-900 py-6 text-center text-caption text-muted-foreground"
            role="status"
          >
            Loading reviews…
          </div>
        )}
        {error && (
          <div className="my-4 border-y border-rose-900/45 bg-rose-950/10 px-3 py-2.5 text-caption text-rose-300">
            Could not load reviews · {error}
          </div>
        )}
        {!loading && !error && rows.length === 0 && (
          status === "open" && !query && view === "all" && !workflow && !filterId ? (
            <div className="flex flex-col items-center py-8 text-center">
              <Terminal size={19} className="text-muted-foreground" />
              <div className="mt-3 text-body font-medium text-neutral-300">No local reviews yet</div>
              <div className="mt-1 max-w-lg text-caption leading-5 text-muted-foreground">
                Review staged, unstaged, committed, or branch changes directly from your workspace. No PR or hosted account is required.
              </div>
              <UiButton
                type="button"
                className="mt-3 gap-2 font-mono"
                onClick={() => {
                  const write = navigator.clipboard?.writeText;
                  if (!write) return;
                  void Promise.resolve(write.call(navigator.clipboard, "lc review")).then(() => {
                    setCommandCopied(true);
                    window.setTimeout(() => setCommandCopied(false), 1200);
                  });
                }}
                tooltip="Copy lc review"
              >
                <span>$ lc review</span>
                <Copy size={11} />
                {commandCopied && <span className="font-sans text-emerald-400">Copied</span>}
              </UiButton>
            </div>
          ) : (
            <div className="py-8 text-center text-label text-muted-foreground">No reviews match this view.</div>
          )
        )}
        {rows.length > 0 && (
          <section className="overflow-hidden rounded-md border border-border/80 bg-background/75">
            <div className="review-directory-grid h-7 items-center px-3 text-caption font-medium uppercase tracking-[0.06em] text-muted-foreground">
              <span>Change</span>
              <span className="review-grid-secondary">Workspace</span>
              <span>Next</span>
              <span className="review-grid-secondary text-right">Updated</span>
            </div>
            {/* The server pages by Updated; early summary rows follow the same newest-first order. */}
            {(rows.every(hasInboxSignals) ? rows : [...rows].sort((a, b) =>
              (Date.parse(b.updated_at || "") || 0) - (Date.parse(a.updated_at || "") || 0)
            )).map((row) => (
              <ReviewRow key={`${row.repo_id}:${row.id}`} row={row} onRename={rename} onArchive={archive} onDelete={remove} />
            ))}
          </section>
        )}
        {nextCursor && (
          <div className="mt-4 flex justify-center">
            <UiButton
              type="button"
              disabled={loadingMore}
              onClick={() => void loadPage(nextCursor, true, generationRef.current)}
            >
              {loadingMore ? "Loading…" : "Load more"}
            </UiButton>
          </div>
        )}
      </PageFrame>
    </div>
  );
}
