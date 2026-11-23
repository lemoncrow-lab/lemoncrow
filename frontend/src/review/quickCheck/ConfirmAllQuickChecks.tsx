import { useState } from "react";

import { Button } from "../../components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogTitle } from "../../components/ui/dialog";
import type { ReviewTarget } from "../types";
import { confirmAllLabel, confirmedCount, confirmedLine, type QuickConfirmResult } from "./confirmAllModel";
import { patchEvidence, readPatches } from "./patchEvidence";

export interface QuickConfirmActions {
  /** Records Looks good for these parts under the reviewer's name (the server's bulk mark). Null when nothing was saved. */
  confirm: (parts: readonly ReviewTarget[]) => Promise<QuickConfirmResult | null>;
  /** Takes back exactly the marks one confirm recorded; a part answered by hand since keeps that answer. */
  undo: (result: QuickConfirmResult) => Promise<boolean>;
}

/**
 * "Confirm all N quick checks" (Overview's quick checks and All files' quick-check group): one click, a confirm step that
 * names the count and what is left out, then one line with Undo. The code of each part is read first, as Quick check's
 * batch reads it; a part whose code cannot be used here is left for its own answer.
 */
export function ConfirmAllQuickChecks(props: {
  parts: readonly ReviewTarget[];
  ownAnswer: number;
  apiReviewId: string;
  sessionId: string;
  revisionId: string;
  disabled?: boolean;
  actions: QuickConfirmActions;
}) {
  const { parts, ownAnswer, actions } = props;
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<QuickConfirmResult | null>(null);
  const [line, setLine] = useState("");

  const confirm = async () => {
    setBusy(true);
    try {
      const read = patchEvidence(props.sessionId, props.revisionId).loaded;
      await readPatches(props.apiReviewId, props.sessionId, props.revisionId, [...new Set(parts.map((part) => part.path))].filter((path) => !read.has(path)));
      const failed = patchEvidence(props.sessionId, props.revisionId).failed;
      const sent = parts.filter((part) => !failed.has(part.path));
      const unread = parts.length - sent.length;
      const result = sent.length > 0 ? await actions.confirm(sent) : { marked: [], single: [], skipped: [] };
      setOpen(false);
      if (!result) { setLine("The quick checks could not be confirmed. Nothing was recorded."); return; }
      setDone(confirmedCount(result) > 0 ? result : null);
      setLine(`${confirmedLine(result)}${unread > 0 ? ` · the code of ${unread} could not be read here, so ${unread === 1 ? "it needs" : "they need"} their own answer` : ""}`);
    } finally { setBusy(false); }
  };

  const undo = async () => {
    if (!done) return;
    setBusy(true);
    try {
      const ok = await actions.undo(done);
      const count = confirmedCount(done);
      setLine(ok ? `${count} ${count === 1 ? "quick check" : "quick checks"} undone.` : "Undo did not finish. Some quick checks may still be confirmed.");
      if (ok) setDone(null);
    } finally { setBusy(false); }
  };

  if (parts.length === 0 && !line) return null;
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="confirm-all-quick">
      {parts.length > 0 ? (
        <Button variant="outline" size="sm" disabled={props.disabled || busy} aria-haspopup="dialog" onClick={() => setOpen(true)}>
          {confirmAllLabel(parts.length)}
        </Button>
      ) : null}
      {line ? <p role="status" className="text-meta text-muted-foreground" data-testid="confirm-all-quick-status">{line}</p> : null}
      {done ? <Button variant="ghost" size="sm" disabled={busy} onClick={() => void undo()}>Undo</Button> : null}
      <Dialog open={open} onOpenChange={(next) => { if (!busy) setOpen(next); }}>
        <DialogContent aria-describedby={undefined}>
          <DialogTitle>{parts.length === 1 ? "Confirm 1 quick check?" : `Confirm ${parts.length} quick checks?`}</DialogTitle>
          <DialogBody className="space-y-2 text-label">
            <p data-testid="confirm-all-quick-count">{parts.length} {parts.length === 1 ? "quick check" : "quick checks"} will be recorded as Looks good, under your name.</p>
            {ownAnswer > 0 ? <p className="text-muted-foreground">{ownAnswer} {ownAnswer === 1 ? "quick check needs its" : "quick checks need their"} own answer and {ownAnswer === 1 ? "is" : "are"} not included.</p> : null}
            <p className="text-muted-foreground">Decisions are not touched. You can undo this right after.</p>
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" disabled={busy} onClick={() => setOpen(false)}>Cancel</Button>
            <Button disabled={busy} onClick={() => void confirm()}>{busy ? "Confirming…" : parts.length === 1 ? "Confirm 1 quick check" : `Confirm ${parts.length}`}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
