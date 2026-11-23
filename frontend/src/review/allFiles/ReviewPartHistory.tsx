import { revisionName } from "../revisionLabel";
import { useEffect, useState } from "react";
import { fetchReviewDevelopmentEvents, fetchTargetHistory } from "../reviewApi";
import { noteStateWord, markEventSentence } from "./allFilesModel";
import type { Annotation, DevelopmentEvent, ReviewMarkEvent, ReviewTarget } from "../types";

interface HistoryResult {
  events: DevelopmentEvent[];
  marks: ReviewMarkEvent[];
  partial: boolean;
  error: string;
}

/** Server-sourced human marks and Revision development events; never a made-up Git log. */
export function ReviewPartHistory({ apiReviewId, revisionId, revisionNumber, part, notes }: {
  apiReviewId: string;
  revisionId: string;
  revisionNumber: number;
  part: ReviewTarget;
  notes: readonly Annotation[];
}) {
  const key = `${apiReviewId}:\u0000${revisionId}:\u0000${part.unit_key}`;
  const [state, setState] = useState<{ key: string; data: HistoryResult } | null>(null);

  useEffect(() => {
    let live = true;
    setState(null);
    void Promise.allSettled([
      fetchTargetHistory(apiReviewId, part.unit_key),
      fetchReviewDevelopmentEvents(apiReviewId, { revisionNumber, scope: "snapshot", view: "changes", limit: 20, path: part.path }),
    ]).then(([marks, events]) => {
      if (!live) return;
      const acceptedMarks = marks.status === "fulfilled" && marks.value.unit_key === part.unit_key ? marks.value.events : [];
      const acceptedEvents = events.status === "fulfilled" && events.value.revision_id === revisionId ? events.value.events.filter((event) => event.path === part.path) : [];
      const partial = marks.status !== "fulfilled" || events.status !== "fulfilled" || events.value.revision_id !== revisionId;
      setState({ key, data: { marks: acceptedMarks, events: acceptedEvents, partial, error: partial ? "Some server history is unavailable; an empty list is not proof no prior activity occurred." : "" } });
    });
    return () => { live = false; };
  }, [key, apiReviewId, revisionId, revisionNumber, part.path, part.unit_key]);

  const data = state?.key === key ? state.data : null;
  return <div className="space-y-3" data-testid="all-files-history">
    <h3 className="text-label font-semibold">History · {part.symbol || part.path}</h3>
    <p className="text-meta text-muted-foreground">{revisionName(revisionNumber)} · saved human marks and development events, not the repository's Git log.</p>
    {!data && <p role="status" className="text-meta">Loading recorded history…</p>}
    {data?.error && <p role="status" className="text-meta text-p1">{data.error}</p>}
    {notes.length > 0 && <section className="space-y-2" aria-label="Human review notes">
      <h4 className="text-meta font-medium">Your notes · {notes.length}</h4>
      {notes.map((note) => <div key={note.id} className="border-l-2 border-p2-solid pl-3 text-meta">
        <p className="whitespace-pre-wrap">{note.body}</p>
        <p className="mt-1 text-muted-foreground">Saved note · {note.revision_id === revisionId ? "this revision" : "inherited"} · {noteStateWord(note.state)}</p>
      </div>)}
    </section>}
    {data && <section className="space-y-2" aria-label="Human answer changes">
      <h4 className="text-meta font-medium">Human answer changes · {data.marks.length}</h4>
      {data.marks.slice(-12).reverse().map((event) => <div key={event.id} className="border-l-2 border-border pl-3 text-meta">
        <p>{markEventSentence(event)}</p>
        <p className="text-muted-foreground">{event.created_at} · Revision {event.revision_id === revisionId ? "current" : "earlier"}</p>
      </div>)}
      {data.marks.length === 0 && <p className="text-meta text-muted-foreground">No human mark events were returned for this target.</p>}
    </section>}
    {data && <section className="space-y-2" aria-label="Recorded development">
      <h4 className="text-meta font-medium">Development events · {data.events.length}</h4>
      {data.events.slice(0, 12).map((event) => <div key={event.id} className="border-l-2 border-border pl-3 text-meta">
        <p>{event.summary || "Development event"}</p>
        <p className="text-muted-foreground">{event.at || "Time unknown"} · {event.source || "Source not recorded"}</p>
      </div>)}
      {data.events.length === 0 && <p className="text-meta text-muted-foreground">No path-matched development events were returned for this revision.</p>}
    </section>}
    <p className="text-meta text-muted-foreground">Git commit history for this symbol is not provided by the Review endpoint.</p>
  </div>;
}
