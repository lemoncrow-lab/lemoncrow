import { useEffect, useState } from "react";
import { api, type HostAdapter } from "../api";

const HOSTS = [
  {
    id: "claude",
    label: "Claude Code",
    icon: "🧩",
    desc: "Full plugin: agents + skills + MCP + hooks",
  },
  {
    id: "codex",
    label: "Codex",
    icon: "📋",
    desc: "MCP config + Codex savings/update hooks",
  },
  {
    id: "opencode",
    label: "OpenCode",
    icon: "🔌",
    desc: "OpenCode config + shared telemetry",
  },
  {
    id: "copilot",
    label: "Copilot",
    icon: "💼",
    desc: "MCP config + custom instructions + shared telemetry",
  },
  {
    id: "gemini",
    label: "Gemini CLI",
    icon: "📎",
    desc: ".gemini/settings.json MCP + shared telemetry",
  },
];

export default function Host() {
  const [hosts, setHosts] = useState<HostAdapter[]>([]);

  useEffect(() => {
    api
      .hosts()
      .then(setHosts)
      .catch(() => setHosts([]));
  }, []);

  return (
    <div className="space-y-4">
      <div className="text-[10px] uppercase tracking-widest text-neutral-500 font-mono">
        Supported hosts
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        {HOSTS.map((hostMeta) => {
          const status = hosts.find((host) => host.host_id === hostMeta.id);
          return (
            <div
              key={hostMeta.id}
              className="border border-neutral-800 bg-neutral-950/80 p-4"
            >
              <div className="flex items-start gap-3">
                <span className="text-2xl">{hostMeta.icon}</span>
                <div className="flex-1 min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <div className="text-base font-semibold text-neutral-100">
                      {hostMeta.label}
                    </div>
                    <span className="border border-neutral-700 px-2 py-0.5 text-[10px] uppercase tracking-widest text-neutral-400">
                      {status?.status ?? "not detected"}
                    </span>
                  </div>
                  <p className="mt-1 text-sm text-neutral-400">
                    {hostMeta.desc}
                  </p>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
