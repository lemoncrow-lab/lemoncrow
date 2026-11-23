/** A real undo affordance stays available after navigating to another screen.
 * A session-storage pointer only finds the operation; server time/state is authoritative.
 */
import { useCallback, useEffect, useState } from "react";
import { Undo2 } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { Button } from "../../components/ui/button";
import { fetchReviewFlowSend, undoReviewFlowSend } from "../reviewApi";
import { buildReviewFlowPath } from "../reviewRoutes";
import { classifyFocus, flowAction } from "../flowKeymap";
import { openFlowSurface } from "../openSurface";
import type { ReviewFlowSend } from "../types";
import { clearPendingSendToken, PENDING_SEND_EVENT, readPendingSendToken, type PendingSendToken } from "./pendingSendToken";

const TICK = 500;

export function GlobalReviewUndoToast() {
  const location = useLocation();
  const navigate = useNavigate();
  const [token, setToken] = useState<PendingSendToken | null>(() => readPendingSendToken());
  const [status, setStatus] = useState<ReviewFlowSend | null>(null);
  const [now, setNow] = useState(Date.now);
  const [undoing, setUndoing] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const sync = () => setToken(readPendingSendToken());
    window.addEventListener(PENDING_SEND_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => { window.removeEventListener(PENDING_SEND_EVENT, sync); window.removeEventListener("storage", sync); };
  }, []);

  const fetchStatus = useCallback(async (current: PendingSendToken) => {
    try {
      const result = await fetchReviewFlowSend(current.reviewId);
      if (result.operation_id !== current.operationId || result.state !== "pending") {
        clearPendingSendToken(current.operationId);
        setStatus(result);
        return;
      }
      setStatus(result);
      setError("");
    } catch {
      // A lost connection must not turn an unknown state into a working Undo.
      setStatus(null);
      setError("Undo status unavailable. Check the original review.");
    }
  }, []);

  useEffect(() => {
    setStatus(null);
    if (!token) return;
    void fetchStatus(token);
    const interval = window.setInterval(() => {
      setNow(Date.now());
      void fetchStatus(token);
    }, TICK);
    return () => window.clearInterval(interval);
  }, [token?.operationId, token?.reviewId, fetchStatus]);

  const undo = useCallback(async () => {
    if (!token || !status || status.state !== "pending" || Date.now() >= Date.parse(status.send_after) || undoing) return;
    setUndoing(true);
    try {
      await undoReviewFlowSend(token.reviewId, token.operationId);
      clearPendingSendToken(token.operationId);
      navigate(buildReviewFlowPath(token.reviewRef, { kind: "wrap" }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Undo was refused by the server.");
      await fetchStatus(token);
    } finally { setUndoing(false); }
  }, [token, status, undoing, fetchStatus, navigate]);

  const live = Boolean(token && status?.state === "pending" && status.operation_id === token.operationId
    && Date.parse(status.send_after) > now);
  const onOriginalDone = token && location.pathname === buildReviewFlowPath(token.reviewRef, { kind: "done" });

  // Z goes through the shared keymap, so it stays quiet under an open dialog, sheet or menu and while typing.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!live || onOriginalDone || event.defaultPrevented) return;
      const node = document.activeElement as HTMLElement | null;
      const action = flowAction(event, {
        screen: "other", surface: openFlowSurface(), undoLive: live,
        focus: node ? classifyFocus({ tagName: node.tagName, isContentEditable: node.isContentEditable, role: node.getAttribute("role") }) : "other",
      });
      if (action?.intent === "undo") {
        event.preventDefault(); void undo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [live, onOriginalDone, undo]);

  if (!token || onOriginalDone || !live) return null;
  const seconds = Math.ceil(Math.max(0, Date.parse(status!.send_after) - now) / 1000);
  const what = status?.has_feedback ? `Sending to ${token.destinationLabel || "the coding agent"}`
    : status?.outcome === "lgtm" ? "Recording your approval" : "Recording your verdict";
  return (
    <aside data-testid="global-review-undo" aria-label="Review send can be undone"
      className="fixed bottom-4 left-4 right-4 z-50 mx-auto flex max-w-md flex-wrap items-center gap-3 rounded-lg border border-border bg-card p-3 shadow-float">
      {/* The announcement is the text only; the countdown is visual so it is not read out every tick. */}
      <span role="status" className="min-w-0 flex-1 text-label font-semibold">{what}</span>
      <span className="font-mono text-meta text-muted-foreground" aria-hidden="true">{seconds}s</span>
      <Button variant="outline" disabled={undoing} aria-label="Undo review send" onClick={() => void undo()}>
        <Undo2 size={16} aria-hidden="true" /> Undo <span className="font-mono text-meta pointer-coarse:hidden">Z</span>
      </Button>
      <Button variant="ghost" onClick={() => navigate(buildReviewFlowPath(token.reviewRef, { kind: "done" }))}>View status</Button>
      {error && <p role="alert" className="basis-full text-meta text-p1">{error}</p>}
    </aside>
  );
}
