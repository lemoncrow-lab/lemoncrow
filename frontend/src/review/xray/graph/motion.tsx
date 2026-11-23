/** Motion for the X-Ray: the reduced-motion preference and the pulse that travels recorded arrows in order. */
import { useCallback, useEffect, useRef, useState } from "react";

import { pointAlong, polylineLength, type Point } from "./geometry";

/** Reduced motion unless the browser says otherwise; without `matchMedia` (tests, old engines) nothing moves. */
export function useReducedMotion(): boolean {
  const query = typeof window !== "undefined" && typeof window.matchMedia === "function" ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  const [reduced, setReduced] = useState(() => (query ? query.matches : true));
  useEffect(() => {
    if (!query) return;
    const update = () => setReduced(query.matches);
    query.addEventListener?.("change", update);
    return () => query.removeEventListener?.("change", update);
  }, [query]);
  return reduced;
}

const SPEED = 0.32; // px per ms
const MIN_MS = 650;
const MAX_MS = 1500;
const GAP_MS = 260;

/** How long the pulse takes over a route: by length, within bounds. */
export function pulseDuration(points: readonly Point[]): number {
  return Math.round(Math.min(MAX_MS, Math.max(MIN_MS, polylineLength(points) / SPEED)));
}

/**
 * Cycles through `ids` one at a time: the current id and a run counter (to restart the same id). Stopped when
 * `enabled` is false or there is nothing to pulse.
 */
export function usePulseCycle(ids: readonly string[], enabled: boolean): { id: string | null; run: number; done(): void } {
  const [state, setState] = useState({ index: 0, run: 0 });
  const key = ids.join("|");
  useEffect(() => setState({ index: 0, run: 0 }), [key]);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  const done = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setState((s) => ({ index: ids.length ? (s.index + 1) % ids.length : 0, run: s.run + 1 })), GAP_MS);
  }, [ids.length]);
  if (!enabled || !ids.length) return { id: null, run: state.run, done };
  return { id: ids[state.index % ids.length] ?? null, run: state.run, done };
}

/** A dot that travels a route once, then calls `onDone`. It moves its own transform; React does not re-render per frame. */
export function Pulse({ points, tone, onDone }: { points: readonly Point[]; tone: string; onDone(): void }) {
  const ref = useRef<SVGGElement | null>(null);
  const finish = useRef(onDone);
  finish.current = onDone;
  useEffect(() => {
    if (points.length < 2) return;
    const duration = pulseDuration(points);
    let frame = 0;
    const start = performance.now();
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      const at = pointAlong(points, eased);
      ref.current?.setAttribute("transform", `translate(${at.x.toFixed(1)} ${at.y.toFixed(1)})`);
      ref.current?.setAttribute("opacity", t > 0.92 ? String((1 - t) / 0.08) : "1");
      if (t < 1) frame = requestAnimationFrame(step);
      else finish.current();
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [points]);
  const start = points[0] ?? { x: 0, y: 0 };
  return (
    <g ref={ref} className="xg-pulse" data-tone={tone} data-testid="xg-pulse" transform={`translate(${start.x} ${start.y})`} aria-hidden="true">
      <circle className="xg-pulse-glow" r={9} />
      <circle className="xg-pulse-dot" r={4} />
    </g>
  );
}
