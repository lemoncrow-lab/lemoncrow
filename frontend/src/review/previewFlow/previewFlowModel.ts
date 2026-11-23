/** P4-1: source-pinned surface discovery, grouping and evidence states. */
import type { ReviewSurfaceInfo, ReviewSurfaceRunResult, ReviewTarget } from "../types";
export type PreviewTab = "old" | "new" | "changes";
export type SurfaceGroup = "Pages" | "API requests" | "Components" | "Tables" | "Checks" | "Other";
export interface SurfaceRow {
  key: string;
  surface: ReviewSurfaceInfo;
  group: SurfaceGroup;
  reached: boolean;
  verifiedPaths: string[];
  /** A numeric comparison exists only after an actual comparison, never from heuristic guesswork. */
  magnitude: number | null;
}
export function surfaceKey(s: Pick<ReviewSurfaceInfo, "id" | "provider">): string {
  return `${s.provider}:${s.id}`;
}
/** A real check: the server marks a test or validation kind, or a service whose runtime is a command that runs and exits. */
export function surfaceGroup(s: ReviewSurfaceInfo): SurfaceGroup {
  const kind = s.kind.toLowerCase(), provider = s.provider.toLowerCase();
  if (/test|check|validation/.test(kind) || /test|check/.test(provider)) return "Checks";
  if (provider === "service") return s.metadata?.runner === "command" ? "Checks" : "Other";
  if (/bruno|api|http|request/.test(`${provider} ${kind}`)) return "API requests";
  if (/web|route|page|html/.test(`${provider} ${kind}`)) return "Pages";
  if (/component|story/.test(`${provider} ${kind}`)) return "Components";
  if (/csv|table|dataset/.test(`${provider} ${kind}`)) return "Tables";
  return "Other";
}
export function isExecutableCheck(s: ReviewSurfaceInfo): boolean {
  return surfaceGroup(s) === "Checks" && s.capabilities.includes("execute") && !!s.runtime;
}
export function reachedPaths(s: ReviewSurfaceInfo, targets: readonly ReviewTarget[]): string[] {
  const changed = new Set(targets.map((t) => t.path));
  return [...new Set(s.affected_paths.filter((path) => changed.has(path)))];
}
export function surfaceRows(surfaces: readonly ReviewSurfaceInfo[], targets: readonly ReviewTarget[], magnitudeByKey: Readonly<Record<string, number>> = {}): SurfaceRow[] {
  return surfaces.map((surface) => {
    const key = surfaceKey(surface);
    const paths = reachedPaths(surface, targets);
    const reported = magnitudeByKey[key];
    return {
      key, surface, group: surfaceGroup(surface), reached: paths.length > 0, verifiedPaths: paths,
      magnitude: Number.isFinite(reported) && reported >= 0 ? reported : null,
    };
  }).sort((a, b) => Number(b.reached) - Number(a.reached)
    || Number(b.magnitude !== null) - Number(a.magnitude !== null)
    || (b.magnitude ?? -1) - (a.magnitude ?? -1)
    || a.group.localeCompare(b.group) || a.surface.title.localeCompare(b.surface.title) || a.key.localeCompare(b.key));
}
export function canCompare(s: ReviewSurfaceInfo): boolean {
  if (s.preview) return s.preview.status !== "unavailable" && s.preview.sides.includes("old") && s.preview.sides.includes("new");
  return s.capabilities.includes("execute") && Boolean(s.runtime);
}
export interface PreviewResult {
  source: "preview" | "run";
  side: "old" | "new";
  revisionId: string;
  display: string;
  mime: string;
  text: string;
  url?: string;
  filename: string;
  durationMs?: number;
  runner?: string;
  status?: string;
  summary?: string;
}
export function runToResult(r: ReviewSurfaceRunResult, expectedRevision: string): PreviewResult {
  if (r.revision_id !== expectedRevision) throw new Error("The returned check belongs to another revision.");
  return { source: "run", side: r.side, revisionId: r.revision_id, display: "text", mime: "text/plain",
    text: r.output || r.summary || "No output was returned.", filename: r.surface_id, durationMs: r.duration_ms,
    runner: r.runner, status: r.status, summary: r.summary };
}
export function compareText(a: PreviewResult | null, b: PreviewResult | null): { changed: boolean; magnitude: number; added: string[]; removed: string[] } | null {
  if (!a || !b || a.revisionId !== b.revisionId || !["text", "json", "html"].includes(a.display) || !["text", "json", "html"].includes(b.display)) return null;
  const oldLines = a.text.split("\n");
  const newLines = b.text.split("\n");
  const oldSet = new Set(oldLines), newSet = new Set(newLines);
  const added = newLines.filter((line) => !oldSet.has(line));
  const removed = oldLines.filter((line) => !newSet.has(line));
  return { changed: a.text !== b.text, magnitude: added.length + removed.length || (a.text !== b.text ? 1 : 0), added, removed };
}
export function previewMeaning(s: ReviewSurfaceInfo): string {
  if (isExecutableCheck(s)) return "Not run on this revision until you run it";
  if (s.preview?.status === "unavailable") return s.preview.reason || "Preview renderer unavailable";
  if (s.preview) return `${s.preview.label || s.preview.renderer} · ready to render on request`;
  if (s.capabilities.includes("execute")) return "Executable surface · not run";
  return "No runnable or renderable preview is configured for this surface";
}
export function surfaceRouteValue(s: ReviewSurfaceInfo): string {
  return encodeURIComponent(surfaceKey(s));
}

