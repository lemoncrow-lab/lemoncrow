/**
 * Workspace impact on the Brief (multi-repo workspace P4): the other repositories of the reviewer's local workspace that
 * depend on what changed, with the evidence for each. The same content as the terminal's WORKSPACE IMPACT section. It is
 * never a finding and counts toward nothing. No line from another repository is shown: only repositories, paths, keys
 * and line numbers (the server leaves each link's `detail` out).
 */
import { useState } from "react";

import { Button } from "../../components/ui/button";
import { plural } from "../overview/overviewModel";
import { plainCode } from "../plainWords";
import type { ReviewWorkspaceEvidence, ReviewWorkspaceImpact, ReviewWorkspaceReached } from "../types";

const SHOWN_REPOSITORIES = 5;
const SHOWN_LINKS = 2;

const list = <T,>(value: readonly T[] | undefined | null): readonly T[] => (Array.isArray(value) ? value : []);

function where(evidence: ReviewWorkspaceEvidence | undefined): string {
  const path = String(evidence?.path ?? "");
  const line = Number(evidence?.line ?? 0);
  return line ? `${path}:${line}` : path;
}

/** What could not be checked, so an empty or short list is not read as "nothing else is affected". */
export function workspaceImpactNotes(impact: Partial<ReviewWorkspaceImpact>): string[] {
  return [
    ...list(impact.errors).map((error) => `${error.repo}: its changes could not be listed`),
    ...list(impact.incomplete).map((repo) => `${repo} was not fully scanned, so reach through it may be missing`),
    ...(impact.members_truncated ? ["The repository limit was reached; later repositories were not scanned"] : []),
  ];
}

/** The slice when it has anything to say; a Review with no workspace has `{}`. */
export function workspaceImpactOf(impact: ReviewWorkspaceImpact | Record<string, never> | null | undefined): Partial<ReviewWorkspaceImpact> | null {
  if (!impact) return null;
  const value: Partial<ReviewWorkspaceImpact> = impact;
  return list(value.reached).length > 0 || workspaceImpactNotes(value).length > 0 ? value : null;
}

function ReachedRepository({ hit, showAll }: { hit: ReviewWorkspaceReached; showAll: boolean }) {
  const hops = Number(hit.hops ?? 1);
  const via = list(hit.path).join(" → ");
  return (
    <li className="flex flex-col gap-0.5" data-testid="brief-workspace-repo">
      <div className="flex flex-wrap items-baseline gap-x-2 text-caption">
        <span className="font-mono font-medium text-foreground">{hit.repo}</span>
        <span className="text-muted-foreground">{plural(hops, "hop")}</span>
        <span className="text-muted-foreground">{hit.confidence} confidence</span>
        <span className="text-muted-foreground">{hit.direct ? "direct" : "repository-level"}</span>
        {hops > 1 && via ? <span className="text-muted-foreground">via {via}</span> : null}
      </div>
      {list(hit.edges).map((edge, index) => {
        const links = list(edge.links);
        const shown = showAll ? links : links.slice(0, SHOWN_LINKS);
        const more = links.length - shown.length + Number(edge.more ?? 0);
        return (
          <ul key={`${edge.kind}:${index}`} className="flex flex-col gap-0.5 pl-3 font-mono text-meta text-muted-foreground">
            {shown.map((link, linkIndex) => (
              <li key={`${link.key}:${linkIndex}`} className="break-all">
                {`${plainCode(edge.kind)} ${link.key}: ${where(link.consumer)} uses ${where(link.provider)}`}
              </li>
            ))}
            {more > 0 ? <li>{`${more} more ${plainCode(edge.kind)} evidence`}</li> : null}
          </ul>
        );
      })}
    </li>
  );
}

export default function BriefWorkspaceImpact({ impact }: { impact: Partial<ReviewWorkspaceImpact> }) {
  const [showAll, setShowAll] = useState(false);
  const reached = list(impact.reached);
  const notes = workspaceImpactNotes(impact);
  const visible = showAll ? reached : reached.slice(0, SHOWN_REPOSITORIES);
  return (
    <>
      {reached.length > 0 ? (
        <p className="text-caption text-foreground">
          {`${reached.length === 1 ? "1 other repository" : `${reached.length} other repositories`} can be affected (${impact.root ?? ""}, this repository is ${impact.repo || "."})`}
        </p>
      ) : null}
      {visible.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {visible.map((hit, index) => <ReachedRepository key={`${hit.repo}:${index}`} hit={hit} showAll={showAll} />)}
        </ul>
      ) : null}
      {!showAll && reached.length > SHOWN_REPOSITORIES ? (
        <Button variant="ghost" size="sm" className="self-start" data-testid="brief-workspace-show-all" onClick={() => setShowAll(true)}>
          {`Show all ${reached.length}`}
        </Button>
      ) : null}
      {notes.map((note) => <p key={note} className="text-caption text-p1">{note}</p>)}
      {reached.length > 0 ? (
        <p className="text-meta text-muted-foreground">Not findings: repositories that depend on what changed, for a person to check.</p>
      ) : null}
    </>
  );
}
