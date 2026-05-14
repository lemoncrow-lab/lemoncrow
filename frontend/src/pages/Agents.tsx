import { useState, useEffect } from "react";
import { api, type Skill } from "../api";
import { getTelemetryConfig, type TelemetryConfig } from "../lib/insightsApi";

interface AgentDef {
  id: string;
  label: string;
  icon: string;
  color: string;
  description: string;
  tools: string[];
  mode: string;
  file: string;
  rules: string[];
}

const AGENTS: AgentDef[] = [
  {
    id: "code",
    label: "atelier:code",
    icon: "💜",
    color: "purple",
    description:
      "Main coding agent. Edits, refactors, fixes bugs, and ships features. MUST use the Atelier reasoning loop on every task.",
    tools: ["* (all tools)"],
    mode: "Context → Implement → Trace",
    file: "integrations/claude/plugin/agents/code.md",
    rules: [
      "Gather Context before starting (retrieve procedures and facts)",
      "Implement task following knowledge; call rescue on repeated failures",
      "Record Trace at completion with observable summary only",
    ],
  },
  {
    id: "explore",
    label: "atelier:explore",
    icon: "🔍",
    color: "cyan",
    description:
      "Read-only repo exploration. Retrieves ReasonBlocks, reads files, runs grep/search. Never edits, never runs migrations, never executes destructive commands.",
    tools: ["Read", "Grep", "Glob", "WebFetch", "context"],
    mode: "Context → Read-only investigation",
    file: "integrations/claude/plugin/agents/explore.md",
    rules: [
      "Call context to fetch matched ReasonBlocks and rules",
      "Read files, run grep/glob searches — never edit",
      "Return tight summary with ReasonBlock IDs and file/line citations",
    ],
  },
  {
    id: "review",
    label: "atelier:review",
    icon: "✅",
    color: "green",
    description:
      "Verifier agent. Reviews finished or in-progress patches against Atelier ReasonBlocks and rubrics. Blocks known dead ends. Uses context and verify but never edits code.",
    tools: ["Read", "Grep", "Glob", "context", "verify"],
    mode: "Verify patch → context → rubric_gate → verdict",
    file: "integrations/claude/plugin/agents/review.md",
    rules: [
      "Call context with task and changed files",
      "Identify ReasonBlocks whose dead_ends overlap with the patch",
      "For high-risk domains, call verify and require status != blocked",
      "Produce verdict: pass | warn | blocked (never approve blocked)",
    ],
  },
  {
    id: "repair",
    label: "atelier:repair",
    icon: "🔧",
    color: "red",
    description:
      "Repair specialist. Activated when a test/command/tool keeps failing the same way. Loads context, asks for rescue, applies smallest patch, and records postmortem trace.",
    tools: ["* (all tools)"],
    mode: "Context → Rescue → Patch → Verify → Postmortem",
    file: "integrations/claude/plugin/agents/repair.md",
    rules: [
      "Retrieve Context to understand current constraints",
      "Ask for rescue with task, error, files, recent_actions",
      "Apply smallest patch, verify deterministically, stop after 2 failed attempts",
      "Record postmortem Trace on completion",
    ],
  },
];

