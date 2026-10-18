/**
 * Review Inbox views, saved filters and per-row signals (P9-INBOX), shared by the
 * local Reviews list and the hosted Inbox. The server keeps workflow, readiness,
 * priority and attention as separate facts; the default row projects only what a
 * reviewer needs to scan, with the full facts available as detail.
 */
import { type FormEvent, type ReactNode, useState } from "react";
import { Bookmark, Trash2 } from "lucide-react";

import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Select, SelectItem } from "../components/ui/select";
import type {
  InboxAxes,
  InboxSignals,
  InboxView,
  InboxWorkflowState,
  SavedInboxFilter,
} from "./types";

export const INBOX_VIEW_OPTIONS: readonly { id: InboxView; label: string }[] = [
  { id: "all", label: "All" },
  { id: "to_review", label: "To review" },
  { id: "assigned_to_my_teams", label: "Assigned to my teams" },
  { id: "authored_by_me", label: "Authored by me" },
  { id: "participated", label: "Participated" },
  { id: "waiting_on_me", label: "Waiting on me" },
  { id: "ready", label: "Ready" },
  { id: "blocked", label: "Blocked / waiting" },
];

export const INBOX_WORKFLOW_OPTIONS: readonly { id: InboxWorkflowState; label: string }[] = [
  { id: "needs_review", label: "Needs review" },
  { id: "waiting_on_reviewer", label: "Waiting on reviewer" },
  { id: "waiting_on_author", label: "Waiting on author" },
  { id: "waiting_on_checks", label: "Waiting on checks" },
  { id: "needs_decision", label: "Needs decision" },
  { id: "ready_to_merge", label: "Ready to merge" },
  { id: "monitoring", label: "Monitoring" },
  { id: "closed", label: "Closed" },
];

/** Narrow a row to one that carries every Inbox signal (a view-mode row). */
export function hasInboxSignals<T extends Partial<InboxSignals>>(row: T): row is T & InboxSignals {
  return Boolean(row.workflow && row.priority && row.next_action && row.readiness && row.axes && row.attention && row.views);
}

export function isInboxView(value: string): value is InboxView {
  return INBOX_VIEW_OPTIONS.some((item) => item.id === value);
}

export interface InboxViewControlsProps {
  view: InboxView;
  onView(view: InboxView): void;
  workflow: InboxWorkflowState | "";
  onWorkflow(state: InboxWorkflowState | ""): void;
  counts: Partial<Record<InboxView, number>>;
  /** Hosted only: the local server has no teams, so it never offers the team view. */
  teamsSupported: boolean;
  /** Rare filtering controls owned by the caller (status, saved views, etc.). */
  advanced?: ReactNode;
  /** Keeps the disclosure honest when a hidden filter is active. */
  advancedActive?: boolean;
}

export function InboxViewControls({
  view,
  onView,
  workflow,
  onWorkflow,
  counts,
  teamsSupported,
  advanced,
  advancedActive = false,
}: InboxViewControlsProps) {
  const views = INBOX_VIEW_OPTIONS.filter((item) => teamsSupported || item.id !== "assigned_to_my_teams");
  const filtersActive = Boolean(workflow) || advancedActive;
  return (
    <>
      <div className="review-inbox-filter review-inbox-view">
        <Select aria-label="Review inbox" value={view} uiSize="xs" className="text-caption"
          onValueChange={(value) => { if (isInboxView(value)) onView(value); }}>
          {views.map((item) => (
            <SelectItem key={item.id} value={item.id}>
              {item.label}{item.id !== "all" && (counts[item.id] ?? 0) > 0 ? ` · ${counts[item.id]}` : ""}
            </SelectItem>
          ))}
        </Select>
      </div>
      <details className="review-inbox-filters relative">
        <summary className="review-field flex h-8 cursor-pointer select-none items-center rounded-md px-2.5 text-caption text-neutral-400 hover:text-neutral-200">
          Filters{filtersActive ? " · active" : ""}
        </summary>
        <div className="absolute right-0 z-30 mt-1 flex min-w-[260px] flex-col gap-2 rounded-md border border-border bg-popover p-2 shadow-xl">
          <div className="review-inbox-filter review-inbox-workflow">
            <Select aria-label="Workflow state" value={workflow || "any"} uiSize="xs" className="w-full text-caption"
              onValueChange={(value) => onWorkflow(value === "any" ? "" : (value as InboxWorkflowState))}>
              <SelectItem value="any">Any workflow</SelectItem>
              {INBOX_WORKFLOW_OPTIONS.map((item) => <SelectItem key={item.id} value={item.id}>{item.label}</SelectItem>)}
            </Select>
          </div>
          {advanced}
        </div>
      </details>
    </>
  );
}

