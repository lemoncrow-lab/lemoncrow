import { Button as UiButton } from "./components/ui/button";
import ReviewPreparation from "./review/ReviewPreparation";
import { retryableLazy } from "./review/retryableLazy";
import { ReviewPreparationProvider } from "./review/reviewPreparationContext";
import { Tip } from "./components/ui/tooltip";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "./components/ui/dropdown-menu";
import { Suspense, createContext, useContext, useEffect, useState, type ComponentType } from "react";
import {
  productAvailable,
  useProductCapabilities,
  useRuntimeCapabilities,
  type ProductFeature,
  type ProductId,
} from "./lib/RuntimeCapabilities";
import { Link, NavLink, Navigate, Route, Routes, useLocation, useSearchParams } from "react-router-dom";
import {
  Activity,
  ClipboardCheck,
  ChevronRight,
  House,
  Loader2,
  Moon,
  Network,
  Settings,
  Sun,
  TrendingUp,
} from "lucide-react";

import { Button, Select, SelectItem, cx } from "./components/WorkbenchUI";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { PortalContainerContext } from "./components/ui/portal-container";
import { isChunkLoadFailure } from "./lib/chunkLoad";
import { applyTheme, getInitialTheme, type Theme } from "./lib/theme";
import { useTimeRange, TIME_RANGE_OPTIONS } from "./lib/TimeRangeContext";

// Every page is its own chunk: the entry bundle carried all of them (~350 KB
// with their markdown stack) and evaluated them before any route rendered.
// retryableLazy keeps a rejected import until reset, so the shell resets these
// whenever its error boundary clears (see resetChunks).
const HomeChunk = retryableLazy(() => import("./pages/Home"));
const RunsChunk = retryableLazy(() => import("./pages/Runs"));
const LearningsChunk = retryableLazy(() => import("./pages/Learnings"));
const UsageChunk = retryableLazy(() => import("./pages/Usage"));
const SystemChunk = retryableLazy(() => import("./pages/System"));
const SettingsChunk = retryableLazy(() => import("./pages/Settings"));
const CodeMapChunk = retryableLazy(() => import("./pages/CodeMap"));
const PAGE_CHUNKS = [HomeChunk, RunsChunk, LearningsChunk, UsageChunk, SystemChunk, SettingsChunk, CodeMapChunk];
const LocalReviewReader = retryableLazy(() => import("./review/ReviewReader"));
const LocalReviewCompareReader = retryableLazy(() => import("./review/ReviewCompareReader"));
const LocalReviewDirectory = retryableLazy(() => import("./review/ReviewDirectory"));

const REVIEW_CHUNK_RECOVERY_KEY = "lemoncrow.review.chunk-recovery";

function ReviewChunkLoaded({ Component }: { Component: ComponentType }) {
  useEffect(() => {
    window.sessionStorage.removeItem(REVIEW_CHUNK_RECOVERY_KEY);
  }, []);
  return <Component />;
}

export interface ReviewComposition {
  Directory: ComponentType;
  Reader: ComponentType;
}

/** Optional replacements supplied by a composition, never imported from it. */
export interface PageComposition {
  Home?: ComponentType;
  Runs?: ComponentType;
  Code?: ComponentType;
  Knowledge?: ComponentType;
  Usage?: ComponentType;
  Settings?: ComponentType;
  System?: ComponentType;
}

export interface ShellRouteComposition {
  path: string;
  Component: ComponentType;
}

export interface ShellComposition {
  HeaderActions?: ComponentType;
  SettingsNavigation?: ComponentType;
  settingsPath?: string;
  routes?: readonly ShellRouteComposition[];
  titles?: readonly { prefix: string; title: string }[];
  /** Shown in place of a Review page when Review is not available, e.g. to name the plan that excludes it. */
  ReviewUnavailable?: ComponentType;
}

const ReviewUnavailableContext = createContext<ComponentType | null>(null);

interface PrimaryNavItem {
  to: string;
  label: string;
  icon: React.ElementType;
  matches: readonly string[];
  product: ProductId;
  feature: ProductFeature;
  annotation?: string;
}