/** One line of an output: what a person compares first ("403 Forbidden", "200 OK", "passed"). */
export function sideSummary(r: PreviewResult): string {
  if (r.source === "run" && r.status) return r.status;
  const line = r.text.split("\n").map((l) => l.trim()).find((l) => l.length > 0) ?? "";
  return line.length > 32 ? `${line.slice(0, 31)}…` : line || "empty";
}
/** "403 → 200" for the Focus label: the first line of each side (an HTTP status keeps only its code), or "unchanged". */
export function changeSummary(old: PreviewResult, next: PreviewResult): string {
  if (old.text === next.text && old.status === next.status) return "unchanged";
  const short = (r: PreviewResult) => { const line = sideSummary(r); return /^\d{3}\b/.test(line) ? line.slice(0, 3) : line; };
  return `${short(old)} → ${short(next)}`;
}
/** Plain-words provenance (SPEC 4.7): what was run, on which revision, how long. No raw token, no identifier. */
export function provenanceText(results: ReadonlyArray<PreviewResult | null>, revision: { head_sha?: string; revision_number: number }): string {
  const label = revision.head_sha ? revision.head_sha.slice(0, 7) : `#${revision.revision_number + 1}`;
  const real = results.filter((r): r is PreviewResult => r !== null);
  const ran = real.some((r) => r.source === "run");
  const ms = real.reduce((sum, r) => sum + (r.durationMs ?? 0), 0);
  const time = real.some((r) => r.durationMs !== undefined) ? ` · ${(ms / 1000).toFixed(1)} s` : "";
  return `Checked in code · ${ran ? "Ran" : "Rendered"} revision ${label} in a sandbox${time}`;
}
/** What is still unchecked for this surface, in words. Empty when both sides were captured. */
export function notChecked(s: ReviewSurfaceInfo, old: PreviewResult | null, next: PreviewResult | null): string[] {
  const rows: string[] = [];
  if (!old) rows.push("Before: not rendered for this revision");
  if (!next) rows.push("After: not rendered for this revision");
  if (s.preview?.status === "unavailable") rows.push(s.preview.reason || "The preview renderer is unavailable");
  return rows;
}

/** Files the surface lists that this change does not touch. Not something left unchecked, so the viewer says it
 * beside "Relates to" rather than under "Not checked" (review-17.4). */
export function pathsOutsideChange(s: ReviewSurfaceInfo, targets: readonly ReviewTarget[]): string[] {
  return s.affected_paths.filter((path) => !targets.some((t) => t.path === path));
}

const PROVIDER_NAME: Record<string, string> = { bruno: "Bruno", vite: "Vite", storybook: "Storybook" };
/** "Bruno" for the provider id "bruno" (SPEC 4.7 writes the tool's name); any other id with its first letter capitalised. */
export function providerName(provider: string): string {
  return PROVIDER_NAME[provider.toLowerCase()] ?? (provider ? provider[0].toUpperCase() + provider.slice(1) : "");
}

/** "GET /api/status" for "GET {{baseUrl}}/api/status": the collection's base-address variable is not part of what is
 * requested (SPEC 4.7 shows `GET /orders/7`). Only a variable that starts the address goes; one inside the path stays. */
export function plainLocator(locator: string): string {
  return locator.replace(/(^|\s)\{\{\s*[\w.-]+\s*\}\}(?=\/)/g, "$1").trim();
}
