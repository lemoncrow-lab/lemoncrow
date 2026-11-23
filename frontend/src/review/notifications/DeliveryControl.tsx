import { useEffect, useState } from "react";

import { Button } from "../../components/ui/button";
import { showFlowToast } from "../flowToast";
import { fetchDelivery, saveDelivery, type Delivery } from "./notificationsApi";

export const DELIVERY_LABEL = "Delivery";
export const DELIVERY_IMMEDIATE = "Immediately";
export const DELIVERY_DIGEST = "Daily digest";
export const DELIVERY_NOTE = "Digest at 9:00 · One message instead of each one";

/** SPEC 4.13's "Delivery: Immediately | Daily digest", in the bell's dialog: the digest goes only to those who chose it. */
export function DeliveryControl() {
  const [current, setCurrent] = useState<Delivery | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    void Promise.resolve(fetchDelivery()).then((next) => { if (live) setCurrent(next ?? null); }).catch(() => undefined);
    return () => { live = false; };
  }, []);
  if (!current) return null;
  const choose = async (mode: "immediate" | "digest") => {
    if (mode === current.delivery || busy) return;
    setBusy(true);
    try { setCurrent(await saveDelivery(current, mode)); }
    catch (failure) { showFlowToast({ text: failure instanceof Error && failure.message ? failure.message : "Couldn't save that. Try again.", tone: "alert" }); }
    finally { setBusy(false); }
  };
  return (
    <div className="mb-3 space-y-1" data-testid="delivery-control" role="group" aria-label={DELIVERY_LABEL}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-meta font-semibold">{DELIVERY_LABEL}</span>
        <Button variant={current.delivery === "immediate" ? "solid" : "outline"} size="sm" aria-pressed={current.delivery === "immediate"} disabled={busy} onClick={() => void choose("immediate")}>{DELIVERY_IMMEDIATE}</Button>
        <Button variant={current.delivery === "digest" ? "solid" : "outline"} size="sm" aria-pressed={current.delivery === "digest"} disabled={busy} onClick={() => void choose("digest")}>{DELIVERY_DIGEST}</Button>
      </div>
      <p className="text-meta text-muted-foreground">{DELIVERY_NOTE}</p>
    </div>
  );
}
