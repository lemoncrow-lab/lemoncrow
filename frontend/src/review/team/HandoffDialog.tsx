import { useEffect, useState } from "react";

import { Button } from "../../components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Textarea } from "../../components/ui/textarea";
import type { TeamState } from "../types";
import { fetchTeammates, handOffReview, type Teammate } from "./teamApi";
import { awayLine, firstName } from "./teamCopy";

export const NOTE_LIMIT = 300;

/**
 * "Hand off this review" (SPEC 4.16): pick a teammate, leave a note, hand off. A teammate who is away is named with the
 * person who takes their reviews, and the handoff goes there. The result is the server's: what it names is what was
 * stored.
 */
export function HandoffDialog({ reviewId, now, onDone, onClose, onCloseAutoFocus }: {
  reviewId: string;
  now: number;
  onDone: (team: TeamState) => void;
  onClose: () => void;
  onCloseAutoFocus?: (event: Event) => void;
}) {
  const [mates, setMates] = useState<Teammate[] | null>(null);
  const [failed, setFailed] = useState("");
  const [to, setTo] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    fetchTeammates(reviewId)
      .then((list) => { if (live) { setMates(list); setTo(list[0]?.id ?? ""); } })
      .catch(() => { if (live) setFailed("Couldn't load your teammates · try again"); });
    return () => { live = false; };
  }, [reviewId]);
  const picked = mates?.find((mate) => mate.id === to) ?? null;
  const lineFor = (mate: Teammate): string => (mate.away ? awayLine(mate.name, mate.away.until, mate.away.reassign_to?.name ?? null, now) : "");
  const send = async () => {
    if (!picked || busy) return;
    setBusy(true);
    setFailed("");
    try {
      onDone(await handOffReview(reviewId, picked.id, note.trim()));
    } catch (reason) {
      setFailed(reason instanceof Error && reason.message ? reason.message : "Couldn't hand this off · try again");
      setBusy(false);
    }
  };
  return (
    <Dialog open onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent data-testid="handoff-dialog" onCloseAutoFocus={onCloseAutoFocus}>
        <DialogHeader>
          <DialogTitle>Hand off this review</DialogTitle>
        </DialogHeader>
        <DialogBody className="space-y-4">
          {mates === null && !failed ? <p role="status" className="text-label text-muted-foreground">Loading your teammates…</p> : null}
          {mates !== null && mates.length === 0 ? <p data-testid="handoff-none" className="text-label text-muted-foreground">There is no one else on this review to hand it to.</p> : null}
          {mates !== null && mates.length > 0 ? (
            <fieldset className="space-y-1">
              <legend className="text-label font-medium">Pick a teammate</legend>
              {mates.map((mate) => (
                <label key={mate.id} className="flex cursor-pointer items-center gap-2">
                  <Input unstyled type="radio" name="handoff-teammate" value={mate.id} checked={to === mate.id} onChange={() => setTo(mate.id)} />
                  <span>
                    {mate.name}
                    {lineFor(mate) ? <span className="text-muted-foreground"> · {lineFor(mate)}</span> : null}
                  </span>
                </label>
              ))}
            </fieldset>
          ) : null}
          {mates !== null && mates.length > 0 ? (
            <>
              <label className="block space-y-1">
                <span className="font-mono text-tag uppercase text-muted-foreground">Note for them</span>
                <Textarea
                  placeholder="What should they know?"
                  rows={3}
                  maxLength={NOTE_LIMIT}
                  value={note}
                  className="text-copy"
                  onChange={(event) => setNote(event.target.value)}
                />
              </label>
              <p data-testid="handoff-line" className="text-label text-muted-foreground">
                {picked && lineFor(picked) ? lineFor(picked) : picked ? `${firstName(picked.name)} sees your note when they open it.` : ""}
              </p>
            </>
          ) : null}
          {failed ? <p role="alert" data-testid="handoff-error" className="text-label text-p0">{failed}</p> : null}
          <div className="flex flex-wrap gap-2">
            <Button variant="solid" data-testid="handoff-send" disabled={!picked || busy} onClick={() => void send()}>Hand off</Button>
            <Button variant="ghost" data-testid="handoff-cancel" onClick={onClose}>Cancel</Button>
          </div>
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}