const PRIMARY_NAV: PrimaryNavItem[] = [
  { to: "/home", label: "Home", icon: House, matches: ["/home"], product: "core", feature: "core.home" },
  { to: "/reviews", label: "Reviews", icon: ClipboardCheck, matches: ["/reviews", "/review", "/r", "/rr"], product: "review", feature: "review.directory", annotation: "beta" },
  { to: "/code", label: "Code", icon: Network, matches: ["/code", "/knowledge"], product: "runtime", feature: "runtime.code_map" },
  { to: "/activity", label: "Activity", icon: Activity, matches: ["/activity", "/runs"], product: "runtime", feature: "runtime.runs" },
  { to: "/usage", label: "Usage", icon: TrendingUp, matches: ["/usage"], product: "core", feature: "core.usage" },
];

const SETTINGS_NAV = [
  { to: "/settings/products", label: "Products", match: "/settings/products", product: "core" as ProductId },
  { to: "/settings/ai", label: "AI & background", match: "/settings/ai", product: "review" as ProductId },
  { to: "/settings/devices", label: "Review connections", match: "/settings/devices", product: "review" as ProductId },
  { to: "/settings/integrations", label: "Integrations", match: "/settings/integrations", product: "runtime" as ProductId },
  { to: "/settings/editor", label: "Editor", match: "/settings/editor", product: "runtime" as ProductId },
  { to: "/settings/diagnostics", label: "Diagnostics", match: "/settings/diagnostics", product: "runtime" as ProductId },
  { to: "/settings/telemetry", label: "Telemetry", match: "/settings/telemetry", product: "runtime" as ProductId },
  { to: "/settings/advanced", label: "Advanced", match: "/settings/advanced", product: "runtime" as ProductId },
] as const;

/**
 * Reusable dismissible notification banner. The mechanism is intentionally kept
 * for future in-app notices: mount it with a message to surface one.
 */
export function NotificationBanner({
  children,
  onDismiss,
}: {
  children: React.ReactNode;
  onDismiss?: () => void;
}) {
  const [dismissed, setDismissed] = useState(false);
  if (dismissed) return null;

  return (
    <div className="border-b border-border bg-muted/60 px-4 py-2.5 text-label text-neutral-300">
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div>{children}</div>
        <Button
          variant="default"
          size="sm"
          onClick={() => {
            setDismissed(true);
            onDismiss?.();
          }}
        >
          Got it
        </Button>
      </div>
    </div>
  );
}

function LegacyRunsRedirect() {
  const location = useLocation();
  const suffix = location.pathname.replace(/^\/runs(?=\/|$)/, "/activity");
  return <Navigate to={`${suffix}${location.search}${location.hash}`} replace />;
}

const BRAND_WORD = "LEMONCROW";
const BRAND_TYPE_MS = 36;
const BRAND_CURSOR_SETTLE_MS = 200;

function LemonCrowBrand({ className }: { className?: string }) {
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [typed, setTyped] = useState(0);
  const [cursorVisible, setCursorVisible] = useState(true);
  const active = hovered || focused;

  useEffect(() => {
    if (!active) {
      setTyped(0);
      setCursorVisible(true);
      return;
    }

    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    if (reducedMotion) {
      setTyped(BRAND_WORD.length);
      setCursorVisible(false);
      return;
    }

    setTyped(0);
    setCursorVisible(true);
    let cursorTimer: number | undefined;
    const typeTimer = window.setInterval(() => {
      setTyped((current) => {
        const next = Math.min(current + 1, BRAND_WORD.length);
        if (next === BRAND_WORD.length) {
          window.clearInterval(typeTimer);
          cursorTimer = window.setTimeout(() => setCursorVisible(false), BRAND_CURSOR_SETTLE_MS);
        }
        return next;
      });
    }, BRAND_TYPE_MS);

    return () => {
      window.clearInterval(typeTimer);
      if (cursorTimer !== undefined) window.clearTimeout(cursorTimer);
    };
  }, [active]);

  const visibleWord = BRAND_WORD.slice(0, typed);
  const brandWidth = active ? "14.5ch" : "2.5ch";

  return (
    <NavLink
      to="/home"
      aria-label="LemonCrow home"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      className={cx(
        "z-20 flex h-7 shrink-0 items-center gap-0.5 overflow-visible font-mono text-heading font-semibold tracking-[0.04em] text-foreground transition-[width] duration-100 ease-out",
        className,
      )}
      style={{ width: brandWidth }}
    >
      <ChevronRight aria-hidden="true" size={14} strokeWidth={2} className="relative -top-px shrink-0" />
      <span
        aria-hidden="true"
        data-testid="lemoncrow-brand-mark"
        className="whitespace-nowrap"
      >
        {active ? `${visibleWord}${cursorVisible ? "_" : ""}` : "_"}
      </span>
    </NavLink>
  );
}

