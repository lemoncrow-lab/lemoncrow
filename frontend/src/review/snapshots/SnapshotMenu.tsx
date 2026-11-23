import { useEffect, useMemo, useState } from "react";
import { ChevronDown, GitCompareArrows } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "../../components/ui/popover";
import { Select, SelectItem } from "../../components/ui/select";
import { fmtWhen } from "../../lib/format";
import type { FlowReviewTools } from "../flowReviewTools";
import { revisionName } from "../revisionLabel";
import { fetchAnnotations, fetchRevisions, historicalReviewHref, sourceCompareHref } from "../reviewApi";
import type { Annotation, ReviewRevisionHistory, RevisionInfo } from "../types";
import {
  baseMovedText, commentCountText, commentCounts, commitText, defaultCompareFrom, newestFirst, newestSaved, partsText,
  snapshotName, snapshotSpec, snapshotTags,
} from "./snapshotModel";

function message(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

/**
 * The top bar's revision chip, opened: every revision of the review, newest first, with what it is (newest saved,
 * running draft, last reviewed, feedback, rewritten), when it was saved, how its parts changed and how many comments
 * were written on it. Open reads one as it was; Compare goes to the Compare screen, against the latest or any pair.
 */
export function SnapshotMenu({ tools, revisionNumber }: { tools: FlowReviewTools; revisionNumber: number }) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [fresh, setFresh] = useState<ReviewRevisionHistory | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [latestNotes, setLatestNotes] = useState<Annotation[] | null>(null);
  const [pick, setPick] = useState<{ from: number; to: number } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const { reviewId, historical } = tools;

  // A fresh list on every open: a revision may have landed since the Reader loaded.
  useEffect(() => {
    if (!open || !reviewId) return;
    let live = true;
    setLoading(true);
    setError("");
    fetchRevisions(reviewId)
      .then((next) => { if (live) setFresh(next); })
      .catch((reason) => { if (live) setError(message(reason)); })
      .finally(() => { if (live) setLoading(false); });
    // A past revision on screen holds that revision's comments; the counts need today's list.
    if (historical) {
      fetchAnnotations(reviewId)
        .then((next) => { if (live) setLatestNotes(next.annotations); })
        .catch(() => { if (live) setLatestNotes(null); });
    }
    return () => { live = false; };
  }, [open, reviewId, historical, attempt]);
  useEffect(() => { if (!open) setPick(null); }, [open]);

  const rows = useMemo(() => newestFirst((fresh ?? tools.revisionHistory)?.revisions ?? []), [fresh, tools.revisionHistory]);
  const countSource = historical ? latestNotes : tools.annotations;
  const counts = useMemo(() => (countSource ? commentCounts(countSource, rows) : null), [countSource, rows]);
  const latest = rows[0] ?? null;
  const saved = newestSaved(rows);
  const defaults = { from: defaultCompareFrom(rows) ?? revisionNumber, to: latest?.revision_number ?? revisionNumber };
  const active = pick ?? defaults;
  const byNumber = new Map(rows.map((row) => [row.revision_number, row]));
  const scope = `r/${tools.reviewRef.replace(/^r\//, "")}`;
  const compare = (from: RevisionInfo | undefined, to: RevisionInfo | undefined) => {
    if (!from || !to || from.id === to.id) return;
    setOpen(false);
    navigate(sourceCompareHref(snapshotSpec(from), snapshotSpec(to), scope));
  };
  const name = revisionName(revisionNumber);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="xs" className="gap-1 px-1" aria-label={`${name}, revision history`} data-testid="revision-menu-trigger">
          <Badge tone={revisionNumber >= 1 ? "info" : "neutral"}>{name}</Badge>
          <ChevronDown size={14} aria-hidden="true" className="text-muted-foreground" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" aria-label="Revisions" className="flex w-[min(30rem,calc(100vw-2rem))] flex-col gap-2 p-0" data-testid="revision-menu">
        <div className="border-b border-border px-3 py-2">
          <p className="text-label font-medium text-foreground">Revisions</p>
          <p className="text-meta text-muted-foreground">
            Viewing {name}{saved && saved.revision_number !== revisionNumber ? ` · newest saved ${revisionName(saved.revision_number)}` : ""}
          </p>
        </div>
        {error ? (
          <div role="alert" className="flex items-center gap-2 px-3 text-meta text-p0">
            <span className="min-w-0 flex-1">Couldn't load the revisions: {error}</span>
            <Button variant="outline" size="xs" disabled={loading} onClick={() => setAttempt((value) => value + 1)}>Retry</Button>
          </div>
        ) : null}
        {rows.length === 0 && !error ? (
          <p role="status" className="px-3 pb-2 text-meta text-muted-foreground">{loading ? "Loading revisions…" : "No saved revisions yet."}</p>
        ) : null}
        {rows.length > 0 ? (
          <ul aria-label="Revisions" className="max-h-[min(24rem,60dvh)] overflow-y-auto px-1">
            {rows.map((row) => {
              const rowName = snapshotName(row);
              const facts = [
                row.created_at ? fmtWhen(row.created_at) : "",
                commitText(row),
                partsText(row),
                baseMovedText(row),
                counts ? commentCountText(counts.get(row.revision_number)) : "",
              ].filter(Boolean);
              return (
                <li key={row.id} className="flex items-start gap-2 rounded-md px-2 py-2 hover:bg-accent" data-testid="revision-row" data-revision={row.revision_number}>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-label font-medium text-foreground">{rowName}</span>
                      {snapshotTags(row, rows, revisionNumber).map((tag) => <Badge key={tag.text} tone={tag.tone}>{tag.text}</Badge>)}
                    </div>
                    {row.title ? <p className="truncate text-meta text-subtle" title={row.title}>{row.title}</p> : null}
                    <p className="text-meta text-muted-foreground">{facts.join(" · ")}</p>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-0.5">
                    {row.revision_number !== revisionNumber ? (
                      <Button asChild variant="ghost" size="compact">
                        <a href={historicalReviewHref(tools.reviewRef, row.revision_number)} aria-label={`Open ${rowName}`}>Open</a>
                      </Button>
                    ) : null}
                    {latest && row.id !== latest.id ? (
                      <Button variant="ghost" size="compact" onClick={() => compare(row, latest)}>
                        <GitCompareArrows size={14} aria-hidden="true" />
                        Compare to latest
                      </Button>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
        ) : null}
        {rows.length > 1 ? (
          <div className="flex flex-wrap items-center gap-2 border-t border-border px-3 py-2" role="group" aria-label="Compare two revisions">
            <Select uiSize="xs" aria-label="Compare from" value={active.from} onValueChange={(value) => setPick({ ...active, from: Number(value) })} className="min-w-[8rem] flex-1">
              {rows.map((row) => <SelectItem key={row.id} value={row.revision_number}>{snapshotName(row)}</SelectItem>)}
            </Select>
            <span className="text-meta text-muted-foreground">to</span>
            <Select uiSize="xs" aria-label="Compare to" value={active.to} onValueChange={(value) => setPick({ ...active, to: Number(value) })} className="min-w-[8rem] flex-1">
              {rows.map((row) => <SelectItem key={row.id} value={row.revision_number}>{snapshotName(row)}</SelectItem>)}
            </Select>
            {active.from !== active.to ? (
              <Button variant="outline" size="xs" onClick={() => compare(byNumber.get(active.from), byNumber.get(active.to))}>Compare</Button>
            ) : (
              <span className="text-meta text-muted-foreground">Choose two revisions</span>
            )}
          </div>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
