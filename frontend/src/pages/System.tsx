import { useEffect, useState } from "react";
import { Navigate, useSearchParams } from "react-router-dom";
import {
  Archive,
  Bot,
  Brain,
  Circle,
  Check,
  CheckCircle,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Command,
  HardDrive,
  Heart,
  Microscope,
  Minus,
  Plus,
  Search,
  Terminal,
  Wrench,
} from "lucide-react";
import {
  api,
  type Agent,
  type HostAdapter,
  type MCPStatus,
  type Skill,
} from "../api";
import { getTelemetryConfig, type TelemetryConfig } from "../lib/insightsApi";
import {
  Alert,
  Card,
  Chip,
  DisclosureCard,
  EmptyState,
  FieldLabel,
} from "../components/WorkbenchUI";

// ---------------------------------------------------------------------------
// Hosts section
// ---------------------------------------------------------------------------

function HostIcon({ id }: { id: string }) {
  const SRC_MAP: Record<string, string> = {
    claude: "/logos/hosts/claude.svg",
    codex: "/logos/hosts/codex.svg",
    opencode: "/logos/hosts/opencode.svg",
    copilot: "/logos/hosts/copilot.svg",
  };

  const ALT_MAP: Record<string, string> = {
    claude: "Anthropic Claude",
    codex: "OpenAI Codex",
    opencode: "OpenCode",
    copilot: "GitHub Copilot",
    antigravity: "Antigravity",
    cursor: "Cursor IDE",
    hermes: "Hermes Agent",
  };

  const INITIALS: Record<string, string> = {
    antigravity: "AG",
    cursor: "CU",
    hermes: "HE",
  };

  const src = SRC_MAP[id];
  if (!src) {
    return (
      <span className="inline-flex h-7 w-7 items-center justify-center border border-neutral-700 bg-neutral-900 text-[10px] font-bold text-neutral-300">
        {INITIALS[id] ?? "◌"}
      </span>
    );
  }

  return (
    <span className="inline-flex h-7 w-7 items-center justify-center overflow-hidden bg-white p-1">
      <img
        src={src}
        alt={ALT_MAP[id] ?? id}
        className="h-full w-full object-contain"
        loading="lazy"
      />
    </span>
  );
}

const HOST_DESC: Record<string, string> = {
  claude: "Generated AGENTS surface, MCP wrapper, and Claude plugin hooks.",
  codex:
    "Codex MCP registration with generated instructions and shared telemetry.",
  opencode:
    "OpenCode MCP config with imported session support and local agents.",
  copilot:
    "VS Code / Copilot MCP config with custom instructions and shared telemetry.",
  antigravity:
    "Antigravity MCP config plus generated AGENTS guidance and agy companion flow.",
  cursor: "Cursor MCP config with project rules and MCP-first guidance.",
  hermes: "Global-only Hermes MCP registration through ~/.hermes/config.yaml.",
};

const HOST_SCOPE_BADGES: Record<string, string> = {
  hermes: "GLOBAL ONLY",
};

const HOST_ORDER = [
  "claude",
  "codex",
  "opencode",
  "copilot",
  "antigravity",
  "cursor",
  "hermes",
] as const;

