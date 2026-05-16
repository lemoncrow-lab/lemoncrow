import { useEffect, useMemo, useState } from "react";
import {
  api,
  type CommandRecord,
  type ConversationEntry,
  type SessionAttachment,
  type ToolCall,
} from "../../api";
import { cx } from "../../components/WorkbenchUI";
import { getFileEditInfo, InlineFileDiff, SideBySideDiff } from "./DiffView";
import { fmtUsd, parseAt, LONG_OUTPUT_THRESHOLD, getNormName } from "./helpers";

const TEXT_EXTENSIONS = new Set([
  "c",
  "cc",
  "cpp",
  "css",
  "go",
  "h",
  "html",
  "java",
  "js",
  "json",
  "jsx",
  "md",
  "py",
  "rb",
  "rs",
  "sh",
  "sql",
  "toml",
  "ts",
  "tsx",
  "txt",
  "xml",
  "yaml",
  "yml",
]);

const GENERIC_MAIN_ARTIFACT_LABELS = new Set([
  "events.jsonl",
  "messages.jsonl",
  "transcript.jsonl",
  "workspace.yaml",
]);

function safeJson(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  try {
    return JSON.stringify(value, null, 2) ?? "";
  } catch {
    return String(value);
  }
}

function pathExtension(path: string | undefined): string {
  if (!path) return "";
  const last = path.split("/").pop() || "";
  const bits = last.split(".");
  return bits.length > 1 ? bits[bits.length - 1].toLowerCase() : "";
}

function isImageAttachment(attachment: SessionAttachment): boolean {
  return (
    attachment.type === "image" ||
    attachment.mime_type?.startsWith("image/") === true ||
    ["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"].includes(
      pathExtension(attachment.path)
    )
  );
}

function isVideoAttachment(attachment: SessionAttachment): boolean {
  return (
    attachment.mime_type?.startsWith("video/") === true ||
    ["mp4", "mov", "webm", "mkv", "avi"].includes(pathExtension(attachment.path))
  );
}

function isTextAttachment(attachment: SessionAttachment): boolean {
  if (attachment.content) return true;
  if (attachment.type === "selection" || attachment.type === "pasted_content")
    return true;
  if (attachment.mime_type?.startsWith("text/")) return true;
  if (
    attachment.mime_type &&
    [
      "application/json",
      "application/javascript",
      "application/typescript",
      "application/xml",
    ].includes(attachment.mime_type)
  ) {
    return true;
  }
  return TEXT_EXTENSIONS.has(pathExtension(attachment.path));
}

function turnKindLabel(turn: ConversationEntry): string {
  switch (turn.kind) {
    case "user_message":
      return "User Prompt";
    case "agent_message":
      return "Assistant";
    case "thinking":
      return "Reasoning";
    case "todo_write":
      return "Todo Write";
    case "attachment":
      return "Attachment";
    case "pasted_content":
      return "Pasted Context";
    case "subagent_event":
      return "Subagent";
    case "shell_command":
      return "Shell";
    case "file_edit":
      return "File Edit";
    default:
      return turn.kind.replaceAll("_", " ");
  }
}

function shouldShowArtifactLabel(turn: ConversationEntry): boolean {
  if (!turn.artifact_label) return false;
  if (turn.source_scope === "subagent") return true;
  return !GENERIC_MAIN_ARTIFACT_LABELS.has(turn.artifact_label);
}

function ModelPill({ model }: { model: string }) {
  return (
    <span className="max-w-full truncate border border-sky-900/30 bg-sky-950/25 px-2 py-0.5 text-[9px] normal-case tracking-normal text-sky-200">
      {model}
    </span>
  );
}

