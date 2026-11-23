import { Button as UiButton } from "../components/ui/button";
import { useEffect, useState, type ReactNode } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import {
  Archive,
  ArrowLeft,
  Brain,
  Circle,
  Check,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Command,
  HardDrive,
  Minus,
  Plus,
  Search,
  Terminal,
} from "lucide-react";
import {
  api,
  type Agent,
  type HealthResponse,
  type HostAdapter,
  type MCPStatus,
  type Skill,
} from "../api";
import {
  Alert,
  Card,
  Chip,
  DisclosureCard,
  EmptyState,
  FieldLabel,
  MetricCard,
  PageFrame,
  PageHeader,
} from "../components/WorkbenchUI";
import { fmtDate } from "../lib/format";
import Telemetry from "./Telemetry";
import ProjectionInspector from "./ProjectionInspector";

const INLINE_LINK =
  "text-foreground underline decoration-foreground/40 underline-offset-4 hover:decoration-foreground";

function IntegrationsLink({ children = "Integrations" }: { children?: ReactNode }) {
  return (
    <Link className={INLINE_LINK} to="/settings/integrations">
      {children}
    </Link>
  );
}

// ---------------------------------------------------------------------------
// Hosts section
// ---------------------------------------------------------------------------

function HostIcon({ id, label }: { id: string; label: string }) {
  const SRC_MAP: Record<string, string> = {
    claude: "/logos/hosts/claude.svg",
    codex: "/logos/hosts/codex.svg",
    // The square app icon: opencode.svg is the wide wordmark, illegible at this size.
    opencode: "/logos/hosts/opencode.png",
    copilot: "/logos/hosts/copilot.svg",
    antigravity: "/logos/hosts/antigravity.png",
    cursor: "/logos/hosts/cursor.svg",
    hermes: "/logos/hosts/hermes.png",
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

  const src = SRC_MAP[id];
  if (!src) {
    return (
      <span
        aria-hidden="true"
        className="inline-flex h-7 w-7 items-center justify-center border border-border bg-muted text-caption font-semibold text-subtle"
      >
        {label.charAt(0).toUpperCase()}
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

// `/hosts` reports "active" once LemonCrow has recorded a session from that host. That is not whether the
// tool is installed or configured (Integrations shows that), so the label says what it counts.
function hostSessionLabel(status: string): string {
  return status === "active" ? "Sessions received" : "No sessions yet";
}

function HostSessionChip({ status }: { status: string }) {
  return (
    <Chip
      tone={status === "active" ? "success" : "neutral"}
      className="normal-case tracking-normal"
    >
      {hostSessionLabel(status)}
    </Chip>
  );
}

const HOST_SCOPE_BADGES: Record<string, string> = {
  hermes: "Global only",
};

// Same order as the Integrations page.
const HOST_ORDER = [
  "claude",
  "codex",
  "opencode",
  "pi",
  "copilot",
  "cursor",
  "antigravity",
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
    <section className="space-y-4">
      <PageHeader
        eyebrow="Settings"
        title="Hosts"
        description={
          <>
            The coding tools LemonCrow works with, and what it adds to each.
            Tools that have sent LemonCrow sessions are listed first. To connect
            a tool, use <IntegrationsLink />.
          </>
        }
      />
      {orderedHosts.length === 0 ? (
        <EmptyState
          title="No supported hosts found"
          description="LemonCrow couldn't load its list of coding tools."
        />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {orderedHosts.map((host) => (
            <Card key={host.host_id} className="bg-background/80 p-4">
              <div className="flex items-start gap-3">
                <span className="shrink-0">
                  <HostIcon id={host.host_id} label={host.label} />
                </span>
                <div className="min-w-0 flex-1 space-y-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <div className="text-title font-semibold text-foreground">
                      {host.label}
                    </div>
                    <HostSessionChip status={host.status} />
                    {HOST_SCOPE_BADGES[host.host_id] && (
                      <Chip tone="warning" className="normal-case tracking-normal">
                        {HOST_SCOPE_BADGES[host.host_id]}
                      </Chip>
                    )}
                  </div>
                  {host.description && (
                    <p className="text-heading text-muted-foreground">
                      {host.description}
                    </p>
                  )}
                  {host.install_command && (
                    <div className="space-y-1">
                      <div className="text-caption uppercase tracking-widest text-muted-foreground font-mono">
                        Install
                      </div>
                      <code className="block break-all border border-border bg-background px-2 py-1 text-caption text-subtle">
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

/**
 * A one-line summary of an agent. Its `description` is routing text for the model ("Always use for
 * read-only implementation planner."), so drop the leading instruction and keep the first sentence.
 */
export function agentSummary(description: string): string {
  const text = description
    .trim()
    .replace(/^(?:always\s+)?use\s+(?:this\s+agent\s+)?(?:for|when)\s+/i, "");
  const first = text.split(/(?<=[.!?])\s+(?=[A-Z])/)[0] ?? text;
  return first.charAt(0).toUpperCase() + first.slice(1);
}

export function AgentsSection() {
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => {
    api
      .agents()
      .then(setAgents)
      .catch((e) => console.error("Failed to load agents:", e));
  }, []);

  return (
    <section className="space-y-4">
      <PageHeader
        eyebrow="Settings"
        title="Agents"
        description="Specialist agents LemonCrow adds to Claude Code. Open one to see its full instructions."
      />
      {agents === null ? (
        <p className="text-body text-muted-foreground">Loading…</p>
      ) : (
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
      )}
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
  return (
    <DisclosureCard
      open={expanded}
      onToggle={onToggle}
      contentClassName="space-y-4"
      header={
        <div className="min-w-0">
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <ChevronRight
              size={14}
              aria-hidden="true"
              className={`shrink-0 text-muted-foreground transition-transform ${expanded ? "rotate-90" : ""}`}
            />
            <span className="font-mono text-heading font-semibold text-foreground">
              {agent.name}
            </span>
            {agent.model && (
              <Chip
                tone="neutral"
                className="normal-case tracking-normal text-caption"
              >
                {agent.model}
              </Chip>
            )}
          </div>
          <p className="text-body text-muted-foreground">
            {agentSummary(agent.description)}
          </p>
        </div>
      }
    >
      {/* Tools */}
      {agent.tools.length > 0 && (
        <div>
          <FieldLabel className="mb-2">
            <ChevronRight size={14} className="inline mr-1" /> tools
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
      )}

      {/* Content (markdown body) */}
      {agent.content && (
        <div>
          <FieldLabel className="mb-2">
            <ChevronRight size={14} className="inline mr-1" /> instructions
          </FieldLabel>
          <pre className="text-caption bg-background px-2 py-2 text-muted-foreground font-mono border border-border block overflow-auto max-h-48 whitespace-pre-wrap">
            {agent.content}
          </pre>
        </div>
      )}

      {/* Source */}
      <div className="pt-2 border-t border-border">
        <FieldLabel className="mb-2">Source</FieldLabel>
        <code className="text-caption bg-background px-2 py-1 text-muted-foreground font-mono border border-border block break-all">
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
  const [failed, setFailed] = useState(false);
  const [expandedSkill, setExpandedSkill] = useState<string | null>(null);

  useEffect(() => {
    api
      .skills()
      .then(setSkills)
      .catch((e) => {
        console.error("Failed to load skills:", e);
        setFailed(true);
      });
  }, []);

  const count = skills?.length;

  return (
    <section className="space-y-4">
      <PageHeader
        eyebrow="Settings"
        title="Skills"
        description={
          count === undefined
            ? "Skills LemonCrow adds to your coding tools."
            : `${count} ${count === 1 ? "skill" : "skills"} LemonCrow adds to your coding tools. Open one to read its instructions.`
        }
      />
      <div className="grid gap-2 sm:grid-cols-2">
        {skills === null ? (
          <EmptyState
            title={failed ? "Couldn't load skills" : "Loading skills…"}
            className="sm:col-span-2"
          />
        ) : skills.length === 0 ? (
          <EmptyState title="No skills installed" className="sm:col-span-2" />
        ) : (
          skills.map((s) => (
            <SkillCard
              key={s.name}
              skill={{ name: s.name, desc: s.description, icon: Check }}
              isExpanded={expandedSkill === s.name}
              onToggle={() =>
                setExpandedSkill(expandedSkill === s.name ? null : s.name)
              }
            />
          ))
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
  const contentId = `skill-instructions-${skill.name}`;

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

  const actionLabel = loading ? "Loading…" : isExpanded ? "Hide" : "Show";

  return (
    <Card className="flex flex-col gap-2 bg-muted/30 p-2">
      <UiButton unstyled
        onClick={toggle}
        aria-expanded={isExpanded}
        aria-controls={isExpanded && content ? contentId : undefined}
        title={`${isExpanded ? "Hide" : "Show"} the instructions for ${skill.name}`}
        className="flex items-start gap-2 w-full text-left"
      >
        <span className="mt-0.5" aria-hidden="true">
          <skill.icon size={14} className="text-ok" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-label font-mono font-medium text-foreground truncate">
            {skill.name}
          </div>
          <div className="text-caption text-muted-foreground leading-tight">
            {skill.desc}
          </div>
        </div>
        <span className="inline-flex shrink-0 items-center gap-1 text-caption text-muted-foreground">
          {actionLabel}
          {!loading &&
            (isExpanded ? (
              <Minus size={14} aria-hidden="true" />
            ) : (
              <Plus size={14} aria-hidden="true" />
            ))}
        </span>
      </UiButton>
      {isExpanded && content && (
        <div id={contentId} className="mt-1 pt-2 border-t border-border">
          <pre className="text-muted-foreground whitespace-pre-wrap font-mono max-h-60 overflow-y-auto bg-background/50 p-2 text-caption">
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
  code_search: "code_intel",
  grep: "retrieval",
  search: "retrieval",
  web_fetch: "retrieval",
  read: "file_io",
  edit: "file_io",
  bash: "execution",
  shell: "execution",
  sql: "execution",
  trace: "state",
  memory: "state",
  compact: "state",
};

// The plural shown on a tool's chip for its main enum parameter: "3 operations", not "3 ops".
const ENUM_PARAM_PLURAL: Record<string, string> = {
  op: "operations",
  action: "actions",
  type: "types",
};

function enumParamCount(name: string, count: number): string {
  if (count === 1) return `1 ${name === "op" ? "operation" : name}`;
  return `${count} ${ENUM_PARAM_PLURAL[name] ?? `${name}s`}`;
}

const NS_META: Record<
  string,
  { icon: React.ElementType; label: string; color: string }
> = {
  reasoning: {
    icon: Brain,
    label: "reasoning",
    color: "text-muted-foreground border-border/50 bg-background/10",
  },
  code_intel: {
    icon: Command,
    label: "code intel",
    color: "text-cyan-300 border-cyan-900/50 bg-cyan-950/10",
  },
  retrieval: {
    icon: Search,
    label: "retrieval",
    color: "text-cyan-300 border-cyan-900/50 bg-cyan-950/10",
  },
  file_io: {
    icon: HardDrive,
    label: "file i/o",
    color: "text-emerald-300 border-emerald-900/50 bg-emerald-950/10",
  },
  execution: {
    icon: Terminal,
    label: "execution",
    color: "text-amber-300 border-amber-900/50 bg-amber-950/10",
  },
  state: {
    icon: Archive,
    label: "state",
    color: "text-amber-300 border-amber-900/50 bg-amber-950/10",
  },
};

function canonicalName(name: string): string {
  return name.startsWith("lemoncrow_") ? name.slice("lemoncrow_".length) : name;
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

  if (err) return <Alert tone="destructive" description={err} />;

  return (
    <section className="space-y-4">
      <PageHeader
        eyebrow="Settings"
        title="MCP tools"
        description="The tools LemonCrow gives your coding agents, grouped by what they do. A count beside a tool is how many operations it offers; open the tool to see them."
      />
      {!mcpTools && <EmptyState title="Loading tools…" />}
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
              <p className="text-caption text-muted-foreground sm:col-span-2">
                {deduped.length} {deduped.length === 1 ? "tool" : "tools"}, from
                LemonCrow's MCP server (<code className="font-mono">lc mcp</code>).
              </p>
              {nsOrder
                .filter((ns) => groups[ns]?.length)
                .map((ns) => {
                  const meta = NS_META[ns] ?? {
                    icon: Circle,
                    label: ns,
                    color:
                      "text-muted-foreground border-border bg-muted/30",
                  };
                  const tools = groups[ns];
                  return (
                    <div key={ns}>
                      <div className="flex items-center gap-2 mb-2">
                        <meta.icon size={14} className="text-muted-foreground" />
                        <span className="text-caption uppercase tracking-widest font-mono text-muted-foreground">
                          {meta.label}
                        </span>
                        <span className="text-caption text-muted-foreground font-mono">
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
                                  className={`w-1.5 h-1.5 flex-shrink-0 ${tool.available ? "bg-emerald-400" : "bg-muted-foreground"}`}
                                />
                                <span className="font-mono font-semibold text-foreground text-body flex-1">
                                  {tool.tool_name}
                                </span>
                                {primaryEnum && (
                                  <span
                                    className="text-caption font-semibold text-cyan-300 border border-cyan-500/30 px-1 py-0.5 mr-2"
                                    title={`${tool.tool_name} takes one of these values for "${primaryEnum.name}". Open it to see them.`}
                                  >
                                    {enumParamCount(
                                      primaryEnum.name,
                                      primaryEnum.options.length
                                    )}
                                  </span>
                                )}
                                {isDev && (
                                  <span className="text-caption font-semibold text-amber-300 border border-amber-500/30 px-1 py-0.5 mr-2">
                                    DEV
                                  </span>
                                )}
                                {isDev && tool.mode === "passive" && (
                                  <span className="text-caption font-semibold text-muted-foreground border border-border px-1 py-0.5 mr-2">
                                    PASSIVE
                                  </span>
                                )}
                                <span className="text-muted-foreground">
                                  {isExpanded ? (
                                    <ChevronUp size={14} />
                                  ) : (
                                    <ChevronDown size={14} />
                                  )}
                                </span>
                              </div>
                              {isExpanded && (
                                <div className="px-4 pb-3 pt-1 border-t border-border/50">
                                  {cleanDescription ? (
                                    <p className="text-body text-subtle leading-relaxed">
                                      {cleanDescription}
                                    </p>
                                  ) : (
                                    <p className="text-body text-muted-foreground italic">
                                      No description available.
                                    </p>
                                  )}
                                  <div className="mt-2 flex items-center gap-3">
                                    <span
                                      className={`text-caption font-mono px-2 py-0.5 ${tool.available ? "bg-emerald-900/30 text-emerald-300" : "bg-accent text-muted-foreground"}`}
                                    >
                                      {tool.mode === "passive"
                                        ? "passive capture"
                                        : tool.available
                                          ? "available"
                                          : "unavailable"}
                                    </span>
                                    <code className="text-caption font-mono text-muted-foreground">
                                      {tool.tool_name}
                                    </code>
                                  </div>
                                  {tool.enum_params &&
                                    tool.enum_params.length > 0 && (
                                      <div className="mt-4 space-y-3">
                                        <FieldLabel className="mb-1">
                                          <ChevronRight
                                            size={14}
                                            className="inline mr-1"
                                          />{" "}
                                          enum params
                                        </FieldLabel>
                                        {tool.enum_params.map((param) => (
                                          <div
                                            key={`${tool.tool_name}-${param.name}`}
                                            className="space-y-2"
                                          >
                                            <div className="flex items-center gap-2 text-caption uppercase tracking-widest text-muted-foreground font-mono">
                                              <code className="border border-border bg-background px-1.5 py-0.5 text-subtle normal-case">
                                                {param.name}
                                              </code>
                                              <span>
                                                {param.options.length} values
                                              </span>
                                            </div>
                                            {param.description && (
                                              <p className="text-caption text-muted-foreground">
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
// Health section — daemon health + per-host adapter status. Also surfaced
// as a compact strip on Overview (see components/HealthStrip.tsx); this is
// the only other place it appears, per the approved IA.
// ---------------------------------------------------------------------------

function HealthSection() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthErr, setHealthErr] = useState(false);
  const [hosts, setHosts] = useState<HostAdapter[] | null>(null);

  useEffect(() => {
    Promise.allSettled([api.health(), api.hosts()]).then(([h, hs]) => {
      if (h.status === "fulfilled") setHealth(h.value);
      else setHealthErr(true);
      if (hs.status === "fulfilled") setHosts(hs.value);
    });
  }, []);

  const activeHosts = (hosts ?? []).filter((h) => h.status === "active");
  const serverValue = health
    ? health.status === "ok"
      ? "Running"
      : health.status
    : healthErr
      ? "Not answering"
      : "…";

  return (
    <section className="space-y-4">
      <PageHeader
        eyebrow="Settings"
        title="Diagnostics"
        description={
          <>
            Whether the local LemonCrow server is answering, and which coding
            tools have sent it sessions. A tool shows “No sessions yet” until
            LemonCrow has recorded a session from it; to connect one, use{" "}
            <IntegrationsLink />.
          </>
        }
      />
      <div className="grid gap-3 sm:grid-cols-2">
        <MetricCard
          label="Local server"
          value={serverValue}
          detail={health?.timestamp ? `Checked ${fmtDate(health.timestamp)}` : undefined}
          tone={
            health?.status === "ok" ? "success" : healthErr ? "destructive" : "neutral"
          }
        />
        <MetricCard
          label="Tools with sessions"
          value={hosts ? `${activeHosts.length} of ${hosts.length}` : "…"}
          tone="info"
        />
      </div>
      {hosts && hosts.length === 0 ? (
        <EmptyState title="No coding tools found" />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {(hosts ?? []).map((host) => (
            <Card key={host.host_id} className="p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="text-heading text-foreground">{host.label}</span>
                <HostSessionChip status={host.status} />
              </div>
              <div className="mt-1 text-caption text-muted-foreground">
                {host.last_import_at
                  ? `Last session imported ${fmtDate(host.last_import_at)} · ${host.imported_session_count ?? 0} sessions`
                  : "No sessions imported yet."}
              </div>
            </Card>
          ))}
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Main System page: /system/:section — Health → Hosts → Agents → Skills →
// MCP → Telemetry → Watchdogs → Projection
// ---------------------------------------------------------------------------

export type Section =
  | "health"
  | "hosts"
  | "agents"
  | "skills"
  | "mcp"
  | "telemetry"
  | "watchdogs"
  | "projection";

export default function System({ forcedSection, after }: { forcedSection?: Section; after?: React.ReactNode }) {
  const { section } = useParams<{ section?: string }>();
  const active = forcedSection ?? (section as Section) ?? "health";

  return (
    <PageFrame className="space-y-4 text-heading">
      {!forcedSection && (
        <UiButton asChild variant="ghost" size="sm" className="-ml-2 w-fit">
          <Link to="/settings/advanced"><ArrowLeft size={14} />Back to Advanced</Link>
        </UiButton>
      )}
      {active === "health" && <HealthSection />}
      {active === "hosts" && <HostsSection />}
      {active === "agents" && <AgentsSection />}
      {active === "skills" && <SkillsSection />}
      {active === "mcp" && <ToolsSection />}
      {active === "telemetry" && <Telemetry />}
      {/* Watchdogs are not built yet, so an old link lands on Advanced instead of a "Coming soon" page. */}
      {active === "watchdogs" && <Navigate to="/settings/advanced" replace />}
      {active === "projection" && (
        <section className="space-y-4">
          <PageHeader
            eyebrow="Developer tool"
            title="Read preview"
            description={
              <>
                Shows what LemonCrow's <code className="font-mono">read</code> tool
                gives a coding agent for one file. <em>compact</em> is what an agent
                gets by default, shortened where that saves tokens;{" "}
                <em>exact</em> is the file as written,{" "}
                <em>summary</em> caps the line count, and <em>range</em> returns
                only the lines you choose.
              </>
            }
          />
          <ProjectionInspector />
        </section>
      )}
      {after}
    </PageFrame>
  );
}