export function HostsSection() {
  const [hosts, setHosts] = useState<HostAdapter[]>([]);

  useEffect(() => {
    api
      .hosts()
      .then(setHosts)
      .catch(() => setHosts([]));
  }, []);

  const orderedHosts = [...hosts].sort((left, right) => {
    const leftStatus = left.status === "active" ? 0 : 1;
    const rightStatus = right.status === "active" ? 0 : 1;
    if (leftStatus !== rightStatus) return leftStatus - rightStatus;

    const leftOrder = HOST_ORDER.indexOf(
      left.host_id as (typeof HOST_ORDER)[number]
    );
    const rightOrder = HOST_ORDER.indexOf(
      right.host_id as (typeof HOST_ORDER)[number]
    );
    const safeLeft = leftOrder === -1 ? Number.MAX_SAFE_INTEGER : leftOrder;
    const safeRight = rightOrder === -1 ? Number.MAX_SAFE_INTEGER : rightOrder;
    if (safeLeft !== safeRight) return safeLeft - safeRight;
    return left.label.localeCompare(right.label);
  });

  return (
    <section className="space-y-3">
      <h2 className="text-xs uppercase tracking-widest text-neutral-500 font-mono">
        Hosts
      </h2>
      <p className="text-xs text-neutral-400">
        Atelier can configure these host surfaces. Detected hosts are shown
        first, but unsupported gaps are still visible so you can wire them up
        directly.
      </p>
      {orderedHosts.length === 0 ? (
        <EmptyState
          title="No supported hosts found"
          description="Host configs are loaded from Atelier's integration catalog."
          className="p-4"
        />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {orderedHosts.map((host) => (
            <Card key={host.host_id} className="bg-neutral-950/80 p-4">
              <div className="flex items-start gap-3">
                <span className="shrink-0">
                  <HostIcon id={host.host_id} />
                </span>
                <div className="min-w-0 flex-1 space-y-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <div className="text-base font-semibold text-neutral-100">
                      {host.label}
                    </div>
                    <Chip
                      tone={host.status === "active" ? "emerald" : "neutral"}
                    >
                      {host.status === "active" ? "detected" : "not detected"}
                    </Chip>
                    {HOST_SCOPE_BADGES[host.host_id] && (
                      <Chip tone="amber">
                        {HOST_SCOPE_BADGES[host.host_id]}
                      </Chip>
                    )}
                  </div>
                  <p className="text-sm text-neutral-400">
                    {host.description ?? HOST_DESC[host.host_id] ?? ""}
                  </p>
                  {host.install_command && (
                    <div className="space-y-1">
                      <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-mono">
                        Install
                      </div>
                      <code className="block break-all border border-neutral-800 bg-neutral-950 px-2 py-1 text-[10px] text-neutral-300">
                        {host.install_command}
                      </code>
                    </div>
                  )}
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Agents section
// ---------------------------------------------------------------------------

const AGENT_ICON: Record<string, React.ElementType> = {
  code: Heart,
  explore: Search,
  review: CheckCircle,
  repair: Wrench,
  research: Microscope,
};

const AGENT_BG: Record<string, string> = {
  purple: "bg-purple-700",
  cyan: "bg-cyan-700",
  green: "bg-green-700",
  red: "bg-red-700",
  blue: "bg-blue-700",
  yellow: "bg-yellow-700",
};

const AGENT_TEXT: Record<string, string> = {
  purple: "text-purple-400",
  cyan: "text-cyan-400",
  green: "text-green-400",
  red: "text-red-400",
  blue: "text-blue-400",
  yellow: "text-yellow-400",
};

export function AgentsSection() {
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => {
    api
      .agents()
      .then(setAgents)
      .catch((e) => console.error("Failed to load agents:", e));
  }, []);

  if (agents === null) {
    return (
      <section className="space-y-3">
        <h2 className="text-xs uppercase tracking-widest text-neutral-500 font-mono">
          Agents
        </h2>
        <p className="text-xs text-neutral-500">Loading…</p>
      </section>
    );
  }

  return (
    <section className="space-y-3">
      <h2 className="text-xs uppercase tracking-widest text-neutral-500 font-mono">
        Agents
      </h2>
      <div className="grid gap-2 sm:grid-cols-2">
        {agents.map((agent) => (
          <AgentCard
            key={agent.id}
            agent={agent}
            expanded={expandedId === agent.id}
            onToggle={() =>
              setExpandedId(expandedId === agent.id ? null : agent.id)
            }
          />
        ))}
      </div>
    </section>
  );
}

function AgentCard({
  agent,
  expanded,
  onToggle,
}: {
  agent: Agent;
  expanded: boolean;
  onToggle: () => void;
}) {
  const bg = AGENT_BG[agent.color] ?? "bg-neutral-800/40";
  const color = AGENT_TEXT[agent.color] ?? "text-neutral-200";
  const Icon = AGENT_ICON[agent.id] ?? Bot;
  return (
    <DisclosureCard
      open={expanded}
      onToggle={onToggle}
      contentClassName="space-y-4"
      header={
        <div className="flex min-w-0 items-start gap-4">
          <div className={`mt-0.5 shrink-0 ${color}`}>
            <Icon size={24} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="mb-1 flex flex-wrap items-center gap-3">
              <span
                className={`${bg} font-mono text-xs px-2 py-1 transition-transform inline-flex items-center gap-2 ${
                  expanded ? "rotate-0" : ""
                }`}
              >
                <ChevronRight
                  size={14}
                  className={`transition-transform ${expanded ? "rotate-90" : ""}`}
                />
                <span className="font-bold text-neutral-200 text-sm">
                  {agent.name}
                </span>
              </span>
              {agent.model && (
                <Chip
                  tone="neutral"
                  className="normal-case tracking-normal text-[10px]"
                >
                  {agent.model}
                </Chip>
              )}
            </div>
            <p className="text-xs text-neutral-400">{agent.description}</p>
          </div>
        </div>
      }
    >
      {/* Tools */}
      <div>
        <FieldLabel className="mb-2">
          <ChevronRight size={10} className="inline mr-1" /> tools
        </FieldLabel>
        <div className="flex flex-wrap gap-1">
          {agent.tools.map((t) => (
            <Chip
              key={t}
              tone="neutral"
              className="normal-case tracking-normal"
            >
              {t}
            </Chip>
          ))}
        </div>
      </div>

      {/* Content (markdown body) */}
      {agent.content && (
        <div>
          <FieldLabel className="mb-2">
            <ChevronRight size={10} className="inline mr-1" /> instructions
          </FieldLabel>
          <pre className="text-[10px] bg-neutral-950 px-2 py-2 text-neutral-400 font-mono border border-neutral-700 block overflow-auto max-h-48 whitespace-pre-wrap">
            {agent.content}
          </pre>
        </div>
      )}

      {/* Source */}
      <div className="pt-2 border-t border-neutral-800">
        <FieldLabel className="mb-2">Source</FieldLabel>
        <code className="text-[10px] bg-neutral-950 px-2 py-1 text-neutral-500 font-mono border border-neutral-700 block break-all">
          {agent.file}
        </code>
      </div>
    </DisclosureCard>
  );
}

// ---------------------------------------------------------------------------
// Skills section
// ---------------------------------------------------------------------------

export function SkillsSection() {
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
    <section className="space-y-3">
      <h2 className="text-xs uppercase tracking-widest text-neutral-500 font-mono">
        Skills
      </h2>
      <p className="text-xs text-neutral-400 mb-3">
        {totalSkillCount === null
          ? "Loading skill catalog..."
          : `${totalSkillCount} common skills in the repo. Click to expand and see full documentation for the ones available in this mode.`}
      </p>
      <div className="grid gap-2 sm:grid-cols-2">
        {visibleSkills.length > 0 ? (
          visibleSkills.map((s) => (
            <SkillCard
              key={s.name}
              skill={{
                name: s.name,
                desc: s.description,
                icon: Check,
              }}
              isExpanded={expandedSkill === s.name}
              onToggle={() =>
                setExpandedSkill(expandedSkill === s.name ? null : s.name)
              }
            />
          ))
        ) : (
          <EmptyState title="Loading skills..." className="p-4 sm:col-span-2" />
        )}
        {hiddenSkillCount > 0 && (
          <Card className="border-dashed bg-neutral-950/40 px-4 py-3 sm:col-span-2">
            <p className="text-[11px] font-mono text-neutral-500">
              {hiddenSkillCount} dev-only skills hidden. Enable dev mode with{" "}
              <code>ATELIER_DEV_MODE=1</code> to install and inspect them.
            </p>
          </Card>
        )}
      </div>
    </section>
  );
}

function SkillCard({
  skill,
  isExpanded,
  onToggle,
}: {
  skill: { name: string; desc: string; icon: React.ElementType };
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
    <Card className="flex flex-col gap-2 bg-neutral-900/30 p-2">
      <button
        onClick={toggle}
        className="flex items-start gap-2 w-full text-left"
      >
        <span className="mt-0.5">
          <skill.icon size={14} className="text-emerald-500" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-[11px] font-mono font-medium text-neutral-200 truncate">
            {skill.name}
          </div>
          <div className="text-[10px] text-neutral-500 leading-tight">
            {skill.desc}
          </div>
        </div>
        <span className="text-neutral-600">
          {loading ? (
            "..."
          ) : isExpanded ? (
            <Minus size={14} />
          ) : (
            <Plus size={14} />
          )}
        </span>
      </button>
      {isExpanded && content && (
        <div className="mt-1 pt-2 border-t border-neutral-800">
          <pre className="text-neutral-400 whitespace-pre-wrap font-mono max-h-60 overflow-y-auto bg-neutral-950/50 p-2 text-[10px]">
            {content}
          </pre>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Tools section
// ---------------------------------------------------------------------------

const NS_MAP: Record<string, string> = {
  context: "reasoning",
  route: "reasoning",
  rescue: "reasoning",
  verify: "reasoning",
  code: "code_intel",
  grep: "retrieval",
  search: "retrieval",
  read: "file_io",
  edit: "file_io",
  shell: "execution",
  sql: "execution",
  trace: "state",
  memory: "state",
  compact: "state",
};

const NS_META: Record<
  string,
  { icon: React.ElementType; label: string; color: string }
> = {
  reasoning: {
    icon: Brain,
    label: "reasoning",
    color: "text-purple-400 border-purple-900/50 bg-purple-950/10",
  },
  code_intel: {
    icon: Command,
    label: "code intel",
    color: "text-cyan-300 border-cyan-900/50 bg-cyan-950/10",
  },
  retrieval: {
    icon: Search,
    label: "retrieval",
    color: "text-sky-300 border-sky-900/50 bg-sky-950/10",
  },
  file_io: {
    icon: HardDrive,
    label: "file i/o",
    color: "text-emerald-300 border-emerald-900/50 bg-emerald-950/10",
  },
  execution: {
    icon: Terminal,
    label: "execution",
    color: "text-orange-300 border-orange-900/50 bg-orange-950/10",
  },
  state: {
    icon: Archive,
    label: "state",
    color: "text-amber-400 border-amber-900/50 bg-amber-950/10",
  },
};

function canonicalName(name: string): string {
  return name.startsWith("atelier_") ? name.slice("atelier_".length) : name;
}

function getNamespace(name: string): string {
  return NS_MAP[name] ?? "other";
}

function descriptionIndicatesDev(description?: string): boolean {
  return !!description && description.startsWith("[DEV]");
}

function isDevTool(tool: MCPStatus): boolean {
  return tool.is_dev === true || descriptionIndicatesDev(tool.description);
}

function primaryEnumParam(tool: MCPStatus) {
  return (
    tool.enum_params?.find(
      (param) => param.name === "op" || param.name === "action"
    ) ??
    tool.enum_params?.[0] ??
    null
  );
}

export function ToolsSection() {
  const [mcpTools, setMcpTools] = useState<MCPStatus[] | null>(null);
  const [expandedTool, setExpandedTool] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .mcp_status()
      .then(setMcpTools)
      .catch((e) => setErr(String(e)));
  }, []);

  if (err) return <Alert tone="danger" description={err} />;

  return (
    <section className="space-y-3">
      <h2 className="text-xs uppercase tracking-widest text-neutral-500 font-mono">
        Tools
      </h2>
      {!mcpTools && <EmptyState title="Loading tools…" className="p-4" />}
      {mcpTools &&
        (() => {
          const seen = new Set<string>();
          const deduped: MCPStatus[] = [];
          for (const t of mcpTools) {
            const canonical = canonicalName(t.tool_name);
            if (!seen.has(canonical)) {
              seen.add(canonical);
              deduped.push({ ...t, tool_name: canonical });
            }
          }

          const groups: Record<string, MCPStatus[]> = {};
          for (const t of deduped) {
            const ns = getNamespace(t.tool_name);
            if (!groups[ns]) groups[ns] = [];
            groups[ns].push(t);
          }

          const nsOrder = [
            "reasoning",
            "code_intel",
            "retrieval",
            "file_io",
            "execution",
            "state",
            "other",
          ];

          return (
            <div className="grid gap-5 sm:grid-cols-2">
              <p className="text-[10px] font-mono text-neutral-600 sm:col-span-2">
                {deduped.length} tools on stdio server: <code>atelier-mcp</code>
              </p>
              {nsOrder
                .filter((ns) => groups[ns]?.length)
                .map((ns) => {
                  const meta = NS_META[ns] ?? {
                    icon: Circle,
                    label: ns,
                    color:
                      "text-neutral-400 border-neutral-800 bg-neutral-900/30",
                  };
                  const tools = groups[ns];
                  return (
                    <div key={ns}>
                      <div className="flex items-center gap-2 mb-2">
                        <meta.icon size={14} className="text-neutral-500" />
                        <span className="text-[10px] uppercase tracking-widest font-mono text-neutral-500">
                          {meta.label}
                        </span>
                        <span className="text-[10px] text-neutral-700 font-mono">
                          ({tools.length})
                        </span>
                      </div>
                      <div className="space-y-px">
                        {tools.map((tool) => {
                          const isExpanded = expandedTool === tool.tool_name;
                          const desc = tool.description;
                          const isDev = isDevTool(tool);
                          const primaryEnum = primaryEnumParam(tool);
                          const cleanDescription = descriptionIndicatesDev(desc)
                            ? desc!.slice("[DEV]".length).trim()
                            : desc;

                          return (
                            <div
                              key={tool.tool_name}
                              className={`border cursor-pointer transition-colors ${meta.color} ${isExpanded ? "border-b-0" : ""}`}
                              onClick={() =>
                                setExpandedTool(
                                  isExpanded ? null : tool.tool_name
                                )
                              }
                            >
                              <div className="flex items-center gap-3 px-4 py-2.5">
                                <span
                                  className={`w-1.5 h-1.5 flex-shrink-0 ${tool.available ? "bg-emerald-400" : "bg-neutral-600"}`}
                                />
                                <span className="font-mono font-semibold text-neutral-200 text-xs flex-1">
                                  {tool.tool_name}
                                </span>
                                {primaryEnum && (
                                  <span className="text-[8px] font-bold text-cyan-300 border border-cyan-500/30 px-1 py-0.5 mr-2">
                                    {primaryEnum.options.length}{" "}
                                    {primaryEnum.name}
                                    {primaryEnum.options.length === 1
                                      ? ""
                                      : "s"}
                                  </span>
                                )}
                                {isDev && (
                                  <span className="text-[8px] font-bold text-amber-500/60 border border-amber-500/30 px-1 py-0.5 mr-2">
                                    DEV
                                  </span>
                                )}
                                {isDev && tool.mode === "passive" && (
                                  <span className="text-[8px] font-bold text-neutral-500 border border-neutral-700 px-1 py-0.5 mr-2">
                                    PASSIVE
                                  </span>
                                )}
                                <span className="text-neutral-600">
                                  {isExpanded ? (
                                    <ChevronUp size={14} />
                                  ) : (
                                    <ChevronDown size={14} />
                                  )}
                                </span>
                              </div>
                              {isExpanded && (
                                <div className="px-4 pb-3 pt-1 border-t border-neutral-800/50">
                                  {cleanDescription ? (
                                    <p className="text-xs text-neutral-300 leading-relaxed">
                                      {cleanDescription}
                                    </p>
                                  ) : (
                                    <p className="text-xs text-neutral-600 italic">
                                      No description available.
                                    </p>
                                  )}
                                  <div className="mt-2 flex items-center gap-3">
                                    <span
                                      className={`text-[10px] font-mono px-2 py-0.5 ${tool.available ? "bg-emerald-900/30 text-emerald-300" : "bg-neutral-800 text-neutral-500"}`}
                                    >
                                      {tool.mode === "passive"
                                        ? "passive capture"
                                        : tool.available
                                          ? "available"
                                          : "unavailable"}
                                    </span>
                                    <code className="text-[10px] font-mono text-neutral-600">
                                      {tool.tool_name}
                                    </code>
                                  </div>
                                  {tool.enum_params &&
                                    tool.enum_params.length > 0 && (
                                      <div className="mt-4 space-y-3">
                                        <FieldLabel className="mb-1">
                                          <ChevronRight
                                            size={10}
                                            className="inline mr-1"
                                          />{" "}
                                          enum params
                                        </FieldLabel>
                                        {tool.enum_params.map((param) => (
                                          <div
                                            key={`${tool.tool_name}-${param.name}`}
                                            className="space-y-2"
                                          >
                                            <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-neutral-500 font-mono">
                                              <code className="border border-neutral-700 bg-neutral-950 px-1.5 py-0.5 text-neutral-300 normal-case">
                                                {param.name}
                                              </code>
                                              <span>
                                                {param.options.length} values
                                              </span>
                                            </div>
                                            {param.description && (
                                              <p className="text-[10px] text-neutral-500">
                                                {param.description}
                                              </p>
                                            )}
                                            <div className="flex flex-wrap gap-1">
                                              {param.options.map((option) => (
                                                <Chip
                                                  key={`${tool.tool_name}-${param.name}-${option}`}
                                                  tone="neutral"
                                                  className="normal-case tracking-normal"
                                                >
                                                  {option}
                                                </Chip>
                                              ))}
                                            </div>
                                          </div>
                                        ))}
                                      </div>
                                    )}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
            </div>
          );
        })()}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Main System page: Host → Agents → Skills → Tools
// ---------------------------------------------------------------------------

function SystemPageFrame({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-8 p-6 text-sm">
      <section className="border border-neutral-800 bg-neutral-950/70 p-5">
        <div className="space-y-2">
          <div className="text-[10px] uppercase tracking-[0.3em] text-neutral-500">
            System
          </div>
          <h1 className="text-2xl font-semibold text-neutral-100">{title}</h1>
          <p className="max-w-3xl text-sm text-neutral-400">{description}</p>
        </div>
      </section>
      <div className="space-y-10">{children}</div>
    </div>
  );
}

const LEGACY_TAB_ROUTES: Record<string, string> = {
  hosts: "/system/hosts",
  agents: "/system/agents",
  skills: "/system/skills",
  mcp: "/system/mcp",
};

export function SystemHosts() {
  return (
    <SystemPageFrame
      title="Host adapters"
      description="Installed host integrations and the environments where Atelier is active."
    >
      <HostsSection />
    </SystemPageFrame>
  );
}

export function SystemAgents() {
  return (
    <SystemPageFrame
      title="Agent catalog"
      description="Available built-in agents, their models, tools, and source definitions."
    >
      <AgentsSection />
    </SystemPageFrame>
  );
}

export function SystemSkills() {
  return (
    <SystemPageFrame
      title="Skill catalog"
      description="Installed skills with descriptions and expandable source content."
    >
      <SkillsSection />
    </SystemPageFrame>
  );
}

export function SystemMcp() {
  return (
    <SystemPageFrame
      title="MCP tools"
      description="Grouped stdio MCP tool availability, descriptions, and runtime mode."
    >
      <ToolsSection />
    </SystemPageFrame>
  );
}

export default function System() {
  const [searchParams] = useSearchParams();
  const legacyTab = searchParams.get("tab");
  return (
    <Navigate
      to={
        legacyTab && LEGACY_TAB_ROUTES[legacyTab]
          ? LEGACY_TAB_ROUTES[legacyTab]
          : "/system/hosts"
      }
      replace
    />
  );
}
