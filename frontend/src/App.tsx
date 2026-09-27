import { lazy, Suspense, useEffect, useState, type ComponentType } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import {
  Activity,
  ClipboardCheck,
  House,
  Loader2,
  Moon,
  Network,
  Settings,
  Sun,
  TrendingUp,
} from "lucide-react";

import Home from "./pages/Home";
import Runs from "./pages/Runs";
import Learnings from "./pages/Learnings";
import Usage from "./pages/Usage";
import System from "./pages/System";
import SettingsPage from "./pages/Settings";
import { Button, Select, cx } from "./components/WorkbenchUI";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { applyTheme, getInitialTheme, type Theme } from "./lib/theme";
import { useTimeRange, TIME_RANGE_OPTIONS } from "./lib/TimeRangeContext";

const CodeMap = lazy(() => import("./pages/CodeMap"));
const LocalReviewReader = lazy(() => import("./review/ReviewReader"));
const LocalReviewCompareReader = lazy(() => import("./review/ReviewCompareReader"));
const LocalReviewDirectory = lazy(() => import("./review/ReviewDirectory"));

export interface ReviewComposition {
  Directory: ComponentType;
  Reader: ComponentType;
}

const LOCAL_REVIEW_COMPOSITION: ReviewComposition = {
  Directory: LocalReviewDirectory,
  Reader: LocalReviewReader,
};

interface PrimaryNavItem {
  to: string;
  label: string;
  icon: React.ElementType;
  matches: readonly string[];
}

const PRIMARY_NAV: PrimaryNavItem[] = [
  { to: "/home", label: "Home", icon: House, matches: ["/home"] },
  { to: "/reviews", label: "Reviews", icon: ClipboardCheck, matches: ["/reviews", "/review", "/r", "/rr"] },
  { to: "/runs", label: "Runs", icon: Activity, matches: ["/runs"] },
  { to: "/code", label: "Code", icon: Network, matches: ["/code", "/knowledge"] },
  { to: "/usage", label: "Usage", icon: TrendingUp, matches: ["/usage"] },
];

const SECONDARY_NAV = {
  settings: [
    { to: "/settings/integrations", label: "Integrations", match: "/settings/integrations" },
    { to: "/settings/diagnostics", label: "Diagnostics", match: "/settings/diagnostics" },
    { to: "/settings/telemetry", label: "Telemetry", match: "/settings/telemetry" },
    { to: "/settings/advanced", label: "Advanced", match: "/settings/advanced" },
  ],
} as const;

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
    <div className="border-b border-brand-500/30 bg-brand-500/10 px-6 py-3 text-sm text-neutral-200">
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div>{children}</div>
        <Button
          variant="accent"
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

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => getInitialTheme());

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      aria-label={`Switch to ${next} theme`}
      className="inline-flex h-7 w-7 items-center justify-center rounded border border-neutral-800 bg-neutral-900/40 text-neutral-400 transition hover:border-neutral-600 hover:text-neutral-200"
    >
      {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
    </button>
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
  return (
    <div className="flex h-dvh min-h-[560px] items-center justify-center bg-neutral-950 px-6 text-neutral-300">
      <div className="w-full max-w-md rounded-lg border border-rose-900/60 bg-neutral-900/50 p-5 shadow-2xl shadow-black/30" role="alert">
        <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-rose-400">LemonCrow Review</div>
        <div className="mt-3 text-[14px] font-semibold text-neutral-100">Review couldn't load</div>
        <p className="mt-1.5 text-[10px] leading-5 text-neutral-500">
          The Review page started but the Reader failed before it could render. Your Review data remains on the server.
        </p>
        <details className="mt-3 text-[10px] text-neutral-600">
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
    <>
      <div
        className="pointer-events-none fixed inset-x-0 top-0 z-[200] h-0.5 overflow-hidden"
        role="progressbar"
        aria-label="Loading"
      >
        <div className="h-full w-full animate-pulse bg-brand-400" />
      </div>
      <div className="pointer-events-none fixed bottom-4 right-4 z-[200] inline-flex items-center gap-2 rounded-md border border-neutral-700/80 bg-neutral-950/95 px-2.5 py-1.5 text-[10px] font-medium text-neutral-300 shadow-xl shadow-black/30">
        <Loader2 size={11} className="animate-spin text-brand-300" />
        Loading…
      </div>
    </>
  );
}

