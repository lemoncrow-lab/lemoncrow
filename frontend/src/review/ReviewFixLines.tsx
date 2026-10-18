import { useState } from "react";

import { Button as UiButton } from "../components/ui/button";

import {
  applyFixLine,
  discardFixLine,
  historicalReviewHref,
  planFixApply,
  recreateFixWorktree,
  resolveFixLine,
  startFixLine,
} from "./reviewApi";
import type { FixApplyPlan, FixLine } from "./types";

export function fixLineChip(line: FixLine): string {
  if (line.worktree_missing) return "Worktree missing";
  if (line.status === "applied") return "Applied to main";
  if (line.status === "applied_with_conflicts") return "Applied with conflicts";
  if (line.status === "abandoned") return "Abandoned";
  const count = line.snapshots.length;
  return `Fixing · ${count} snapshot${count === 1 ? "" : "s"}`;
}

function shellQuote(path: string): string {
  return `'${path.replace(/'/g, "'\\''")}'`;
}

interface ReviewFixLinesProps {
  reviewId: string;
  revisionId: string;
  snapshotNumber: number;
  line: FixLine | null;
  canFix: boolean;
  onChanged(): void;
}

export default function ReviewFixLines({
  reviewId,
  revisionId,
  snapshotNumber,
  line,
  canFix,
  onChanged,
}: ReviewFixLinesProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [plan, setPlan] = useState<FixApplyPlan | null>(null);
  const [dirty, setDirty] = useState<{
    files: string[];
    action: "discard" | "resolved";
  } | null>(null);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (reason: unknown) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy(false);
    }
  }

  if (!line) {
    if (!canFix) return null;
    return (
      <div className="mt-2">
        <UiButton
          variant="ghost"
          size="compact"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              await startFixLine(reviewId, revisionId);
              onChanged();
            })
          }
        >
          Fix in worktree
        </UiButton>
        {error && (
          <div className="mt-1 text-caption text-rose-400">{error}</div>
        )}
      </div>
    );
  }

  const activeLine = line;
  const open =
    activeLine.status === "fixing" || activeLine.status === "applied_with_conflicts";
  const latest = activeLine.snapshots[activeLine.snapshots.length - 1];
  const commentsText = activeLine.checklist
    .filter((item) => item.state !== "resolved")
    .map(
      (item) =>
        `${item.path}:${item.start_line}-${item.end_line}  ${item.body}`,
    )
    .join("\n");

  async function close(action: "discard" | "resolved", force: boolean) {
    const result =
      action === "discard"
        ? await discardFixLine(reviewId, activeLine.id, force)
        : await resolveFixLine(reviewId, activeLine.id, force);
    if (!result.closed) {
      setDirty({ files: result.dirty_files, action });
      return;
    }
    setDirty(null);
    onChanged();
  }

  async function apply(markers: boolean) {
    const result = await applyFixLine(reviewId, activeLine.id, markers);
    setPlan(null);
    if (!result.applied) {
      setPlan(result.plan);
      return;
    }
    onChanged();
  }

  async function prepareApply() {
    const next = await planFixApply(reviewId, activeLine.id);
    if (next.conflicts.length > 0 || next.warnings.length > 0) {
      setPlan(next);
      return;
    }
    await apply(false);
  }

  return (
    <div
      className="mt-2 border-l border-border pl-3"
      data-testid="fix-line"
    >
      <div className="flex flex-wrap items-center gap-2 text-caption">
        <span className="rounded-md bg-muted px-1.5 py-0.5 text-muted-foreground">
          {fixLineChip(line)}
        </span>
        {line.snapshots.map((item) => (
          <a
            key={item.id}
            className="font-mono text-sky-300/80 hover:underline"
            href={historicalReviewHref(
              line.child_review_id,
              item.display_number,
            )}
          >
            {`${snapshotNumber}.${item.fix_number}`}
          </a>
        ))}
      </div>

      {line.status === "applied" && latest && (
        <div className="mt-1 text-caption text-emerald-400/80">
          {`Applied ${snapshotNumber}.${latest.fix_number} → main · ${line.applied_files.length} file${line.applied_files.length === 1 ? "" : "s"}`}
        </div>
      )}

      {open && (
        <>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-caption text-muted-foreground">
            <code data-testid="fix-worktree-path">{line.worktree_path}</code>
            <UiButton
              variant="ghost"
              size="compact"
              onClick={() =>
                void navigator.clipboard.writeText(
                  `cd ${shellQuote(line.worktree_path)}`,
                )
              }
            >
              Copy cd command
            </UiButton>
            {commentsText && (
              <UiButton
                variant="ghost"
                size="compact"
                onClick={() =>
                  void navigator.clipboard.writeText(commentsText)
                }
              >
                Copy comments
              </UiButton>
            )}
          </div>

          {line.checklist.length > 0 && (
            <ul className="mt-1 space-y-0.5 text-caption">
              {line.checklist.map((item) => (
                <li key={item.annotation_id} className="flex gap-2">
                  <span className="shrink-0 text-muted-foreground">
                    {item.state === "touched" && latest
                      ? `Touched in ${snapshotNumber}.${latest.fix_number}`
                      : item.state === "resolved"
                        ? "Resolved"
                        : "Open"}
                  </span>
                  <span className="font-mono">
                    {`${item.path}:${item.start_line}`}
                  </span>
                  <span className="truncate">{item.body}</span>
                </li>
              ))}
            </ul>
          )}

          <div className="mt-2 flex flex-wrap gap-2">
            {line.worktree_missing ? (
              <UiButton
                variant="ghost"
                size="compact"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    await recreateFixWorktree(reviewId, line.id);
                    onChanged();
                  })
                }
              >
                Recreate worktree
              </UiButton>
            ) : line.status === "fixing" ? (
              <UiButton
                size="compact"
                disabled={busy}
                onClick={() => void run(prepareApply)}
              >
                Apply fix to main
              </UiButton>
            ) : (
              <UiButton
                size="compact"
                disabled={busy}
                onClick={() =>
                  void run(() => close("resolved", false))
                }
              >
                Done resolving
              </UiButton>
            )}
            <UiButton
              variant="ghost"
              size="compact"
              disabled={busy}
              onClick={() => void run(() => close("discard", false))}
            >
              Discard fix line
            </UiButton>
          </div>
        </>
      )}

      {plan && (
        <div className="mt-2 rounded-md border border-amber-500/40 p-2 text-caption">
          {plan.conflicts.length > 0 ? (
            <>
              <div>
                Nothing written. These files conflict with the main worktree:
              </div>
              <ul className="font-mono">
                {plan.conflicts.map((path) => (
                  <li key={path}>{path}</li>
                ))}
              </ul>
            </>
          ) : (
            <div>No conflicts. Review these warnings before applying.</div>
          )}
          {plan.warnings.map((warning) => (
            <div key={warning} className="text-amber-400/80">
              {warning}
            </div>
          ))}
          <div className="mt-2 flex gap-2">
            <UiButton
              size="compact"
              disabled={busy}
              onClick={() =>
                void run(() => apply(plan.conflicts.length > 0))
              }
            >
              {plan.conflicts.length > 0
                ? "Apply with conflict markers"
                : "Apply fix to main"}
            </UiButton>
            <UiButton
              variant="ghost"
              size="compact"
              onClick={() => setPlan(null)}
            >
              Keep fixing
            </UiButton>
          </div>
        </div>
      )}

      {dirty && (
        <div className="mt-2 rounded-md border border-rose-500/40 p-2 text-caption">
          <div>The fix worktree has uncommitted work that will be lost:</div>
          <ul className="font-mono">
            {dirty.files.map((path) => (
              <li key={path}>{path}</li>
            ))}
          </ul>
          <div className="mt-2 flex gap-2">
            <UiButton
              size="compact"
              variant="destructive"
              disabled={busy}
              onClick={() =>
                void run(() => close(dirty.action, true))
              }
            >
              {dirty.action === "discard"
                ? "Discard anyway"
                : "Finish anyway"}
            </UiButton>
            <UiButton
              variant="ghost"
              size="compact"
              onClick={() => setDirty(null)}
            >
              Keep fixing
            </UiButton>
          </div>
        </div>
      )}

      {error && (
        <div className="mt-1 text-caption text-rose-400">{error}</div>
      )}
    </div>
  );
}
