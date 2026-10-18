import { Button as UiButton } from "../components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "../components/ui/popover";
import { ToggleGroup } from "../components/ui/toggle-group";
import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { ExternalLink, FileCode2, Link2, Monitor, Moon, Smartphone, Sun, SunMoon, Tablet as TabletIcon, Unlink2 } from "lucide-react";

import type { Theme } from "../lib/theme";
import ImageOverlayCompare from "./ImageOverlayCompare";
import { currentReviewId, fetchLiveWebPreview, fetchReviewVerification, fetchSurfaceImpactWithRetry, fetchWebPreview, type SurfaceImpactScore } from "./reviewApi";
import { setBoundedCacheEntry, touchCacheEntry } from "./previewCache";
import SurfacePicker from "./SurfacePicker";
import type { ReviewVerificationCheck, ReviewVerificationProjection, WebPreview } from "./types";

export type WebSurfaceMode = "preview" | "compare" | "screenshots";
export type ViewportPreset = "desktop" | "tablet" | "mobile";
export type PreviewTheme = "auto" | "light" | "dark";
type PreviewLoadStatus = "preparing" | "ready" | "failed";
export interface WebRouteOption {
  route: string;
  surfaceKey: string;
  /** One changed file whose reachability proves this route is affected. */
  documentPath?: string;
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
  requestedMode?: "preview" | "compare";
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
const verificationLoads = new Map<string, Promise<ReviewVerificationProjection>>();

/** One in-flight Snapshot verification read shared by every Web surface of the Change. */
function loadSnapshotVerification(reviewId: string, revisionId: string): Promise<ReviewVerificationProjection> {
  const key = `${reviewId}\u0000${revisionId}`;
  const pending = verificationLoads.get(key);
  if (pending) return pending;
  const request = fetchReviewVerification(reviewId, revisionId).finally(() => verificationLoads.delete(key));
  verificationLoads.set(key, request);
  return request;
}

function routeIdentity(route: string): string {
  return route.split("?")[0].split("#")[0].replace(/\/+$/, "") || "/";
}

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
      mode: parsed.mode === "compare" || parsed.mode === "screenshots" ? parsed.mode : "preview",
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
    <div role="group" aria-label="Preview device" className="inline-flex shrink-0 overflow-hidden rounded border border-neutral-700 bg-background">
      {(Object.keys(VIEWPORTS) as ViewportPreset[]).map((preset) => {
        const item = VIEWPORTS[preset];
        const selected = value === preset;
        return (
          <UiButton
            key={preset}
            type="button"
            variant="ghost"
            aria-label={`${item.label} viewport`}
            aria-pressed={selected}
            onClick={() => onChange(preset)}
            className={`h-7 w-8 rounded-none border-r border-border last:border-r-0 ${selected
              ? "bg-violet-500/15 text-violet-200"
              : "text-muted-foreground hover:bg-muted hover:text-neutral-50"}`}
          >
            <DeviceIcon preset={preset} />
          </UiButton>
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
    <UiButton
      type="button"
      variant="ghost"
      aria-label={`Preview theme: ${label}. Switch to ${nextLabel}`}
      onClick={() => onChange(next)}
      className="h-7 w-8 shrink-0 rounded border-neutral-700 bg-background text-violet-200 hover:bg-muted"
    >
      <Icon size={13} strokeWidth={1.7} />
    </UiButton>
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
      className={`flex shrink-0 flex-col overflow-hidden rounded-md border border-neutral-700 shadow-sm ${theme === "dark" ? "bg-background" : "bg-white"}`}
      data-testid={`web-live-${side}`}
      style={{ width: `${viewportWidth}px`, height: `${FRAME_HEIGHT}px` }}
    >
      {showSideHeader && (
        <div className="flex h-7 shrink-0 items-center justify-between border-b border-border bg-background px-2 font-mono text-caption uppercase tracking-wider text-muted-foreground">
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
          className={`block min-h-0 flex-1 border-0 ${theme === "dark" ? "bg-background" : "bg-white"}`}
          style={{ width: `${viewportWidth}px`, colorScheme: theme }}
        />
      ) : error ? (
        <div className="flex min-h-0 flex-1 items-center justify-center bg-surface-sunken px-6 py-10 text-center text-label text-neutral-400">
          <div className="max-w-xl rounded border border-rose-900/60 bg-rose-950/15 px-4 py-3">
            <div className="font-medium text-rose-300">Preview build failed</div>
            <div className="mt-2 flex items-center justify-center gap-3">
              <details className="text-left">
                <summary className="cursor-pointer text-caption text-muted-foreground hover:text-neutral-50">View details</summary>
                <pre className="mt-2 max-h-32 max-w-lg overflow-auto whitespace-pre-wrap text-caption leading-4 text-rose-400/80">{error}</pre>
              </details>
              <UiButton
                type="button"
                variant="outline"
                size="xs"
                onClick={() => setRetry((value) => value + 1)}
              >
                Retry
              </UiButton>
            </div>
          </div>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 items-center justify-center bg-surface-sunken text-caption text-muted-foreground">
          <div className="text-center">Preparing preview for this Snapshot…</div>
        </div>
      )}
    </section>
  );
}

function FrozenScreenshotCompare({
  revisionId,
  path,
  route,
  viewportWidth,
}: {
  revisionId: string;
  path: string;
  route: string;
  viewportWidth: number;
}) {
  const [urls, setUrls] = useState<{ before: string; after: string } | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    let beforeUrl = "";
    let afterUrl = "";
    setUrls(null);
    setError("");
    if (!revisionId) {
      setError("Snapshot identity is unavailable for frozen screenshot comparison.");
      return () => { live = false; };
    }
    void Promise.all([
      fetchWebPreview(currentReviewId(), path, "old", route, viewportWidth, 5000, revisionId),
      fetchWebPreview(currentReviewId(), path, "new", route, viewportWidth, 5000, revisionId),
    ]).then(([beforeBlob, afterBlob]) => {
      if (!live) return;
      beforeUrl = URL.createObjectURL(beforeBlob);
      afterUrl = URL.createObjectURL(afterBlob);
      setUrls({ before: beforeUrl, after: afterUrl });
    }).catch((reason: unknown) => {
      if (live) setError(String(reason instanceof Error ? reason.message : reason));
    });
    return () => {
      live = false;
      if (beforeUrl) URL.revokeObjectURL(beforeUrl);
      if (afterUrl) URL.revokeObjectURL(afterUrl);
    };
  }, [path, revisionId, route, viewportWidth]);

  if (error) {
    return (
      <div className="flex min-h-[420px] items-center justify-center bg-muted p-6 text-center text-label text-rose-300">
        <div className="max-w-xl rounded border border-rose-900/60 bg-rose-950/15 px-4 py-3">
          <div className="font-medium">Frozen screenshot diff unavailable</div>
          <div className="mt-1 text-caption text-muted-foreground">{error}</div>
        </div>
      </div>
    );
  }
  if (!urls) {
    return <div className="flex min-h-[420px] items-center justify-center bg-muted text-caption text-muted-foreground">Rendering exact Snapshot screenshots…</div>;
  }
  return (
    <div className="bg-muted p-3" data-testid="web-frozen-screenshot-compare">
      <ImageOverlayCompare
        beforeSrc={urls.before}
        afterSrc={urls.after}
        beforeLabel="Before · old source"
        afterLabel="After · new source"
      />
      <div className="mt-2 text-micro text-muted-foreground">
        Frozen PNGs rendered independently from the old/new sides of the same immutable Snapshot. Source remains authoritative.
      </div>
    </div>
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
  requestedMode,
  onActivate,
  onOpenPath,
  onOpenDedicated,
  onNavigateSurface,
}: WebReviewPreviewProps) {
  const [preferences, setPreferences] = useState<SurfacePreferences>(() => loadPreferences(surfaceKey));
  const [compareScale, setCompareScale] = useState(1);
  const [newStatus, setNewStatus] = useState<PreviewLoadStatus>("preparing");
  const [oldStatus, setOldStatus] = useState<PreviewLoadStatus>("preparing");
  const [routeScores, setRouteScores] = useState<Record<string, SurfaceImpactScore>>({});
  const [rankingRoutes, setRankingRoutes] = useState(false);
  const routeImpactInflightRef = useRef(new Map<string, Promise<SurfaceImpactScore>>());
  const viewportWidth = VIEWPORTS[preferences.viewport].width;
  const effectiveTheme: Theme = preferences.previewTheme === "auto" ? chromeTheme : preferences.previewTheme;
  const route = preview.default_route || preview.routes[0] || "/";
  const compareShellRef = useRef<HTMLDivElement>(null);
  const beforeFrameRef = useRef<HTMLIFrameElement>(null);
  const afterFrameRef = useRef<HTMLIFrameElement>(null);
  const [browserChecks, setBrowserChecks] = useState<ReviewVerificationCheck[]>([]);
  useEffect(() => {
    setBrowserChecks([]);
    if (!revisionId) return;
    let live = true;
    try {
      void loadSnapshotVerification(currentReviewId(), revisionId)
        .then((projection) => {
          if (live) setBrowserChecks(projection.checks.filter((check) => check.browser && check.state === "current"));
        })
        .catch(() => {
          // Browser evidence is optional context on this surface; Overview owns the authoritative/degraded answer.
        });
    } catch {
      return;
    }
    return () => {
      live = false;
    };
  }, [revisionId]);
  const routeBrowserChecks = browserChecks.filter(
    (check) => check.browser?.route && routeIdentity(check.browser.route) === routeIdentity(route),
  );

  const onNewStatus = useCallback((status: PreviewLoadStatus) => setNewStatus(status), []);
  const onOldStatus = useCallback((status: PreviewLoadStatus) => setOldStatus(status), []);

  const updatePreferences = (patch: Partial<SurfacePreferences>) => {
    setPreferences((current) => {
      const next = { ...current, ...patch };
      persistPreferences(surfaceKey, next);
      return next;
    });
  };

  useEffect(() => {
    if (!requestedMode) return;
    setPreferences((current) => {
      if (current.mode === requestedMode) return current;
      const next = { ...current, mode: requestedMode };
      persistPreferences(surfaceKey, next);
      return next;
    });
  }, [requestedMode, surfaceKey]);

  const rankTargets = useMemo(
    () => routeOptions.filter((option) => Boolean(option.documentPath)),
    [routeOptions],
  );

  const loadRouteImpact = useCallback((option: WebRouteOption, attempts = 3): Promise<SurfaceImpactScore> => {
    if (!revisionId) return Promise.reject(new Error("route impact requires a revision"));
    const inflightKey = `${revisionId}\u0000${option.surfaceKey}`;
    const pending = routeImpactInflightRef.current.get(inflightKey);
    if (pending) return pending;
    const separator = option.surfaceKey.indexOf(":");
    const provider = separator > 0 ? option.surfaceKey.slice(0, separator) : "web";
    const surfaceId = separator > 0 ? option.surfaceKey.slice(separator + 1) : option.surfaceKey;
    const request = fetchSurfaceImpactWithRetry(
      currentReviewId(), provider, surfaceId, revisionId, attempts,
    ).then((score) => {
      setRouteScores((current) => ({ ...current, [option.surfaceKey]: score }));
      return score;
    }).finally(() => {
      routeImpactInflightRef.current.delete(inflightKey);
    });
    routeImpactInflightRef.current.set(inflightKey, request);
    return request;
  }, [revisionId]);

  useEffect(() => {
    let live = true;
    if (!revisionId || rankTargets.length < 2) {
      setRouteScores({});
      setRankingRoutes(false);
      return () => { live = false; };
    }

    setRouteScores({});
    setRankingRoutes(true);
    const orderedTargets = [...rankTargets].sort((a, b) => {
      if (a.surfaceKey === surfaceKey) return -1;
      if (b.surfaceKey === surfaceKey) return 1;
      return (a.priority ?? Number.MAX_SAFE_INTEGER) - (b.priority ?? Number.MAX_SAFE_INTEGER)
        || a.route.localeCompare(b.route);
    });

    const run = async () => {
      const pending = new Set(orderedTargets.map((option) => option.surfaceKey));
      for (let pass = 0; pass < 3 && live && pending.size > 0; pass += 1) {
        for (const option of orderedTargets) {
          if (!live || !pending.has(option.surfaceKey)) continue;
          if (option.surfaceKey !== surfaceKey || pass > 0) {
            // Yield between background captures so opening Review is never held
            // hostage by screenshot ranking for routes the reviewer may not visit.
            await new Promise<void>((resolve) => globalThis.setTimeout(resolve, 0));
          }
          try {
            await loadRouteImpact(option, 3);
            if (!live) return;
            pending.delete(option.surfaceKey);
          } catch {
            // Keep the route queued instead of freezing a false "unavailable"
            // state. A later pass and selecting the route both get another try.
          }
        }
        if (pending.size > 0 && pass < 2) {
          await new Promise<void>((resolve) => globalThis.setTimeout(resolve, pass === 0 ? 1200 : 4000));
        }
      }
      if (live) setRankingRoutes(false);
    };
    void run();
    return () => { live = false; };
  }, [loadRouteImpact, rankTargets, revisionId]);

  const rankedRouteOptions = useMemo(() => [...routeOptions].sort((a, b) => {
    const aScore = routeScores[a.surfaceKey];
    const bScore = routeScores[b.surfaceKey];
    if (aScore && bScore) {
      return bScore.magnitude - aScore.magnitude
        || (a.priority ?? Number.MAX_SAFE_INTEGER) - (b.priority ?? Number.MAX_SAFE_INTEGER)
        || a.route.localeCompare(b.route);
    }
    if (aScore) return -1;
    if (bScore) return 1;
    return (a.priority ?? Number.MAX_SAFE_INTEGER) - (b.priority ?? Number.MAX_SAFE_INTEGER)
      || a.route.localeCompare(b.route);
  }), [routeOptions, routeScores]);

  const navigateRoute = (direction: -1 | 1) => {
    if (rankedRouteOptions.length < 2) return;
    const index = Math.max(0, rankedRouteOptions.findIndex((option) => option.surfaceKey === surfaceKey));
    const next = rankedRouteOptions[(index + direction + rankedRouteOptions.length) % rankedRouteOptions.length];
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
      else if (key === "3") updatePreferences({ mode: "screenshots" });
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
  const routeValue = rankedRouteOptions.some((option) => option.surfaceKey === surfaceKey) ? surfaceKey : "";
  const previewStatus: PreviewLoadStatus = preferences.mode === "screenshots"
    ? "ready"
    : preferences.mode === "compare"
      ? (newStatus === "failed" || oldStatus === "failed" ? "failed" : newStatus === "ready" && oldStatus === "ready" ? "ready" : "preparing")
      : newStatus;

  return (
    <div
      className="bg-surface-sunken"
      data-testid={`web-review-preview:${documentPath}`}
      onMouseEnter={onActivate}
      onFocusCapture={onActivate}
    >
      <div className="sticky top-0 z-30 flex h-10 min-w-0 items-center gap-2 overflow-visible border-b border-border bg-surface-code/95 px-3 backdrop-blur">
        <UiButton
          type="button"
          variant="ghost"
          onClick={onOpenDedicated}
          aria-label={`Open web surface ${surfaceTitle} in dedicated tab`}
          tooltip={`${framework} rendered surface · open in dedicated tab`}
          className="h-auto shrink-0 gap-1 border-transparent px-0 text-caption font-medium uppercase tracking-[0.12em] text-sky-400/75 hover:bg-transparent hover:text-sky-300"
        >
          <Monitor size={12} /> Web <span className="normal-case tracking-normal text-sky-300/90">· {serviceName}</span> <ExternalLink size={10} />
        </UiButton>
        {rankedRouteOptions.length > 1 ? (
          <SurfacePicker
            ariaLabel="Rendered route"
            value={routeValue}
            storageKey={`lemoncrow-review-surface-filter:${currentReviewId()}:web`}
            placeholder="Search affected routes, changed files…"
            options={rankedRouteOptions.map((option) => {
              const score = routeScores[option.surfaceKey];
              const visual = score?.label
                || (option.documentPath ? (rankingRoutes ? "visual diff queued…" : "visual diff pending") : "");
              return {
                value: option.surfaceKey,
                label: option.label || option.route,
                detail: [visual, option.detail].filter(Boolean).join(" · "),
                searchText: option.searchText,
              };
            })}
            onChange={(nextSurfaceKey) => {
              const next = routeOptions.find((option) => option.surfaceKey === nextSurfaceKey);
              if (next && !routeScores[nextSurfaceKey]) {
                void loadRouteImpact(next, 4).catch(() => undefined);
              }
              onNavigateSurface(nextSurfaceKey);
            }}
          />
        ) : (
          <span className="max-w-48 shrink-0 truncate font-mono text-label text-neutral-200" title={surfaceTitle}>{route}</span>
        )}

        {rankingRoutes && (
          <span className="shrink-0 font-mono text-micro text-muted-foreground" title="Ranking affected routes by frozen old/new screenshot delta">
            Ranking pixels…
          </span>
        )}

        {revisionId && (
          <span className="shrink-0 rounded border border-violet-900/50 bg-violet-950/20 px-1.5 py-0.5 font-mono text-micro text-violet-300" title={revisionId}>
            Snapshot {revisionId}
          </span>
        )}

        {routeBrowserChecks.slice(0, 2).map((check, index) => (
          <span
            key={check.check_key || `${check.name}:${index}`}
            className={`shrink-0 rounded border px-1.5 py-0.5 font-mono text-micro ${
              check.status === "PASS"
                ? "border-emerald-900/50 text-emerald-300"
                : check.status === "FAIL"
                  ? "border-rose-900/50 text-rose-300"
                  : "border-amber-900/50 text-amber-300"
            }`}
            title={`${check.name}${check.required ? " · required" : " · optional"} — ${check.detail || check.status}. Full browser evidence is in Overview → Snapshot verification.`}
          >
            Browser {check.status}
          </span>
        ))}

        {previewStatus !== "ready" && (
          <span className={`shrink-0 text-caption ${previewStatus === "failed" ? "text-rose-400/80" : "text-amber-400/70"}`}>
            {previewStatus === "failed" ? "Failed" : "Preparing…"}
          </span>
        )}

        <span className="min-w-2 flex-1" />
        <ToggleGroup
          aria-label="Surface mode"
          variant="segment"
          className="shrink-0"
          value={preferences.mode}
          onChange={(value) => updatePreferences({ mode: value as WebSurfaceMode })}
          options={[
            { value: "preview", label: "Preview", title: "Preview · 1" },
            { value: "compare", label: "Compare", title: "Live compare · 2" },
            ...(revisionId ? [{ value: "screenshots", label: "Pixels", title: "Frozen screenshot diff · 3" }] : []),
          ]}
        />

        {preferences.mode === "compare" && (
          <UiButton
            type="button"
            variant="ghost"
            size="icon"
            aria-label={preferences.linkedScroll ? "Unlink compare scrolling" : "Link compare scrolling"}
            aria-pressed={preferences.linkedScroll}
            onClick={() => updatePreferences({ linkedScroll: !preferences.linkedScroll })}
            className={`h-7 w-7 ${preferences.linkedScroll ? "text-sky-300" : "text-muted-foreground"}`}
          >
            {preferences.linkedScroll ? <Link2 size={13} /> : <Unlink2 size={13} />}
          </UiButton>
        )}

        <Popover>
          <PopoverTrigger asChild>
            <UiButton type="button" variant="ghost" size="compact" aria-label="Web preview options" className="h-7">
              View
            </UiButton>
          </PopoverTrigger>
          <PopoverContent align="end" aria-label="Web preview options" className="w-72 p-2.5">
            <div className="space-y-3">
              <div>
                <div className="review-kicker mb-1.5">Viewport</div>
                <DevicePicker value={preferences.viewport} onChange={(viewport) => updatePreferences({ viewport })} />
              </div>
              <div className="flex items-center justify-between gap-3 border-t border-border pt-2.5">
                <span className="text-caption text-muted-foreground">Preview theme</span>
                <PreviewThemeToggle
                  value={preferences.previewTheme}
                  chromeTheme={chromeTheme}
                  onChange={(previewTheme) => updatePreferences({ previewTheme })}
                />
              </div>
              <div className="border-t border-border pt-2.5 text-micro leading-4 text-muted-foreground">
                <span className="font-medium text-neutral-400">Shortcuts</span>
                <span className="ml-2 font-mono">1/2/3 modes · D/T/M viewport · {"{"}/{"}"} routes</span>
              </div>
              {affectedPaths.length > 0 && (
                <div className="border-t border-border pt-2.5">
                  <div className="review-kicker mb-1">Affected files · {affectedPaths.length}</div>
                  <div className="max-h-40 overflow-auto">
                    {affectedPaths.map((path) => (
                      <UiButton unstyled
                        key={path}
                        type="button"
                        onClick={() => onOpenPath(path)}
                        className="flex w-full items-center gap-2 px-1 py-1.5 text-left font-mono text-caption text-neutral-400 hover:text-neutral-50"
                      >
                        <FileCode2 size={11} className="shrink-0 text-muted-foreground" />
                        <span className="truncate" title={path}>{path}</span>
                      </UiButton>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </PopoverContent>
        </Popover>
      </div>

      {preferences.mode === "screenshots" ? (
        <FrozenScreenshotCompare revisionId={revisionId} path={documentPath} route={route} viewportWidth={viewportWidth} />
      ) : preferences.mode === "compare" ? (
        <div ref={compareShellRef} data-testid="web-live-compare-shell" className="overflow-hidden bg-muted p-3" style={{ height: `${compareHeight}px` }}>
          <div className="flex h-full w-full gap-3">
            <ScaledCompareFrame scale={compareScale} frameRef={beforeFrameRef} revisionId={revisionId} path={documentPath} side="old" route={route} label="Before" viewportWidth={viewportWidth} theme={effectiveTheme} onStatusChange={onOldStatus} />
            <ScaledCompareFrame scale={compareScale} frameRef={afterFrameRef} revisionId={revisionId} path={documentPath} side="new" route={route} label="After" viewportWidth={viewportWidth} theme={effectiveTheme} onStatusChange={onNewStatus} />
          </div>
        </div>
      ) : (
        <div data-testid="web-live-preview-shell" className="h-[min(78vh,820px)] min-h-[520px] overflow-x-auto overflow-y-hidden bg-muted p-3">
          <div className="mx-auto h-full w-max">
            <LiveFrame revisionId={revisionId} path={documentPath} side="new" route={route} label="Preview" viewportWidth={viewportWidth} theme={effectiveTheme} onStatusChange={onNewStatus} />
          </div>
        </div>
      )}
    </div>
  );
}
