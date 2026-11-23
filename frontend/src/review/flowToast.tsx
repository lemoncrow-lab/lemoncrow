import { useCallback, useEffect, useId, useState, useSyncExternalStore, type ReactNode } from "react";
import { useInRouterContext, useLocation } from "react-router-dom";
import { X } from "lucide-react";

import { Button } from "../components/ui/button";

/** SPEC 4.20 (R12): a toast stays 5 s, 10 s when it offers Undo; the timer pauses on hover and focus. */
export const TOAST_MS = 5_000;
export const UNDO_TOAST_MS = 10_000;

export interface FlowToast {
  text: string;
  /** "alert" for a failure the person must see; "status" otherwise. */
  tone?: "status" | "alert";
  undo?: () => void;
}

/**
 * The one toast of the new flow. A module-level store holds it, so a screen and the palette share a single toast:
 * the newest replaces the last, identical text restarts the timer. Hosts render it; only the first mounted host does.
 */
interface Held extends FlowToast { serial: number }
let held: Held | null = null;
let serial = 0;
const listeners = new Set<() => void>();
const hosts: string[] = [];
function emit(): void { listeners.forEach((listener) => listener()); }
function subscribe(listener: () => void): () => void { listeners.add(listener); return () => { listeners.delete(listener); }; }

export function showFlowToast(next: FlowToast): void {
  serial += 1;
  held = { ...next, serial };
  emit();
}
export function dismissFlowToast(): void {
  if (!held) return;
  held = null;
  emit();
}
/** The toast on screen now; for tests. */
export function currentFlowToast(): FlowToast | null { return held; }

function Host(): ReactNode {
  const id = useId();
  const toast = useSyncExternalStore(subscribe, () => held, () => null);
  const [claimed, setClaimed] = useState(false);
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    hosts.push(id);
    emit();
    setClaimed(true);
    return () => {
      hosts.splice(hosts.indexOf(id), 1);
      emit();
    };
  }, [id]);
  // Re-render when the set of hosts changes, so the first one always owns the toast.
  useSyncExternalStore(subscribe, () => hosts.join(","), () => "");

  // Hover or focus ends with the toast: the unmounted element never fires mouseleave or blur.
  const serialNow = toast?.serial ?? 0;
  useEffect(() => { setPaused(false); }, [serialNow]);

  useEffect(() => {
    if (!toast || paused || hosts[0] !== id) return;
    const timer = window.setTimeout(dismissFlowToast, toast.undo ? UNDO_TOAST_MS : TOAST_MS);
    return () => window.clearTimeout(timer);
  }, [toast, paused, id]);

  if (!claimed || !toast || hosts[0] !== id) return null;
  return (
    <div
      key={toast.serial}
      role={toast.tone === "alert" ? "alert" : "status"}
      data-testid="flow-toast"
      className="fixed inset-x-4 bottom-16 z-20 mx-auto flex max-w-md items-center gap-2 rounded-lg bg-foreground px-3 py-2 text-label text-background shadow-float sm:bottom-12"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
    >
      <span className="min-w-0 flex-1">{toast.text}</span>
      {toast.undo ? (
        <Button variant="outline" className="min-w-11 border-current text-background" onClick={() => { toast.undo?.(); dismissFlowToast(); }}>
          Undo
        </Button>
      ) : null}
      <Button variant="ghost" size="icon" className="text-background" aria-label="Dismiss" onClick={dismissFlowToast}>
        <X size={16} aria-hidden="true" />
      </Button>
    </div>
  );
}

/** Clears the toast when the route changes (R12). */
function ClearOnRoute(): null {
  const location = useLocation();
  useEffect(() => { dismissFlowToast(); }, [location.pathname]);
  return null;
}

export function FlowToastHost(): ReactNode {
  const inRouter = useInRouterContext();
  return <>{inRouter ? <ClearOnRoute /> : null}<Host /></>;
}

/** Show a toast from a screen; render `element` once on the screen (a second host stays quiet). */
export function useFlowToast(): { show: (toast: FlowToast) => void; dismiss: () => void; element: ReactNode } {
  const show = useCallback((next: FlowToast) => showFlowToast(next), []);
  const dismiss = useCallback(() => dismissFlowToast(), []);
  // A screen's toast goes with the screen.
  useEffect(() => dismissFlowToast, []);
  return { show, dismiss, element: <FlowToastHost /> };
}
