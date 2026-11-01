import { useState } from "react";

import { Button as UiButton } from "../components/ui/button";
import type { ReviewWorkspaceEvidence, ReviewWorkspaceImpact, ReviewWorkspaceReached } from "./types";

/**
 * Workspace impact (multi-repo workspace P4): which other repositories of the reviewer's local workspace depend on
 * what changed, with the evidence for each. The same content as the terminal's WORKSPACE IMPACT section
 * (`review/workspace_impact.py` `heading` / `impact_lines`). It is never a finding and never counts toward
 * attention. No source line from another repository is shown: only repositories, paths, keys and line numbers.
 */

const SHOWN_REPOSITORIES = 5;
const SHOWN_LINKS = 2;
const CLOSING = "Not findings: repositories that depend on what changed, for a person to check.";

function plural(count: number, word: string): string {
  return `${count} ${count === 1 ? word : `${word}s`}`;
}

function where(evidence: ReviewWorkspaceEvidence | undefined): string {
  const path = String(evidence?.path ?? "");
  const line = Number(evidence?.line ?? 0);
  return line ? `${path}:${line}` : path;
}

function list<T>(value: readonly T[] | undefined | null): readonly T[] {
  return Array.isArray(value) ? value : [];
}

function partialNotes(impact: Partial<ReviewWorkspaceImpact>): string[] {
  return [
    ...list(impact.errors).map((error) => `Partial: ${error.repo}: changes not listed`),
    ...list(impact.incomplete).map((repo) => `Partial: ${repo} was not fully scanned; reach through it may be missing`),
    ...(impact.members_truncated ? ["Partial: the repository bound was reached; later repositories were not scanned"] : []),
  ];
}

function ReachedRow({ hit, showAll }: { hit: ReviewWorkspaceReached; showAll: boolean }) {
  const hops = Number(hit.hops ?? 1);
  const via = list(hit.path).join(" → ");
  return (
    <li className="flex flex-col gap-0.5" data-testid="workspace-impact-repo">
      <div className="flex flex-wrap items-baseline gap-x-2 text-caption">
        <span className="font-mono font-medium text-foreground">{hit.repo}</span>
        <span className="text-muted-foreground">{plural(hops, "hop")}</span>
        <span className="text-muted-foreground">{hit.confidence}</span>
        <span className="text-muted-foreground">{hit.direct ? "direct" : "repository-level"}</span>
        {hops > 1 && via && <span className="text-muted-foreground">via {via}</span>}
      </div>
      {list(hit.edges).map((edge, index) => {
        const links = list(edge.links);
        const shown = showAll ? links : links.slice(0, SHOWN_LINKS);
        const more = links.length - shown.length + Number(edge.more ?? 0);
        return (
          <ul key={`${edge.kind}:${index}`} className="flex flex-col gap-0.5 pl-3 font-mono text-meta text-muted-foreground">
            {shown.map((link, linkIndex) => (
              <li key={`${link.key}:${linkIndex}`} className="break-all">
                {`${edge.kind} ${link.key}: ${where(link.consumer)} uses ${where(link.provider)}`}
              </li>
            ))}
            {more > 0 && <li>{`${more} more ${edge.kind} evidence`}</li>}
          </ul>
        );
      })}
    </li>
  );
}

export default function BriefWorkspaceImpact({ impact }: { impact?: Partial<ReviewWorkspaceImpact> | null }) {
  const [showAll, setShowAll] = useState(false);
  if (!impact) return null;
  const reached = list(impact.reached);
  const notes = partialNotes(impact);
  if (reached.length === 0 && notes.length === 0) return null;
  const visible = showAll ? reached : reached.slice(0, SHOWN_REPOSITORIES);
  const count = reached.length;
  return (
    <section className="hunk" aria-label="Workspace impact" data-testid="brief-workspace-impact">
      <div className="hunk-head">
        <h3 className="hunk-label">Workspace impact</h3>
      </div>
      <div className="flex flex-col gap-2 p-3">
        {count > 0 && (
          <p className="text-caption text-foreground">
            {`${count} other ${count === 1 ? "repository" : "repositories"} can be affected (${impact.root ?? ""}, this repository is ${impact.repo || "."})`}
          </p>
        )}
        {visible.length > 0 && (
          <ul className="flex flex-col gap-2">
            {visible.map((hit, index) => <ReachedRow key={`${hit.repo}:${index}`} hit={hit} showAll={showAll} />)}
          </ul>
        )}
        {!showAll && count > SHOWN_REPOSITORIES && (
          <UiButton variant="ghost" size="compact" type="button" className="self-start" onClick={() => setShowAll(true)}>
            {`Show all ${count}`}
          </UiButton>
        )}
        {notes.map((note) => <p key={note} className="text-caption text-amber-300/90">{note}</p>)}
        {count > 0 && <p className="text-meta text-muted-foreground">{CLOSING}</p>}
      </div>
    </section>
  );
}
