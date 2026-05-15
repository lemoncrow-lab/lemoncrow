import { useEffect, useState } from "react";
import {
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import Overview from "./pages/Overview";
import Traces from "./pages/Traces";
import Sessions from "./pages/Sessions";
import SessionDetail from "./pages/SessionDetail";
import Learnings from "./pages/Learnings";
import Savings from "./pages/Savings";
import Host from "./pages/Host";
import Agents from "./pages/Agents";
import Tools from "./pages/Tools";
import Insights from "./pages/Insights";
import Telemetry from "./pages/Telemetry";
import Memory from "./pages/Memory";
import Outcomes from "./pages/Outcomes";
import Reports from "./pages/Reports";
import Watchdogs from "./pages/Watchdogs";
import Analytics from "./pages/Analytics";
import External from "./pages/External";
import Optimizations from "./pages/Optimizations";
import {
  acknowledgeTelemetry,
  getTelemetryConfig,
  type TelemetryConfig,
} from "./lib/insightsApi";
import { Chip, cx } from "./components/WorkbenchUI";
import { useTimeRange, TIME_RANGE_OPTIONS } from "./lib/TimeRangeContext";

interface NavItem {
  to: string;
  label: string;
  icon: string;
  isDev?: boolean;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/overview", label: "Overview", icon: "◫" },
  { to: "/sessions", label: "Sessions", icon: "▶" },
  { to: "/traces", label: "Traces", icon: "⌇" },
  { to: "/memory", label: "Memory", icon: "⬡" },
  { to: "/insights", label: "Insights", icon: "✦" },
  { to: "/outcomes", label: "Outcomes", icon: "◎" },
  { to: "/reports", label: "Reports", icon: "📄" },
  { to: "/savings", label: "Savings", icon: "₿", isDev: true },
  { to: "/watchdogs", label: "Watchdogs", icon: "⚑", isDev: true },
  { to: "/knowledge/blocks", label: "Knowledge", icon: "🧠", isDev: true },
  { to: "/host", label: "Hosts", icon: "⌘" },
  { to: "/tools", label: "Tools", icon: "⎇" },
  { to: "/agents", label: "Agents", icon: "☷" },
  { to: "/telemetry", label: "Telemetry", icon: "◎" },
  { to: "/analytics", label: "Analytics", icon: "📊" },
  { to: "/external", label: "External", icon: "◬" },
  { to: "/optimizations", label: "Optimizations", icon: "⇲" },
];

function TelemetryDisclosure() {
  const [config, setConfig] = useState<TelemetryConfig | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    getTelemetryConfig()
      .then(setConfig)
      .catch(() => undefined);
  }, []);

  if (!config || config.acknowledged || dismissed) return null;

  return (
    <div className="border-b border-amber-900/60 bg-amber-950/30 px-6 py-3 text-sm text-amber-100">
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div>
          Atelier collects anonymous usage telemetry to improve the product.
          Disable any time with{" "}
          <code className="bg-black/30 px-1">atelier telemetry off</code> or
          <code className="ml-1 bg-black/30 px-1">ATELIER_TELEMETRY=0</code>.
        </div>
        <button
          type="button"
          className="border border-amber-500/60 px-3 py-1 font-mono text-xs uppercase tracking-widest text-amber-100 hover:bg-amber-500/10"
          onClick={() => {
            setDismissed(true);
            acknowledgeTelemetry().catch(() => undefined);
          }}
        >
          Got it
        </button>
      </div>
    </div>
  );
}

