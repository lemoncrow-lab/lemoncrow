/**
 * The pan and zoom field the pictures sit in. Wheel and drag pan; ctrl or ⌘ with the wheel (and pinch) zooms around the
 * pointer; + / − / 0 zoom from the keyboard. A new `focus` box (a walkthrough step, a new view) eases the camera to it,
 * or jumps there when motion is reduced.
 */
import { Maximize2, Minus, Plus } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

import { Button } from "../../../components/ui/button";
import type { Box } from "./geometry";

export interface Camera {
  x: number;
  y: number;
  k: number;
}

const MIN_K = 0.2;
const MAX_K = 2.5;
const MARGIN = 24;
/** Below this scale a whole picture is too small to read; it is shown at this scale from its top instead. */
const READABLE_K = 0.72;

const clampK = (k: number) => Math.min(MAX_K, Math.max(MIN_K, k));

/**
 * Where the camera stands to show `box` in a `w`×`h` field. The whole picture (`whole`) is centred when it fits at a
 * readable scale and otherwise shown from its top, at the width that fits; a focus box is centred, up to 115%.
 */
export function cameraFor(box: Box, w: number, h: number, whole: boolean): Camera {
  const roomW = Math.max(1, w - MARGIN * 2);
  const roomH = Math.max(1, h - MARGIN * 2);
  const all = Math.min(roomW / box.w, roomH / box.h);
  if (whole) {
    const k = clampK(Math.min(1, all >= READABLE_K ? all : Math.max(all, Math.min(roomW / box.w, READABLE_K))));
    const x = (w - box.w * k) / 2 - box.x * k;
    const y = box.h * k <= roomH ? (h - box.h * k) / 2 - box.y * k : MARGIN - box.y * k;
    return { x: Math.round(x), y: Math.round(y), k };
  }
  const k = clampK(Math.min(1.15, all));
  return { x: Math.round((w - box.w * k) / 2 - box.x * k), y: Math.round((h - box.h * k) / 2 - box.y * k), k };
}

export interface ViewportProps {
  /** The picture's size. */
  width: number;
  height: number;
  /** Where to look; null shows the whole picture. */
  focus: Box | null;
  /** Changes when the camera should move again (a new view, step or side). */
  focusKey: string;
  reduced: boolean;
  label: string;
  /** Room kept clear at the bottom (the walkthrough's caption). */
  bottomInset?: number;
  onBackground?(): void;
  children: ReactNode;
}

