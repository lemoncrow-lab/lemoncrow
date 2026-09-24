import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { ExternalLink, FileCode2, Link2, Monitor, Moon, Smartphone, Sun, SunMoon, Tablet as TabletIcon, Unlink2 } from "lucide-react";

import type { Theme } from "../lib/theme";
import { currentReviewId, fetchLiveWebPreview } from "./reviewApi";
import { setBoundedCacheEntry, touchCacheEntry } from "./previewCache";
import SurfacePicker from "./SurfacePicker";
import type { WebPreview } from "./types";

export type WebSurfaceMode = "preview" | "compare";
export type ViewportPreset = "desktop" | "tablet" | "mobile";
export type PreviewTheme = "auto" | "light" | "dark";
type PreviewLoadStatus = "preparing" | "ready" | "failed";
export interface WebRouteOption {
  route: string;
  surfaceKey: string;
  label?: string;
  detail?: string;
  searchText?: string;
  priority?: number;
}

interface WebReviewPreviewProps {
  preview: WebPreview;
  documentPath: string;
  /** Exact Review revision backing the live surface. */
  revisionId?: string;
  surfaceKey: string;
  surfaceTitle: string;
  serviceName: string;
  affectedPaths: string[];
  framework: string;
  chromeTheme: Theme;
  active: boolean;
  routeOptions: WebRouteOption[];
  onActivate(): void;
  onOpenPath(path: string): void;
  onOpenDedicated(): void;
  onNavigateSurface(surfaceKey: string): void;
}

interface SurfacePreferences {
  mode: WebSurfaceMode;
  viewport: ViewportPreset;
  previewTheme: PreviewTheme;
  linkedScroll: boolean;
}

const VIEWPORTS: Record<ViewportPreset, { label: string; width: number; shortcut: string }> = {
  desktop: { label: "Desktop", width: 1440, shortcut: "D" },
  tablet: { label: "Tablet", width: 768, shortcut: "T" },
  mobile: { label: "Mobile", width: 390, shortcut: "M" },
};

const FRAME_HEIGHT = 760;
const livePreviewUrls = new Map<string, string>();
const livePreviewLoads = new Map<string, Promise<string>>();

function liveKey(revisionId: string, path: string, side: "old" | "new", route: string): string {
  return `${currentReviewId()}\u0000${revisionId}\u0000${path}\u0000${side}\u0000${route}`;
}

function loadLivePreview(revisionId: string, path: string, side: "old" | "new", route: string): Promise<string> {
  const key = liveKey(revisionId, path, side, route);
  const cached = touchCacheEntry(livePreviewUrls, key);
  if (cached) return Promise.resolve(cached);
  const pending = livePreviewLoads.get(key);
  if (pending) return pending;
  const request = fetchLiveWebPreview(currentReviewId(), path, side, route, revisionId)
    .then(({ url }) => {
      setBoundedCacheEntry(livePreviewUrls, key, url);
      livePreviewLoads.delete(key);
      return url;
    })
    .catch((error) => {
      livePreviewLoads.delete(key);
      throw error;
    });
  livePreviewLoads.set(key, request);
  return request;
}

function storageKey(surfaceKey: string): string {
  return `lemoncrow-review-surface:${currentReviewId()}:${surfaceKey}`;
}

function loadPreferences(surfaceKey: string): SurfacePreferences {
  const fallback: SurfacePreferences = {
    mode: "preview",
    viewport: "desktop",
    previewTheme: "auto",
    linkedScroll: true,
  };
  try {
    const raw = sessionStorage.getItem(storageKey(surfaceKey));
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Partial<SurfacePreferences>;
    return {
      mode: parsed.mode === "compare" ? "compare" : "preview",
      viewport: parsed.viewport && parsed.viewport in VIEWPORTS ? parsed.viewport : "desktop",
      previewTheme: parsed.previewTheme === "light" || parsed.previewTheme === "dark" ? parsed.previewTheme : "auto",
      linkedScroll: parsed.linkedScroll !== false,
    };
  } catch {
    return fallback;
  }
}

function persistPreferences(surfaceKey: string, preferences: SurfacePreferences): void {
  try {
    sessionStorage.setItem(storageKey(surfaceKey), JSON.stringify(preferences));
  } catch {
    // Session persistence is a convenience; Review remains fully usable without it.
  }
}

function themedUrl(url: string, theme: Theme): string {
  const next = new URL(url);
  next.searchParams.set("lc-theme", theme);
  return next.toString();
}

