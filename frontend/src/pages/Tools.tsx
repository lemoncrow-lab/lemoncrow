import { useEffect, useState } from "react";
import { api, type MCPStatus } from "../api";
import { getTelemetryConfig, type TelemetryConfig } from "../lib/insightsApi";

// Namespace grouping — maps canonical tool names (without atelier_ prefix) to namespaces
const NS_MAP: Record<string, string> = {
  // brain — plan / validate / rescue / quality-gate
  reasoning: "brain",
  lint: "brain",
  route: "brain",
  rescue: "brain",
  verify: "brain",
  // capture — observability & recording
  trace: "capture",
  // storage — memory and retrieval
  memory: "storage",
  // files — file system operations
  read: "files",
  edit: "files",
  search: "files",
  sql: "files",
  // infra — context lifecycle
  compact: "infra",
};

const NS_META: Record<string, { icon: string; label: string; color: string }> =
  {
    brain: {
      icon: "🧠",
      label: "brain",
      color: "text-purple-400 border-purple-900/50 bg-purple-950/10",
    },
    capture: {
      icon: "📇",
      label: "capture",
      color: "text-amber-400 border-amber-900/50 bg-amber-950/10",
    },
    storage: {
      icon: "🗄️",
      label: "storage",
      color: "text-emerald-400 border-emerald-900/50 bg-emerald-950/10",
    },
    files: {
      icon: "📁",
      label: "files",
      color: "text-neutral-400 border-neutral-800 bg-neutral-900/30",
    },
    infra: {
      icon: "⚙️",
      label: "infra",
      color: "text-sky-400 border-sky-900/50 bg-sky-950/10",
    },
  };

function canonicalName(name: string): string {
  return name.startsWith("atelier_") ? name.slice("atelier_".length) : name;
}

export default function Tools() {
  const [mcpTools, setMcpTools] = useState<MCPStatus[] | null>(null);
  const [config, setConfig] = useState<TelemetryConfig | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [infoOpen, setInfoOpen] = useState(false);
  const [expandedTool, setExpandedTool] = useState<string | null>(null);

  useEffect(() => {
    api
      .mcp_status()
      .then(setMcpTools)
      .catch((e) => setErr(String(e)));

    getTelemetryConfig()
      .then(setConfig)
      .catch(() => undefined);
  }, []);

  if (err) return <div className="text-red-400">Error: {err}</div>;

  return (
    <div className="space-y-6">
      {/* Feature Info */}
      <div>
        <button
          onClick={() => setInfoOpen(!infoOpen)}
          className="text-[10px] text-neutral-600 hover:text-neutral-400 font-mono flex items-center gap-1 py-1"
        >
          <span>{infoOpen ? "▼" : "▶"}</span> about
        </button>
        {infoOpen && (
          <section className="border border-neutral-800 bg-neutral-900/50 p-5">
            <div className="flex items-start gap-4">
              <div className="text-3xl flex-shrink-0">🔌</div>
              <div className="flex-1">
                <div className="flex items-center gap-3 mb-2">
                  <h2 className="font-mono font-bold text-neutral-200 text-lg">
                    MCP Tools & Capabilities
                  </h2>
                  <span className="text-[10px] px-2 py-0.5 font-mono font-bold uppercase tracking-wide bg-emerald-900/30 text-emerald-300">
                    stable
                  </span>
                </div>
                <p className="font-mono text-[11px] text-neutral-500 mb-3">
                  Standard Protocol for Agent Integration
                </p>
                <p className="text-xs text-neutral-300 leading-relaxed mb-3">
                  Complete Model Context Protocol (MCP) server exposing all
                  Atelier capabilities: reasoning context, traces, rubric gates,
                  block extraction, and plan validation.
                  Standards-compliant—works with any MCP-compatible agent.
                </p>
                <div className="text-xs text-emerald-300/90 space-y-1">
                  <p>✓ Full Atelier API via standard MCP protocol</p>
                  <p>✓ Works with any MCP-compatible agent</p>
                  <p>✓ Stdio server for seamless integration</p>
                </div>
              </div>
            </div>
          </section>
        )}
      </div>

      <div className="space-y-6 text-sm">
        {!mcpTools && (
          <div className="text-neutral-500 text-xs">Loading tools…</div>
        )}
        {mcpTools &&
          (() => {
            // Deduplicate: prefer canonical name, skip atelier_* if canonical already present
            const seen = new Set<string>();
            const deduped: MCPStatus[] = [];
            for (const t of mcpTools) {
              const canonical = canonicalName(t.tool_name);
              if (!seen.has(canonical)) {
                seen.add(canonical);
                deduped.push({ ...t, tool_name: canonical });
              }
            }

            // Group by namespace; unknowns go to "other"
            const groups: Record<string, MCPStatus[]> = {};
            for (const t of deduped) {
              const ns = NS_MAP[t.tool_name] ?? "other";
              if (!groups[ns]) groups[ns] = [];
              groups[ns].push(t);
            }

            const nsOrder = [
              "brain",
              "capture",
              "storage",
              "infra",
              "files",
              "other",
            ];

            return (
              <div className="space-y-5">
                <p className="text-[10px] font-mono text-neutral-600">
                  {deduped.length} tools on stdio server:{" "}
                  <code>uv run atelier-mcp</code>
                </p>
                {nsOrder
                  .filter((ns) => groups[ns]?.length)
                  .map((ns) => {
                    const meta = NS_META[ns] ?? {
                      icon: "•",
                      label: ns,
                      color:
                        "text-neutral-400 border-neutral-800 bg-neutral-900/30",
                    };
                    return (
                      <div key={ns}>
                        <div className="flex items-center gap-2 mb-2">
                          <span className="text-sm">{meta.icon}</span>
                          <span className="text-[10px] uppercase tracking-widest font-mono text-neutral-500">
                            {meta.label}
                          </span>
                          <span className="text-[10px] text-neutral-700 font-mono">
                            ({groups[ns].length})
                          </span>
                        </div>
                        <div className="space-y-px">
                          {groups[ns]
                            .filter((tool) => {
                              const isDev =
                                !!tool.description &&
                                tool.description.startsWith("[DEV]");
                              return !isDev || config?.dev_mode;
                            })
                            .map((tool) => {
                              const isExpanded = expandedTool === tool.tool_name;
                              const desc = tool.description;
                              const isDev = !!desc && desc.startsWith("[DEV]");
                              const cleanDescription = isDev
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
                                  {isDev && (
                                    <span className="text-[8px] font-bold text-amber-500/60 border border-amber-500/30 px-1 py-0.5 mr-2">
                                      DEV
                                    </span>
                                  )}
                                  <span className="text-[10px] text-neutral-600">
                                    {isExpanded ? "▲" : "▼"}
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
                                        {tool.available
                                          ? "available"
                                          : "unavailable"}
                                      </span>
                                      <code className="text-[10px] font-mono text-neutral-600">
                                        {tool.tool_name}
                                      </code>
                                    </div>
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
      </div>
    </div>
  );
}