export interface SavedInboxFilterControlsProps {
  filters: SavedInboxFilter[];
  activeId: string;
  /** `""` clears the applied filter. */
  onApply(id: string): void;
  onSave(name: string): Promise<void>;
  onDelete(id: string): Promise<void>;
  /** Rename the applied filter; absent where the server has no filter update route. */
  onRename?(id: string, name: string): Promise<void>;
  /** Replace the applied filter's definition with the selection shown now. */
  onUpdate?(id: string): Promise<void>;
  /** The shown selection differs from the applied filter, so Update has something to save. */
  updateAvailable?: boolean;
}

export function SavedInboxFilterControls({
  filters, activeId, onApply, onSave, onDelete, onRename, onUpdate, updateAvailable = false,
}: SavedInboxFilterControlsProps) {
  const [naming, setNaming] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await action();
      return true;
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return;
    const trimmed = name.trim();
    const done = renaming && onRename
      ? await run(() => onRename(activeId, trimmed))
      : await run(() => onSave(trimmed));
    if (done) {
      setNaming(false);
      setRenaming(false);
      setName("");
    }
  };
  const active = filters.find((item) => item.id === activeId);

  return (
    <div className="review-inbox-saved-filters flex items-center gap-2">
      <Select aria-label="Saved filter" value={activeId || "none"} uiSize="xs" className="text-caption"
        onValueChange={(value) => onApply(value === "none" ? "" : value)}>
        <SelectItem value="none">No saved filter</SelectItem>
        {filters.map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}
      </Select>
      {naming ? (
        <form onSubmit={(event) => void save(event)} className="flex items-center gap-1">
          <Input value={name} maxLength={80} onChange={(event) => setName(event.target.value)}
            aria-label={renaming ? "New saved filter name" : "Saved filter name"} placeholder="Name this view" className="h-7 w-40 text-caption" />
          <Button type="submit" size="xs" disabled={busy || !name.trim()}>{renaming ? "Rename" : "Save"}</Button>
          <Button type="button" size="xs" variant="ghost" onClick={() => { setNaming(false); setRenaming(false); setName(""); }}>Cancel</Button>
        </form>
      ) : (
        <>
          <Button type="button" size="xs" variant="ghost" onClick={() => setNaming(true)}>
            <Bookmark size={11} />Save view
          </Button>
          {active && onRename && (
            <Button type="button" size="xs" variant="ghost" aria-label="Rename saved filter" disabled={busy}
              onClick={() => { setRenaming(true); setNaming(true); setName(active.name); }}>
              Rename
            </Button>
          )}
          {active && onUpdate && updateAvailable && (
            <Button type="button" size="xs" variant="ghost" aria-label="Save the current view to this filter" disabled={busy}
              onClick={() => void run(() => onUpdate(activeId))}>
              Update filter
            </Button>
          )}
        </>
      )}
      {activeId && (
        <Button type="button" size="icon" variant="ghost" aria-label="Delete saved filter" disabled={busy}
          onClick={() => void run(() => onDelete(activeId))}>
          <Trash2 size={11} />
        </Button>
      )}
      {error && <span role="alert" className="text-caption text-rose-300">{error}</span>}
    </div>
  );
}