export function Viewport({ width, height, focus, focusKey, reduced, label, bottomInset = 0, onBackground, children }: ViewportProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ w: 1200, h: 720 });
  const [camera, setCamera] = useState<Camera>({ x: MARGIN, y: MARGIN, k: 1 });
  const cameraRef = useRef(camera);
  cameraRef.current = camera;
  const animation = useRef(0);
  const moved = useRef(false);
  const [dragging, setDragging] = useState(false);
  const drag = useRef<{ id: number; x: number; y: number; camera: Camera; active: boolean } | null>(null);
  const swallowClick = useRef(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const w = el.clientWidth || 1200;
      const h = el.clientHeight || 720;
      setSize((s) => (s.w === w && s.h === h ? s : { w, h }));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const goTo = useCallback((next: Camera, animate: boolean) => {
    cancelAnimationFrame(animation.current);
    if (!animate || typeof requestAnimationFrame !== "function") {
      setCamera(next);
      return;
    }
    const from = cameraRef.current;
    const start = performance.now();
    const duration = 520;
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      const e = 1 - Math.pow(1 - t, 3);
      setCamera({ x: from.x + (next.x - from.x) * e, y: from.y + (next.y - from.y) * e, k: from.k + (next.k - from.k) * e });
      if (t < 1) animation.current = requestAnimationFrame(step);
    };
    animation.current = requestAnimationFrame(step);
  }, []);
  useEffect(() => () => cancelAnimationFrame(animation.current), []);

  const target = useCallback(() => {
    const h = Math.max(160, size.h - bottomInset);
    return focus ? cameraFor(focus, size.w, h, false) : cameraFor({ x: 0, y: 0, w: width, h: height }, size.w, h, true);
  }, [focus, size, width, height, bottomInset]);

  // A new focus moves the camera (eased after the first time); a resize re-fits unless the reader has moved it.
  const first = useRef(true);
  const lastKey = useRef<string | null>(null);
  useLayoutEffect(() => {
    const keyChanged = lastKey.current !== focusKey;
    lastKey.current = focusKey;
    if (!keyChanged && moved.current) return;
    if (keyChanged) moved.current = false;
    goTo(target(), keyChanged && !first.current && !reduced);
    first.current = false;
  }, [focusKey, target, goTo, reduced]);

  const zoomAt = useCallback((factor: number, px: number, py: number) => {
    moved.current = true;
    cancelAnimationFrame(animation.current);
    setCamera((c) => {
      const k = clampK(c.k * factor);
      const f = k / c.k;
      return { k, x: px - (px - c.x) * f, y: py - (py - c.y) * f };
    });
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = el.getBoundingClientRect();
      if (event.ctrlKey || event.metaKey) {
        zoomAt(Math.exp(-event.deltaY * 0.0025), event.clientX - rect.left, event.clientY - rect.top);
        return;
      }
      moved.current = true;
      cancelAnimationFrame(animation.current);
      const dx = event.shiftKey && !event.deltaX ? event.deltaY : event.deltaX;
      const dy = event.shiftKey && !event.deltaX ? 0 : event.deltaY;
      setCamera((c) => ({ ...c, x: c.x - dx, y: c.y - dy }));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomAt]);

  const zoomCentre = (factor: number) => zoomAt(factor, size.w / 2, (size.h - bottomInset) / 2);
  const fit = () => {
    moved.current = false;
    goTo(target(), !reduced);
  };

  return (
    <div
      ref={ref}
      className="xg-canvas"
      data-testid="xg-canvas"
      data-dragging={dragging ? "true" : undefined}
      tabIndex={0}
      aria-label={label}
      style={{
        backgroundSize: `${Math.round(18 * camera.k * 100) / 100}px ${Math.round(18 * camera.k * 100) / 100}px`,
        backgroundPosition: `${Math.round(camera.x)}px ${Math.round(camera.y)}px`,
      }}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget || event.metaKey || event.ctrlKey || event.altKey) return;
        if (event.key === "+" || event.key === "=") zoomCentre(1.2);
        else if (event.key === "-" || event.key === "_") zoomCentre(1 / 1.2);
        else if (event.key === "0") fit();
        else return;
        event.preventDefault();
      }}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, camera: cameraRef.current, active: false };
      }}
      onPointerMove={(event) => {
        const d = drag.current;
        if (!d || d.id !== event.pointerId) return;
        const dx = event.clientX - d.x;
        const dy = event.clientY - d.y;
        if (!d.active && Math.hypot(dx, dy) < 4) return;
        if (!d.active) {
          d.active = true;
          setDragging(true);
          moved.current = true;
          cancelAnimationFrame(animation.current);
          event.currentTarget.setPointerCapture?.(event.pointerId);
        }
        setCamera({ ...d.camera, x: d.camera.x + dx, y: d.camera.y + dy });
      }}
      onPointerUp={(event) => {
        const d = drag.current;
        drag.current = null;
        if (d?.active) {
          swallowClick.current = true;
          setDragging(false);
          event.currentTarget.releasePointerCapture?.(event.pointerId);
        }
      }}
      onPointerCancel={() => {
        drag.current = null;
        setDragging(false);
      }}
      onClickCapture={(event) => {
        if (swallowClick.current) {
          swallowClick.current = false;
          event.stopPropagation();
          event.preventDefault();
        }
      }}
      onClick={(event) => {
        if ((event.target as Element).closest?.(".xg-zoom")) return;
        onBackground?.();
      }}
    >
      <svg className="xg-svg" width={size.w} height={size.h} role="presentation">
        <g transform={`translate(${camera.x.toFixed(2)} ${camera.y.toFixed(2)}) scale(${camera.k.toFixed(4)})`}>{children}</g>
      </svg>
      <div className="xg-zoom" onPointerDown={(event) => event.stopPropagation()}>
        <Button variant="ghost" size="icon" aria-label="Zoom out" tooltip="Zoom out (−)" onClick={() => zoomCentre(1 / 1.2)} icon={<Minus size={14} aria-hidden="true" />} />
        <span className="xg-zoom-level">{Math.round(camera.k * 100)}%</span>
        <Button variant="ghost" size="icon" aria-label="Zoom in" tooltip="Zoom in (+)" onClick={() => zoomCentre(1.2)} icon={<Plus size={14} aria-hidden="true" />} />
        <Button variant="ghost" size="icon" aria-label="Fit to screen" tooltip="Fit (0)" onClick={fit} icon={<Maximize2 size={14} aria-hidden="true" />} />
      </div>
    </div>
  );
}
