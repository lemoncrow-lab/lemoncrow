/**
 * Reading and requesting the X-Ray graph document (docs/reference/xray-graph.md, "Endpoint").
 *
 * `fetchXRayGraph` resolves to a settled state (`ready` or `unavailable`), polling while the server says `pending` and
 * handing each pending answer to `onUpdate` (it may already carry the index-only graph). A server without the endpoint
 * answers 404; that is `unavailable` with `missing: true`, so the screen can fall back to the file Map picture.
 */
import { requestBrowserJson } from "../../reviewApi";
import type { XRayGraph, XRayGraphResponse } from "./xrayGraphTypes";

export interface XRayGraphState extends XRayGraphResponse {
  /** The server has no X-Ray graph endpoint at all (404). */
  missing?: boolean;
}

export type JsonRequest = <T>(path: string, init?: RequestInit) => Promise<T>;

export interface FetchXRayGraphOptions {
  /** Each `pending` answer, before the next poll. */
  onUpdate?: (state: XRayGraphState) => void;
  signal?: AbortSignal;
  /** First wait between polls; it grows by half each time up to `maxIntervalMs`. */
  intervalMs?: number;
  maxIntervalMs?: number;
  request?: JsonRequest;
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

const MISSING_REASON = "This server does not draw X-Ray graphs yet.";
const UNREADABLE_REASON = "The X-Ray graph could not be read.";

function statusOf(error: unknown): number | undefined {
  return typeof error === "object" && error !== null && "status" in error && typeof (error as { status: unknown }).status === "number"
    ? (error as { status: number }).status
    : undefined;
}

function abortError(): Error {
  return Object.assign(new Error("aborted"), { name: "AbortError" });
}

function defaultSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError());
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/** A graph that can be drawn: version 1 with the arrays the renderer walks. Anything else is unreadable. */
export function readableGraph(value: unknown): XRayGraph | null {
  if (!value || typeof value !== "object") return null;
  const graph = value as Partial<XRayGraph>;
  if (graph.version !== 1 || !Array.isArray(graph.lanes) || !Array.isArray(graph.nodes) || !Array.isArray(graph.views) || graph.views.length === 0) return null;
  return {
    ...(graph as XRayGraph),
    edges: Array.isArray(graph.edges) ? graph.edges : [],
    flows: Array.isArray(graph.flows) ? graph.flows : [],
    degraded: Array.isArray(graph.degraded) ? graph.degraded : [],
  };
}

/** One answer, normalised: `ready` without a drawable graph is unavailable, and a pending graph is kept only if drawable. */
export function normaliseXRayGraphState(body: XRayGraphResponse): XRayGraphState {
  const graph = body.graph === undefined ? null : readableGraph(body.graph);
  if (body.state === "ready") return graph ? { state: "ready", graph } : { state: "unavailable", reason: UNREADABLE_REASON };
  if (body.state === "pending") return graph ? { state: "pending", graph } : { state: "pending" };
  return { state: "unavailable", reason: body.reason || "There is no X-Ray graph for this revision." };
}

function graphPath(reviewId: string, revisionId: string): string {
  return `/api/reviews/${encodeURIComponent(reviewId)}/xray-graph?revision_id=${encodeURIComponent(revisionId)}`;
}

/** One GET, with 404 read as a server that has no such endpoint. Other failures throw. */
export async function getXRayGraphOnce(reviewId: string, revisionId: string, request: JsonRequest = requestBrowserJson, signal?: AbortSignal): Promise<XRayGraphState> {
  try {
    const body = await request<XRayGraphResponse>(graphPath(reviewId, revisionId), signal ? { signal } : undefined);
    return normaliseXRayGraphState(body);
  } catch (error) {
    if (statusOf(error) === 404) return { state: "unavailable", reason: MISSING_REASON, missing: true };
    throw error;
  }
}

export async function fetchXRayGraph(reviewId: string, revisionId: string, options: FetchXRayGraphOptions = {}): Promise<XRayGraphState> {
  const { onUpdate, signal, request = requestBrowserJson, sleep = defaultSleep } = options;
  const maxInterval = options.maxIntervalMs ?? 10_000;
  let interval = options.intervalMs ?? 2_000;
  for (;;) {
    if (signal?.aborted) throw abortError();
    const state = await getXRayGraphOnce(reviewId, revisionId, request, signal);
    if (state.state !== "pending") return state;
    onUpdate?.(state);
    await sleep(interval, signal);
    interval = Math.min(maxInterval, Math.round(interval * 1.5));
  }
}

export type XRayGraphRequestResult = { ok: true; state: XRayGraphState } | { ok: false; reason: string };

/**
 * `POST .../xray-graph/generate`: ask for a (re)drawing. With `ai: true` the server spends Review AI budget; when AI is
 * off or over budget it answers 409 with the reason and keeps the index-only graph, and that reason is what we show.
 */
export async function requestXRayGraph(
  reviewId: string,
  revisionId: string,
  { ai }: { ai: boolean },
  request: JsonRequest = requestBrowserJson,
): Promise<XRayGraphRequestResult> {
  try {
    const body = await request<Partial<XRayGraphResponse> | null>(`/api/reviews/${encodeURIComponent(reviewId)}/xray-graph/generate`, {
      method: "POST",
      body: JSON.stringify({ revision_id: revisionId, ai }),
    });
    // The contract does not fix the success body; a state answer is used as is, anything else means "started".
    const state = body && typeof body.state === "string" ? normaliseXRayGraphState(body as XRayGraphResponse) : { state: "pending" as const };
    return { ok: true, state };
  } catch (error) {
    const message = error instanceof Error && error.message && !/^\d+$/.test(error.message) ? error.message : "";
    if (statusOf(error) === 409) return { ok: false, reason: message || "AI drawing is not available for this Review right now." };
    return { ok: false, reason: message || "The X-Ray could not be redrawn." };
  }
}