const YOUR_REVIEW: Record<string, string> = {
  complete: "complete",
  in_progress: "in progress",
  not_started: "not started",
  waiting_on_author: "waiting on author",
  author: "you authored",
  not_requested: "not requested",
};
const TEAM_REVIEW: Record<string, string> = {
  complete: "complete",
  waiting: "waiting",
  needs_more_reviewers: "needs reviewers",
  no_requests: "no requests",
  not_applicable: "not applicable",
};
const MERGE: Record<string, string> = {
  ready: "ready",
  not_ready: "not ready",
  unknown: "unknown",
  not_connected: "not connected",
};

function axisLabel(map: Record<string, string>, state: string): string {
  return map[state] ?? state.replaceAll("_", " ");
}

export function inboxAxesText(axes: InboxAxes): [string, string, string] {
  const waiting = axes.team_review.waiting_on?.length ? ` on ${axes.team_review.waiting_on.join(", ")}` : "";
  return [
    `Your review ${axisLabel(YOUR_REVIEW, axes.your_review.state)}`,
    `Team review ${axisLabel(TEAM_REVIEW, axes.team_review.state)}${axes.team_review.state === "waiting" ? waiting : ""}`,
    `Merge ${axisLabel(MERGE, axes.scm.state)}`,
  ];
}

function workflowTone(state: InboxSignals["workflow"]["state"]): string {
  if (state === "ready_to_merge") return "text-emerald-300";
  if (state === "needs_review" || state === "waiting_on_reviewer") return "text-amber-200";
  if (state === "needs_decision") return "text-sky-300";
  if (state === "closed") return "text-muted-foreground";
  return "text-foreground";
}

export function nextActionText(signals: InboxSignals): string {
  const next = signals.next_action;
  if (next.for_me) return `Your turn: ${next.my_reasons[0]?.fact || next.my_reasons[0]?.kind || "needs you"}`;
  if (next.owner_ids.length) return `Waiting on ${next.owner_ids.join(", ")}`;
  return "";
}

export function attentionExplanation(signals: InboxSignals): string {
  return signals.attention.factors.map((factor) => `${factor.label} +${factor.points}: ${factor.detail}`).join("\n");
}

/** One row's scan path: one state, the next action, and priority when exceptional.
 * The full readiness axes and attention score stay available to assistive/diagnostic
 * inspection without making every reviewer learn the Inbox scoring model. */
export function InboxRowSignals({ signals }: { signals: InboxSignals }) {
  const axes = inboxAxesText(signals.axes);
  const next = nextActionText(signals);
  const priority = signals.priority.level;
  const attention = attentionExplanation(signals);
  const details = [signals.readiness.reason, ...axes, attention].filter(Boolean).join("\n");
  // The exact blocking actions ("Blocked: Finish review · Waiting for checks") when nothing else is next.
  const blocking = !next && signals.readiness.state === "not_ready" && signals.readiness.blocking?.length
    ? signals.readiness.reason
    : "";
  return (
    <div className="review-inbox-signals min-w-0 text-caption" data-testid="inbox-signals" title={details || undefined}>
      <div className="flex min-w-0 items-center gap-2">
        <span className={`truncate font-semibold ${workflowTone(signals.workflow.state)}`} title={signals.workflow.explanation}
          data-testid="inbox-workflow">
          {signals.workflow.label}
        </span>
        {priority !== "normal" && (
          <span className={priority === "urgent" || priority === "high" ? "text-rose-300" : "text-muted-foreground"}
            title={signals.priority.overridden ? "Priority set by a person" : "Computed priority"}>
            {priority} priority
          </span>
        )}
      </div>
      {next && <div className="mt-0.5 truncate text-muted-foreground" title={next}>{next}</div>}
      {blocking && (
        <div className="mt-0.5 truncate text-muted-foreground" data-testid="inbox-blocking" title={blocking}>{blocking}</div>
      )}
      <span className="sr-only" data-testid="inbox-readiness-detail">{axes.join(" · ")}</span>
      {signals.attention.factors.length > 0 && (
        <span className="sr-only" data-testid="inbox-attention" title={attention}>
          Attention details available
        </span>
      )}
    </div>
  );
}