function MobilePrimaryNav({ pathname, items }: { pathname: string; items: readonly PrimaryNavItem[] }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <UiButton
          type="button"
          aria-label="Open navigation"
          size="icon"
          variant="ghost"
          className="z-20 h-8 w-9 shrink-0 gap-0.5 rounded-md font-mono text-heading font-semibold tracking-[0.04em] text-foreground md:hidden"
        >
          <ChevronRight aria-hidden="true" size={14} strokeWidth={2} className="relative -top-px shrink-0" />
          <span aria-hidden="true">_</span>
        </UiButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        sideOffset={5}
        aria-label="Mobile navigation"
        className="w-56 p-1 md:hidden"
      >
        {items.map((item) => {
          const active = item.matches.some((prefix) => pathMatches(pathname, prefix));
          return (
            <DropdownMenuItem key={item.to} asChild>
              <Link
                to={item.to}
                aria-current={active ? "page" : undefined}
                className={cx(
                  "flex h-10 items-center gap-2.5 px-2.5 text-caption font-medium",
                  active && "bg-accent text-foreground",
                )}
              >
                <item.icon size={14} />
                <span className="relative">
                  {item.label}
                  {item.annotation && (
                    <span
                      aria-hidden="true"
                      className="ml-1.5 font-serif text-micro font-medium italic tracking-wide text-red-500/80"
                    >
                      {item.annotation}
                    </span>
                  )}
                </span>
              </Link>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => getInitialTheme());

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <UiButton
      type="button"
      onClick={() => setTheme(next)}
      aria-label={`Switch to ${next} theme`}
      size="icon"
      variant="outline"
    >
      {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
    </UiButton>
  );
}

function ReviewFallback({ label }: { label: string }) {
  return (
    <div className="flex min-h-[560px] items-center justify-center gap-2 text-sm text-neutral-400">
      <Loader2 size={15} className="animate-spin" />
      {label}
    </div>
  );
}

function ReviewRouteFailure({ error, retry }: { error: Error; retry: () => void }) {
  useEffect(() => {
    if (!isChunkLoadFailure(error)) return;
    const fingerprint = error.message;
    if (window.sessionStorage.getItem(REVIEW_CHUNK_RECOVERY_KEY) === fingerprint) return;
    window.sessionStorage.setItem(REVIEW_CHUNK_RECOVERY_KEY, fingerprint);
    window.location.reload();
  }, [error]);

  return (
    <div className="flex h-dvh min-h-[560px] items-center justify-center bg-background px-6 text-neutral-300">
      <div className="w-full max-w-md rounded-lg border border-rose-900/60 bg-background/95 p-4 shadow-2xl shadow-black/30" role="alert">
        <div className="text-caption font-semibold uppercase tracking-[0.16em] text-rose-400">LemonCrow Review</div>
        <div className="mt-3 text-title font-semibold text-foreground">Review couldn't load</div>
        <p className="mt-1.5 text-caption leading-5 text-muted-foreground">
          The Review page started but the Reader failed before it could render. Your Review data remains on the server.
        </p>
        <details className="mt-3 text-caption text-muted-foreground">
          <summary className="cursor-pointer hover:text-neutral-300">Error details</summary>
          <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words font-mono text-rose-300/80">{error.message || String(error)}</pre>
        </details>
        <div className="mt-4 flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={retry}>Retry</Button>
          <Button variant="ghost" size="sm" onClick={() => window.location.reload()}>Reload page</Button>
        </div>
      </div>
    </div>
  );
}

function GlobalRequestIndicator() {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    let showTimer: number | undefined;
    const onActivity = (event: Event) => {
      const detail = (event as CustomEvent<{ active?: number }>).detail;
      const active = Number(detail?.active || 0);
      if (showTimer !== undefined) {
        window.clearTimeout(showTimer);
        showTimer = undefined;
      }
      if (active > 0) {
        showTimer = window.setTimeout(() => {
          setVisible(true);
          showTimer = undefined;
        }, 120);
      } else {
        setVisible(false);
      }
    };

    window.addEventListener("lemoncrow:request-activity", onActivity);
    return () => {
      if (showTimer !== undefined) window.clearTimeout(showTimer);
      window.removeEventListener("lemoncrow:request-activity", onActivity);
    };
  }, []);

  if (!visible) return null;
  return (
    <div
      className="pointer-events-none fixed inset-x-0 top-0 z-indicator h-0.5 overflow-hidden"
      role="progressbar"
      aria-label="Loading"
    >
      <div className="h-full w-full animate-pulse bg-brand-400" />
    </div>
  );
}

function pathMatches(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}

function SecondaryNav({ pathname }: { pathname: string }) {
  const capabilities = useProductCapabilities();
  const items = pathMatches(pathname, "/settings") || pathMatches(pathname, "/system")
    ? SETTINGS_NAV.filter((item) => productAvailable(capabilities, item.product))
    : null;

  if (!items) return null;
  return (
    <div className="border-b border-neutral-900 bg-background/70 px-5">
      <nav className="flex h-9 items-center gap-4 overflow-x-auto">
        {items.map((item) => {
          const active = pathMatches(pathname, item.match) || (item.match === "/settings/advanced" && pathMatches(pathname, "/system"));
          return (
            <Link
              key={item.to}
              to={item.to}
              aria-current={active ? "page" : undefined}
              className={cx(
                "inline-flex h-9 shrink-0 items-center border-b text-caption font-semibold uppercase tracking-[0.08em] transition",
                active
                  ? "border-brand-500 text-neutral-200"
                  : "border-transparent text-muted-foreground hover:text-neutral-300",
              )}
            >
              {item.label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}

function ProductUnavailable({ product }: { product: Exclude<ProductId, "core"> }) {
  const capabilities = useProductCapabilities();
  const state = capabilities.products[product];
  const label = product === "review" ? "LemonCrow Review" : "LemonCrow Runtime";
  const status = state.installed ? "not available" : "not installed";
  if (capabilities.products.core.mode === "hosted") {
    return (
      <div className="flex min-h-[420px] items-center justify-center px-6 py-12 text-neutral-200" role="status">
        <div className="w-full max-w-lg rounded-lg border border-border bg-muted/20 p-4">
          <div className="text-micro font-semibold uppercase tracking-[0.16em] text-muted-foreground">{label}</div>
          <h1 className="mt-2 text-title font-semibold text-foreground">{label} isn't enabled for your organization</h1>
          <p className="mt-2 text-body leading-6 text-muted-foreground">
            Your organization doesn't have {label}. Ask your LemonCrow workspace administrator for access.
          </p>
          <Link className="mt-4 inline-flex text-caption font-medium text-neutral-300 hover:text-foreground" to="/account">
            View your products
          </Link>
        </div>
      </div>
    );
  }
  return (
    <div className="flex min-h-[420px] items-center justify-center px-6 py-12 text-neutral-200" role="status">
      <div className="w-full max-w-lg rounded-lg border border-border bg-muted/20 p-4">
        <div className="text-micro font-semibold uppercase tracking-[0.16em] text-muted-foreground">{label}</div>
        <h1 className="mt-2 text-title font-semibold text-foreground">{label} is {status}</h1>
        <p className="mt-2 text-body leading-6 text-muted-foreground">
          This page belongs to {label}. Other installed LemonCrow products are unaffected.
        </p>
        <Link className="mt-4 inline-flex text-caption font-medium text-neutral-300 hover:text-foreground" to="/settings/products">
          View installed products
        </Link>
      </div>
    </div>
  );
}

function HostedOnlyPage() {
  return (
    <div className="flex min-h-[420px] items-center justify-center px-6 py-12 text-neutral-200" role="status">
      <div className="w-full max-w-lg rounded-lg border border-border bg-muted/20 p-4">
        <div className="text-micro font-semibold uppercase tracking-[0.16em] text-muted-foreground">LemonCrow Dashboard</div>
        <h1 className="mt-2 text-title font-semibold text-foreground">This page is available on LemonCrow hosted</h1>
        <p className="mt-2 text-body leading-6 text-muted-foreground">
          The local Dashboard shows sessions, usage, savings, settings and system.
        </p>
        <Link className="mt-4 inline-flex text-caption font-medium text-neutral-300 hover:text-foreground" to="/home">
          Back to Home
        </Link>
      </div>
    </div>
  );
}

function ProductGate({
  product,
  feature,
  children,
}: {
  product: Exclude<ProductId, "core">;
  feature?: ProductFeature;
  children: React.ReactNode;
}) {
  const capabilities = useProductCapabilities();
  const ReviewUnavailable = useContext(ReviewUnavailableContext);
  if (!productAvailable(capabilities, product)) {
    return product === "review" && ReviewUnavailable ? <ReviewUnavailable /> : <ProductUnavailable product={product} />;
  }
  return feature && !capabilities.features[feature] ? <HostedOnlyPage /> : children;
}

/**
 * The full-index Knowledge sections need endpoints a local Dashboard and a
 * hosted deployment do not serve. A hosted reader is sent to Code (its nearest
 * served surface) rather than a page of tabs that each fail with a 404.
 */
function KnowledgeSectionGate({ children }: { children: React.ReactNode }) {
  const capabilities = useProductCapabilities();
  if (
    capabilities.products.core.mode === "hosted"
    && productAvailable(capabilities, "runtime")
    && !capabilities.features["runtime.knowledge"]
  ) {
    return <Navigate to="/code" replace />;
  }
  return <ProductGate product="runtime" feature="runtime.knowledge">{children}</ProductGate>;
}

/** A local Dashboard serves Project Knowledge but not the full-index Knowledge sections. */
function KnowledgeIndex() {
  const capabilities = useProductCapabilities();
  if (!capabilities.features["runtime.knowledge"] && capabilities.features["runtime.project_knowledge"]) {
    return (
      <ProductGate product="runtime" feature="runtime.project_knowledge">
        <Navigate to="/knowledge/project" replace />
      </ProductGate>
    );
  }
  return (
    <KnowledgeSectionGate>
      <Navigate to="/knowledge/blocks" replace />
    </KnowledgeSectionGate>
  );
}

export default function App({
  reviewComposition,
  shellComposition,
  pageComposition,
}: {
  reviewComposition?: ReviewComposition;
  shellComposition?: ShellComposition;
  pageComposition?: PageComposition;
}) {
  const PageHome = pageComposition?.Home ?? HomeChunk.current();
  const PageRuns = pageComposition?.Runs ?? RunsChunk.current();
  const PageCode = pageComposition?.Code ?? CodeMapChunk.current();
  const PageKnowledge = pageComposition?.Knowledge ?? LearningsChunk.current();
  const PageUsage = pageComposition?.Usage ?? UsageChunk.current();
  const PageSettings = pageComposition?.Settings ?? SettingsChunk.current();
  const PageSystem = pageComposition?.System ?? SystemChunk.current();
  const ShellSettingsNavigation = shellComposition?.SettingsNavigation;
  const settingsPath = shellComposition?.settingsPath ?? "/settings/products";
  const [, setReviewChunkGeneration] = useState(0);
  const ReviewDirectory = reviewComposition?.Directory ?? LocalReviewDirectory.current();
  const ReviewReader = reviewComposition?.Reader ?? LocalReviewReader.current();
  const ReviewCompareReader = LocalReviewCompareReader.current();
  const ShellHeaderActions = shellComposition?.HeaderActions;
  const resetChunks = () => {
    for (const chunk of PAGE_CHUNKS) chunk.reset();
    if (!reviewComposition) {
      LocalReviewDirectory.reset();
      LocalReviewReader.reset();
      LocalReviewCompareReader.reset();
    }
    setReviewChunkGeneration((generation) => generation + 1);
  };
  const { range, setRange } = useTimeRange();
  const capabilities = useProductCapabilities();
  const runtimeCapabilities = useRuntimeCapabilities();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const primaryNav = PRIMARY_NAV.filter(
    (item) => productAvailable(capabilities, item.product) && capabilities.features[item.feature],
  );

  const fullScreenReview =
    location.pathname === "/review" ||
    location.pathname.startsWith("/r/") ||
    location.pathname.startsWith("/rr/") ||
    location.pathname.startsWith("/reviews/");
  const settingsCompositionActive = Boolean(
    ShellSettingsNavigation
      && (pathMatches(location.pathname, "/settings") || pathMatches(location.pathname, "/system")),
  );
  useEffect(() => {
    const titles: Array<[string, string]> = [
      ...(shellComposition?.titles?.map(({ prefix, title }) => [prefix, title] as [string, string]) ?? []),
      ["/home", "Home"],
      ["/reviews", "Reviews"],
      ["/activity", "Activity"],
      ["/runs", "Activity"],
      ["/code", "Code"],
      ["/knowledge", "Code"],
      ["/usage", "Usage"],
      ["/settings", "Settings"],
      ["/system", "Settings"],
    ];
    const hit = titles.find(([prefix]) => pathMatches(location.pathname, prefix));
    document.title = fullScreenReview ? "Review" : hit ? `${hit[1]} · LemonCrow` : "LemonCrow";
  }, [fullScreenReview, location.pathname, shellComposition?.titles]);

  const showWindow =
    pathMatches(location.pathname, "/usage") ||
    pathMatches(location.pathname, "/settings/telemetry");
  const showRunType = runtimeCapabilities.swarms && pathMatches(location.pathname, "/activity");
  const runType = searchParams.get("type") === "swarms" ? "swarms" : "sessions";
  const setRunType = (next: string) => {
    const params = new URLSearchParams(searchParams);
    if (next === "swarms") params.set("type", "swarms");
    else params.delete("type");
    setSearchParams(params, { replace: true });
  };
  const [shellRoot, setShellRoot] = useState<HTMLDivElement | null>(null);

  return (
    <PortalContainerContext.Provider value={shellRoot}>
      <div
        ref={setShellRoot}
        className={
          fullScreenReview
            ? "review-app min-h-full text-neutral-200"
            : "workbench-shell min-h-full bg-background text-neutral-200"
        }
      >
        <GlobalRequestIndicator />
        {!fullScreenReview && (
          <>
            <header className="border-b border-border bg-background/95">
              <div className="flex h-10 items-center gap-2 px-3 sm:gap-4 sm:px-4">
                <MobilePrimaryNav pathname={location.pathname} items={primaryNav} />
                <LemonCrowBrand className="hidden md:flex" />
                <nav aria-label="Primary navigation" className="hidden min-w-0 flex-1 items-center gap-0.5 overflow-x-auto md:flex">
                  {primaryNav.map((item) => {
                    const active = item.matches.some((prefix) =>
                      pathMatches(location.pathname, prefix),
                    );
                    return (
                      <Link
                        key={item.to}
                        to={item.to}
                        aria-current={active ? "page" : undefined}
                        className={cx(
                          "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-2.5 text-caption font-medium transition",
                          active
                            ? "bg-neutral-800/80 text-foreground"
                            : "text-muted-foreground hover:bg-muted hover:text-neutral-200",
                        )}
                      >
                        <item.icon size={12} />
                        <span className="relative">
                          {item.label}
                          {item.annotation && (
                            <span
                              aria-hidden="true"
                              className="absolute -right-3 -top-2 whitespace-nowrap font-serif text-micro font-medium italic tracking-wide text-red-500/80"
                            >
                              {item.annotation}
                            </span>
                          )}
                        </span>
                      </Link>
                    );
                  })}
                </nav>
                <div className="min-w-0 flex-1 md:hidden" aria-hidden="true" />
                {showRunType && (
                  <div className="flex shrink-0 items-center gap-2">
                    <span className="text-micro font-semibold uppercase tracking-widest text-muted-foreground">View</span>
                    <Select value={runType} onValueChange={setRunType} uiSize="xs" aria-label="Activity view">
                      <SelectItem value="sessions">Sessions</SelectItem>
                      <SelectItem value="swarms">Swarms</SelectItem>
                    </Select>
                  </div>
                )}
                {showWindow && (
                  <div className="hidden h-8 items-center gap-2 rounded-md border border-border bg-muted/40 px-2 lg:flex">
                    <span className="text-micro font-bold uppercase tracking-widest text-muted-foreground">
                      Window
                    </span>
                    <Select
                      value={range}
                      onValueChange={(value) => setRange(value as typeof range)}
                      uiSize="xs"
                      className="border-0 bg-transparent px-0 py-0 text-caption text-neutral-300"
                      aria-label="Global time window"
                    >
                      {TIME_RANGE_OPTIONS.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </Select>
                  </div>
                )}
                <Tip content="Settings">
                  <NavLink
                    to={settingsPath}
                    aria-label="Settings"
                    className={cx(
                      "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border transition",
                      pathMatches(location.pathname, "/settings") ||
                        pathMatches(location.pathname, "/system")
                        ? "border-border bg-neutral-800/80 text-foreground"
                        : "border-border bg-muted/40 text-muted-foreground hover:text-neutral-200",
                    )}
                  >
                    <Settings size={14} />
                  </NavLink>
                </Tip>
                {ShellHeaderActions && <ShellHeaderActions />}
                <ThemeToggle />
              </div>
            </header>
            {!settingsCompositionActive && <SecondaryNav pathname={location.pathname} />}
          </>
        )}

        <main className={fullScreenReview ? "min-h-screen bg-background" : "min-h-[calc(100vh-41px)] bg-background"}>
          <div className={settingsCompositionActive ? "mx-auto flex w-full max-w-[1440px] items-start" : "contents"}>
            {settingsCompositionActive && ShellSettingsNavigation && (
              <aside className="sticky top-[41px] hidden h-[calc(100vh-41px)] w-60 shrink-0 overflow-y-auto border-r border-border px-3 py-5 md:block">
                <ShellSettingsNavigation />
              </aside>
            )}
            <div className={settingsCompositionActive ? "min-w-0 flex-1" : "contents"}>
              {settingsCompositionActive && ShellSettingsNavigation && (
                <div className="border-b border-border px-4 py-3 md:hidden">
                  <ShellSettingsNavigation />
                </div>
              )}
          <ReviewPreparationProvider>
            {capabilities.products.review.available && <ReviewPreparation />}
            <ErrorBoundary
              label={fullScreenReview ? "Review" : "Page"}
              resetKey={location.pathname}
              onReset={resetChunks}
              fallback={fullScreenReview ? ((error, retry) => (
                <ReviewRouteFailure error={error} retry={retry} />
              )) : undefined}
            >
              <Suspense fallback={null}>
                <ReviewUnavailableContext.Provider value={shellComposition?.ReviewUnavailable ?? null}>
                <Routes>
                  <Route path="/" element={<Navigate to="/home" replace />} />
                  {/* ReviewPreparation above draws this route; without Review it gets the plan page. */}
                  <Route path="/reviews/preparing/:preparationId" element={<ProductGate product="review">{null}</ProductGate>} />
                  <Route path="/home" element={<PageHome />} />
                  <Route path="/activity" element={<ProductGate product="runtime"><PageRuns /></ProductGate>} />
                  <Route path="/activity/:id" element={<ProductGate product="runtime"><PageRuns /></ProductGate>} />
                  <Route path="/runs/*" element={<LegacyRunsRedirect />} />
                  <Route
                    path="/code"
                    element={
                      <ProductGate product="runtime" feature="runtime.code_map">
                        <Suspense fallback={<ReviewFallback label="Loading code index…" />}>
                          <PageCode />
                        </Suspense>
                      </ProductGate>
                    }
                  />
                  <Route path="/knowledge" element={<KnowledgeIndex />} />
                  <Route path="/knowledge/project" element={<ProductGate product="runtime" feature="runtime.project_knowledge"><PageKnowledge /></ProductGate>} />
                  <Route path="/knowledge/:section" element={<KnowledgeSectionGate><PageKnowledge /></KnowledgeSectionGate>} />
                  <Route path="/knowledge/:section/:rubricId" element={<KnowledgeSectionGate><PageKnowledge /></KnowledgeSectionGate>} />
                  <Route path="/usage" element={<PageUsage />} />
                  <Route
                    path="/reviews"
                    element={
                      <ProductGate product="review">
                        <Suspense fallback={<ReviewFallback label="Loading reviews…" />}>
                          <ReviewChunkLoaded Component={ReviewDirectory} />
                        </Suspense>
                      </ProductGate>
                    }
                  />
                  <Route path="/settings" element={<Navigate to={settingsPath} replace />} />
                  <Route path="/settings/:section" element={<PageSettings />} />
                  <Route path="/system" element={<ProductGate product="runtime"><Navigate to={ShellSettingsNavigation ? settingsPath : "/settings/advanced"} replace /></ProductGate>} />
                  <Route path="/system/:section" element={<ProductGate product="runtime"><PageSystem /></ProductGate>} />
                  <Route
                    path="/r/:reviewRef/compare/*"
                    element={
                      <ProductGate product="review">
                        <Suspense fallback={<ReviewFallback label="Loading comparison…" />}>
                          <ReviewChunkLoaded Component={ReviewCompareReader} />
                        </Suspense>
                      </ProductGate>
                    }
                  />
                  <Route
                    path="/r/x/*"
                    element={
                      <ProductGate product="review">
                        <Suspense fallback={<ReviewFallback label="Loading comparison…" />}>
                          <ReviewChunkLoaded Component={ReviewCompareReader} />
                        </Suspense>
                      </ProductGate>
                    }
                  />
                  <Route
                    path="/r/:reviewRef"
                    element={
                      <ProductGate product="review">
                        <Suspense fallback={null}>
                          <ReviewChunkLoaded Component={ReviewReader} />
                        </Suspense>
                      </ProductGate>
                    }
                  />
                  <Route
                    path="/rr/:revisionRef"
                    element={
                      <ProductGate product="review">
                        <Suspense fallback={null}>
                          <ReviewChunkLoaded Component={ReviewReader} />
                        </Suspense>
                      </ProductGate>
                    }
                  />
                  <Route
                    path="/reviews/:reviewId/revisions/:revisionNumber"
                    element={
                      <ProductGate product="review">
                        <Suspense fallback={null}>
                          <ReviewChunkLoaded Component={ReviewReader} />
                        </Suspense>
                      </ProductGate>
                    }
                  />
                  <Route
                    path="/reviews/:reviewId"
                    element={
                      <ProductGate product="review">
                        <Suspense fallback={null}>
                          <ReviewChunkLoaded Component={ReviewReader} />
                        </Suspense>
                      </ProductGate>
                    }
                  />
                  <Route
                    path="/review"
                    element={
                      <ProductGate product="review">
                        <Suspense fallback={null}>
                          <ReviewChunkLoaded Component={ReviewReader} />
                        </Suspense>
                      </ProductGate>
                    }
                  />
                  {shellComposition?.routes?.map(({ path, Component }) => (
                    <Route key={path} path={path} element={<Component />} />
                  ))}
                  <Route path="*" element={<Navigate to="/home" replace />} />
                </Routes>
                </ReviewUnavailableContext.Provider>
              </Suspense>
            </ErrorBoundary>
          </ReviewPreparationProvider>
            </div>
          </div>
        </main>
      </div>
    </PortalContainerContext.Provider>
  );
}
