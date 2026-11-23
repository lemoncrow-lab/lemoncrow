import { useCallback, useEffect, useRef, useState } from "react";
import { FileSignature } from "lucide-react";

import { Button } from "../../components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "../../components/ui/dropdown-menu";
import { fetchReviewRecordFile, fetchReviewRecordPdf, fetchReviewRecordStatus, fetchReviewRecordVerify, retractReviewRecord } from "../reviewApi";
import { readFlowTimings, signingGapMs } from "../flowTimings";
import { showFlowToast } from "../flowToast";
import { fileName, saveBlob } from "../ReviewRecordExport";
import type { ReviewRecordStatus, ReviewRecordSummary, ReviewRecordVerifyResult } from "../types";
import { LEGAL_SENTENCE, RECORD_INTRO, canRetract, latestHumanRecord, noRecordText, recordLineText, retractButtonText, retractRefusalText } from "./recordModel";
import { VerifyRecordDialog } from "./VerifyRecordDialog";

export interface SignedRecordLineProps {
  reviewId: string;
  /** Hosted: show this person's record, not the newest of a review that has several people's. */
  decidedBy?: string;
  revisionId: string;
  /** `overview.revision.revision_number`: the server's zero-based count; the line says Revision n + 1. */
  revisionNumber: number;
  /** With no record, the old way to still get a file: an explicitly UNSIGNED export. Absent: no download is offered. */
  onDownloadUnsigned?: () => Promise<string>;
  /**
   * Back to Wrap up (SPEC 4.18: "Retract returns to Wrap up"). Called once a retraction is stored, and offered as
   * "Return to Wrap up" on a record that is already retracted. Absent: the line only shows the new state.
   */
  onOpenWrap?: () => void;
}

const MENU_ITEM = "";

/**
 * The record the line shows. A retracted one while the server says a verdict is signing is the record a send after
 * the retraction replaces (SPEC 4.18), so the line says Signing and keeps asking until the new one lands.
 */
function shownRecord(status: ReviewRecordStatus, revisionId: string, decidedBy: string): ReviewRecordSummary | null {
  const record = latestHumanRecord(status.records, revisionId, decidedBy);
  return record?.retracted && status.signing_due === true ? null : record;
}

