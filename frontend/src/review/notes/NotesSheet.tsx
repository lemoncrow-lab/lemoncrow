import { useEffect, useMemo, useState } from "react";
import { MessageSquareText, X } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { EmptyState } from "../../components/ui/empty-state";
import { Sheet, SheetClose, SheetContent, SheetLede, SheetTitle } from "../../components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../components/ui/tabs";
import AnnotationCard from "../AnnotationCard";
import { isUnanchoredComment, markerSide } from "../annotationModel";
import { BelowThresholdList } from "../findingThreshold";
import type { FlowReviewTools } from "../flowReviewTools";
import { partRoute } from "../overview/overviewModel";
import { revisionName } from "../revisionLabel";
import { buildReviewFlowPath } from "../reviewRoutes";
import type { Annotation, AnnotationDraft, AnnotationKind, ReviewTarget } from "../types";
import { inNotesFilter, listedNotes, NOTES_FILTERS, noteLocation, notesCounts, noteTarget, type NotesFilter } from "./notesModel";

const EMPTY: Record<NotesFilter, { title: string; description: string }> = {
  open: { title: "Nothing open", description: "Every comment and finding is resolved or dismissed." },
  rereview: { title: "Nothing to re-review", description: "When the author marks a comment addressed, it waits here for you." },
  resolved: { title: "Nothing resolved yet", description: "Comments you resolve and findings you dismiss land here." },
  orphaned: { title: "No moved lines", description: "Every comment still points at its lines." },
};

/** A new note at `anchor`'s lines: a reply in its thread, or the request a finding turns into. */
function draftAt(anchor: Annotation, body: string, kind: AnnotationKind, parentId: string, markTarget: boolean, unitKey?: string): AnnotationDraft | null {
  const fileLevel = Boolean(anchor.file_level);
  const start = fileLevel ? 0 : anchor.start_line;
  if (!fileLevel && start < 1) return null;
  return {
    path: anchor.path,
    start_line: start,
    end_line: fileLevel ? 0 : anchor.end_line,
    side: markerSide(anchor.side),
    body,
    kind,
    file_level: fileLevel,
    parent_id: parentId || undefined,
    target_unit_key: unitKey,
    mark_target: markTarget && Boolean(unitKey),
  };
}

/**
 * Every comment and AI finding in the review in one list (the top bar's "Notes" and ⌘K): Open, Needs re-review,
 * Resolved and Lines moved, each counted. A row is the same card the part shows, so resolving, replying and the
 * finding actions (Request change, Dismiss, Fix) work here too, comments whose lines moved included. What you resolve
 * stays where it is until you switch tabs, so the next click never lands on a neighbour.
 */
