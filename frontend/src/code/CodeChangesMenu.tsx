import { useState } from "react";
import { ChevronDown, GitPullRequest, Loader2 } from "lucide-react";

import { Button as UiButton } from "../components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "../components/ui/popover";
import type { CodeChanges } from "../api";
import { sessionLabel } from "./codeModel";
import type { ReviewScope } from "./CodeInspector";

export interface CodeChangesMenuProps {
  changes: CodeChanges | null;
  marks: ReadonlyMap<string, string>;
  reviewBusy: boolean;
  reviewError: string | null;
  /** Absent where Review capture from Code is not served (a local Dashboard). */
  onReview?: (scope: ReviewScope | null) => void;
  onFilter: (session: string) => void;
}

/**
 * Uncommitted work grouped by the agent session that edited it. Several agents
 * often share one worktree, so each group can be reviewed on its own; a file
 * edited by more than one session appears under each, marked shared.
 */
export default function CodeChangesMenu({
  changes,
  marks,
  reviewBusy,
  reviewError,
  onReview,
  onFilter,
}: CodeChangesMenuProps) {
  const [open, setOpen] = useState(false);

  const total = changes?.total ?? 0;
  const sessions = changes?.sessions ?? [];

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <UiButton type="button" size="xs" variant="outline">
          {reviewBusy ? (
            <Loader2 size={11} className="animate-spin" />
          ) : (
            <GitPullRequest size={11} />
          )}
          Changes{" "}
          <span className="tabular-nums text-muted-foreground">
            {changes ? total : "…"}
          </span>
          <ChevronDown size={11} />
        </UiButton>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        aria-label="Uncommitted changes"
        className="w-80 p-2 text-label"
      >
        {!changes ? (
          <p className="p-2 text-muted-foreground">Reading the worktree…</p>
        ) : total === 0 ? (
          <p className="p-2 text-muted-foreground">
            No uncommitted changes in this worktree.
          </p>
        ) : (
          <>
            <ul className="space-y-1">
              {sessions.map((session) => (
                <li
                  key={session.id}
                  className="rounded-md border border-border/80 p-2"
                >
                  <div className="flex items-center gap-2">
                    <span className="inline-grid h-4 min-w-4 shrink-0 place-items-center rounded-full bg-cyan-400 px-1 text-micro font-bold text-neutral-950">
                      {marks.get(session.id)}
                    </span>
                    <UiButton
                      unstyled
                      type="button"
                      className="min-w-0 flex-1 truncate text-left text-neutral-200 hover:text-neutral-50"
                      onClick={() => {
                        onFilter(session.id);
                        setOpen(false);
                      }}
                      title={`${session.host} · ${session.session_id}`}
                    >
                      {sessionLabel(session)}
                    </UiButton>
                    <span className="shrink-0 tabular-nums text-muted-foreground">
                      {session.files.length}
                    </span>
                  </div>
                  <div className="mt-1.5 flex items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-caption text-muted-foreground">
                      {session.host}
                      {session.shared_files.length
                        ? ` · ${session.shared_files.length} shared`
                        : ""}
                    </span>
                    {onReview && (
                      <UiButton
                        type="button"
                        size="xs"
                        variant="outline"
                        disabled={reviewBusy}
                        onClick={() => onReview({ session: session.id })}
                      >
                        Review
                      </UiButton>
                    )}
                  </div>
                </li>
              ))}
              {changes.unattributed.length > 0 && (
                <li className="rounded-md border border-dashed border-border p-2">
                  <div className="flex items-center gap-2">
                    <UiButton
                      unstyled
                      type="button"
                      className="min-w-0 flex-1 truncate text-left text-neutral-300 hover:text-neutral-50"
                      onClick={() => {
                        onFilter("unattributed");
                        setOpen(false);
                      }}
                      title="No session recorded editing these files: your own edits, hosts that record nothing, or edits made before capture began."
                    >
                      {sessions.length ? "Unattributed" : "Uncommitted changes"}
                    </UiButton>
                    <span className="shrink-0 tabular-nums text-muted-foreground">
                      {changes.unattributed.length}
                    </span>
                    {onReview && (
                      <UiButton
                        type="button"
                        size="xs"
                        variant="outline"
                        disabled={reviewBusy}
                        onClick={() => onReview({ paths: changes.unattributed })}
                      >
                        Review
                      </UiButton>
                    )}
                  </div>
                </li>
              )}
            </ul>
            <div className="mt-2 flex items-center gap-2 border-t border-border px-1 pt-2">
              <span className="min-w-0 flex-1 text-caption leading-4 text-muted-foreground">
                A session is an author only where it recorded the edit.
              </span>
              {onReview && (
                <UiButton
                  type="button"
                  size="xs"
                  variant="ghost"
                  disabled={reviewBusy}
                  onClick={() => onReview(null)}
                >
                  Review all {total}
                </UiButton>
              )}
            </div>
          </>
        )}
        {reviewError && (
          <p className="mt-2 px-1 text-caption leading-4 text-red-300/80">
            {reviewError}
          </p>
        )}
      </PopoverContent>
    </Popover>
  );
}