/** SPEC 4.18: the record, collapsed to one plain line with a "…" menu (Details, Download JSON, Download PDF, Verify). */
export function SignedRecordLine({ reviewId, revisionId, revisionNumber, onDownloadUnsigned, onOpenWrap, decidedBy = "" }: SignedRecordLineProps) {
  const [status, setStatus] = useState<ReviewRecordStatus | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [details, setDetails] = useState<ReviewRecordVerifyResult | null>(null);
  const [detailsError, setDetailsError] = useState("");
  const [verifyOpen, setVerifyOpen] = useState(false);
  const [notice, setNotice] = useState("");
  // The shared toast announces; the visible line stays plain text. The module-level toast is used directly so this
  // component's mount or unmount never clears a toast shown in the same commit.
  const say = useCallback((text: string, tone: "status" | "alert" = "status") => { setNotice(text); showFlowToast({ text, tone }); }, []);
  const [retracting, setRetracting] = useState(false);
  const [retractError, setRetractError] = useState("");

  // The server signs in the background once the verdict is stored, so the record can land a moment after this
  // line first draws. While it says so (`signing_due`) we keep asking, with growing gaps (P6-1 O, flowTimings).
  // After the cap the line says "Still signing · check again"; that action asks once and restarts the gaps.
  const [capped, setCapped] = useState(false);
  const lookRef = useRef<(attempt: number) => void>(() => {});
  useEffect(() => {
    let active = true;
    let timer = 0;
    const timings = readFlowTimings(window);
    let last: ReviewRecordStatus | null = null;
    setStatus(null); setLoadFailed(false); setCapped(false);
    const next = (attempt: number) => {
      if (attempt >= timings.signingLooks) { setCapped(true); return; }
      setCapped(false);
      timer = window.setTimeout(() => look(attempt + 1), signingGapMs(attempt, timings));
    };
    const look = (attempt: number) => {
      window.clearTimeout(timer);
      fetchReviewRecordStatus(reviewId).then((fresh) => {
        if (!active) return;
        last = fresh;
        setStatus(fresh); setLoadFailed(false);
        const found = shownRecord(fresh, revisionId, decidedBy) !== null;
        if (found || !fresh.signing.available || fresh.signing_due !== true) { setCapped(false); return; }
        next(attempt);
      }).catch(() => {
        if (!active) return;
        // One failed ask never ends the wait: while the last known status said signing is due, keep asking.
        if (last?.signing_due === true) next(attempt);
        else if (last === null) { setLoadFailed(true); setCapped(true); }
      });
    };
    lookRef.current = look;
    look(1);
    return () => { active = false; window.clearTimeout(timer); };
  }, [reviewId, revisionId, decidedBy]);

  const record: ReviewRecordSummary | null = status ? shownRecord(status, revisionId, decidedBy) : null;

  useEffect(() => {
    if (!detailsOpen || !record) return;
    let active = true;
    setDetailsError("");
    fetchReviewRecordVerify(reviewId, record.record_id).then((next) => { if (active) setDetails(next); })
      .catch(() => { if (active) setDetailsError("The details could not be loaded."); });
    return () => { active = false; };
  }, [detailsOpen, reviewId, record?.record_id, record?.retracted?.at]);

  const downloadJson = useCallback(async () => {
    try {
      if (record) {
        saveBlob(await fetchReviewRecordFile(reviewId, record.record_id), fileName(record));
        say(`Downloaded ${fileName(record)}.`);
      } else if (onDownloadUnsigned) say(await onDownloadUnsigned());
    } catch { say("The record could not be downloaded.", "alert"); }
  }, [record, reviewId, onDownloadUnsigned, say]);

  const downloadPdf = useCallback(async () => {
    if (!record) return;
    const name = `review-record-${record.record_id}.${record.signing_state === "signed" ? "pdf" : "unsigned.pdf"}`;
    try {
      saveBlob(await fetchReviewRecordPdf(reviewId, record.record_id), name);
      say(`Downloaded ${name}.`);
    } catch { say("The PDF could not be downloaded.", "alert"); }
  }, [record, reviewId, say]);

  const lineRef = useRef<HTMLParagraphElement>(null);
  // The server signs the retraction and refuses a late one; this only asks, then shows the state the server now
  // reports (SPEC 4.18). When the button goes away (retracted, or the server says it can no longer be used), focus
  // moves to the record line so it never falls to the page (SPEC 5.3). The button stays in the tab order while it
  // works (aria-disabled, not disabled), because a disabled button drops focus.
  const showStatus = useCallback((next: ReviewRecordStatus) => {
    setStatus(next);
    if (!canRetract(latestHumanRecord(next.records, revisionId), Date.now())) lineRef.current?.focus();
  }, [revisionId]);
  const retract = useCallback(async () => {
    if (!record || retracting) return;
    setRetracting(true); setRetractError("");
    try {
      await retractReviewRecord(reviewId, record.record_id);
      showStatus(await fetchReviewRecordStatus(reviewId));
      onOpenWrap?.();
    } catch (reason) {
      const text = retractRefusalText(reason);
      setRetractError(text);
      showFlowToast({ text, tone: "alert" });
      // A refusal because the window ended or the key changed: show the record as the server now says it is.
      void fetchReviewRecordStatus(reviewId).then(showStatus).catch(() => {});
    } finally { setRetracting(false); }
  }, [record, retracting, reviewId, onOpenWrap, showStatus]);

  const checkAgain = () => { lineRef.current?.focus(); setCapped(false); lookRef.current(1); };
  // P6-1 O: a record for an earlier revision means the decision moved on; the server's own snapshot says which.
  const moved = status !== null && typeof status.snapshot_number === "number" && !!status.snapshot_id && status.snapshot_id !== revisionId && status.signing.available !== false;
  const text = loadFailed ? "The signed record could not be loaded."
    : record ? recordLineText(record, revisionNumber + 1)
    : moved && status.signing_due !== true ? `Not signed · the review has moved on to Revision ${status.snapshot_number + 1}`
    : noRecordText(status ? status.signing.available : null, status?.signing_due === true);
  const stalled = !record && capped && (loadFailed || status?.signing_due === true);

  return (
    <section className="rounded-lg border border-border bg-card p-4" data-testid="done-record" aria-label="Signed record">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <FileSignature size={16} aria-hidden="true" className="shrink-0" />
          <p ref={lineRef} tabIndex={-1} className="min-w-0 text-label outline-none" data-testid="record-line">
            {record ? <>
              {text.replace(/ · Details$/, "")} · <Button variant="link" aria-expanded={detailsOpen} aria-controls="record-details" onClick={() => setDetailsOpen((open) => !open)}>Details</Button>
            </> : stalled ? <>
              {loadFailed ? "The signed record could not be loaded" : "Still signing"} · <Button variant="link" onClick={checkAgain}>check again</Button>
            </> : text}
          </p>
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild><Button variant="ghost" className="min-w-11" aria-label="Record options">•••</Button></DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {record && <DropdownMenuItem className={MENU_ITEM} onSelect={() => setDetailsOpen(true)}>Details</DropdownMenuItem>}
            <DropdownMenuItem className={MENU_ITEM} disabled={!record && !onDownloadUnsigned} onSelect={() => void downloadJson()}>Download JSON</DropdownMenuItem>
            <DropdownMenuItem className={MENU_ITEM} disabled={!record} onSelect={() => void downloadPdf()}>Download PDF</DropdownMenuItem>
            <DropdownMenuItem className={MENU_ITEM} disabled={!record} onSelect={() => setVerifyOpen(true)}>Verify</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {detailsOpen && record && <div id="record-details" className="mt-3 space-y-3" data-testid="record-details">
        <p className="text-label text-muted-foreground">{RECORD_INTRO}</p>
        {detailsError ? <p role="alert" className="text-label">{detailsError}</p> : details ? <dl className="divide-y divide-border">
          {details.details.map((row) => <div key={row.label} className="grid gap-1 py-2 sm:grid-cols-[11rem_1fr]">
            <dt className="text-meta font-semibold">{row.label}</dt>
            <dd className="break-words text-label" data-testid={`record-row-${row.label}`}>{row.value}</dd>
          </div>)}
        </dl> : <p role="status" className="text-label text-muted-foreground">Loading the record…</p>}
        <p className="text-meta text-muted-foreground">{LEGAL_SENTENCE}</p>
      </div>}
      {canRetract(record, Date.now()) && <div className="mt-3">
        {/* An outlined button, so it reads as an action and not as a line of the record (it can't be taken back). */}
        <Button variant="outline" aria-disabled={retracting} onClick={() => void retract()} data-testid="record-retract">
          {retracting ? "Retracting…" : retractButtonText(record, Date.now())}
        </Button>
      </div>}
      {record?.retracted && onOpenWrap && <div className="mt-3">
        <Button variant="outline" onClick={onOpenWrap} data-testid="record-return-wrap">Return to Wrap up</Button>
      </div>}
      {retractError && <p className="mt-2 text-label" data-testid="record-retract-error">{retractError}</p>}
      {notice && <p className="mt-2 text-meta" data-testid="record-notice">{notice}</p>}
      {record && <VerifyRecordDialog
        open={verifyOpen}
        onOpenChange={setVerifyOpen}
        reviewId={reviewId}
        record={record}
        onDownloadJson={() => void downloadJson()}
        onDownloadPdf={() => void downloadPdf()}
        onChecked={setDetails}
      />}
    </section>
  );
}
