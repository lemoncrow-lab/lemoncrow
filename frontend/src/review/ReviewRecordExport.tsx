import { useCallback, useEffect, useState } from "react";
import { Download, FileSignature } from "lucide-react";

import { Badge } from "../components/ui/badge";
import { Button as UiButton } from "../components/ui/button";
import { exportReviewRecord, fetchReviewRecordFile, fetchReviewRecordStatus } from "./reviewApi";
import type { ReviewRecordExportResult, ReviewRecordStatus, ReviewRecordSummary } from "./types";

/** How every surface names an unsigned record; never shortened or softened. */
export const UNSIGNED_RECORD_LABEL = "UNSIGNED record (no signature)";

function recordLabel(record: ReviewRecordSummary): string {
  if (record.signing_state !== "signed") return UNSIGNED_RECORD_LABEL;
  return record.label || `Signed ${record.key_id}`;
}

function fileName(record: ReviewRecordSummary): string {
  const suffix = record.signing_state === "signed" ? "dsse.json" : "unsigned.dsse.json";
  return `review-record-${record.record_id}.${suffix}`;
}

function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/**
 * Review Status > Review record: export the decided Review as a DSSE / in-toto
 * record signed with the organization (or personal) key, or as an explicitly
 * UNSIGNED record, and download records already exported.
 */
export default function ReviewRecordExport({
  reviewId,
  snapshotId,
  readOnly = false,
}: {
  reviewId: string;
  /** The Snapshot the sheet shows; the server refuses when a newer one exists. */
  snapshotId?: string;
  readOnly?: boolean;
}) {
  const [status, setStatus] = useState<ReviewRecordStatus | null>(null);
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<ReviewRecordExportResult | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await fetchReviewRecordStatus(reviewId));
      setLoadError("");
    } catch (reason) {
      setLoadError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [reviewId]);

  useEffect(() => {
    void load();
  }, [load]);

  const download = useCallback(async (record: ReviewRecordSummary) => {
    try {
      saveBlob(await fetchReviewRecordFile(reviewId, record.record_id), fileName(record));
    } catch (reason) {
      setError(`Record download failed: ${reason instanceof Error ? reason.message : String(reason)}`);
    }
  }, [reviewId]);

  const runExport = useCallback(async (signing: "auto" | "unsigned") => {
    setBusy(true);
    setError("");
    try {
      const exported = await exportReviewRecord(reviewId, {
        signing,
        ...(snapshotId ? { expected_revision_id: snapshotId } : {}),
      });
      setResult(exported);
      await load();
      await download(exported.record);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      await load();
    } finally {
      setBusy(false);
    }
  }, [reviewId, snapshotId, load, download]);

  if (loadError) {
    return (
      <div className="rounded-md border border-border/70 bg-muted/20 px-3 py-2.5 text-caption text-muted-foreground" role="status">
        Review records are unavailable: {loadError}
      </div>
    );
  }
  if (!status) return <div className="text-caption text-muted-foreground">Loading review record…</div>;

  const signing = status.signing;
  const canAct = !readOnly && status.decision !== null;
  return (
    <div className="space-y-3">
      <div className="rounded-md border border-border/80 bg-muted/25 p-3">
        <div className="flex items-start gap-2">
          <FileSignature size={12} className="mt-0.5 shrink-0 text-neutral-400" />
          <div className="min-w-0 flex-1">
            <div className="text-caption font-semibold text-neutral-200">Signed review record</div>
            <div className="mt-1 text-caption leading-4 text-muted-foreground">
              {status.decision
                ? `Snapshot ${status.snapshot_number} · ${status.decision.outcome.replaceAll("_", " ")} by ${status.decision.actor.id || "unknown"}`
                : "No decision on the latest Snapshot yet; a record is exported once the Review is decided."}
            </div>
            <div
              className={`mt-1 text-caption leading-4 ${signing.available ? "text-emerald-300" : "text-amber-300"}`}
              data-testid="record-signing-status"
            >
              {signing.available
                ? `Signs with ${signing.mode === "user_key" ? "your personal key" : "the organization key"} ${signing.key_id}`
                : `Signing unavailable: ${signing.reason || signing.code}`}
            </div>
          </div>
        </div>
        {canAct && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <UiButton
              variant="ghost"
              size="compact"
              type="button"
              disabled={busy || !signing.available}
              onClick={() => void runExport("auto")}
              tooltip={signing.available ? "Export and download a signed record of this decision." : signing.reason}
            >
              {busy ? "Exporting…" : "Export signed record"}
            </UiButton>
            <UiButton
              variant="ghost"
              size="compact"
              type="button"
              disabled={busy}
              onClick={() => void runExport("unsigned")}
              tooltip="Export a record with no signature. It is labeled UNSIGNED everywhere and never verifies as signed."
            >
              Export UNSIGNED record
            </UiButton>
          </div>
        )}
        {error && <div className="mt-2 text-caption text-rose-300" role="alert">{error}</div>}
        {result && (
          <div className="mt-2 text-caption leading-4 text-muted-foreground" data-testid="record-export-result">
            <span className={result.verification.status === "verified" ? "text-emerald-300" : "text-amber-300"}>
              {result.verification.headline}
            </span>
            {" · "}
            {result.matches_store && result.reproduced ? "matches the stored Review" : "does NOT match the stored Review"}
          </div>
        )}
      </div>
      {status.records.length > 0 && (
        <div className="divide-y divide-neutral-900 border-y border-neutral-900" aria-label="Exported review records">
          {status.records.map((record) => (
            <div key={record.record_id} className="flex items-center gap-2 py-2">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-x-2">
                  {record.signing_state === "signed" ? (
                    <Badge tone="secondary" className="text-emerald-300">signed</Badge>
                  ) : (
                    <Badge tone="secondary" className="text-amber-300">UNSIGNED</Badge>
                  )}
                  <span className="truncate text-caption text-neutral-300">{recordLabel(record)}</span>
                </div>
                <div className="mt-0.5 font-mono text-caption text-muted-foreground">
                  {record.issued_at.replace("T", " ").slice(0, 19)} · {record.payload_digest.slice(0, 12)}
                </div>
              </div>
              <UiButton
                variant="ghost"
                size="icon"
                type="button"
                aria-label={`Download ${recordLabel(record)}`}
                onClick={() => void download(record)}
              >
                <Download size={12} />
              </UiButton>
            </div>
          ))}
        </div>
      )}
      <div className="text-caption leading-4 text-muted-foreground">
        Verify offline with <span className="font-mono">lc review verify-record &lt;file&gt; --keys &lt;keys.json&gt;</span>.
      </div>
    </div>
  );
}