export default function Agents() {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [config, setConfig] = useState<TelemetryConfig | null>(null);
  const [expandedSkill, setExpandedSkill] = useState<string | null>(null);

  const hiddenSkillCount = config === null ? 0 : config.dev_mode ? 0 : 4;
  const visibleSkills = skills ?? [];
  const totalSkillCount =
    skills === null ? null : visibleSkills.length + hiddenSkillCount;

  useEffect(() => {
    api
      .skills()
      .then(setSkills)
      .catch((e) => console.error("Failed to load skills:", e));

    getTelemetryConfig()
      .then(setConfig)
      .catch(() => undefined);
  }, []);

  return (
    <div className="space-y-8 text-sm">
      {/* Agent Cards */}
      <section className="space-y-3">
        {AGENTS.map((agent) => (
          <AgentCard
            key={agent.id}
            agent={agent}
            expanded={expandedId === agent.id}
            onToggle={() =>
              setExpandedId(expandedId === agent.id ? null : agent.id)
            }
          />
        ))}
      </section>

      {/* Skills Section */}
      <section>
        <h2 className="text-xs uppercase tracking-widest text-neutral-500 mb-4 font-mono">
          Skills
        </h2>
        <p className="text-xs text-neutral-400 mb-3">
          {totalSkillCount === null
            ? "Loading skill catalog..."
            : `${totalSkillCount} common skills in the repo. Click to expand and see full documentation for the ones available in this mode.`}
        </p>
        <div className="grid grid-cols-1 gap-2">
          {visibleSkills.length > 0 ? (
            visibleSkills.map((s) => (
              <SkillCard
                key={s.name}
                skill={{
                  name: s.name,
                  desc: s.description,
                  icon: "✓",
                }}
                isExpanded={expandedSkill === s.name}
                onToggle={() =>
                  setExpandedSkill(expandedSkill === s.name ? null : s.name)
                }
              />
            ))
          ) : (
            <div className="text-neutral-500 text-xs">Loading skills...</div>
          )}
          {hiddenSkillCount > 0 && (
            <div className="border border-dashed border-neutral-800 bg-neutral-950/40 px-4 py-3">
              <p className="text-[11px] font-mono text-neutral-500">
                {hiddenSkillCount} dev-only skills hidden. Enable dev mode with{" "}
                <code>ATELIER_DEV_MODE=1</code> to install and inspect them.
              </p>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

function AgentCard({
  agent,
  expanded,
  onToggle,
}: {
  agent: AgentDef;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <div
      id={`agent-${agent.id}`}
      className="border border-neutral-800 bg-neutral-900/50 overflow-hidden transition-all"
    >
      {/* Header */}
      <button
        onClick={onToggle}
        className="w-full px-5 py-4 text-left hover:bg-neutral-800/50 transition-colors flex items-start justify-between"
      >
        <div className="flex-1 flex items-start gap-4 min-w-0">
          {/* Icon */}
          <div className="text-2xl flex-shrink-0 mt-0.5">{agent.icon}</div>

          {/* Title & Details */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-3 mb-1 flex-wrap">
              {/* Expandable indicator */}
              <span
                className={`text-neutral-500 font-mono text-xs transition-transform ${
                  expanded ? "rotate-90" : ""
                }`}
              >
                ❯
              </span>
              <h3 className="font-mono font-bold text-neutral-200 text-sm">
                {agent.label}
              </h3>
            </div>
            <p className="text-xs text-neutral-400">{agent.description}</p>
          </div>
        </div>
      </button>

      {/* Expanded Content */}
      {expanded && (
        <div className="border-t border-neutral-800 bg-neutral-950/50 px-5 py-4 space-y-4">
          {/* Tools */}
          <div>
            <div className="text-[10px] uppercase tracking-widest text-neutral-500 mb-2 font-mono flex items-center gap-1">
              <span>❯</span> tools
            </div>
            <div className="flex flex-wrap gap-1">
              {agent.tools.map((t) => (
                <code
                  key={t}
                  className="text-[10px] bg-neutral-950 px-2 py-1 text-neutral-300 font-mono border border-neutral-700"
                >
                  {t}
                </code>
              ))}
            </div>
          </div>

          {/* Rules */}
          <div>
            <div className="text-[10px] uppercase tracking-widest text-neutral-500 mb-2 font-mono flex items-center gap-1">
              <span>❯</span> rules
            </div>
            <ul className="space-y-1">
              {agent.rules.map((r, i) => (
                <li
                  key={i}
                  className="text-xs text-neutral-300 leading-relaxed"
                >
                  {r}
                </li>
              ))}
            </ul>
          </div>

          {/* Mode */}
          <div>
            <div className="text-[10px] uppercase tracking-widest text-neutral-500 mb-2 font-mono flex items-center gap-1">
              <span>❯</span> mode
            </div>
            <code className="text-[10px] bg-neutral-950 px-2 py-1 text-neutral-300 font-mono border border-neutral-700 block">
              {agent.mode}
            </code>
          </div>

          {/* Source */}
          <div className="pt-2 border-t border-neutral-800">
            <div className="text-[10px] uppercase tracking-widest text-neutral-500 mb-2 font-mono">
              Source
            </div>
            <code className="text-[10px] bg-neutral-950 px-2 py-1 text-neutral-500 font-mono border border-neutral-700 block break-all">
              {agent.file}
            </code>
          </div>
        </div>
      )}
    </div>
  );
}

function SkillCard({
  skill,
  isExpanded,
  onToggle,
}: {
  skill: { name: string; desc: string; icon: string };
  isExpanded: boolean;
  onToggle: () => void;
}) {
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const toggle = async () => {
    if (isExpanded) {
      onToggle();
      return;
    }
    if (content) {
      onToggle();
      return;
    }
    setLoading(true);
    try {
      const skillData = await api.skill(skill.name);
      if (skillData) {
        setContent(skillData.content);
        onToggle();
      }
    } catch (e) {
      console.error("Failed to load skill:", e);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="border border-neutral-800 p-2 bg-neutral-900/30 flex flex-col gap-2">
      <button
        onClick={toggle}
        className="flex items-start gap-2 w-full text-left"
      >
        <span className="mt-0.5">{skill.icon}</span>
        <div className="min-w-0 flex-1">
          <div className="text-[11px] font-mono font-medium text-neutral-200 truncate">
            {skill.name}
          </div>
          <div className="text-[10px] text-neutral-500 leading-tight">
            {skill.desc}
          </div>
        </div>
        <span className="text-neutral-600">
          {loading ? "..." : isExpanded ? "−" : "+"}
        </span>
      </button>
      {isExpanded && content && (
        <div className="mt-1 pt-2 border-t border-neutral-800">
          <pre className="text-neutral-400 whitespace-pre-wrap font-mono max-h-60 overflow-y-auto bg-neutral-950/50 p-2 text-[10px]">
            {content}
          </pre>
        </div>
      )}
    </div>
  );
}