function DeviceIcon({ preset }: { preset: ViewportPreset }) {
  if (preset === "desktop") return <Monitor size={14} strokeWidth={1.7} />;
  if (preset === "tablet") return <TabletIcon size={14} strokeWidth={1.7} />;
  return <Smartphone size={14} strokeWidth={1.7} />;
}

function DevicePicker({ value, onChange }: { value: ViewportPreset; onChange(value: ViewportPreset): void }) {
  return (
    <div role="group" aria-label="Preview device" className="inline-flex shrink-0 overflow-hidden rounded border border-neutral-700 bg-neutral-950">
      {(Object.keys(VIEWPORTS) as ViewportPreset[]).map((preset) => {
        const item = VIEWPORTS[preset];
        const selected = value === preset;
        return (
          <button
            key={preset}
            type="button"
            aria-label={`${item.label} viewport`}
            aria-pressed={selected}
            title={`${item.label} · ${item.width}px · ${item.shortcut}`}
            onClick={() => onChange(preset)}
            className={`flex h-7 w-8 items-center justify-center border-r border-neutral-800 last:border-r-0 ${selected
              ? "bg-violet-500/15 text-violet-200"
              : "text-neutral-500 hover:bg-neutral-900 hover:text-neutral-50"}`}
          >
            <DeviceIcon preset={preset} />
          </button>
        );
      })}
    </div>
  );
}

function PreviewThemeToggle({ value, chromeTheme, onChange }: { value: PreviewTheme; chromeTheme: Theme; onChange(value: PreviewTheme): void }) {
  const next: PreviewTheme = value === "auto" ? "light" : value === "light" ? "dark" : "auto";
  const label = value === "auto" ? `Auto · ${chromeTheme}` : value === "light" ? "Light" : "Dark";
  const nextLabel = next === "auto" ? `Auto · ${chromeTheme}` : next === "light" ? "Light" : "Dark";
  const Icon = value === "auto" ? SunMoon : value === "light" ? Sun : Moon;
  return (
    <button
      type="button"
      aria-label={`Preview theme: ${label}. Switch to ${nextLabel}`}
      title={`Preview theme: ${label} · click for ${nextLabel}`}
      onClick={() => onChange(next)}
      className="inline-flex h-7 w-8 shrink-0 items-center justify-center rounded border border-neutral-700 bg-neutral-950 text-violet-200 hover:bg-neutral-900"
    >
      <Icon size={13} strokeWidth={1.7} />
    </button>
  );
}

function LiveFrame({
  revisionId,
  path,
  side,
  route,
  label,
  viewportWidth,
  theme,
  frameRef,
  showSideHeader = false,
  presentationScale,
  onStatusChange,
}: {
  revisionId: string;
  path: string;
  side: "old" | "new";
  route: string;
  label: string;
  viewportWidth: number;
  theme: Theme;
  frameRef?: RefObject<HTMLIFrameElement>;
  showSideHeader?: boolean;
  presentationScale?: number;
  onStatusChange?(status: PreviewLoadStatus): void;
}) {
  const key = liveKey(revisionId, path, side, route);
  const [url, setUrl] = useState(() => touchCacheEntry(livePreviewUrls, key) ?? "");
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let live = true;
    const cached = touchCacheEntry(livePreviewUrls, key);
    if (cached) {
      setUrl(cached);
      setError("");
      onStatusChange?.("ready");
      return () => { live = false; };
    }
    setUrl("");
    setError("");
    onStatusChange?.("preparing");
    void loadLivePreview(revisionId, path, side, route)
      .then((next) => {
        if (live) {
          setUrl(next);
          onStatusChange?.("ready");
        }
      })
      .catch((reason: unknown) => {
        if (live) {
          setError(String(reason instanceof Error ? reason.message : reason));
          onStatusChange?.("failed");
        }
      });
    return () => { live = false; };
  }, [key, onStatusChange, path, retry, revisionId, route, side]);

  const resolvedUrl = url ? themedUrl(url, theme) : "";
  return (
    <section
      className={`flex shrink-0 flex-col overflow-hidden rounded-md border border-neutral-700 shadow-sm ${theme === "dark" ? "bg-neutral-950" : "bg-white"}`}
      data-testid={`web-live-${side}`}
      style={{ width: `${viewportWidth}px`, height: `${FRAME_HEIGHT}px` }}
    >
      {showSideHeader && (
        <div className="flex h-7 shrink-0 items-center justify-between border-b border-neutral-800 bg-neutral-950 px-2 font-mono text-[10px] uppercase tracking-wider text-neutral-500">
          <span>{label}</span>
          <span>{viewportWidth}px{presentationScale != null ? ` · ${Math.round(presentationScale * 100)}%` : ""}</span>
        </div>
      )}
      {resolvedUrl ? (
        <iframe
          ref={frameRef}
          src={resolvedUrl}
          title={`${label} ${route}`}
          data-testid={`web-live-frame-${side}`}
          sandbox="allow-scripts allow-same-origin"
          referrerPolicy="no-referrer"
          className={`block min-h-0 flex-1 border-0 ${theme === "dark" ? "bg-neutral-950" : "bg-white"}`}
          style={{ width: `${viewportWidth}px`, colorScheme: theme }}
        />
      ) : error ? (
        <div className="flex min-h-0 flex-1 items-center justify-center bg-surface-sunken px-6 py-10 text-center text-[11px] text-neutral-400">
          <div className="max-w-xl rounded border border-rose-900/60 bg-rose-950/15 px-4 py-3">
            <div className="font-medium text-rose-300">Preview build failed</div>
            <div className="mt-2 flex items-center justify-center gap-3">
              <details className="text-left">
                <summary className="cursor-pointer text-[10px] text-neutral-500 hover:text-neutral-50">View details</summary>
                <pre className="mt-2 max-h-32 max-w-lg overflow-auto whitespace-pre-wrap text-[10px] leading-4 text-rose-400/80">{error}</pre>
              </details>
              <button
                type="button"
                onClick={() => setRetry((value) => value + 1)}
                className="border border-neutral-700 px-2 py-1 text-[10px] text-neutral-300 hover:border-neutral-500"
              >
                Retry
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 items-center justify-center bg-surface-sunken text-[10px] text-neutral-500">
          <div className="text-center">Preparing preview for this revision…</div>
        </div>
      )}
    </section>
  );
}

