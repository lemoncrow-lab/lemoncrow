import { useRef } from "react";

import { Button } from "../../components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import type { TeamState } from "../types";
import { firstName, initials, presenceBody } from "./teamCopy";

/**
 * "Maya is reviewing this" (SPEC 4.16): opening a review a teammate is in asks first. Dialog, trapped focus, Esc closes
 * and returns focus to the row that opened it. Ink "Review together", "Review anyway", "Pick another"; links
 * "I'll take this" and "Hand off".
 */
export function PresenceDialog({ team, now, onTogether, onAnyway, onOther, onTake, onHandOff, onClose }: {
  team: TeamState;
  now: number;
  onTogether: () => void;
  onAnyway: () => void;
  onOther: () => void;
  onTake: () => void;
  onHandOff: () => void;
  onClose: () => void;
}) {
  // "Pick another" moves on to a different row, so focus is not handed back to this one.
  const movingOn = useRef(false);
  const person = team.reviewing[0];
  if (!person) return null;
  const name = firstName(person.name);
  return (
    <Dialog open onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent aria-describedby="presence-body" data-testid="presence-dialog" onCloseAutoFocus={(event) => { if (movingOn.current) event.preventDefault(); }}>
        <DialogHeader>
          <DialogTitle>{name} is reviewing this</DialogTitle>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <p id="presence-body" className="flex items-start gap-2 text-body">
            <span aria-hidden="true" className="mt-0.5 inline-flex size-6 shrink-0 items-center justify-center rounded-full border border-border bg-muted font-mono text-tag">{initials(person.name)}</span>
            <span data-testid="presence-text">{presenceBody(person, now)}</span>
          </p>
          <div className="flex flex-wrap gap-2">
            <Button variant="solid" data-testid="presence-together" onClick={onTogether}>Review together</Button>
            <Button variant="outline" data-testid="presence-anyway" onClick={onAnyway}>Review anyway</Button>
            <Button variant="outline" data-testid="presence-other" onClick={() => { movingOn.current = true; onOther(); }}>Pick another</Button>
          </div>
          <div className="flex flex-wrap gap-x-4">
            <Button variant="link" data-testid="presence-take" onClick={onTake}>I'll take this</Button>
            <Button variant="link" data-testid="presence-handoff" onClick={onHandOff}>Hand off</Button>
          </div>
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}
