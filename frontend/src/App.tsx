import { useEffect, useRef, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import {
  Activity,
  BarChart3,
  Bot,
  Brain,
  ChevronDown,
  ChevronUp,
  Command,
  Database,
  FileText,
  Flag,
  Hexagon,
  LayoutGrid,
  Play,
  Settings,
  Sparkles,
  TrendingUp,
  Zap,
} from "lucide-react";
import Overview from "./pages/Overview";
import Sessions from "./pages/Sessions";
import Learnings from "./pages/Learnings";
import Savings from "./pages/Savings";
import System, { SystemAgents, SystemHosts, SystemMcp, SystemSkills } from "./pages/System";
import Telemetry from "./pages/Telemetry";
import Memory from "./pages/Memory";
import Reports from "./pages/Reports";
import Watchdogs from "./pages/Watchdogs";
import Analytics from "./pages/Analytics";
import Optimizations from "./pages/Optimizations";
import {
  acknowledgeTelemetry,
  getTelemetryConfig,
  type TelemetryConfig,
} from "./lib/insightsApi";
import { Button, Chip, Select, cx } from "./components/WorkbenchUI";
import { useTimeRange, TIME_RANGE_OPTIONS } from "./lib/TimeRangeContext";

interface NavItem {
  to: string;
  label: string;
  icon: React.ElementType;
  isDev?: boolean;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/overview", label: "Overview", icon: LayoutGrid },
  { to: "/sessions", label: "Sessions", icon: Play },
  { to: "/memory", label: "Memory", icon: Database },
  { to: "/analytics", label: "Analytics", icon: BarChart3 },
  { to: "/optimizations", label: "Optimizations", icon: Zap },
];

interface MenuSection {
  label: string;
  to: string;
  icon: React.ElementType;
  isDev?: boolean;
}

const MENU_SECTIONS: MenuSection[] = [
  { to: "/system/hosts", label: "Hosts", icon: Hexagon },
  { to: "/system/agents", label: "Agents", icon: Bot },
  { to: "/system/skills", label: "Skills", icon: Sparkles },
  { to: "/system/mcp", label: "MCP", icon: Command },
  { to: "/reports", label: "Reports", icon: FileText },
  { to: "/telemetry", label: "Telemetry", icon: Activity },
  { to: "/savings", label: "Savings", icon: TrendingUp, isDev: true },
  { to: "/watchdogs", label: "Watchdogs", icon: Flag, isDev: true },
  { to: "/knowledge/blocks", label: "Knowledge", icon: Brain, isDev: true },
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
    <div className="border-b border-purple-900/60 bg-purple-950/30 px-6 py-3 text-sm text-purple-100">
      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div>
          Atelier collects anonymous usage telemetry to improve the product.
          Disable any time with{" "}
          <code className="bg-black/30 px-1">atelier telemetry off</code> or
          <code className="ml-1 bg-black/30 px-1">ATELIER_TELEMETRY=0</code>.
        </div>
        <Button
          variant="accent"
          size="sm"
          onClick={() => {
            setDismissed(true);
            acknowledgeTelemetry().catch(() => undefined);
          }}
        >
          Got it
        </Button>
      </div>
    </div>
  );
}

function GearMenu({ devMode }: { devMode?: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  const visible = MENU_SECTIONS.filter((s) => !s.isDev || devMode);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className={cx(
          "inline-flex items-center gap-1.5 border px-3 py-1.5 text-xs transition",
          open
            ? "border-purple-500/60 bg-purple-500/10 text-purple-400"
            : "border-neutral-800 bg-neutral-900/40 text-neutral-400 hover:border-neutral-600 hover:text-neutral-200"
        )}
        aria-label="System menu"
      >
        <Settings size={14} />
        <span className="hidden sm:inline">System</span>
        <span className="text-neutral-600">
          {open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
        </span>
      </button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 w-44 border border-neutral-800 bg-neutral-950 shadow-lg">
          {visible.map((section) => {
            const end =
              section.to === "/system" || section.to.startsWith("/system/");
            return (
              <NavLink
                key={section.to}
                to={section.to}
                end={end}
                onClick={() => setOpen(false)}
                className={({ isActive }) =>
                  cx(
                    "flex items-center gap-2 px-3 py-2 text-xs transition",
                    isActive
                      ? "bg-purple-500/10 text-purple-400"
                      : "text-neutral-400 hover:bg-neutral-900 hover:text-neutral-200"
                  )
                }
              >
                <span className="w-4 flex justify-center">
                  <section.icon size={14} />
                </span>
                <span>{section.label}</span>
                {section.isDev && (
                  <span className="ml-auto text-[8px] font-bold text-amber-500/60">
                    DEV
                  </span>
                )}
              </NavLink>
            );
            })}
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [config, setConfig] = useState<TelemetryConfig | null>(null);
  const { range, setRange } = useTimeRange();

  useEffect(() => {
    getTelemetryConfig()
      .then(setConfig)
      .catch(() => undefined);
  }, []);

  return (
    <div className="min-h-full bg-gradient-to-b from-[#0a0a0a] to-[#0f0f0f] font-mono text-neutral-200">
      <header className="border-b border-neutral-800 bg-neutral-950/95 px-6 py-4">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-4">
            <h1 className="text-lg font-bold tracking-wide text-brand">
              ❯ ATELIER - The Agents Runtime
            </h1>
            {config?.dev_mode && <Chip tone="purple">DEV MODE</Chip>}
          </div>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-2 border border-neutral-800 bg-neutral-900/40 px-3 py-1.5">
              <span className="text-[10px] font-bold uppercase tracking-widest text-neutral-500">
                Window
              </span>
              <Select
                value={range}
                onChange={(e) => setRange(e.target.value as any)}
                uiSize="xs"
                className="border-0 bg-transparent px-0 py-0 text-xs text-neutral-300 hover:text-purple-400"
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
            <GearMenu devMode={config?.dev_mode} />
          </div>
        </div>
      </header>

      <TelemetryDisclosure />

      <nav className="border-neutral-800 bg-neutral-950/70 px-6 py-3">
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
                <item.icon size={14} />
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
        <div className="">
          <Routes>
            <Route path="/" element={<Navigate to="/overview" replace />} />
            <Route path="/overview" element={<Overview />} />
            <Route
              path="/quickstart"
              element={<Navigate to="/system/hosts" replace />}
            />
            <Route path="/sessions" element={<Sessions />} />
            <Route path="/sessions/:id" element={<Sessions />} />
            <Route path="/runs" element={<Navigate to="/sessions" replace />} />
            <Route
              path="/trace"
              element={<Navigate to="/sessions" replace />}
            />
            <Route
              path="/traces"
              element={<Navigate to="/sessions" replace />}
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
            <Route path="/insights" element={<Navigate to="/overview" replace />} />
            <Route path="/telemetry" element={<Telemetry />} />
            <Route path="/memory" element={<Memory />} />
            <Route path="/outcomes" element={<Navigate to="/overview" replace />} />
            <Route path="/reports" element={<Reports />} />
            <Route path="/system" element={<System />} />
            <Route path="/system/hosts" element={<SystemHosts />} />
            <Route path="/system/agents" element={<SystemAgents />} />
            <Route path="/system/skills" element={<SystemSkills />} />
            <Route path="/system/mcp" element={<SystemMcp />} />
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
            <Route path="/external" element={<Navigate to="/overview" replace />} />
            <Route path="/optimizations" element={<Optimizations />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