function ScaledCompareFrame({ scale, frameRef, ...props }: {
  scale: number;
  frameRef: RefObject<HTMLIFrameElement>;
  revisionId: string;
  path: string;
  side: "old" | "new";
  route: string;
  label: string;
  viewportWidth: number;
  theme: Theme;
  onStatusChange?(status: PreviewLoadStatus): void;
}) {
  const visualWidth = props.viewportWidth * scale;
  const visualHeight = FRAME_HEIGHT * scale;
  return (
    <div className="flex min-w-0 flex-1 justify-center overflow-hidden">
      <div className="shrink-0" style={{ width: `${visualWidth}px`, height: `${visualHeight}px` }}>
        <div style={{
          width: `${props.viewportWidth}px`,
          height: `${FRAME_HEIGHT}px`,
          transform: `scale(${scale})`,
          transformOrigin: "top left",
        }}>
          <LiveFrame {...props} frameRef={frameRef} showSideHeader presentationScale={scale} />
        </div>
      </div>
    </div>
  );
}

export default function WebReviewPreview({
  preview,
  documentPath,
  revisionId = "",
  surfaceKey,
  surfaceTitle,
  serviceName,
  affectedPaths,
  framework,
  chromeTheme,
  active,
  routeOptions,
  onActivate,
  onOpenPath,
  onOpenDedicated,
  onNavigateSurface,
}: WebReviewPreviewProps) {
  const [preferences, setPreferences] = useState<SurfacePreferences>(() => loadPreferences(surfaceKey));
  const [compareScale, setCompareScale] = useState(1);
  const [newStatus, setNewStatus] = useState<PreviewLoadStatus>("preparing");
  const [oldStatus, setOldStatus] = useState<PreviewLoadStatus>("preparing");
  const viewportWidth = VIEWPORTS[preferences.viewport].width;
  const effectiveTheme: Theme = preferences.previewTheme === "auto" ? chromeTheme : preferences.previewTheme;
  const route = preview.default_route || preview.routes[0] || "/";
  const compareShellRef = useRef<HTMLDivElement>(null);
  const beforeFrameRef = useRef<HTMLIFrameElement>(null);
  const afterFrameRef = useRef<HTMLIFrameElement>(null);

  const onNewStatus = useCallback((status: PreviewLoadStatus) => setNewStatus(status), []);
  const onOldStatus = useCallback((status: PreviewLoadStatus) => setOldStatus(status), []);

  const updatePreferences = (patch: Partial<SurfacePreferences>) => {
    setPreferences((current) => {
      const next = { ...current, ...patch };
      persistPreferences(surfaceKey, next);
      return next;
    });
  };

  const navigateRoute = (direction: -1 | 1) => {
    if (routeOptions.length < 2) return;
    const index = Math.max(0, routeOptions.findIndex((option) => option.surfaceKey === surfaceKey));
    const next = routeOptions[(index + direction + routeOptions.length) % routeOptions.length];
    if (next) onNavigateSurface(next.surfaceKey);
  };

  useEffect(() => {
    if (preferences.mode !== "compare") return;
    const shell = compareShellRef.current;
    if (!shell) return;
    const update = () => {
      const innerWidth = Math.max(0, shell.clientWidth - 24);
      if (innerWidth <= 0) return;
      const sideWidth = Math.max(1, (innerWidth - 12) / 2);
      setCompareScale(Math.min(1, sideWidth / viewportWidth));
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(shell);
    return () => observer.disconnect();
  }, [preferences.mode, viewportWidth]);

  useEffect(() => {
    if (preferences.mode !== "compare" || !preferences.linkedScroll) return;
    const onMessage = (event: MessageEvent) => {
      const payload = event.data as { type?: unknown; ratio?: unknown } | null;
      if (!payload || payload.type !== "lc-review-preview-scroll" || typeof payload.ratio !== "number") return;
      const beforeWindow = beforeFrameRef.current?.contentWindow;
      const afterWindow = afterFrameRef.current?.contentWindow;
      let target: HTMLIFrameElement | null = null;
      if (event.source === beforeWindow) target = afterFrameRef.current;
      else if (event.source === afterWindow) target = beforeFrameRef.current;
      if (!target?.contentWindow || !target.src) return;
      target.contentWindow.postMessage(
        { type: "lc-review-preview-scroll-to", ratio: Math.max(0, Math.min(1, payload.ratio)) },
        new URL(target.src).origin,
      );
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [preferences.linkedScroll, preferences.mode, route]);

  useEffect(() => {
    if (!active) return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable)) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const key = event.key.toLowerCase();
      if (key === "1") updatePreferences({ mode: "preview" });
      else if (key === "2") updatePreferences({ mode: "compare" });
      else if (key === "d") updatePreferences({ viewport: "desktop" });
      else if (key === "t") updatePreferences({ viewport: "tablet" });
      else if (key === "m") updatePreferences({ viewport: "mobile" });
      else if (event.key === "{") navigateRoute(-1);
      else if (event.key === "}") navigateRoute(1);
      else return;
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const compareHeight = Math.min(820, Math.max(520, FRAME_HEIGHT * compareScale + 24));
  const routeValue = routeOptions.some((option) => option.surfaceKey === surfaceKey) ? surfaceKey : "";
  const previewStatus: PreviewLoadStatus = preferences.mode === "compare"
    ? (newStatus === "failed" || oldStatus === "failed" ? "failed" : newStatus === "ready" && oldStatus === "ready" ? "ready" : "preparing")
    : newStatus;

  return (
    <div
      className="bg-surface-sunken"
      data-testid={`web-review-preview:${documentPath}`}
      onMouseEnter={onActivate}
      onFocusCapture={onActivate}
    >
      <div className="sticky top-0 z-30 flex h-10 min-w-0 items-center gap-2 overflow-visible border-b border-neutral-800 bg-surface-code/95 px-3 backdrop-blur">
        <button
          type="button"
          onClick={onOpenDedicated}
          aria-label={`Open web surface ${surfaceTitle} in dedicated tab`}
          title={`${framework} rendered surface · open in dedicated tab`}
          className="inline-flex shrink-0 items-center gap-1 text-[10px] font-medium uppercase tracking-[0.12em] text-sky-400/75 hover:text-sky-300"
        >
          <Monitor size={12} /> Web <span className="normal-case tracking-normal text-sky-300/90">· {serviceName}</span> <ExternalLink size={10} />
        </button>
        {routeOptions.length > 1 ? (
          <SurfacePicker
            ariaLabel="Rendered route"
            value={routeValue}
            storageKey={`lemoncrow-review-surface-filter:${currentReviewId()}:web`}
            placeholder="Search routes, roots, frameworks…"
            options={routeOptions.map((option) => ({
              value: option.surfaceKey,
              label: option.label || option.route,
              detail: option.detail,
              searchText: option.searchText,
            }))}
            onChange={onNavigateSurface}
          />
        ) : (
          <span className="max-w-48 shrink-0 truncate font-mono text-[11px] text-neutral-200" title={surfaceTitle}>{route}</span>
        )}

        {previewStatus !== "ready" && (
          <span className={`shrink-0 text-[10px] ${previewStatus === "failed" ? "text-rose-400/80" : "text-amber-400/70"}`}>
            {previewStatus === "failed" ? "Failed" : "Preparing…"}
          </span>
        )}

        <span className="min-w-2 flex-1" />
        <div role="group" aria-label="Surface mode" className="review-segment shrink-0">
          <button type="button" aria-label="Preview mode" aria-pressed={preferences.mode === "preview"} title="Preview · 1" onClick={() => updatePreferences({ mode: "preview" })} className={`review-segment-button ${preferences.mode === "preview" ? "review-segment-button-active" : ""}`}>Preview</button>
          <button type="button" aria-label="Compare mode" aria-pressed={preferences.mode === "compare"} title="Compare · 2" onClick={() => updatePreferences({ mode: "compare" })} className={`review-segment-button ${preferences.mode === "compare" ? "review-segment-button-active" : ""}`}>Compare</button>
        </div>

        <details data-review-dropdown className="relative shrink-0">
          <summary
            role="button"
            aria-label="Web preview options"
            className="review-compact-action h-7 cursor-pointer list-none [&::-webkit-details-marker]:hidden"
          >
            View
          </summary>
          <div className="review-menu absolute right-0 top-8 z-50 w-72 p-2.5">
            <div className="space-y-3">
              <div>
                <div className="review-kicker mb-1.5">Viewport</div>
                <DevicePicker value={preferences.viewport} onChange={(viewport) => updatePreferences({ viewport })} />
              </div>
              <div className="flex items-center justify-between gap-3 border-t border-neutral-900 pt-2.5">
                <span className="text-[10px] text-neutral-500">Preview theme</span>
                <PreviewThemeToggle
                  value={preferences.previewTheme}
                  chromeTheme={chromeTheme}
                  onChange={(previewTheme) => updatePreferences({ previewTheme })}
                />
              </div>
              {preferences.mode === "compare" && (
                <div className="flex items-center justify-between gap-3 border-t border-neutral-900 pt-2.5">
                  <span className="text-[10px] text-neutral-500">Linked scrolling</span>
                  <button
                    type="button"
                    aria-label={preferences.linkedScroll ? "Unlink compare scrolling" : "Link compare scrolling"}
                    aria-pressed={preferences.linkedScroll}
                    title={preferences.linkedScroll ? "Scrolling linked" : "Scrolling independent"}
                    onClick={() => updatePreferences({ linkedScroll: !preferences.linkedScroll })}
                    className={`review-icon-button h-7 w-7 ${preferences.linkedScroll ? "text-sky-300" : "text-neutral-600"}`}
                  >
                    {preferences.linkedScroll ? <Link2 size={13} /> : <Unlink2 size={13} />}
                  </button>
                </div>
              )}
              {affectedPaths.length > 0 && (
                <div className="border-t border-neutral-900 pt-2.5">
                  <div className="review-kicker mb-1">Affected files · {affectedPaths.length}</div>
                  <div className="max-h-40 overflow-auto">
                    {affectedPaths.map((path) => (
                      <button
                        key={path}
                        type="button"
                        onClick={() => onOpenPath(path)}
                        className="flex w-full items-center gap-2 px-1 py-1.5 text-left font-mono text-[10px] text-neutral-400 hover:text-neutral-50"
                      >
                        <FileCode2 size={11} className="shrink-0 text-neutral-600" />
                        <span className="truncate">{path}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </details>
      </div>

      {preferences.mode === "compare" ? (
        <div ref={compareShellRef} data-testid="web-live-compare-shell" className="overflow-hidden bg-neutral-900 p-3" style={{ height: `${compareHeight}px` }}>
          <div className="flex h-full w-full gap-3">
            <ScaledCompareFrame scale={compareScale} frameRef={beforeFrameRef} revisionId={revisionId} path={documentPath} side="old" route={route} label="Before" viewportWidth={viewportWidth} theme={effectiveTheme} onStatusChange={onOldStatus} />
            <ScaledCompareFrame scale={compareScale} frameRef={afterFrameRef} revisionId={revisionId} path={documentPath} side="new" route={route} label="After" viewportWidth={viewportWidth} theme={effectiveTheme} onStatusChange={onNewStatus} />
          </div>
        </div>
      ) : (
        <div data-testid="web-live-preview-shell" className="h-[min(78vh,820px)] min-h-[520px] overflow-x-auto overflow-y-hidden bg-neutral-900 p-3">
          <div className="mx-auto h-full w-max">
            <LiveFrame revisionId={revisionId} path={documentPath} side="new" route={route} label="Preview" viewportWidth={viewportWidth} theme={effectiveTheme} onStatusChange={onNewStatus} />
          </div>
        </div>
      )}
    </div>
  );
}