function TextBlock({
  text,
  className,
  forceExpand,
}: {
  text: string;
  className?: string;
  forceExpand: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const isLong = text.length > LONG_OUTPUT_THRESHOLD;
  const showFull = forceExpand || expanded || !isLong;

  return (
    <div className={cx("relative", className)}>
      <div className={cx("whitespace-pre-wrap", !showFull && "max-h-40 overflow-hidden")}>
        {text}
      </div>
      {!showFull && (
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-[#0a0a0a] to-transparent" />
      )}
      {isLong && !forceExpand && (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="mt-4 border border-neutral-800 px-2.5 py-1 text-[9px] font-mono uppercase tracking-[0.2em] text-neutral-400 transition hover:border-neutral-600 hover:text-neutral-200"
        >
          {expanded ? "Collapse ▲" : "Expand ▼"}
        </button>
      )}
    </div>
  );
}

function TodoCard({
  turn,
}: {
  turn: ConversationEntry;
}) {
  const todos = turn.todos ?? [];
  return (
    <div className="w-full border border-amber-900/30 bg-amber-950/[0.08] p-5 shadow-2xl">
      <div className="mb-4 flex items-center justify-between gap-3 border-b border-amber-900/20 pb-3">
        <div>
          <div className="text-[10px] font-mono uppercase tracking-[0.3em] text-amber-500/80">
            Task List
          </div>
          <div className="mt-1 text-sm font-semibold text-amber-100">
            {turn.summary}
          </div>
        </div>
        <div className="text-[11px] font-mono font-bold text-amber-400">
          {todos.length} item{todos.length === 1 ? "" : "s"}
        </div>
      </div>
      <div className="space-y-2">
        {todos.map((todo, index) => (
          <div
            key={`${todo.content}-${index}`}
            className="grid gap-2 border border-amber-900/15 bg-black/20 px-3 py-2 md:grid-cols-[1fr_auto]"
          >
            <div className="text-sm leading-6 text-neutral-100">{todo.content}</div>
            <div className="flex flex-wrap items-center gap-2 md:justify-end">
              {todo.priority && (
                <span className="border border-red-900/30 bg-red-950/20 px-2 py-0.5 text-[9px] font-mono uppercase tracking-[0.2em] text-red-300">
                  {todo.priority}
                </span>
              )}
              {todo.status && (
                <span className="border border-amber-900/30 bg-amber-950/20 px-2 py-0.5 text-[9px] font-mono uppercase tracking-[0.2em] text-amber-200">
                  {todo.status}
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function AttachmentPreview({
  attachment,
  forceExpand,
}: {
  attachment: SessionAttachment;
  forceExpand: boolean;
}) {
  const [remoteContent, setRemoteContent] = useState<string | null>(
    attachment.content ?? null
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileUrl = attachment.path ? api.fileContentUrl(attachment.path) : null;
  const wantsTextPreview =
    remoteContent === null && Boolean(attachment.path) && isTextAttachment(attachment);

  useEffect(() => {
    if (!wantsTextPreview || !fileUrl) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(fileUrl)
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`${response.status} ${response.statusText}`);
        }
        return response.text();
      })
      .then((text) => {
        if (!cancelled) {
          setRemoteContent(text);
          setLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [fileUrl, wantsTextPreview]);

  if (fileUrl && isImageAttachment(attachment)) {
    return (
      <img
        src={fileUrl}
        alt={attachment.display_name || attachment.title || "attachment"}
        className="max-h-[28rem] w-full rounded-sm border border-neutral-800 object-contain"
      />
    );
  }

  if (fileUrl && isVideoAttachment(attachment)) {
    return (
      <video
        src={fileUrl}
        controls
        className="max-h-[28rem] w-full rounded-sm border border-neutral-800 bg-black"
      />
    );
  }

  if (loading) {
    return (
      <div className="text-[10px] font-mono uppercase tracking-[0.2em] text-neutral-500">
        Loading preview...
      </div>
    );
  }

  if (error) {
    return <div className="text-xs text-red-400">{error}</div>;
  }

  if (remoteContent) {
    return (
      <div className="border border-neutral-800 bg-black/30 p-3 text-[11px] font-mono text-neutral-300">
        <TextBlock text={remoteContent} forceExpand={forceExpand} />
      </div>
    );
  }

  return null;
}

function AttachmentCard({
  turn,
  forceExpand,
}: {
  turn: ConversationEntry;
  forceExpand: boolean;
}) {
  const attachments = turn.attachments ?? [];
  return (
    <div className="w-full space-y-3 border border-cyan-900/20 bg-cyan-950/[0.05] p-5 shadow-2xl">
      <div className="flex items-center justify-between gap-3 border-b border-cyan-900/15 pb-3">
        <div>
          <div className="text-[10px] font-mono uppercase tracking-[0.3em] text-cyan-400/80">
            {turn.kind === "pasted_content" ? "Pasted Context" : "Attached Context"}
          </div>
          <div className="mt-1 text-sm font-semibold text-neutral-100">
            {turn.summary}
          </div>
        </div>
        <div className="text-[11px] font-mono font-bold text-cyan-300">
          {attachments.length} asset{attachments.length === 1 ? "" : "s"}
        </div>
      </div>
      {attachments.map((attachment, index) => {
        const fileUrl = attachment.path ? api.fileContentUrl(attachment.path) : null;
        return (
          <div
            key={`${attachment.path || attachment.display_name || attachment.title || attachment.type}-${index}`}
            className="space-y-3 border border-neutral-800 bg-black/20 p-4"
          >
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-xs font-semibold text-neutral-100">
                  {attachment.display_name || attachment.title || attachment.type}
                </div>
                {attachment.path && (
                  <div className="mt-1 truncate font-mono text-[10px] text-neutral-500">
                    {attachment.path}
                  </div>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="border border-cyan-900/20 bg-cyan-950/20 px-2 py-0.5 text-[9px] font-mono uppercase tracking-[0.2em] text-cyan-200">
                  {attachment.type}
                </span>
                {attachment.size_label && (
                  <span className="text-[10px] font-mono text-neutral-500">
                    {attachment.size_label}
                  </span>
                )}
                {attachment.line_count !== undefined && (
                  <span className="text-[10px] font-mono text-neutral-500">
                    {attachment.line_count} lines
                  </span>
                )}
                {fileUrl && (
                  <a
                    href={fileUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="border border-neutral-800 px-2 py-0.5 text-[9px] font-mono uppercase tracking-[0.2em] text-neutral-400 transition hover:border-cyan-500/40 hover:text-cyan-200"
                  >
                    Open
                  </a>
                )}
              </div>
            </div>
            <AttachmentPreview attachment={attachment} forceExpand={forceExpand} />
          </div>
        );
      })}
    </div>
  );
}

function SubagentCard({ turn }: { turn: ConversationEntry }) {
  const status = (turn.subagent_status || "started").toUpperCase();
  return (
    <div className="w-full border border-violet-900/25 bg-violet-950/[0.06] p-5 shadow-2xl">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-violet-900/15 pb-3">
        <div>
          <div className="text-[10px] font-mono uppercase tracking-[0.3em] text-violet-400/80">
            Subagent Lifecycle
          </div>
          <div className="mt-1 text-sm font-semibold text-neutral-100">
            {turn.subagent_name || turn.summary}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {turn.model && <ModelPill model={turn.model} />}
          <span className="border border-violet-900/25 bg-violet-950/25 px-2 py-0.5 text-[10px] font-mono uppercase tracking-[0.2em] text-violet-200">
            {status}
          </span>
        </div>
      </div>
      {(turn.subagent_description || turn.content) && (
        <div className="mt-4 whitespace-pre-wrap text-sm leading-6 text-neutral-300">
          {turn.subagent_description || turn.content}
        </div>
      )}
      {turn.subagent_id && (
        <div className="mt-4 font-mono text-[10px] uppercase tracking-[0.2em] text-neutral-500">
          {turn.subagent_id}
        </div>
      )}
    </div>
  );
}

function ToolConversationCard({
  turn,
  forceExpand,
}: {
  turn: ConversationEntry;
  forceExpand: boolean;
}) {
  const argsText = useMemo(() => safeJson(turn.arguments), [turn.arguments]);
  const showContent = Boolean(
    turn.content && turn.content.trim() && turn.content.trim() !== argsText.trim()
  );

  return (
    <div className="w-full space-y-4 border border-neutral-800/70 bg-[#0d0d0d] p-5 shadow-2xl">
      <div className="flex items-center justify-between gap-3 border-b border-neutral-800/60 pb-3">
        <div>
          <div className="text-[10px] font-mono uppercase tracking-[0.3em] text-neutral-500">
            Tool Call
          </div>
          <div className="mt-1 text-sm font-semibold text-neutral-100">
            {turn.tool_name || getNormName(turn)}
          </div>
        </div>
      </div>
      {turn.arguments !== undefined && (
        <div className="space-y-2">
          <div className="text-[9px] font-mono uppercase tracking-[0.2em] text-neutral-500">
            Arguments
          </div>
          <div className="border border-neutral-800 bg-black/30 p-3 text-[11px] font-mono text-neutral-300">
            <TextBlock text={argsText} forceExpand={forceExpand} />
          </div>
        </div>
      )}
      {showContent && (
        <div className="space-y-2">
          <div className="text-[9px] font-mono uppercase tracking-[0.2em] text-neutral-500">
            Result
          </div>
          <div className="border border-neutral-800 bg-black/30 p-3 text-[11px] font-mono text-neutral-300">
            <TextBlock text={turn.content || ""} forceExpand={forceExpand} />
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ConversationTurn
// ---------------------------------------------------------------------------

export function ConversationTurn({
  turn,
  forceExpand,
}: {
  turn: ConversationEntry;
  forceExpand: boolean;
}) {
  const isUser = turn.kind === "user_message";
  const isAgent = turn.kind === "agent_message";
  const isThinking = turn.kind === "thinking";
  const isTodo = turn.kind === "todo_write" && (turn.todos?.length ?? 0) > 0;
  const isAttachment =
    turn.kind === "attachment" ||
    turn.kind === "pasted_content" ||
    (turn.attachments?.length ?? 0) > 0;
  const isSubagent = turn.kind === "subagent_event";
  const isTool = !isUser && !isAgent && !isThinking;
  const [internalExpanded, setInternalExpanded] = useState(false);
  const isLong = (turn.content?.length || 0) > LONG_OUTPUT_THRESHOLD;
  const isExpanded = forceExpand || internalExpanded || !isLong;
  const toolDisplayName = turn.tool_name || getNormName(turn);
  const fileEditInfo = isTool ? getFileEditInfo(turn) : null;
  const showArtifactLabel = shouldShowArtifactLabel(turn);

  return (
    <div
      className={cx(
        "group relative flex flex-col space-y-4",
        isUser ? "items-end pl-24" : "items-start pr-24"
      )}
    >
      <div
        className={cx(
          "flex w-full items-center justify-between px-2 text-[10px] font-mono font-bold uppercase tracking-widest",
          isUser ? "flex-row-reverse text-right" : "flex-row"
        )}
      >
        <div
          className={cx(
            "flex min-w-0 flex-wrap items-center gap-2",
            isUser ? "justify-end" : "justify-start"
          )}
        >
          <span
            className={cx(
              "border-[0.5px] px-2 py-0.5",
              isUser
                ? "border-emerald-500/20 bg-emerald-500/[0.03] text-emerald-500"
                : isAgent
                  ? "border-violet-500/20 bg-violet-500/[0.03] text-violet-500"
                  : isThinking
                    ? "border-cyan-500/20 bg-cyan-500/[0.03] text-cyan-500"
                    : "border-neutral-800 bg-neutral-900/50 text-neutral-500"
            )}
          >
            {turnKindLabel(turn)}
          </span>
          {toolDisplayName && isTool && !isTodo && !isSubagent && (
            <span className="truncate text-[11px] normal-case tracking-normal text-amber-400/80">
              {toolDisplayName}
            </span>
          )}
          {turn.model && <ModelPill model={turn.model} />}
          {turn.count && turn.count > 1 && (
            <span className="border border-neutral-800 bg-neutral-900/60 px-2 py-0.5 text-[9px] text-neutral-400">
              ×{turn.count}
            </span>
          )}
          {turn.source_scope === "subagent" && (
            <span className="border border-violet-900/20 bg-violet-950/20 px-2 py-0.5 text-[9px] text-violet-300">
              subagent
            </span>
          )}
          {showArtifactLabel && (
            <span className="truncate text-[10px] font-normal normal-case tracking-normal text-neutral-500">
              source · {turn.artifact_label}
            </span>
          )}
        </div>

        <div className="flex items-center gap-3">
          {turn.cost && turn.cost > 0 && (
            <div className="text-[9px] font-black tracking-tight text-red-500">
              - {fmtUsd(turn.cost)}
            </div>
          )}
          <span className="font-normal tracking-tight text-neutral-500 transition-colors group-hover:text-neutral-300">
            {parseAt(turn.at)?.toLocaleTimeString() ?? ""}
          </span>
        </div>
      </div>

      {fileEditInfo ? (
        fileEditInfo.oldString !== undefined ? (
          <SideBySideDiff
            path={fileEditInfo.path}
            oldString={fileEditInfo.oldString}
            newString={fileEditInfo.newString ?? ""}
            forceExpand={forceExpand}
          />
        ) : (
          <InlineFileDiff
            path={fileEditInfo.path}
            diff={fileEditInfo.diff}
            forceExpand={forceExpand}
          />
        )
      ) : isTodo ? (
        <TodoCard turn={turn} />
      ) : isAttachment ? (
        <AttachmentCard turn={turn} forceExpand={forceExpand} />
      ) : isSubagent ? (
        <SubagentCard turn={turn} />
      ) : isTool && (turn.arguments !== undefined || turn.tool_name) ? (
        <ToolConversationCard turn={turn} forceExpand={forceExpand} />
      ) : (
        <div
          className={cx(
            "max-w-full border-[0.5px] shadow-2xl transition-all duration-500",
            isUser
              ? "border-emerald-900/20 bg-emerald-950/[0.03] p-6 text-neutral-100"
              : isAgent
                ? "border-violet-900/20 bg-violet-950/[0.03] p-8 text-neutral-200"
                : isThinking
                  ? "border-cyan-900/10 border-l-2 border-l-cyan-600/40 bg-black/20 p-6"
                  : "w-full border-neutral-800/60 bg-[#0d0d0d] p-6"
          )}
        >
          <div
            className={cx(
              isThinking
                ? "text-xs italic leading-relaxed tracking-tight text-cyan-200/40"
                : isUser || isAgent
                  ? "text-sm leading-7 text-neutral-200"
                  : "font-mono text-xs leading-relaxed tracking-tight text-neutral-300"
            )}
          >
            {!turn.content && (
              <div className="mb-4 border-b border-neutral-800/40 pb-3 font-mono text-[11px] font-bold uppercase tracking-wide text-neutral-100/90">
                {turn.summary}
              </div>
            )}
            {turn.content && (
              <div
                className={cx(
                  !isExpanded && "max-h-32 overflow-hidden",
                  isUser || isAgent ? "whitespace-pre-wrap" : "whitespace-pre-wrap"
                )}
              >
                {turn.content}
                {!isExpanded && (
                  <div className="pointer-events-none absolute bottom-0 left-0 h-16 w-full bg-gradient-to-t from-[#0a0a0a] to-transparent" />
                )}
              </div>
            )}
            {isLong && !forceExpand && (
              <button
                type="button"
                onClick={() => setInternalExpanded((value) => !value)}
                className="mt-6 border border-neutral-800 bg-neutral-900/50 px-3 py-1 text-[9px] font-black uppercase tracking-[0.2em] text-neutral-500 transition-all hover:border-neutral-500 hover:text-neutral-200"
              >
                {internalExpanded ? "Shrink ▲" : `Expand (${turn.content?.length || 0} chars) ▼`}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ToolCallDetail
// ---------------------------------------------------------------------------

export function ToolCallDetail({
  tool,
  forceExpand,
}: {
  tool: ToolCall;
  forceExpand: boolean;
}) {
  const [internalExpanded, setInternalExpanded] = useState(false);
  const expanded = forceExpand || internalExpanded;
  return (
    <div className="overflow-hidden rounded-none border border-neutral-800 bg-[#0d0d0d] group/tool">
      <button
        onClick={() => setInternalExpanded(!internalExpanded)}
        className="w-full p-4 text-left transition-all hover:bg-neutral-800/20"
      >
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-4">
            <span className="bg-blue-500/10 px-2 py-0.5 text-[8px] font-black uppercase tracking-[0.2em] text-blue-500 border border-blue-500/20">
              SYSTEM_TOOL
            </span>
            <span className="text-xs font-mono font-bold tracking-wide text-neutral-400 transition-colors group-hover/tool:text-blue-400">
              {tool.count > 1 && (
                <span className="mr-2 text-blue-500/80">{tool.count}x</span>
              )}
              {tool.name}
            </span>
          </div>
          <span className="text-[9px] font-black uppercase tracking-widest text-neutral-500 transition-colors group-hover/tool:text-neutral-300">
            {expanded ? "Collapse ▲" : "Inspect ▼"}
          </span>
        </div>
      </button>
      {expanded && (
        <div className="space-y-6 border-t border-neutral-800/60 bg-black/60 p-6 shadow-[inset_0_2px_10px_rgba(0,0,0,0.5)]">
          {tool.args && (
            <div className="space-y-2">
              <div className="text-[8px] font-black uppercase tracking-[0.3em] text-neutral-500">
                Parameters
              </div>
              <pre className="overflow-x-auto whitespace-pre-wrap border border-neutral-800/40 bg-[#080808] p-4 text-[10px] font-mono text-neutral-500">
                {JSON.stringify(tool.args, null, 2)}
              </pre>
            </div>
          )}
          {tool.result_summary && (
            <div className="space-y-2">
              <div className="text-[8px] font-black uppercase tracking-[0.3em] text-neutral-500">
                Outcome
              </div>
              <pre className="overflow-x-auto whitespace-pre-wrap border border-neutral-800/40 bg-[#080808] p-4 text-[10px] font-mono text-emerald-500/60">
                {tool.result_summary}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// CommandDetail
// ---------------------------------------------------------------------------

export function CommandDetail({
  command,
  forceExpand,
}: {
  command: string | CommandRecord;
  forceExpand: boolean;
}) {
  const [internalExpanded, setInternalExpanded] = useState(false);
  const expanded = forceExpand || internalExpanded;
  const isObj = typeof command !== "string";
  const text = isObj ? command.command : command;
  const rc = isObj ? command.exit_code : null;
  const ok = rc === 0 || rc === null;

  return (
    <div className="overflow-hidden rounded-none border border-neutral-800 bg-[#0d0d0d] group/cmd">
      <button
        onClick={() => setInternalExpanded(!internalExpanded)}
        className="w-full p-4 text-left transition-all hover:bg-neutral-800/20"
      >
        <div className="flex items-center justify-between gap-4">
          <div className="flex min-w-0 flex-1 items-center gap-4">
            <span className="border border-amber-500/20 bg-amber-500/10 px-2 py-0.5 text-[8px] font-black uppercase tracking-[0.2em] text-amber-600">
              SHELL_CMD
            </span>
            <span className="truncate font-mono text-xs text-amber-200/80 transition-colors group-hover/cmd:text-amber-100">
              $ {text}
            </span>
          </div>
          <div className="ml-4 flex items-center gap-6">
            {isObj && (
              <span
                className={cx(
                  "text-[10px] font-black tracking-widest font-mono",
                  ok ? "text-emerald-600" : "text-red-600"
                )}
              >
                EXIT_{rc ?? "?"}
              </span>
            )}
            <span className="text-[9px] font-black uppercase tracking-widest text-neutral-500 transition-colors group-hover/cmd:text-neutral-300">
              {expanded ? "Hide ▲" : "Logs ▼"}
            </span>
          </div>
        </div>
      </button>
      {expanded && isObj && (command.stdout || command.stderr) && (
        <div className="space-y-4 border-t border-neutral-800/60 bg-black/60 p-6 text-[10px] font-mono shadow-[inset_0_2px_10px_rgba(0,0,0,0.5)]">
          {command.stdout && (
            <div className="space-y-2">
              <div className="text-[8px] font-black uppercase tracking-[0.3em] text-neutral-500">
                stdout
              </div>
              <pre className="whitespace-pre-wrap border border-neutral-800/30 bg-[#080808] p-3 text-neutral-500">
                {command.stdout}
              </pre>
            </div>
          )}
          {command.stderr && (
            <div className="space-y-2">
              <div className="text-[8px] font-black uppercase tracking-[0.3em] text-red-700">
                stderr
              </div>
              <pre className="whitespace-pre-wrap border border-red-900/20 bg-red-950/[0.05] p-3 text-red-500/50">
                {command.stderr}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
