import { useCallback, useEffect, useState } from "react";
import { Download, Info } from "lucide-react";

import { Button } from "../../components/ui/button";
import { fetchReviewRecordFile, fetchReviewRecordPdf, fetchReviewRecordStatus } from "../reviewApi";
import { showFlowToast } from "../flowToast";
import { fileName, saveBlob } from "../ReviewRecordExport";
import type { ReviewRecordSummary } from "../types";
import { latestRetractedRecord, retractedNotice } from "./recordModel";
import { VerifyRecordDialog } from "./VerifyRecordDialog";

export interface RetractedRecordCardProps {
  reviewId: string;
  revisionId: string;
  /** `overview.revision.revision_number`: the server's zero-based count; the card says Revision n + 1. */
  revisionNumber: number;
  /** The checkout folder name shown before the commit ("shop"); empty leaves it out. */
  repo: string;
}

/**
 * Wrap up (SPEC 4.18): "You retracted the earlier record · Revision 1 · shop @ 4e1b9c2 · 15:10 UTC. Sending again
 * signs a new one." with Verify and Download JSON. It shows while the newest record of the person's verdict on this
 * revision is the retracted one; a newer send replaces it, and it never appears without a retraction on the server.
 */
export function RetractedRecordCard({ reviewId, revisionId, revisionNumber, repo }: RetractedRecordCardProps) {
  const [record, setRecord] = useState<ReviewRecordSummary | null>(null);
  const [verifyOpen, setVerifyOpen] = useState(false);
  const [notice, setNotice] = useState("");
  // The toast announces; the visible line stays as plain text.
  const say = useCallback((text: string, tone: "status" | "alert" = "status") => { setNotice(text); showFlowToast({ text, tone }); }, []);

  useEffect(() => {
    let active = true;
    setRecord(null);
    fetchReviewRecordStatus(reviewId).then((status) => {
      if (active) setRecord(latestRetractedRecord(status.records, revisionId));
    }).catch(() => { /* no retraction is claimed when it cannot be read */ });
    return () => { active = false; };
  }, [reviewId, revisionId]);

  const downloadJson = useCallback(async () => {
    if (!record) return;
    try {
      saveBlob(await fetchReviewRecordFile(reviewId, record.record_id), fileName(record));
      say(`Downloaded ${fileName(record)}.`);
    } catch { say("The record could not be downloaded.", "alert"); }
  }, [record, reviewId, say]);

  const downloadPdf = useCallback(async () => {
    if (!record) return;
    const name = `review-record-${record.record_id}.pdf`;
    try {
      saveBlob(await fetchReviewRecordPdf(reviewId, record.record_id), name);
      say(`Downloaded ${name}.`);
    } catch { say("The PDF could not be downloaded.", "alert"); }
  }, [record, reviewId, say]);

  if (!record) return null;
  return (
    <section className="rounded-lg border border-border bg-card p-4" data-testid="wrap-retracted" aria-label="Retracted record">
      <p className="flex items-start gap-2 text-label"><Info size={16} aria-hidden="true" className="mt-0.5 shrink-0" /><span data-testid="wrap-retracted-text">{retractedNotice(record, revisionNumber + 1, repo)}</span></p>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="outline" aria-label="Verify the retracted record" onClick={() => setVerifyOpen(true)}>Verify</Button>
        <Button variant="ghost" onClick={() => void downloadJson()}><Download size={16} aria-hidden="true" /> Download JSON</Button>
      </div>
      {notice && <p className="mt-2 text-meta" data-testid="wrap-retracted-notice">{notice}</p>}
      <VerifyRecordDialog open={verifyOpen} onOpenChange={setVerifyOpen} reviewId={reviewId} record={record} onDownloadJson={() => void downloadJson()} onDownloadPdf={() => void downloadPdf()} />
    </section>
  );
}
