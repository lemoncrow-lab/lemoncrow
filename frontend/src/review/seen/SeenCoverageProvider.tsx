import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "../../components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { classifyFocus, flowAction } from "../flowKeymap";
import { fetchReviewSeenCoverage, postReviewSeenCoverage } from "../reviewApi";
import { buildReviewFlowPath } from "../reviewRoutes";
import { partName, partSlug } from "../overview/overviewModel";
import type { ReviewTarget } from "../types";
import {
  blocksForTarget, emptyLedger, lineRangesLabel, meterLabel, missingRisky, parseLedger, seenCount, seenStorageKey, unmeasuredKey, withSeen,
  type SeenBlock, type SeenLedger, type SeenObservation,
} from "./seenModel";

export interface SeenCoverageContextValue {
  enabled: boolean;
  ledger: SeenLedger;
  reportSeen: (block: SeenBlock, elapsedMs: number, source: SeenObservation["source"]) => void;
  focusAnswered: (target: ReviewTarget) => void;
  goToWrap: () => void;
  nextUnseen: () => void;
  coverage: (target: ReviewTarget) => ReturnType<typeof seenCount>;
  omitted: boolean;
  omissionRecorded: boolean;
}

const empty = emptyLedger("", "", "");
const SeenCoverageContext = createContext<SeenCoverageContextValue>({
  enabled: false, ledger: empty, reportSeen: () => undefined, focusAnswered: () => undefined,
  goToWrap: () => undefined, nextUnseen: () => undefined,
  coverage: (target) => seenCount(target, empty), omitted: false, omissionRecorded: false,
});
export function useSeenCoverage(): SeenCoverageContextValue { return useContext(SeenCoverageContext); }

function readLedger(reviewId: string, reviewerId: string, revisionId: string) {
  try {
    return parseLedger(window.localStorage.getItem(seenStorageKey(reviewId, reviewerId, revisionId)), reviewId, reviewerId, revisionId);
  } catch {
    return emptyLedger(reviewId, reviewerId, revisionId);
  }
}

/**
 * One owner across Overview, Focus, Quick check, All files and Wrap. `recordable` is false while the Reader shows a
 * revision that is no longer the Review's latest: the server keeps coverage of the latest one only and refuses any
 * other, so what is seen then stays local.
 */
