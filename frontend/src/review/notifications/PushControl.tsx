import { useEffect, useState } from "react";

import { Button } from "../../components/ui/button";
import { showFlowToast } from "../flowToast";
import {
  fetchPushStatus, PUSH_BLOCKED_NOTE, PUSH_IOS_NOTE, PUSH_OFF_LABEL, PUSH_ON_LABEL, PUSH_ON_NOTE, pushState, turnOffPush, turnOnPush,
  type PushState, type PushStatus,
} from "./pushApi";

/** The phone and closed-browser half of NOTIF-2, in the bell's dialog: nothing is drawn where this server cannot push. */
export function PushControl() {
  const [status, setStatus] = useState<PushStatus | null>(null);
  const [state, setState] = useState<PushState>("unsupported");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    void fetchPushStatus()
      .then(async (next) => { const known = await pushState(next); if (live) { setStatus(next); setState(known); } })
      .catch(() => undefined);
    return () => { live = false; };
  }, []);
  if (state === "unsupported" || !status) return null;
  const run = async (action: () => Promise<PushState>) => {
    setBusy(true);
    try { setState(await action()); }
    catch (failure) { showFlowToast({ text: failure instanceof Error && failure.message ? failure.message : "Couldn't change notifications on this device.", tone: "alert" }); }
    finally { setBusy(false); }
  };
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2" data-testid="push-control">
      {state === "needs-home-screen" ? <p className="text-meta text-muted-foreground" data-testid="push-state">{PUSH_IOS_NOTE}</p> : null}
      {state === "denied" ? <p className="text-meta text-muted-foreground" data-testid="push-state">{PUSH_BLOCKED_NOTE}</p> : null}
      {state === "off" ? (
        <Button variant="outline" size="sm" disabled={busy} data-testid="push-on" onClick={() => void run(() => turnOnPush(status.public_key))}>{PUSH_ON_LABEL}</Button>
      ) : null}
      {state === "on" ? (
        <>
          <p className="text-meta text-muted-foreground" data-testid="push-state">{PUSH_ON_NOTE}</p>
          <Button variant="ghost" size="sm" disabled={busy} data-testid="push-off" onClick={() => void run(turnOffPush)}>{PUSH_OFF_LABEL}</Button>
        </>
      ) : null}
    </div>
  );
}
