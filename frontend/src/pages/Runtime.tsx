import { useNavigate, useParams } from "react-router-dom";
import { Activity, Play, TrendingUp } from "lucide-react";
import { MetricCard, ToggleGroup } from "../components/WorkbenchUI";
import Sessions from "./Sessions";
import Watchdogs from "./Watchdogs";
import Savings from "./Savings";
import Insights from "./Insights";

type RuntimeSection = "operate" | "savings" | "telemetry";

const SECTIONS: Array<{
  id: RuntimeSection;
  label: string;
  icon: React.ElementType;
  description: string;
}> = [
  {
    id: "operate",
    label: "Sessions + Watchdogs",
    icon: Play,
    description:
      "Observable execution plus guardrails and preset risk profiles.",
  },
  {
    id: "savings",
    label: "Savings",
    icon: TrendingUp,
    description: "Cost, token, and time reduction evidence.",
  },
  {
    id: "telemetry",
    label: "Telemetry",
    icon: Activity,
    description: "Usage analytics and privacy audit.",
  },
];

export default function Runtime() {
  const { section } = useParams<{ section?: string }>();
  const navigate = useNavigate();
  const active = (section as RuntimeSection) || "operate";

  return (
    <div className="space-y-6">
      <section className="grid grid-cols-2 gap-3">
        <MetricCard
          label="Sub-tabs"
          value={String(SECTIONS.length)}
          detail="Execution, guardrails, value, and analytics."
          tone="cyan"
        />
        <MetricCard
          label="Current view"
          value={
            SECTIONS.find((item) => item.id === active)?.label ??
            "Sessions + Watchdogs"
          }
          detail="All runtime slices stay inside one area."
          tone="neutral"
        />
      </section>

      <ToggleGroup
        variant="underline"
        size="sm"
        options={SECTIONS.map((item) => ({
          value: item.id,
          label: (
            <span className="flex items-center gap-2">
              <item.icon size={14} />
              <span>{item.label}</span>
            </span>
          ),
          title: item.description,
        }))}
        value={active}
        onChange={(value) =>
          navigate(`/runtime/${value as RuntimeSection}`, { replace: true })
        }
      />

      {active === "operate" && (
        <div className="space-y-8 h-[calc(100vh-400px)]">
          <Sessions />
          <Watchdogs />
        </div>
      )}
      {active === "savings" && <Savings />}
      {active === "telemetry" && <Insights />}
    </div>
  );
}
