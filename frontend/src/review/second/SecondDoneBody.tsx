import { CheckCircle2, Clock3, ShieldAlert } from "lucide-react";
import { forwardRef, useState } from "react";

import { Button } from "../../components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogTitle } from "../../components/ui/dialog";
import { SignedRecordLine } from "../record/SignedRecordLine";
import type { DoneCopy } from "./secondCopy";
import { firstName } from "./secondCopy";
import type { SecondReviewerStatus } from "./secondApi";

const ICON = { warning: Clock3, success: CheckCircle2, danger: ShieldAlert } as const;

/**
 * Done for the first reviewer while a second signature is waited for, given, or refused (SPEC 4.16, R3, R13). The
 * numbers in the card are the server's count of signatures that exist and verify.
 */
export const SecondDoneBody = forwardRef<
  HTMLHeadingElement,
  { copy: DoneCopy; status: SecondReviewerStatus; revisionLabel: string; reviewId: string; revisionId: string; revisionNumber: number }
>(function SecondDoneBody({ copy, status, revisionLabel, reviewId, revisionId, revisionNumber }, ref) {
    const [noteOpen, setNoteOpen] = useState(false);
    const Icon = ICON[copy.tone];
    const note = status.second?.note ?? "";
    const who = status.reviewer ? firstName(status.reviewer.name) : "The second reviewer";
    return (
      <div role="region" aria-label="Done" data-testid="done-second" data-second-state={status.state} className="min-w-0 flex-1">
        <section className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
          <Icon size={20} aria-hidden="true" />
          <p className="font-mono text-tag text-muted-foreground">Review · {revisionLabel}</p>
          <h1 ref={ref} tabIndex={-1} className="text-title font-semibold focus:outline-none" data-testid="done-title">{copy.title}</h1>
          <div className="rounded-lg border border-border bg-card p-4" data-testid="done-second-card">
            <h2 className="text-heading font-semibold" data-testid="done-second-card-title">{copy.card}</h2>
            <p className="mt-1 text-label text-muted-foreground" data-testid="done-second-card-body">{copy.body}</p>
          </div>
          {copy.told ? <p className="text-label text-muted-foreground" data-testid="done-second-told">{copy.told}</p> : null}
          <SignedRecordLine reviewId={reviewId} revisionId={revisionId} revisionNumber={revisionNumber} decidedBy={status.first_reviewer?.id ?? ""} />
          {status.state === "disagrees" && note ? (
            <div>
              <Button variant="outline" data-testid="done-read-note" onClick={() => setNoteOpen(true)}>Read {who}'s note</Button>
              <Dialog open={noteOpen} onOpenChange={setNoteOpen}>
                <DialogContent aria-describedby={undefined}>
                  <DialogTitle>{who}'s note</DialogTitle>
                  <DialogBody><p className="whitespace-pre-wrap text-label" data-testid="done-note-text">{note}</p></DialogBody>
                  <DialogFooter><Button variant="outline" onClick={() => setNoteOpen(false)}>Close</Button></DialogFooter>
                </DialogContent>
              </Dialog>
            </div>
          ) : null}
        </section>
      </div>
    );
});