export default function App() {
  const location = useLocation();
  const [config, setConfig] = useState<TelemetryConfig | null>(null);
  const { range, setRange } = useTimeRange();

  useEffect(() => {
    getTelemetryConfig()
      .then(setConfig)
      .catch(() => undefined);
  }, []);

  const pageTitle =
    NAV_ITEMS.find((item) => location.pathname.startsWith(item.to))?.label ??
    "Overview";

  return (
    <div className="min-h-full bg-gradient-to-b from-[#0a0a0a] to-[#0f0f0f] font-mono text-neutral-200">
      <header className="border-b border-neutral-800 bg-neutral-950/95 px-6 py-4">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-4">
            <h1 className="text-lg font-bold tracking-wide text-purple-500">
              ❯ ATELIER - The Agents Runtime
            </h1>
            {config?.dev_mode && (
              <span className="border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] font-bold text-amber-500">
                DEV MODE
              </span>
            )}
          </div>
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2 border border-neutral-800 bg-neutral-900/40 px-3 py-1.5">
              <span className="text-[10px] font-bold uppercase tracking-widest text-neutral-500">
                Window
              </span>
              <select
                value={range}
                onChange={(e) => setRange(e.target.value as any)}
                className="bg-transparent text-xs font-mono text-neutral-300 outline-none hover:text-purple-400"
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
              </select>
            </div>
            <Chip tone="amber">{pageTitle}</Chip>
          </div>
        </div>
      </header>

      <TelemetryDisclosure />

      <nav className="border-b border-neutral-800 bg-neutral-950/70 px-6 py-4">
        <div className="flex flex-wrap gap-2">
          {NAV_ITEMS.filter((item) => !item.isDev || config?.dev_mode).map(
            (item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  cx(
                    "inline-flex items-center gap-2 border px-3 py-2 text-xs transition",
                    isActive
                      ? "border-purple-500/60 bg-purple-500/10 text-purple-400"
                      : "border-neutral-800 bg-neutral-900/40 text-neutral-400 hover:border-neutral-600 hover:text-neutral-200"
                  )
                }
              >
                <span>{item.icon}</span>
                <span>{item.label}</span>
                {item.isDev && (
                  <span className="ml-1 text-[8px] font-bold text-amber-500/60">
                    DEV
                  </span>
                )}
              </NavLink>
            )
          )}
        </div>
      </nav>

      <main className="min-h-[calc(100vh-180px)] bg-gradient-to-br from-neutral-950 to-neutral-950/80">
        <div className="px-6 py-6">
          <Routes>
            <Route path="/" element={<Navigate to="/overview" replace />} />
            <Route path="/overview" element={<Overview />} />
            <Route
              path="/quickstart"
              element={<Navigate to="/host" replace />}
            />
            <Route path="/sessions" element={<Sessions />} />
            <Route path="/sessions/:id" element={<SessionDetail />} />
            <Route path="/traces" element={<Traces />} />
            <Route
              path="/runs"
              element={<Navigate to="/sessions" replace />}
            />
            <Route
              path="/trace"
              element={<Navigate to="/traces" replace />}
            />
            <Route
              path="/knowledge"
              element={<Navigate to="/knowledge/blocks" replace />}
            />
            <Route
              path="/knowledge/:section"
              element={
                config?.dev_mode ? (
                  <Learnings />
                ) : (
                  <Navigate to="/overview" replace />
                )
              }
            />
            <Route
              path="/knowledge/:section/:rubricId"
              element={
                config?.dev_mode ? (
                  <Learnings />
                ) : (
                  <Navigate to="/overview" replace />
                )
              }
            />
            <Route
              path="/learnings"
              element={<Navigate to="/knowledge/blocks" replace />}
            />
            <Route
              path="/learnings/:section"
              element={
                config?.dev_mode ? (
                  <Learnings />
                ) : (
                  <Navigate to="/overview" replace />
                )
              }
            />
            <Route
              path="/learnings/:section/:rubricId"
              element={
                config?.dev_mode ? (
                  <Learnings />
                ) : (
                  <Navigate to="/overview" replace />
                )
              }
            />
            <Route path="/savings" element={<Savings />} />
            <Route path="/insights" element={<Insights />} />
            <Route path="/telemetry" element={<Telemetry />} />
            <Route path="/memory" element={<Memory />} />
            <Route path="/outcomes" element={<Outcomes />} />
            <Route path="/reports" element={<Reports />} />
            <Route path="/agents" element={<Agents />} />
            <Route path="/tools" element={<Tools />} />
            <Route path="/host" element={<Host />} />
            <Route
              path="/watchdogs"
              element={
                config?.dev_mode ? (
                  <Watchdogs />
                ) : (
                  <Navigate to="/overview" replace />
                )
              }
            />
            <Route path="/analytics" element={<Analytics />} />
            <Route path="/external" element={<External />} />
            <Route path="/optimizations" element={<Optimizations />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