export function SeenCoverageProvider({ reviewRef, apiReviewId, reviewerId, revisionId, targets, recordable = true, children }: {
  reviewRef: string; apiReviewId: string; reviewerId: string; revisionId: string;
  targets: readonly ReviewTarget[]; recordable?: boolean; children: ReactNode;
}) {
  const navigate = useNavigate();
  const storageKey = seenStorageKey(apiReviewId, reviewerId, revisionId);
  const [stored, setStored] = useState(() => ({ key: storageKey, ledger: readLedger(apiReviewId, reviewerId, revisionId) }));
  const [dialogOpen, setDialogOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const syncPending = useRef(new Set<string>());
  const openItRef = useRef<HTMLButtonElement>(null);
  const ledger = stored.key === storageKey ? stored.ledger : readLedger(apiReviewId, reviewerId, revisionId);
  const missing = useMemo(() => missingRisky(targets, ledger), [targets, ledger]);
  const visibleMissing = missing.slice(0, 30);
  const missingBlocks = missing.flatMap(({ target, blocks }) => blocks.length ? blocks.map((block) => block.key) : [unmeasuredKey(target)]);

  useEffect(() => {
    setStored({ key: storageKey, ledger: readLedger(apiReviewId, reviewerId, revisionId) });
    setDialogOpen(false);
    setMessage("");
    syncPending.current.clear();
  }, [storageKey, apiReviewId, reviewerId, revisionId]);

  useEffect(() => {
    if (stored.key !== storageKey) return;
    try { window.localStorage.setItem(storageKey, JSON.stringify(stored.ledger)); } catch { /* Storage disabled: no reload persistence claim. */ }
  }, [storageKey, stored]);

  useEffect(() => {
    let current = true;
    fetchReviewSeenCoverage(apiReviewId).then((server) => {
      if (!current || server.revision_id !== revisionId || server.basis !== "reviewer_reported_not_independent_evidence") return;
      const blocks = new Map(targets.flatMap((target) => blocksForTarget(target)).map((block) => [block.key, block]));
      const relevantSkipKeys = new Set([...blocks.keys(), ...targets.filter((target) => blocksForTarget(target).length === 0).map(unmeasuredKey)]);
      setStored((previous) => {
        if (previous.key !== storageKey) return previous;
        let next = previous.ledger;
        for (const row of server.seen) {
          const block = blocks.get(row.block_key);
          if (!block || row.unit_key !== block.unitKey) continue;
          next = withSeen(next, block, row.elapsed_ms, row.source === "focus_answer" ? "focus_answer" : "dwell", true);
        }
        const relevant = server.skipped.filter((row) => relevantSkipKeys.has(row.block_key));
        if (relevant.length) next = { ...next, omission: { blockKeys: relevant.map((row) => row.block_key), serverRecorded: true, recordedAt: relevant.at(-1)?.reported_at || "", recordedCount: relevant.length } };
        return { key: previous.key, ledger: next };
      });
    }).catch(() => { /* Fail closed: local observations stay unconfirmed, never attested. */ });
    return () => { current = false; };
  }, [apiReviewId, revisionId, storageKey, targets.length]);

  useEffect(() => {
    const storage = (event: StorageEvent) => {
      if (event.key !== storageKey || event.newValue === null) return;
      setStored({ key: storageKey, ledger: parseLedger(event.newValue, apiReviewId, reviewerId, revisionId) });
    };
    window.addEventListener("storage", storage);
    return () => window.removeEventListener("storage", storage);
  }, [apiReviewId, reviewerId, revisionId, storageKey]);

  const reportSeen = useCallback((block: SeenBlock, elapsedMs: number, source: SeenObservation["source"]) => {
    const before = readLedger(apiReviewId, reviewerId, revisionId);
    if (before.rows[block.key]?.serverRecorded || syncPending.current.has(block.key)) return;
    setStored((previous) => previous.key === storageKey ? { ...previous, ledger: withSeen(previous.ledger, block, elapsedMs, source) } : previous);
    if (!recordable) return;
    syncPending.current.add(block.key);
    void postReviewSeenCoverage(apiReviewId, {
      action: "seen", revision_id: revisionId, unit_key: block.unitKey, block_key: block.key,
      side: block.side, start: block.start, end: block.end, hunk: block.hunk,
      elapsed_ms: Math.round(elapsedMs), source,
    }).then((result) => {
      if (result.revision_id !== revisionId || result.action !== "seen") return;
      setStored((previous) => previous.key === storageKey ? { ...previous, ledger: withSeen(previous.ledger, block, elapsedMs, source, true) } : previous);
    }).catch(() => { /* Local exposure stays visible but never counts as server-recorded. */ })
      .finally(() => { syncPending.current.delete(block.key); });
  }, [apiReviewId, recordable, reviewerId, revisionId, storageKey]);

  const focusAnswered = useCallback((target: ReviewTarget) => {
    for (const block of blocksForTarget(target)) reportSeen(block, block.thresholdMs, "focus_answer");
  }, [reportSeen]);

  const goToWrap = useCallback(() => {
    if (!missing.length) {
      navigate(buildReviewFlowPath(reviewRef, { kind: "wrap" }));
      return;
    }
    setDialogOpen(true);
    setMessage("");
  }, [missing.length, navigate, reviewRef]);

  const nextUnseen = useCallback(() => {
    const first = missing[0]?.target;
    if (first) navigate(buildReviewFlowPath(reviewRef, { kind: "all", part: partSlug(first, targets) }));
  }, [missing, navigate, reviewRef, targets]);

  const openPart = () => {
    const first = missing[0]?.target;
    setDialogOpen(false);
    if (first) navigate(buildReviewFlowPath(reviewRef, { kind: "all", part: partSlug(first, targets) }));
  };

  const wrapAnyway = async () => {
    if (busy) return;
    setBusy(true);
    setMessage("");
    // Each append-only event is bounded to 500 keys, so a very large Review needs several. They cannot be
    // made atomic, and the server keeps what it accepted (a repeat is harmless): count what was confirmed
    // and say exactly that, never "none" when some batches were kept and will be signed.
    let recordedCount = 0;
    try {
      for (let start = 0; start < missingBlocks.length; start += 500) {
        const batch = missingBlocks.slice(start, start + 500);
        const result = await postReviewSeenCoverage(apiReviewId, { revision_id: revisionId, action: "skipped", block_keys: batch });
        if (result.revision_id !== revisionId || result.action !== "skipped") break;
        recordedCount += batch.length;
      }
    } catch { /* Never create a false signed disclosure: only confirmed batches are counted. */ }
    const confirmed = missingBlocks.length > 0 && recordedCount === missingBlocks.length;
    setStored((previous) => {
      if (previous.key !== storageKey) return previous;
      return { ...previous, ledger: { ...previous.ledger, omission: { blockKeys: missingBlocks, recordedAt: new Date().toISOString(), serverRecorded: confirmed, recordedCount } } };
    });
    setBusy(false);
    setDialogOpen(false);
    if (!confirmed) {
      setMessage(recordedCount > 0
        ? `The server recorded ${recordedCount} of ${missingBlocks.length} skipped blocks and kept them for the signed record. The other ${missingBlocks.length - recordedCount} are not included.`
        : "Your skipped parts could not be recorded by the server. They are not included in a signed record.");
    }
    navigate(buildReviewFlowPath(reviewRef, { kind: "wrap" }));
  };

  // The coverage dialog's keys come from flowKeymap (surface "coverage"): Enter opens the part, Shift+Enter wraps up anyway.
  const dialogActions = useRef({ openPart, wrapAnyway });
  dialogActions.current = { openPart, wrapAnyway };
  useEffect(() => {
    if (!dialogOpen) return;
    const onKey = (event: KeyboardEvent) => {
      const el = document.activeElement as HTMLElement | null;
      const focus = el ? classifyFocus({ tagName: el.tagName, isContentEditable: el.isContentEditable, role: el.getAttribute("role") }) : "other";
      const action = flowAction(event, { screen: "other", surface: "coverage", focus });
      if (action?.intent === "coverageOpen") { event.preventDefault(); dialogActions.current.openPart(); }
      else if (action?.intent === "coverageWrapAnyway") { event.preventDefault(); void dialogActions.current.wrapAnyway(); }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [dialogOpen]);

  const context = useMemo(() => ({
    enabled: true, ledger, reportSeen, focusAnswered, goToWrap, nextUnseen,
    coverage: (target: ReviewTarget) => seenCount(target, ledger),
    omitted: Boolean(ledger.omission?.blockKeys?.length),
    omissionRecorded: ledger.omission?.serverRecorded === true,
  }), [ledger, reportSeen, focusAnswered, goToWrap, nextUnseen]);

  return <SeenCoverageContext.Provider value={context}>
    {children}
    {message && <p role="alert" className="fixed bottom-20 right-4 z-50 max-w-sm rounded-md border border-p1-line bg-card p-3 text-label">{message}</p>}
    <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
      <DialogContent aria-describedby="coverage-explanation" data-testid="seen-coverage-dialog"
        onOpenAutoFocus={(event) => { event.preventDefault(); openItRef.current?.focus(); }}>
        <DialogHeader><DialogTitle>You haven't seen {missing.length} risky {missing.length === 1 ? "part" : "parts"}</DialogTitle></DialogHeader>
        <DialogBody className="space-y-3">
          <p id="coverage-explanation" className="text-label text-muted-foreground">Seen means the changed lines stayed on screen long enough to read. You can still wrap up. Anything you skip is written in the signed record.</p>
          <ul aria-label="Risky parts not seen" className="max-h-64 list-inside list-disc space-y-2 overflow-y-auto text-label" data-testid="seen-risky-list">
            {visibleMissing.map(({ target, blocks }) => {
              const counts = seenCount(target, ledger);
              return <li key={target.target_id}>
                {partName(target)} · {blocks.length ? `${lineRangesLabel(blocks)} · ${meterLabel(counts.linesSeen, counts.linesTotal)}` : "changed lines unavailable for this part"}
              </li>;
            })}
            {missing.length > visibleMissing.length && <li>{missing.length - visibleMissing.length} more risky parts</li>}
          </ul>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={() => void wrapAnyway()} disabled={busy}>Wrap up anyway <kbd className="ml-1 hidden text-tag sm:inline">Shift+Enter</kbd></Button>
          <Button ref={openItRef} onClick={openPart} disabled={busy}>{missing.length > 1 ? "Open the first one" : "Open it"} <kbd className="ml-1 hidden text-tag sm:inline">Enter</kbd></Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </SeenCoverageContext.Provider>;
}
