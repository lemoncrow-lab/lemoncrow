import { ArrowDownLeft, ArrowUpRight, CircleDot, Expand, GitFork, Loader2 } from "lucide-react";
import FileTypeIcon from "../components/FileTypeIcon";
import type { CodeMapNode, CodeMapSymbol } from "../api";

function CallList({
  title,
  icon,
  nodes,
  onPick,
}: {
  title: string;
  icon: React.ReactNode;
  nodes: CodeMapNode[];
  onPick: (node: CodeMapNode) => void;
}) {
  return (
    <div>
      <div className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-neutral-400">
        {icon} {title} · {nodes.length}
      </div>
      <ul className="max-h-40 space-y-0.5 overflow-y-auto pr-1">
        {nodes.map((node) => (
          <li key={node.id}>
            <button
              type="button"
              onClick={() => onPick(node)}
              title={`${node.qualified_name} — ${node.path}:${node.line}`}
              className="flex w-full items-baseline gap-1.5 px-1.5 py-1 text-left text-[11px] text-neutral-300 transition hover:bg-neutral-900 hover:text-white"
            >
              <span className="truncate">{node.label}</span>
              <span className="ml-auto shrink-0 text-[10px] text-neutral-500">{node.path.split("/").pop()}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function CodeMapInspector({
  selectedNode,
  detail,
  callers,
  callees,
  incomingCount,
  outgoingCount,
  expandingId,
  onPick,
  onFocus,
}: {
  selectedNode: CodeMapNode | null;
  detail: CodeMapSymbol | null;
  callers: CodeMapNode[];
  callees: CodeMapNode[];
  incomingCount: number;
  outgoingCount: number;
  expandingId: string | null;
  onPick(node: CodeMapNode): void;
  onFocus(nodeId: string): void;
}) {
  if (!selectedNode) {
    return (
      <div className="p-6">
        <GitFork size={22} className="text-brand-300" />
        <h3 className="mt-4 text-sm font-semibold text-neutral-100">Explore the map</h3>
        <p className="mt-2 text-xs leading-5 text-neutral-300">
          Click any file or symbol. Double-click a symbol to focus its callers and callees.
        </p>
      </div>
    );
  }

  return (
    <div>
      <div className="border-b border-neutral-800 p-5">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 border border-cyan-500/30 bg-cyan-500/10 p-2 text-cyan-200">
            <CircleDot size={15} />
          </div>
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-widest text-neutral-400">
              {selectedNode.node_type || "symbol"} · {selectedNode.kind}
            </div>
            <h3 className="mt-1 break-words text-sm font-semibold text-neutral-100">
              {selectedNode.qualified_name || selectedNode.label}
            </h3>
          </div>
        </div>
        {selectedNode.node_type !== "file" && (
          <>
            <div className="mt-4 flex items-center gap-3 text-[11px] text-neutral-300">
              <span className="inline-flex items-center gap-1.5">
                <ArrowDownLeft size={13} className="text-violet-300" /> {incomingCount} callers
              </span>
              <span className="inline-flex items-center gap-1.5">
                <ArrowUpRight size={13} className="text-cyan-300" /> {outgoingCount} callees
              </span>
            </div>
            {(callers.length > 0 || callees.length > 0) && (
              <div className="mt-3 space-y-3">
                {callers.length > 0 && (
                  <CallList title="Callers" icon={<ArrowDownLeft size={11} className="text-violet-300" />} nodes={callers} onPick={onPick} />
                )}
                {callees.length > 0 && (
                  <CallList title="Callees" icon={<ArrowUpRight size={11} className="text-cyan-300" />} nodes={callees} onPick={onPick} />
                )}
              </div>
            )}
            <button
              type="button"
              onClick={() => onFocus(selectedNode.id)}
              disabled={expandingId !== null}
              className="mt-4 inline-flex items-center gap-2 border border-brand-500/50 bg-brand-500/10 px-3 py-2 text-xs text-brand-300 hover:bg-brand-500/20 disabled:opacity-60"
            >
              {expandingId === selectedNode.id ? <Loader2 size={13} className="animate-spin" /> : <Expand size={13} />} Focus callers + callees
            </button>
          </>
        )}
      </div>
      <div className="border-b border-neutral-800 p-5">
        <div className="flex items-start gap-2 text-xs text-neutral-300">
          <FileTypeIcon path={selectedNode.path} size={14} className="mt-0.5" />
          <span className="break-all">
            {selectedNode.path}{selectedNode.node_type !== "file" ? `:${selectedNode.line}–${selectedNode.end_line}` : ""}
          </span>
        </div>
        <div className="mt-3 flex flex-wrap gap-2 text-[10px] text-neutral-300">
          <span className="border border-neutral-700 px-2 py-1">{selectedNode.language || "Other"}</span>
          <span className="border border-neutral-700 px-2 py-1">{selectedNode.community || "root"}</span>
        </div>
      </div>
      <div className="p-4">
        <div className="mb-3 text-[10px] font-semibold uppercase tracking-widest text-neutral-300">
          {selectedNode.node_type === "file" ? "File node" : "Exact symbol source"}
        </div>
        {selectedNode.node_type === "file" ? (
          <p className="text-xs leading-5 text-neutral-300">
            File metadata is visible in the full map. Select one of its symbol nodes to open an exact source range.
          </p>
        ) : detail ? (
          <pre className="max-h-[430px] overflow-auto whitespace-pre border border-neutral-800 bg-surface-code p-3 text-[11px] leading-5 text-neutral-200">
            <code className="border-0 bg-transparent p-0 text-inherit">{detail.source || detail.signature || "Source is unavailable."}</code>
          </pre>
        ) : (
          <div className="flex items-center gap-2 text-xs text-neutral-400">
            <Loader2 size={13} className="animate-spin" /> Loading exact range…
          </div>
        )}
      </div>
    </div>
  );
}
