import { useNavigate, useParams } from "react-router-dom";
import { MetricCard } from "../components/WorkbenchUI";
import Traces from "./Traces";
import Watchdogs from "./Watchdogs";
import Savings from "./Savings";
import Insights from "./Insights";

type RuntimeSection = "operate" | "savings" | "telemetry";

const SECTIONS: Array<{
  id: RuntimeSection;
  label: string;
  icon: string;
  description: string;
}> = [
  {
    id: "operate",
    label: "Runs + Watchdogs",
    icon: "▶⚑",
    description:
      "Observable execution plus guardrails and preset risk profiles.",
  },
  {
    id: "savings",
    label: "Savings",
    icon: "₿",
    description: "Cost, token, and time reduction evidence.",
  },
  {
    id: "telemetry",
    label: "Telemetry",
    icon: "◎",
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
            "Runs + Watchdogs"
          }
          detail="All runtime slices stay inside one area."
          tone="neutral"
        />
      </section>

      <section className="border border-neutral-800 bg-neutral-950/70 p-5">
        <div className="flex flex-wrap gap-0 border-b border-neutral-800">
          {SECTIONS.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => navigate(`/runtime/${item.id}`, { replace: true })}
              title={item.description}
              className={`flex items-center gap-2 border-b-2 px-4 py-2 text-xs font-bold transition ${
                active === item.id
                  ? "border-neutral-500 bg-neutral-900/30 text-neutral-100"
                  : "border-transparent text-neutral-500 hover:text-neutral-200"
              }`}
            >
              <span>{item.icon}</span>
              <span>{item.label}</span>
            </button>
          ))}
        </div>
      </section>

      {active === "operate" && (
        <div className="space-y-8">
          <Traces />
          <Watchdogs />
        </div>
      )}
      {active === "savings" && <Savings />}
      {active === "telemetry" && <Insights />}
    </div>
  );
}