export function NotesSheet({ tools, open, onOpenChange }: { tools: FlowReviewTools; open: boolean; onOpenChange(open: boolean): void }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [filter, setFilter] = useState<NotesFilter>("open");
  const [kept, setKept] = useState<ReadonlySet<string>>(() => new Set());
  const [composing, setComposing] = useState("");
  useEffect(() => {
    if (!open) return;
    setFilter("open");
    setKept(new Set());
    setComposing("");
  }, [open]);

  const notes = useMemo(() => listedNotes(tools.annotations), [tools.annotations]);
  const counts = useMemo(() => notesCounts(tools.annotations), [tools.annotations]);
  const visible = notes.filter((note) => kept.has(note.id) || inNotesFilter(note, filter));
  const all = tools.annotations as Annotation[];
  const blocked = tools.busy || tools.readOnly;
  const keep = (id: string) => setKept((current) => new Set(current).add(id));

  const showPart = (target: ReviewTarget) => {
    onOpenChange(false);
    const path = buildReviewFlowPath(tools.reviewRef, partRoute(target, tools.targets));
    navigate(tools.historical ? `${path}${location.search}` : path);
  };
  const submit = (note: Annotation, target: ReviewTarget | null, body: string, kind: AnnotationKind, parentId: string, markTarget: boolean) => {
    const anchor = parentId ? all.find((item) => item.id === parentId) ?? note : note;
    const draft = draftAt(anchor, body, kind, parentId, parentId ? false : markTarget, parentId ? undefined : target?.unit_key || undefined);
    if (!draft) return;
    keep(note.id);
    void Promise.resolve(tools.createNote(draft)).then((saved) => { if (saved && !parentId) setComposing(""); });
  };

  const lede = [
    `${counts.open} open`,
    counts.rereview > 0 ? `${counts.rereview} need re-review` : "",
    tools.historical ? `as of ${revisionName(tools.revisionNumber)}, read-only` : tools.readOnly ? "read-only" : "",
  ].filter(Boolean).join(" · ");

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-[min(600px,100%)]" data-testid="notes-sheet">
        <div className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-2">
          <SheetTitle className="min-w-0 flex-1 text-heading font-medium">Notes</SheetTitle>
          <SheetClose asChild>
            <Button variant="ghost" size="icon" className="shrink-0" aria-label="Close"><X size={16} aria-hidden="true" /></Button>
          </SheetClose>
        </div>
        <SheetLede>{lede}</SheetLede>
        <Tabs value={filter} onValueChange={(value) => { setKept(new Set()); setComposing(""); setFilter(value as NotesFilter); }} className="flex min-h-0 flex-1 flex-col">
          <div className="shrink-0 overflow-x-auto border-b border-border px-4 py-2">
            <TabsList aria-label="Which notes">
              {NOTES_FILTERS.map((item) => (
                <TabsTrigger key={item.id} value={item.id}>
                  {item.label}
                  <span className="font-mono text-tag text-muted-foreground">{counts[item.id]}</span>
                </TabsTrigger>
              ))}
            </TabsList>
          </div>
          <TabsContent value={filter} className="min-h-0 flex-1 overflow-y-auto px-4 py-2">
            {visible.length === 0 ? (
              <EmptyState
                frame={false}
                icon={<MessageSquareText size={20} aria-hidden="true" />}
                title={notes.length === 0 ? "No comments yet" : EMPTY[filter].title}
                description={notes.length === 0 ? "Comments and AI findings on any part show up here." : EMPTY[filter].description}
              />
            ) : (
              <div className="flex flex-col">
                <BelowThresholdList items={visible} render={(note) => {
                  const target = noteTarget(note, tools.targets);
                  const moved = isUnanchoredComment(note);
                  return (
                    <article key={note.id} className="border-t border-border py-3 first:border-t-0" data-testid="notes-row" data-note={note.id}>
                      <div className="flex min-w-0 flex-wrap items-center gap-2 text-meta text-muted-foreground">
                        <span className="min-w-0 flex-1 truncate font-mono" title={noteLocation(note)}>{noteLocation(note)}</span>
                        {moved ? <Badge tone="warning">Lines moved</Badge> : null}
                        {target ? <Button variant="ghost" size="compact" onClick={() => showPart(target)}>Show part</Button> : null}
                      </div>
                      {moved ? (
                        <p className="mt-1 text-meta text-muted-foreground" data-testid="notes-moved-reason">
                          {note.anchor_method_label || "Not anchored"}: {note.anchor_detail || "no reason recorded"}
                        </p>
                      ) : null}
                      <AnnotationCard
                        comments={[{ ...note, below_threshold: false }]}
                        all={all}
                        label="Request a change"
                        composing={composing === note.id}
                        busy={blocked}
                        targetMarkAvailable={Boolean(target)}
                        draftBody={note.title ? `${note.title}\n\n${note.body}` : note.body}
                        draftKind="request_change"
                        draftFocusKey={note.id}
                        onSubmit={(body, kind, parentId, markTarget) => submit(note, target, body, kind, parentId, markTarget)}
                        onCancel={() => setComposing("")}
                        onResolve={(id) => { keep(note.id); tools.setState(id, "resolved"); }}
                        onReopen={(id) => { keep(note.id); tools.setState(id, "open"); }}
                        onFindingDisposition={(id, disposition, reason) => { keep(note.id); tools.setDisposition(id, disposition, reason); }}
                        onFindingRequestChange={(finding) => {
                          keep(finding.id);
                          tools.setDisposition(finding.id, "confirmed_by_reviewer");
                          setComposing(finding.id);
                        }}
                      />
                    </article>
                  );
                }} />
              </div>
            )}
          </TabsContent>
        </Tabs>
      </SheetContent>
    </Sheet>
  );
}
