import { useEffect, useRef, useState, type FormEvent } from "react";
import { History } from "lucide-react";

import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";
import { reviewHref } from "./reviewApi";
import type { ReviewProjectContext, ReviewProjectContextEntry, ReviewProjectEvolutionEvent } from "./types";

/**
 * PK2 landed history. Every surface that shows Project Knowledge labels it as
 * history from the canonical branch, never as a fact about this Snapshot, and
 * links each entry to the evolution event / Review it came from.
 */

const EVENT_LABEL: Record<string, string> = {
  baseline: "Baseline observed",
  merged_change: "Landed Change",
  source_advance: "Canonical branch advanced",
};

function eventLabel(event: ReviewProjectEvolutionEvent | null): string {
  if (!event) return "";
  return `${EVENT_LABEL[event.kind] ?? event.kind} · ${event.observed_at.slice(0, 10)}`;
}

function stateTone(entry: ReviewProjectContextEntry): string {
  if (entry.stale_for_snapshot) return "text-amber-300";
  if (entry.state === "superseded" || entry.state === "historical") return "text-muted-foreground";
  if (entry.state === "uncertain") return "text-amber-200";
  return "text-cyan-300";
}

export function ProjectHistoryEntry({ entry }: { entry: ReviewProjectContextEntry }) {
  const [open, setOpen] = useState(false);
  const reviewLinks = entry.citations.filter((citation) => citation.resolved && citation.review_id);
  return (
    <li className="flex flex-col gap-0.5" data-testid="project-history-entry" data-state={entry.state}>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-caption text-foreground">{entry.summary}</span>
      </div>
      <div className="flex flex-wrap items-center gap-x-2 text-micro">
        <span className={stateTone(entry)} data-testid="project-history-freshness">{entry.freshness_label}</span>
        <span className="text-muted-foreground">{`${entry.subject.key} · ${entry.knowledge_kind}`}</span>
        {entry.context_class === "ai_narration" && <span className="text-muted-foreground">AI narration · not evidence</span>}
        <UiButton variant="ghost" size="compact" type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
          {open ? "Hide source history" : "Source history"}
        </UiButton>
      </div>
      {open && (
        <ul className="ml-2 flex flex-col gap-0.5 border-l border-border/70 pl-2 text-micro text-muted-foreground" data-testid="project-history-citations">
          {entry.history.origin && <li>{`Introduced: ${eventLabel(entry.history.origin)}`}</li>}
          {entry.history.superseded_by && <li>{`Superseded: ${eventLabel(entry.history.superseded_by)}`}</li>}
          {reviewLinks.map((citation) => (
            <li key={citation.ref}>
              <a className="text-cyan-300 underline-offset-2 hover:underline" href={reviewHref(citation.review_id ?? "")}>
                {citation.label ?? "Open Review"}
              </a>
            </li>
          ))}
          {entry.citations
            .filter((citation) => !(citation.resolved && citation.review_id) && citation.kind !== "generation")
            .map((citation) => <li key={citation.ref}>{citation.label ?? citation.ref}</li>)}
          {(entry.citations_omitted ?? 0) > 0 && <li>{`${entry.citations_omitted} more sources not shown`}</li>}
        </ul>
      )}
    </li>
  );
}

export function ProjectHistoryList({ context, testId }: { context: ReviewProjectContext; testId: string }) {
  const rows = [...context.entries, ...context.narration];
  return (
    <div className="flex flex-col gap-1.5" data-testid={testId}>
      <ul className="flex flex-col gap-1.5">
        {rows.map((entry) => <ProjectHistoryEntry key={entry.entry_id} entry={entry} />)}
      </ul>
      {context.omitted > 0 && (
        <p className="text-micro text-muted-foreground">{`${context.omitted} more not shown · history is bounded`}</p>
      )}
    </div>
  );
}

/**
 * Brief: a compact "Relevant history" section, rendered only when landed
 * history exists for this Change. Loading is a plain read; it never starts AI.
 */
