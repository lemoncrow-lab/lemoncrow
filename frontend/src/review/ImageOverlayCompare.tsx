import { useRef, useState } from "react";

interface ImageOverlayCompareProps {
  beforeSrc: string;
  afterSrc: string;
  beforeLabel: string;
  afterLabel: string;
  className?: string;
}

export default function ImageOverlayCompare({
  beforeSrc,
  afterSrc,
  beforeLabel,
  afterLabel,
  className = "",
}: ImageOverlayCompareProps) {
  const [position, setPosition] = useState(50);
  const [showHint, setShowHint] = useState(true);
  const stageRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);

  const positionFromPointer = (clientX: number) => {
    const bounds = stageRef.current?.getBoundingClientRect();
    if (!bounds || bounds.width <= 0) return;
    setPosition(Math.max(0, Math.min(100, ((clientX - bounds.left) / bounds.width) * 100)));
  };

  return (
    <div className={`overflow-hidden rounded border border-border bg-background ${className}`} data-testid="image-overlay-compare">
      <div className="max-h-[640px] overflow-auto bg-muted p-2">
        <div ref={stageRef} className="relative grid w-full place-items-start overflow-hidden bg-white">
          <img src={beforeSrc} alt={`${beforeLabel} screenshot`} className="col-start-1 row-start-1 block h-auto w-full" />
          <img
            src={afterSrc}
            alt={`${afterLabel} screenshot`}
            className="col-start-1 row-start-1 block h-auto w-full"
            style={{ clipPath: `inset(0 ${100 - position}% 0 0)` }}
          />
          <div
            role="slider"
            tabIndex={0}
            aria-label="Screenshot comparison divider"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(position)}
            aria-valuetext={`${Math.round(position)}% after image revealed`}
            data-testid="image-overlay-divider"
            className="group absolute inset-y-0 z-10 w-5 -translate-x-1/2 cursor-col-resize touch-none outline-none"
            style={{ left: `${position}%` }}
            onPointerDown={(event) => {
              event.preventDefault();
              draggingRef.current = true;
              setShowHint(false);
              event.currentTarget.setPointerCapture(event.pointerId);
              positionFromPointer(event.clientX);
            }}
            onPointerMove={(event) => {
              if (!draggingRef.current) return;
              positionFromPointer(event.clientX);
            }}
            onPointerUp={(event) => {
              draggingRef.current = false;
              if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                event.currentTarget.releasePointerCapture(event.pointerId);
              }
            }}
            onPointerCancel={() => {
              draggingRef.current = false;
            }}
            onKeyDown={(event) => {
              const step = event.shiftKey ? 10 : 1;
              const movesDivider = ["ArrowLeft", "ArrowDown", "ArrowRight", "ArrowUp", "Home", "End"].includes(event.key);
              if (movesDivider) setShowHint(false);
              if (event.key === "ArrowLeft" || event.key === "ArrowDown") {
                event.preventDefault();
                setPosition((value) => Math.max(0, value - step));
              } else if (event.key === "ArrowRight" || event.key === "ArrowUp") {
                event.preventDefault();
                setPosition((value) => Math.min(100, value + step));
              } else if (event.key === "Home") {
                event.preventDefault();
                setPosition(0);
              } else if (event.key === "End") {
                event.preventDefault();
                setPosition(100);
              }
            }}
          >
            <div
              aria-hidden="true"
              className="pointer-events-none absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-sky-300 shadow-[0_0_0_1px_rgba(0,0,0,0.45)] transition-[width] group-hover:w-0.5 group-focus-visible:w-0.5"
            />
            {showHint && (
              <div
                aria-hidden="true"
                data-testid="image-overlay-drag-hint"
                className="pointer-events-none absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 whitespace-nowrap rounded-full border border-neutral-700/80 bg-background/90 px-2 py-1 text-caption font-medium text-neutral-200 shadow-lg backdrop-blur"
              >
                ↔ Drag to compare
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
