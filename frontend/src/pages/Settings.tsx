import { ChevronRight, Wrench } from "lucide-react";
import { Link, useParams } from "react-router-dom";

import { Card, PageFrame } from "../components/WorkbenchUI";
import Integrations from "./Integrations";
import System from "./System";

type SettingsSection = "integrations" | "diagnostics" | "telemetry" | "advanced";

const ADVANCED = [
  ["Hosts", "/system/hosts", "Raw host adapter status and imports."],
  ["Agents", "/system/agents", "Internal agent definitions."],
  ["Skills", "/system/skills", "Installed runtime skills."],
  ["MCP", "/system/mcp", "Internal MCP/runtime capability detail."],
  ["Watchdogs", "/system/watchdogs", "Runtime watchdog configuration."],
  ["Projection", "/system/projection", "Low-level projection inspector."],
] as const;

export default function Settings() {
  const { section } = useParams<{ section?: string }>();
  const active = (section as SettingsSection) || "integrations";

  if (active === "integrations") return <Integrations />;
  if (active === "diagnostics") return <System forcedSection="health" />;
  if (active === "telemetry") return <System forcedSection="telemetry" />;

  return (
    <PageFrame className="space-y-4">
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {ADVANCED.map(([label, to, description]) => (
          <Link key={to} to={to}>
            <Card className="h-full p-3 transition hover:border-neutral-700 hover:bg-neutral-900/50">
              <div className="flex items-start gap-3">
                <Wrench size={13} className="mt-0.5 shrink-0 text-neutral-500" />
                <div className="min-w-0 flex-1">
                  <div className="text-xs font-medium text-neutral-200">{label}</div>
                  <p className="mt-1 text-[10px] leading-4 text-neutral-600">{description}</p>
                </div>
                <ChevronRight size={13} className="mt-0.5 shrink-0 text-neutral-700" />
              </div>
            </Card>
          </Link>
        ))}
      </div>
    </PageFrame>
  );
}