export default function BriefProjectContext({ load }: { load: (term?: string) => Promise<ReviewProjectContext | null> }) {
  const [context, setContext] = useState<ReviewProjectContext | null>(null);
  const [term, setTerm] = useState("");
  const [searched, setSearched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [searchError, setSearchError] = useState("");
  const generationRef = useRef(0);
  useEffect(() => {
    const generation = ++generationRef.current;
    setContext(null);
    setTerm("");
    setSearched(false);
    setSearchError("");
    Promise.resolve()
      .then(() => load())
      .then((next) => { if (generationRef.current === generation) setContext(next); })
      .catch(() => { if (generationRef.current === generation) setContext(null); });
    return () => { generationRef.current += 1; };
  }, [load]);
  const search = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const generation = ++generationRef.current;
    const bounded = term.trim().slice(0, 256);
    setBusy(true);
    setSearchError("");
    Promise.resolve()
      .then(() => load(bounded))
      .then((next) => {
        if (generationRef.current !== generation) return;
        setContext(next);
        setSearched(Boolean(bounded));
      })
      .catch(() => { if (generationRef.current === generation) setSearchError("Historical search is unavailable."); })
      .finally(() => { if (generationRef.current === generation) setBusy(false); });
  };
  if (!context || !context.available) return null;
  return (
    <section aria-label="Relevant history" className="flex flex-col gap-1.5 border-t border-border/70 py-3" data-testid="brief-project-context">
      <div className="flex items-center gap-2 text-label font-medium text-foreground">
        <History size={14} aria-hidden="true" className="text-muted-foreground" />
        <span>Relevant history</span>
        <span className="text-micro font-normal text-muted-foreground">Landed history · advisory, not evidence for this Snapshot</span>
      </div>
      <form onSubmit={search} className="flex min-w-0 flex-wrap items-center gap-1.5" data-testid="project-history-search">
        <UiInput uiSize="xs" aria-label="Search landed project history" placeholder="Search landed history" value={term}
          onChange={(event) => setTerm(event.target.value)} maxLength={256} className="min-w-[160px] flex-1" />
        <UiButton variant="outline" size="compact" type="submit" disabled={busy}>{busy ? "Searching…" : "Search"}</UiButton>
      </form>
      {searchError && <p role="status" className="text-micro text-amber-300">{searchError}</p>}
      {context.entries.length + context.narration.length === 0
        ? <p className="text-caption text-muted-foreground">
            {searched ? "No matching landed history. This does not prove the change is safe." : "No history matched these changes. Search earlier project decisions or documentation."}
          </p>
        : <ProjectHistoryList context={context} testId="brief-project-context-list" />}
    </section>
  );
}

/**
 * Map: history for one selected component or edge, loaded only on an explicit
 * request and drawn apart from the exact current edges.
 */
export function MapProjectHistory({
  load,
  selectionKey,
}: {
  load: () => Promise<ReviewProjectContext | null>;
  /** Identity of the selected component or edge; a new selection starts idle again. */
  selectionKey: string;
}) {
  // Keyed by the selection: a new selection mounts fresh state, so the previous
  // selection's history never paints (even for one frame) under a new node.
  return <MapProjectHistoryForSelection key={selectionKey} load={load} />;
}

function MapProjectHistoryForSelection({ load }: { load: () => Promise<ReviewProjectContext | null> }) {
  const [state, setState] = useState<"idle" | "loading" | "loaded" | "failed">("idle");
  const [context, setContext] = useState<ReviewProjectContext | null>(null);
  // One request per mounted selection (the button shows only while idle); an
  // answer that lands after the selection changed updates an unmounted body.
  const request = () => {
    setState("loading");
    Promise.resolve()
      .then(load)
      .then((next) => {
        setContext(next);
        setState("loaded");
      })
      .catch(() => setState("failed"));
  };
  const rows = context ? context.entries.length + context.narration.length : 0;
  return (
    <section
      aria-label="Landed history"
      className="flex flex-col gap-1.5 rounded-md border border-dashed border-border/80 px-2 py-1.5"
      data-testid="map-project-history"
    >
      <p className="text-micro text-muted-foreground">Landed history · not part of this Snapshot</p>
      {state === "idle" && (
        <UiButton variant="ghost" size="compact" type="button" onClick={request}>Show landed history</UiButton>
      )}
      {state === "loading" && <p className="text-micro text-muted-foreground">Loading history…</p>}
      {state === "failed" && <p className="text-micro text-amber-300">History is unavailable.</p>}
      {state === "loaded" && context && rows > 0 && <ProjectHistoryList context={context} testId="map-project-history-list" />}
      {state === "loaded" && (!context || rows === 0) && (
        <p className="text-micro text-muted-foreground">
          {context && !context.available && context.reason === "not_configured"
            ? "Project Knowledge is not configured for this repository."
            : "No landed history for this selection."}
        </p>
      )}
    </section>
  );
}