function pathMatches(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}

function SecondaryNav({ pathname }: { pathname: string }) {
  const items = pathMatches(pathname, "/settings")
    ? SECONDARY_NAV.settings
    : pathMatches(pathname, "/system")
      ? SECONDARY_NAV.settings
      : null;

  if (!items) return null;
  return (
    <div className="border-b border-neutral-900 bg-neutral-950/70 px-5">
      <nav className="flex h-9 items-center gap-4 overflow-x-auto">
        {items.map((item) => {
          const active = pathMatches(pathname, item.match);
          return (
            <Link
              key={item.to}
              to={item.to}
              aria-current={active ? "page" : undefined}
              className={cx(
                "inline-flex h-9 shrink-0 items-center border-b text-[10px] font-semibold uppercase tracking-[0.08em] transition",
                active
                  ? "border-brand-500 text-neutral-200"
                  : "border-transparent text-neutral-600 hover:text-neutral-300",
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

export default function App({
  reviewComposition = LOCAL_REVIEW_COMPOSITION,
}: {
  reviewComposition?: ReviewComposition;
}) {
  const ReviewDirectory = reviewComposition.Directory;
  const ReviewReader = reviewComposition.Reader;
  const { range, setRange } = useTimeRange();
  const location = useLocation();

  const fullScreenReview =
    location.pathname === "/review" ||
    location.pathname.startsWith("/r/") ||
    location.pathname.startsWith("/rr/") ||
    location.pathname.startsWith("/reviews/");
  useEffect(() => {
    const titles: Array<[string, string]> = [
      ["/home", "Home"],
      ["/reviews", "Reviews"],
      ["/runs", "Runs"],
      ["/code", "Code"],
      ["/knowledge", "Code"],
      ["/usage", "Usage"],
      ["/settings", "Settings"],
      ["/system", "Settings"],
    ];
    const hit = titles.find(([prefix]) => pathMatches(location.pathname, prefix));
    document.title = fullScreenReview ? "Review" : hit ? `${hit[1]} · LemonCrow` : "LemonCrow";
  }, [fullScreenReview, location.pathname]);

  const showWindow =
    pathMatches(location.pathname, "/usage") ||
    pathMatches(location.pathname, "/settings/telemetry");

  return (
    <div
      className={
        fullScreenReview
          ? "review-app min-h-full text-neutral-200"
          : "workbench-shell min-h-full bg-neutral-950 text-neutral-200"
      }
    >
      <GlobalRequestIndicator />
      {!fullScreenReview && (
        <>
          <header className="border-b border-neutral-800 bg-neutral-950/95">
            <div className="flex h-10 items-center gap-4 px-4">
              <NavLink
                to="/home"
                className="shrink-0 text-[13px] font-semibold tracking-[0.04em] text-neutral-100"
              >
                ❯ LEMONCROW
              </NavLink>
              <nav className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto">
                {PRIMARY_NAV.map((item) => {
                  const active = item.matches.some((prefix) =>
                    pathMatches(location.pathname, prefix),
                  );
                  return (
                    <Link
                      key={item.to}
                      to={item.to}
                      aria-current={active ? "page" : undefined}
                      className={cx(
                        "inline-flex h-7 shrink-0 items-center gap-1.5 rounded px-2.5 text-[10px] font-medium transition",
                        active
                          ? "bg-neutral-800/80 text-neutral-100"
                          : "text-neutral-500 hover:bg-neutral-900 hover:text-neutral-200",
                      )}
                    >
                      <item.icon size={12} />
                      <span>{item.label}</span>
                    </Link>
                  );
                })}
              </nav>
              {showWindow && (
                <div className="hidden h-8 items-center gap-2 rounded border border-neutral-800 bg-neutral-900/40 px-2 lg:flex">
                  <span className="text-[9px] font-bold uppercase tracking-widest text-neutral-600">
                    Window
                  </span>
                  <Select
                    value={range}
                    onChange={(event) => setRange(event.target.value as typeof range)}
                    uiSize="xs"
                    className="border-0 bg-transparent px-0 py-0 text-[10px] text-neutral-300"
                    aria-label="Global time window"
                  >
                    {TIME_RANGE_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value} className="bg-neutral-900">
                        {option.label}
                      </option>
                    ))}
                  </Select>
                </div>
              )}
              <NavLink
                to="/settings/integrations"
                aria-label="Settings"
                title="Settings"
                className={cx(
                  "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded border transition",
                  pathMatches(location.pathname, "/settings") ||
                  pathMatches(location.pathname, "/system")
                    ? "border-neutral-700 bg-neutral-800/80 text-neutral-100"
                    : "border-neutral-800 bg-neutral-900/40 text-neutral-500 hover:border-neutral-600 hover:text-neutral-200",
                )}
              >
                <Settings size={14} />
              </NavLink>
              <ThemeToggle />
            </div>
          </header>
          <SecondaryNav pathname={location.pathname} />
        </>
      )}

      <main className={fullScreenReview ? "min-h-screen bg-neutral-950" : "min-h-[calc(100vh-41px)] bg-neutral-950"}>
        <ErrorBoundary
          label={fullScreenReview ? "Review" : "Page"}
          fallback={fullScreenReview ? ((error, retry) => <ReviewRouteFailure error={error} retry={retry} />) : undefined}
        >
          <Routes>
            <Route path="/" element={<Navigate to="/home" replace />} />
            <Route path="/home" element={<Home />} />
            <Route path="/runs" element={<Runs />} />
            <Route path="/runs/:id" element={<Runs />} />
            <Route
              path="/code"
              element={
                <Suspense fallback={<ReviewFallback label="Loading code index…" />}>
                  <CodeMap />
                </Suspense>
              }
            />
            <Route path="/knowledge" element={<Navigate to="/knowledge/blocks" replace />} />
            <Route path="/knowledge/:section" element={<Learnings />} />
            <Route path="/knowledge/:section/:rubricId" element={<Learnings />} />
            <Route path="/usage" element={<Usage />} />
            <Route
              path="/reviews"
              element={
                <Suspense fallback={<ReviewFallback label="Loading reviews…" />}>
                  <ReviewDirectory />
                </Suspense>
              }
            />
            <Route path="/settings" element={<Navigate to="/settings/integrations" replace />} />
            <Route path="/settings/:section" element={<SettingsPage />} />
            <Route path="/system" element={<Navigate to="/settings/advanced" replace />} />
            <Route path="/system/:section" element={<System />} />
            <Route
              path="/r/:reviewRef/compare/*"
              element={
                <Suspense fallback={<ReviewFallback label="Loading comparison…" />}>
                  <LocalReviewCompareReader />
                </Suspense>
              }
            />
            <Route
              path="/r/x/*"
              element={
                <Suspense fallback={<ReviewFallback label="Loading comparison…" />}>
                  <LocalReviewCompareReader />
                </Suspense>
              }
            />
            <Route
              path="/r/:reviewRef"
              element={
                <Suspense fallback={<ReviewFallback label="Loading review workspace…" />}>
                  <ReviewReader />
                </Suspense>
              }
            />
            <Route
              path="/rr/:revisionRef"
              element={
                <Suspense fallback={<ReviewFallback label="Loading historical review…" />}>
                  <ReviewReader />
                </Suspense>
              }
            />
            <Route
              path="/reviews/:reviewId/revisions/:revisionNumber"
              element={
                <Suspense fallback={<ReviewFallback label="Loading historical review…" />}>
                  <ReviewReader />
                </Suspense>
              }
            />
            <Route
              path="/reviews/:reviewId"
              element={
                <Suspense fallback={<ReviewFallback label="Loading review workspace…" />}>
                  <ReviewReader />
                </Suspense>
              }
            />
            <Route
              path="/review"
              element={
                <Suspense fallback={<ReviewFallback label="Loading review workspace…" />}>
                  <ReviewReader />
                </Suspense>
              }
            />
            <Route path="*" element={<Navigate to="/home" replace />} />
          </Routes>
        </ErrorBoundary>
      </main>
    </div>
  );
}
