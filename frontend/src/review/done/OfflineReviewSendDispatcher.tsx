/** One foreground browser reconciler for user-confirmed sends held on this tab.
 * No background claim: a closed browser stops this part, and the local server
 * validates the exact revision/feedback before it ever owns a send.
 */
import { useEffect } from "react";
import { useLocation } from "react-router-dom";
import { queueReviewFlowSend } from "../reviewApi";
import { clearOfflineSend, OFFLINE_SEND_EVENT, readOfflineSend, saveOfflineSend, serverAcceptedSend } from "./offlineReviewSend";
import { savePendingSendToken } from "./pendingSendToken";

const RETRY_MS = 4_000;
export function OfflineReviewSendDispatcher() {
  const location = useLocation();
  useEffect(() => {
    let active = true;
    let working = "";
    const reconcile = async () => {
      const draft = readOfflineSend();
      // Wrap is responsible for its own first POST. Never race its preview
      // verification or submit action with the global reconciler.
      if (!draft || draft.state !== "waiting" || navigator.onLine === false || location.pathname.endsWith("/wrap")
        || working) return;
      working = draft.operationId;
      if (!draft.attempted) saveOfflineSend({ ...draft, attempted: true });
      try {
        const send = await queueReviewFlowSend(draft.reviewId, {
          expectedRevisionId: draft.revisionId, outcome: draft.outcome, summary: draft.summary,
          feedback: draft.feedback, clientOperationId: draft.operationId,
        });
        if (!serverAcceptedSend(send, draft)) throw new Error("server returned an unexpected send operation");
        if (active) {
          clearOfflineSend(draft.operationId);
          savePendingSendToken(draft.reviewRef, draft.reviewId, send);
        }
      } catch (error) {
        if (!active) return;
        const status = (error as { status?: number })?.status;
        if (typeof status === "number" && status !== 502 && status !== 503 && status !== 504) {
          saveOfflineSend({
            ...draft, attempted: true, state: "needs_review",
            error: error instanceof Error ? error.message : "Server refused this send. Review the current revision.",
          });
        }
        // Transport errors and 5xx never invent a successful or cancelled
        // send. Another attempt uses the SAME ID; the server deduplicates.
      } finally { working = ""; }
    };
    const tick = () => void reconcile();
    window.addEventListener("online", tick);
    window.addEventListener(OFFLINE_SEND_EVENT, tick);
    const timer = window.setInterval(tick, RETRY_MS);
    tick();
    return () => {
      active = false;
      window.removeEventListener("online", tick);
      window.removeEventListener(OFFLINE_SEND_EVENT, tick);
      window.clearInterval(timer);
    };
  }, [location.pathname]);
  return null;
}
