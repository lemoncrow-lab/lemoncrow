import { useRef, type PointerEvent } from "react";
import { Button } from "./button";
import { cn } from "../../lib/utils";

interface PanelResizeHandleProps {
  label: string;
  onResize(width: number): void;
  className?: string;
}

/** Place inside a positioned panel; its parent supplies the current width. */
export function PanelResizeHandle({ label, onResize, className }: PanelResizeHandleProps) {
  const drag = useRef<{ pointerId: number; x: number; width: number } | null>(null);
  const resize = (width: number) => {
    const maximum = Math.max(180, Math.min(520, window.innerWidth * 0.45));
    onResize(Math.round(Math.max(180, Math.min(maximum, width))));
  };
  const stop = (event: PointerEvent<HTMLButtonElement>) => {
    if (!drag.current || drag.current.pointerId !== event.pointerId) return;
    drag.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };

  return (
    <Button
      unstyled
      aria-label={label}
      className={cn("absolute inset-y-0 right-0 z-10 flex w-2 cursor-col-resize touch-none select-none items-center justify-center hover:bg-muted focus-visible:bg-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring pointer-coarse:w-4", className)}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        const panel = event.currentTarget.parentElement;
        if (!panel) return;
        drag.current = { pointerId: event.pointerId, x: event.clientX, width: panel.getBoundingClientRect().width };
        event.currentTarget.setPointerCapture(event.pointerId);
        event.preventDefault();
      }}
      onPointerMove={(event) => {
        const current = drag.current;
        if (current && current.pointerId === event.pointerId) resize(current.width + event.clientX - current.x);
      }}
      onPointerUp={stop}
      onPointerCancel={stop}
      onLostPointerCapture={() => { drag.current = null; }}
      onKeyDown={(event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        const width = event.currentTarget.parentElement?.getBoundingClientRect().width;
        if (width !== undefined) resize(width + (event.key === "ArrowRight" ? 20 : -20));
      }}
    >
      <span aria-hidden="true" className="h-8 w-px bg-border" />
    </Button>
  );
}
