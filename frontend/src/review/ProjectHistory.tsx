/**
 * Project history: what the repository's landed history says about the files this change touches. A plain read of
 * derived Project Knowledge that starts no AI or background work. It is shown as background, never as a fact about the
 * revision on screen, and nothing renders when the repository has no history, so a Review without it looks the same.
 */
import { useEffect, useMemo, useState } from "react";

import { Label } from "./overview/OverviewParts";
import { plural } from "./overview/overviewModel";
import { fetchReviewProjectContext, reviewHref } from "./reviewApi";
import type { ReviewProjectContext, ReviewProjectContextEntry } from "./types";

export interface ProjectHistoryProps {
  apiReviewId: string;
  revisionNumber: number;
  purpose?: "brief" | "map";
  /** History for these paths; the server uses the change's own paths when absent. */
  paths?: readonly string[];
}

/** The server accepts at most this many paths in one read. */
const MAX_PATHS = 16;

/** A bare "this path exists" row says nothing; the Map keeps inventory only where it records a replaced file. */
export function historyRows(
  context: ReviewProjectContext
): ReviewProjectContextEntry[] {
  return [...context.entries, ...context.narration].filter(
    (row) => row.knowledge_kind !== "inventory" || row.state === "superseded"
  );
}

function HistoryRow({ row }: { row: ReviewProjectContextEntry }) {
  const reviews = row.citations.filter((citation) => citation.review_id);
  const narrated = row.context_class === "ai_narration";
  return (
    <li
      className="flex min-w-0 flex-col gap-0.5 [overflow-wrap:anywhere]"
      data-testid="project-history-row"
    >
      <span className="font-mono text-meta text-muted-foreground">
        {row.subject.key}
      </span>
      <span className="text-caption text-foreground">{row.summary}</span>
      <span
        className="text-meta text-muted-foreground"
        data-testid="project-history-freshness"
      >
        {narrated
          ? `AI narration, not evidence · ${row.freshness_label}`
          : row.freshness_label}
      </span>
      {row.citations.length > 0 ? (
        <details
          className="text-meta text-muted-foreground"
          data-testid="project-history-sources"
        >
          <summary className="cursor-pointer">Sources</summary>
          <ul className="mt-1 flex flex-col gap-0.5 pl-2">
            {row.citations.map((citation) => (
              <li key={citation.ref}>{citation.label ?? citation.ref}</li>
            ))}
            {row.citations_omitted ? (
              <li>{`${row.citations_omitted} more`}</li>
            ) : null}
          </ul>
        </details>
      ) : null}
      {reviews.map((citation) => (
        <a
          key={citation.ref}
          className="self-start text-meta underline underline-offset-2"
          href={reviewHref(citation.review_id ?? "")}
          data-testid="project-history-review"
        >
          Open the Review it came from
        </a>
      ))}
    </li>
  );
}

export default function ProjectHistory({
  apiReviewId,
  revisionNumber,
  purpose = "brief",
  paths,
}: ProjectHistoryProps) {
  const pathKey = (paths ?? []).slice(0, MAX_PATHS).join("\n");
  const request = `${apiReviewId}:${revisionNumber}:${purpose}:${pathKey}`;
  const [loaded, setLoaded] = useState<{
    request: string;
    context: ReviewProjectContext;
  } | null>(null);

  useEffect(() => {
    let live = true;
    fetchReviewProjectContext(
      apiReviewId,
      revisionNumber,
      purpose,
      pathKey ? { paths: pathKey.split("\n") } : {}
    )
      .then((payload) => {
        if (live) setLoaded({ request, context: payload.project_context });
      })
      // History is optional context: a failed read leaves the screen as it is without it.
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [apiReviewId, pathKey, purpose, request, revisionNumber]);

  const context = loaded?.request === request ? loaded.context : null;
  const rows = useMemo(
    () => (context?.available ? historyRows(context) : []),
    [context]
  );
  if (!context || rows.length === 0) return null;
  const hidden =
    context.entries.length + context.narration.length - rows.length;
  const more = context.omitted + hidden;

  return (
    <section
      aria-label="Project history"
      className="rounded-lg border border-border bg-card"
      data-testid="project-history"
    >
      <div className="flex min-h-9 items-center px-3 py-2">
        <Label>{`Project history · ${plural(rows.length, "entry", "entries")}`}</Label>
      </div>
      <div className="flex flex-col gap-3 border-t border-border p-3">
        <p
          className="text-meta text-muted-foreground"
          data-testid="project-history-note"
        >
          {context.authority_note}
        </p>
        <ul className="flex flex-col gap-3">
          {rows.map((row) => (
            <HistoryRow key={row.entry_id} row={row} />
          ))}
        </ul>
        {more > 0 ? (
          <p
            className="text-meta text-muted-foreground"
            data-testid="project-history-more"
          >{`${more} more not shown`}</p>
        ) : null}
      </div>
    </section>
  );
}
