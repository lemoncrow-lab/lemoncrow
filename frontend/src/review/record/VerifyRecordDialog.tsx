import { useCallback, useEffect, useState } from "react";
import { Copy, Download } from "lucide-react";

import { verifyTail } from "../second/secondCopy";
import { Alert } from "../../components/ui/alert";
import { Button } from "../../components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogTitle } from "../../components/ui/dialog";
import { fetchReviewRecordVerify } from "../reviewApi";
import type { ReviewRecordSummary, ReviewRecordVerifyResult } from "../types";
import { LEGAL_SENTENCE, recordLink, verifyCommand, verifyFooter, verifyTone } from "./recordModel";

export interface VerifyRecordDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  reviewId: string;
  record: ReviewRecordSummary;
  onDownloadJson: () => void;
  onDownloadPdf: () => void;
  /** Fires with each fresh check so the Details beside the dialog read the same record state. */
  onChecked?: (result: ReviewRecordVerifyResult) => void;
}

/** SPEC 4.18: a real dialog (focus trapped) whose result row is a status. Every open checks the record again. */
export function VerifyRecordDialog({ open, onOpenChange, reviewId, record, onDownloadJson, onDownloadPdf, onChecked }: VerifyRecordDialogProps) {
  const [result, setResult] = useState<ReviewRecordVerifyResult | null>(null);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState("");
  useEffect(() => {
    if (!open) return;
    let active = true;
    setResult(null); setError(""); setCopied("");
    fetchReviewRecordVerify(reviewId, record.record_id).then((next) => {
      if (!active) return;
      setResult(next);
      onChecked?.(next);
    }).catch(() => { if (active) setError("The record could not be checked. Nothing about it has changed."); });
    return () => { active = false; };
    // onChecked is the caller's setter; a new function each render must not re-check the record.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, reviewId, record.record_id]);

  const command = verifyCommand(recordLink(window.location.origin, reviewId, record.record_id));
  const copy = useCallback(() => {
    void navigator.clipboard.writeText(command).then(() => setCopied("Copied the command."), () => setCopied("Could not copy. Select the command and copy it yourself."));
  }, [command]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent aria-describedby={undefined} className="max-w-xl" data-testid="verify-record-dialog">
        <div className="shrink-0 border-b border-border px-4 py-3"><DialogTitle>Verify signed record</DialogTitle></div>
        <DialogBody className="space-y-4">
          <div role="status" data-testid="verify-result" data-outcome={result?.outcome.outcome ?? (error ? "error" : "checking")}>
            {result ? <Alert tone={verifyTone(result.outcome.outcome)} description={result.outcome.headline + verifyTail(result.outcome.outcome, result.second_signature)} />
              : error ? <Alert tone="warning" description={error} />
                : <Alert tone="neutral" description="Checking the record…" />}
          </div>
          {result && <p className="text-meta text-muted-foreground" data-testid="verify-footer">{verifyFooter(result)}</p>}
          <div className="space-y-2">
            <p className="break-all rounded-md border border-border bg-muted p-2 font-mono text-meta" data-testid="verify-command">{command}</p>
            <Button variant="outline" onClick={copy}><Copy size={16} aria-hidden="true" /> Copy command</Button>
            {copied && <p role="status" className="text-meta text-muted-foreground">{copied}</p>}
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={onDownloadJson}><Download size={16} aria-hidden="true" /> Download JSON</Button>
            <Button variant="outline" onClick={onDownloadPdf}><Download size={16} aria-hidden="true" /> Download PDF</Button>
          </div>
          <p className="text-meta text-muted-foreground">{LEGAL_SENTENCE}</p>
        </DialogBody>
        <DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)}>Close</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
