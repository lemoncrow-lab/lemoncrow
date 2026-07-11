import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import {
  Brain,
  GitBranch,
  LayoutGrid,
  Moon,
  Play,
  Settings,
  Sun,
  Wallet,
} from "lucide-react";
import Overview from "./pages/Overview";
import Sessions from "./pages/Sessions";
import Learnings from "./pages/Learnings";
import Costs from "./pages/Costs";
import Swarms from "./pages/Swarms";
import System from "./pages/System";
import { Button, Select, cx } from "./components/WorkbenchUI";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { applyTheme, getInitialTheme, type Theme } from "./lib/theme";
import { useTimeRange, TIME_RANGE_OPTIONS } from "./lib/TimeRangeContext";

interface NavItem {
  to: string;
  label: string;
  icon: React.ElementType;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/overview", label: "Overview", icon: LayoutGrid },
  { to: "/sessions", label: "Sessions", icon: Play },
  { to: "/swarms", label: "Swarms", icon: GitBranch },
  { to: "/costs", label: "Costs", icon: Wallet },
  { to: "/knowledge", label: "Knowledge", icon: Brain },
  { to: "/system", label: "System", icon: Settings },
];

/**
 * Reusable dismissible notification banner. The mechanism is intentionally kept
 * for future in-app notices: mount it with a message to surface one. Nothing
 * renders until it is mounted (the telemetry notice was removed by request).
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
      className="inline-flex items-center border border-neutral-800 bg-neutral-900/40 p-2 text-neutral-400 transition hover:border-neutral-600 hover:text-neutral-200"
    >
      {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
    </button>
  );
}

export default function App() {
  const { range, setRange } = useTimeRange();

  return (
    <div className="min-h-full bg-gradient-to-b from-surface to-surface-tint font-mono text-neutral-200">
      <header className="border-b border-neutral-800 bg-neutral-950/95 px-6 py-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-4">
            <h1 className="text-lg font-bold tracking-wide text-brand">
              ❯ LEMONCROW - The Agents Runtime
            </h1>
          </div>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-2 border border-neutral-800 bg-neutral-900/40 px-3 py-1.5">
              <span className="text-[10px] font-bold uppercase tracking-widest text-neutral-400">
                Window
              </span>
              <Select
                value={range}
                onChange={(e) => setRange(e.target.value as any)}
                uiSize="xs"
                className="border-0 bg-transparent px-0 py-0 text-xs text-neutral-300 hover:text-brand-400"
                aria-label="Global time window"
              >
                {TIME_RANGE_OPTIONS.map((option) => (
                  <option
                    key={option.value}
                    value={option.value}
                    className="bg-neutral-900"
                  >
                    {option.label}
                  </option>
                ))}
              </Select>
            </div>
            <ThemeToggle />
          </div>
        </div>
      </header>

      <nav className="border-neutral-800 bg-neutral-950/70 px-6 py-3">
        <div className="flex flex-wrap gap-2">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                cx(
                  "inline-flex items-center gap-2 border px-3 py-2 text-xs transition",
                  isActive
                    ? "border-brand-500/60 bg-brand-500/10 text-brand-400"
                    : "border-neutral-800 bg-neutral-900/40 text-neutral-400 hover:border-neutral-600 hover:text-neutral-200"
                )
              }
            >
              <item.icon size={14} />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </div>
      </nav>

      <main className="min-h-[calc(100vh-180px)] bg-gradient-to-br from-neutral-950 to-neutral-950/80">
        <div className="">
          <ErrorBoundary label="Page">
            <Routes>
              <Route path="/" element={<Navigate to="/overview" replace />} />
              <Route path="/overview" element={<Overview />} />
              <Route path="/sessions" element={<Sessions />} />
              <Route path="/sessions/:id" element={<Sessions />} />
              <Route
                path="/knowledge"
                element={<Navigate to="/knowledge/blocks" replace />}
              />
              <Route path="/knowledge/:section" element={<Learnings />} />
              <Route
                path="/knowledge/:section/:rubricId"
                element={<Learnings />}
              />
              <Route
                path="/costs"
                element={<Navigate to="/costs/spend" replace />}
              />
              <Route path="/costs/:section" element={<Costs />} />
              <Route path="/swarms" element={<Swarms />} />
              <Route
                path="/system"
                element={<Navigate to="/system/health" replace />}
              />
              <Route path="/system/:section" element={<System />} />
            </Routes>
          </ErrorBoundary>
        </div>
      </main>
    </div>
  );
}
