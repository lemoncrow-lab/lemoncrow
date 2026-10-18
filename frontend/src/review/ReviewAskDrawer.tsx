import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";
import { Textarea as UiTextarea } from "../components/ui/textarea";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu";
import { ArrowUp, Check, ChevronDown, Copy, Cpu, FileCode2, FileText, History, LoaderCircle, Plus, RotateCcw, Send, Sparkles, Square, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import { createReviewAskConversation, fetchReviewAsk, reviewHref, sendReviewAskHandoff, streamReviewAsk, uploadReviewEvidence, type ReviewAskStreamEvent } from "./reviewApi";
import { askDraftKey, loadAskDraft, saveAskDraft } from "./reviewDraftPersistence";
import { activeReviewFriction } from "./reviewFriction";
import type {
  ReviewAskDriver,
  ReviewAskConversation,
  ReviewAskConversationOrigin,
  ReviewAskConversationSummary,
  ReviewAskResult,
  ReviewAskHandoffResult,
  ReviewAskInput,
  ReviewAskMessage,
  ReviewAskReference,
  ReviewAskScope,
  ReviewEvidence,
  ReviewMapSelection,
} from "./types";

export interface ReviewAskSelection {
  context?: ReviewMapSelection;
  label?: string;
  target_unit_key?: string;
  path?: string;
  symbol?: string;
  side?: "new" | "old" | "additions" | "deletions";
  start_line?: number;
  end_line?: number;
  selected_text?: string;
}

interface PendingAskTurn {
  question: string;
  driver: ReviewAskDriver;
  model: string;
  mentionedPaths: string[];
  attachments: ReviewEvidence[];
  status: "starting" | "working" | "failed";
  elapsedMs: number;
  error: string;
  answer: string;
}

export interface AskTransport {
  load(conversationId?: string): Promise<ReviewAskConversation>;
  startConversation?(origin: ReviewAskConversationOrigin): Promise<ReviewAskConversationSummary>;
  stream(input: ReviewAskInput, onEvent: (event: ReviewAskStreamEvent) => void, signal?: AbortSignal): Promise<ReviewAskResult>;
  allowedDrivers?: ReviewAskDriver[];
  upload?(file: File): Promise<ReviewEvidence>;
}

/**
 * A turn the reviewer stopped stays in the conversation as local-only messages
 * with whatever answer had streamed; ids carry this prefix so they are told
 * apart from durable server messages and survive a conversation reload.
 */
const STOPPED_TURN_PREFIX = "local-stopped:";
const LOCAL_TURN_SCOPE: ReviewAskScope = { target_unit_key: "", path: "", symbol: "", side: "", start_line: 0, end_line: 0 };

function isStoppedTurn(message: ReviewAskMessage): boolean {
  return message.id.startsWith(STOPPED_TURN_PREFIX);
}

interface ReviewAskDrawerProps {
  reviewId: string;
  revisionNumber: number;
  baseRevisionNumber?: number;
  latestRevisionNumber: number;
  revisionId?: string;
  filePaths?: string[];
  scope?: ReviewAskSelection | null;
  onClearScope(): void;
  onClose(): void;
  onSwitchLatest(): void;
  onOpenReference(reference: ReviewAskReference): void;
  /** A live Code worktree uses the same conversation UI with its own transport. */
  transport?: AskTransport;
}

const SUGGESTIONS = [
  "Walk me through this change",
  "What should I review carefully?",
  "Could this be simpler?",
];
const CODE_SUGGESTIONS = ["Explain this code", "What should I check here?", "Could this be simpler?"];

const ASK_DRIVER_KEY = "lemoncrow.review.ask.driver";
const ASK_MODEL_KEY = "lemoncrow.review.ask.model";

const ASK_ATTACHMENT_MAX_BYTES = 1024 * 1024;
const ASK_LARGE_PASTE_THRESHOLD = 8_000;
const ASK_ATTACHMENT_EXTENSIONS = new Set([
  ".txt", ".md", ".mdx", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".xml", ".csv", ".tsv",
  ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".kts", ".scala",
  ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".cs", ".rb", ".php", ".swift", ".sql",
  ".sh", ".bash", ".zsh", ".fish", ".ps1", ".html", ".css", ".scss", ".less", ".vue", ".svelte",
  ".graphql", ".gql", ".proto", ".diff", ".patch", ".log", ".ini", ".cfg", ".conf",
]);

function askAttachmentSupported(file: File): boolean {
  if (file.type.startsWith("text/")) return true;
  if (["application/json", "application/xml", "application/yaml", "application/x-yaml", "application/toml"].includes(file.type)) return true;
  const dot = file.name.lastIndexOf(".");
  return dot >= 0 && ASK_ATTACHMENT_EXTENSIONS.has(file.name.slice(dot).toLowerCase());
}

interface MentionDraft {
  start: number;
  end: number;
  query: string;
}

function fileName(path: string): string {
  const parts = path.split("/");
  return parts.at(-1) || path;
}

function mentionDraftAt(value: string, cursor: number): MentionDraft | null {
  const before = value.slice(0, Math.max(0, cursor));
  const match = /(?:^|\s)@([^\s@]*)$/.exec(before);
  if (!match) return null;
  const query = match[1] ?? "";
  return {
    start: before.length - query.length - 1,
    end: before.length,
    query,
  };
}

const DRIVER_LABELS: Record<ReviewAskDriver, string> = {
  configured: "Default",
  claude: "Claude",
  codex: "Codex",
  opencode: "OpenCode",
  ollama: "Ollama",
  openai: "OpenAI",
  litellm: "LiteLLM",
};

function defaultDriverModels(driver: ReviewAskDriver, models: string[]): string[] {
  if (driver === "claude") return ["sonnet", "opus", "haiku"];
  if (driver === "codex") return [];
  if (driver === "opencode") return models.filter((item) => item.startsWith("opencode/") || item.startsWith("ollama/"));
  if (driver === "ollama") return models.filter((item) => item.startsWith("ollama/"));
  if (driver === "litellm") return models;
  return [];
}

function AskPicker({
  label,
  value,
  options,
  onChange,
  align = "left",
}: {
  label: string;
  value: string;
  options: Array<{ value: string; label: string }>;
  onChange(value: string): void;
  align?: "left" | "right";
}) {
  const selected = options.find((option) => option.value === value)?.label ?? value;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <UiButton
          type="button"
          variant="outline"
          size="xs"
          aria-label={label}
          className="h-7 max-w-[150px] gap-1.5 rounded-full border-border bg-muted/70 px-2.5 font-medium text-neutral-300 hover:bg-muted hover:text-foreground"
        >
          <Cpu size={11} className="shrink-0 text-muted-foreground" />
          <span className="truncate">{selected}</span>
          <ChevronDown size={11} className="shrink-0 text-muted-foreground" />
        </UiButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align={align === "right" ? "end" : "start"} className="min-w-[190px] max-w-[280px]">
        <DropdownMenuRadioGroup value={value} onValueChange={onChange}>
          {options.map((option) => (
            <DropdownMenuRadioItem key={option.value} value={option.value}>
              <span className="min-w-0 flex-1 truncate">{option.label}</span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function rangeLabel(start = 0, end = 0): string {
  if (start <= 0) return "";
  return start === end || end <= 0 ? `line ${start}` : `lines ${start}–${end}`;
}

function scopeLabel(scope: ReviewAskSelection | null | undefined): string {
  if (scope?.context) {
    const selection = scope.context;
    const subject = scope.label || selection.symbol || selection.path || selection.relation || (selection.kind === "review_map_edge" ? "relationship" : "node");
    const exactness = selection.exactness === "current_index" ? " · current index context" : "";
    return `Review Map · ${subject}${exactness}`;
  }
  if (!scope?.path) return "";
  const parts = [scope.path];
  if (scope.symbol) parts.push(scope.symbol);
  const range = rangeLabel(scope.start_line, scope.end_line);
  if (range) parts.push(range);
  return parts.join(" · ");
}

function referenceLabel(reference: ReviewAskReference): string {
  if (reference.map_selection) return `Map · ${reference.label || reference.map_selection.relation || reference.map_selection.symbol || reference.map_selection.path || "context"}`;
  const name = reference.symbol || reference.path || reference.label || "Source";
  const range = rangeLabel(reference.start_line, reference.end_line);
  return range ? `${name} · ${range}` : name;
}

/**
 * PK2: history the answer drew on. Visibly not a Snapshot source: it opens the
 * landed Review it came from when this workspace holds it, and nothing else.
 */
function ProjectKnowledgeChip({ reference }: { reference: ReviewAskReference }) {
  const label = `History · ${reference.label || "project knowledge"}`;
  const title = [reference.freshness_label, reference.exactness === "ai_narration" ? "AI narration, not evidence" : "Landed history, not evidence for this Snapshot"]
    .filter(Boolean)
    .join(" · ");
  const className = "inline-flex max-w-full items-center gap-1 rounded-full border border-dashed border-border px-2 py-1 text-micro text-muted-foreground";
  return reference.review_id ? (
    <a href={reviewHref(reference.review_id)} title={title} className={`${className} hover:text-neutral-200`} data-testid="ask-project-knowledge-reference">
      <History size={9} aria-hidden="true" />
      <span className="truncate">{label}</span>
    </a>
  ) : (
    <span title={title} className={className} data-testid="ask-project-knowledge-reference">
      <History size={9} aria-hidden="true" />
      <span className="truncate">{label}</span>
    </span>
  );
}

function deliveryLabel(state: string): string {
  if (state === "sent") return "Change sent";
  if (state === "dispatching") return "Sending change…";
  if (state === "uncertain") return "Delivery uncertain";
  if (state === "failed") return "Retry change";
  if (state === "blocked") return "Change blocked";
  return "Send change";
}

function conversationOriginLabel(origin: ReviewAskConversationOrigin): string {
  if (origin.label) return origin.label;
  const range = rangeLabel(origin.start_line, origin.end_line);
  if (origin.path && origin.symbol) return [origin.path, origin.symbol, range].filter(Boolean).join(" · ");
  if (origin.path) return [origin.path, range].filter(Boolean).join(" · ");
  if (origin.kind === "map") return "Review Map";
  if (origin.revision_number != null) return `Snapshot ${origin.revision_number}`;
  return origin.kind === "code" ? "Current worktree" : "Whole change";
}

function conversationAge(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) return "now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "Yesterday" : `${days}d`;
}

export default function ReviewAskDrawer({
  reviewId,
  revisionNumber,
  baseRevisionNumber,
  latestRevisionNumber,
  revisionId = "",
  filePaths = [],
  scope,
  onClearScope,
  onClose,
  onSwitchLatest,
  onOpenReference,
  transport,
}: ReviewAskDrawerProps) {
  const driverStorageKey = transport ? "lemoncrow.code.ask.driver" : ASK_DRIVER_KEY;
  const modelStorageKey = transport ? "lemoncrow.code.ask.model" : ASK_MODEL_KEY;
  const [messages, setMessages] = useState<ReviewAskMessage[]>([]);
  const [conversations, setConversations] = useState<ReviewAskConversationSummary[]>([]);
  const [activeConversationId, setActiveConversationId] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [latest, setLatest] = useState(latestRevisionNumber);
  const askDraftStorageKey = useMemo(
    () => transport ? "" : askDraftKey(reviewId, revisionId, scope),
    [transport, reviewId, revisionId, scope],
  );
  const [body, setBody] = useState(() => transport ? "" : loadAskDraft(reviewId, revisionId, scope));
  const skipDraftPersistRef = useRef(false);
  const [loading, setLoading] = useState(true);
  const [asking, setAsking] = useState(false);
  const [pendingTurn, setPendingTurn] = useState<PendingAskTurn | null>(null);
  const [error, setError] = useState("");
  const [driver, setDriver] = useState<ReviewAskDriver>(() => {
    const saved = window.localStorage.getItem(driverStorageKey);
    return saved === "claude"
      || saved === "codex"
      || saved === "opencode"
      || saved === "ollama"
      || saved === "openai"
      || saved === "litellm"
      ? saved
      : "configured";
  });
  const [model, setModel] = useState(() => window.localStorage.getItem(modelStorageKey) ?? "");
  const [models, setModels] = useState<string[]>([]);
  const [modelsByDriver, setModelsByDriver] = useState<Partial<Record<ReviewAskDriver, string[]>>>({});
  const [serverDrivers, setServerDrivers] = useState<ReviewAskDriver[] | undefined>(undefined);
  const allowedDrivers = transport?.allowedDrivers ?? serverDrivers;
  const [handoffs, setHandoffs] = useState<Record<string, ReviewAskHandoffResult>>({});
  const [mentionedPaths, setMentionedPaths] = useState<string[]>([]);
  const [attachments, setAttachments] = useState<ReviewEvidence[]>([]);
  const [uploadingAttachments, setUploadingAttachments] = useState(false);
  const [draggingAttachment, setDraggingAttachment] = useState(false);
  const [mentionDraft, setMentionDraft] = useState<MentionDraft | null>(null);
  const [mentionIndex, setMentionIndex] = useState(0);
  const [copiedPromptId, setCopiedPromptId] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const mentionPickerRef = useRef<HTMLDivElement>(null);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const askAbortRef = useRef<AbortController | null>(null);
  const stoppedTurnsRef = useRef(0);

  useEffect(() => () => {
    // The Review server records an Ask turn whether or not this client is still
    // listening, so closing the drawer can safely drop the stream. A custom
    // transport (Code chat) saves its turn only when its own stream completes,
    // so its stream is left to finish.
    if (!transport) askAbortRef.current?.abort();
  }, [transport]);

  useEffect(() => {
    if (transport || !askDraftStorageKey) return;
    skipDraftPersistRef.current = true;
    setBody(loadAskDraft(reviewId, revisionId, scope));
  }, [transport, askDraftStorageKey, reviewId, revisionId, scope]);

  useEffect(() => {
    if (transport || !askDraftStorageKey) return;
    if (skipDraftPersistRef.current) {
      skipDraftPersistRef.current = false;
      return;
    }
    saveAskDraft(reviewId, revisionId, scope, body);
  }, [transport, askDraftStorageKey, reviewId, revisionId, scope, body]);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError("");
    void (transport ? transport.load() : fetchReviewAsk(reviewId))
      .then((result) => {
        if (!live) return;
        setMessages((current) => [...result.messages, ...current.filter(isStoppedTurn)]);
        setConversations(result.conversations ?? []);
        setActiveConversationId(result.active_conversation_id ?? result.messages[0]?.conversation_id ?? "");
        setLatest(Math.max(latestRevisionNumber, result.latest_revision_number));
        setModels(result.ai?.models ?? []);
        setModelsByDriver(result.ai?.models_by_driver ?? {});
        setServerDrivers(result.ai?.drivers);
        setDriver((current) => current === "configured" ? (result.ai?.driver ?? current) : current);
        setModel((current) => current || result.ai?.model || "");
      })
      .catch((reason: unknown) => {
        if (!live) return;
        setError(String(reason instanceof Error ? reason.message : reason));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [reviewId, latestRevisionNumber, transport]);

  useEffect(() => {
    setLatest((current) => Math.max(current, latestRevisionNumber));
  }, [latestRevisionNumber]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages.length, pendingTurn?.status]);

  const visibleLatest = Math.max(latest, latestRevisionNumber);
  const sourceScope = useMemo(() => scopeLabel(scope), [scope]);
  const currentConversationOrigin = useMemo<ReviewAskConversationOrigin>(() => {
    if (scope?.context) {
      return {
        kind: "map",
        label: scope.label || scope.context.symbol || scope.context.path || "Review Map",
        revision_number: transport ? null : revisionNumber,
        path: scope.context.path,
        symbol: scope.context.symbol,
        start_line: scope.context.line,
      };
    }
    return {
      kind: transport ? "code" : scope?.path || scope?.symbol || scope?.start_line ? "selection" : "review",
      revision_number: transport ? null : revisionNumber,
      path: scope?.path,
      symbol: scope?.symbol,
      start_line: scope?.start_line,
      end_line: scope?.end_line,
    };
  }, [revisionNumber, scope, transport]);

  const openConversation = async (conversationId: string) => {
    if (asking || loading || conversationId === activeConversationId) {
      setHistoryOpen(false);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const result = await (transport ? transport.load(conversationId) : fetchReviewAsk(reviewId, conversationId));
      setMessages((current) => [
        ...result.messages,
        ...current.filter((message) => isStoppedTurn(message) && message.conversation_id === conversationId),
      ]);
      if (result.conversations) setConversations(result.conversations);
      setActiveConversationId(result.active_conversation_id ?? conversationId);
      setLatest((current) => Math.max(current, result.latest_revision_number));
      setHistoryOpen(false);
      setBody("");
      setMentionedPaths([]);
      setAttachments([]);
      setPendingTurn(null);
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setLoading(false);
    }
  };

  const startConversation = async () => {
    if (asking || loading) return;
    const active = conversations.find((item) => item.id === activeConversationId);
    if (active?.message_count === 0) {
      setMessages([]);
      setPendingTurn(null);
      setBody("");
      setMentionedPaths([]);
      setAttachments([]);
      setHistoryOpen(false);
      window.requestAnimationFrame(() => textareaRef.current?.focus());
      return;
    }
    setError("");
    try {
      const created = transport?.startConversation
        ? await transport.startConversation(currentConversationOrigin)
        : await createReviewAskConversation(reviewId, currentConversationOrigin);
      setConversations((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setActiveConversationId(created.id);
      setMessages([]);
      setPendingTurn(null);
      setBody("");
      setMentionedPaths([]);
      setAttachments([]);
      setHistoryOpen(false);
      window.requestAnimationFrame(() => textareaRef.current?.focus());
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    }
  };

  const noteConversationTurn = (conversationId: string, question: string) => {
    if (!conversationId) return;
    const now = new Date().toISOString();
    setActiveConversationId(conversationId);
    setConversations((current) => {
      const existing = current.find((item) => item.id === conversationId);
      const updated: ReviewAskConversationSummary = existing
        ? {
            ...existing,
            title: existing.title === "New conversation" ? question.replace(/\s+/g, " ").trim().slice(0, 160) : existing.title,
            updated_at: now,
            message_count: existing.message_count + 2,
          }
        : {
            id: conversationId,
            title: question.replace(/\s+/g, " ").trim().slice(0, 160) || "New conversation",
            origin: currentConversationOrigin,
            created_at: now,
            updated_at: now,
            message_count: 2,
          };
      return [updated, ...current.filter((item) => item.id !== conversationId)];
    });
  };

  const visibleModels = useMemo(
    () => modelsByDriver[driver] ?? defaultDriverModels(driver, models),
    [modelsByDriver, models, driver],
  );

  useEffect(() => {
    if (driver !== "opencode") return;
    const available = modelsByDriver.opencode ?? defaultDriverModels("opencode", models);
    if (available.length === 0 || (model && available.includes(model))) return;
    const nextModel = available[0];
    setModel(nextModel);
    window.localStorage.setItem(modelStorageKey, nextModel);
  }, [driver, model, models, modelsByDriver, modelStorageKey]);

  useEffect(() => {
    if (allowedDrivers && !allowedDrivers.includes(driver)) setDriver("configured");
  }, [driver, allowedDrivers]);

  const mentionOptions = useMemo(() => {
    if (!mentionDraft) return [];
    const query = mentionDraft.query.toLowerCase();
    return filePaths
      .filter((path) => !mentionedPaths.includes(path))
      .map((path, index) => {
        const lowered = path.toLowerCase();
        const name = fileName(path).toLowerCase();
        const rank = !query ? index : name.startsWith(query) ? -300 : lowered.startsWith(query) ? -200 : lowered.includes(query) ? -100 : 1000;
        return { path, rank, index };
      })
      .filter((item) => item.rank < 1000)
      .sort((left, right) => left.rank - right.rank || left.index - right.index)
      .slice(0, 8)
      .map((item) => item.path);
  }, [filePaths, mentionDraft, mentionedPaths]);

  useEffect(() => {
    if (mentionIndex < mentionOptions.length) return;
    setMentionIndex(0);
  }, [mentionIndex, mentionOptions.length]);

  useEffect(() => {
    if (!mentionDraft) return;

    const dismissOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (textareaRef.current?.contains(target)) return;
      if (mentionPickerRef.current?.contains(target)) return;
      setMentionDraft(null);
      setMentionIndex(0);
    };

    document.addEventListener("pointerdown", dismissOnOutsidePointer, true);
    return () => document.removeEventListener("pointerdown", dismissOnOutsidePointer, true);
  }, [mentionDraft]);

  const selectMention = (path: string) => {
    if (!mentionDraft) return;
    const cursor = mentionDraft.start;
    setBody((current) => `${current.slice(0, mentionDraft.start)}${current.slice(mentionDraft.end)}`);
    setMentionedPaths((current) => current.includes(path) ? current : [...current, path]);
    setMentionDraft(null);
    setMentionIndex(0);
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(cursor, cursor);
    });
  };

  const attachExternalFiles = async (files: File[]) => {
    if (files.length === 0 || (transport && !transport.upload)) return;
    if (!transport && !revisionId) {
      setError("This Snapshot is still resolving; external files cannot be attached yet.");
      return;
    }
    const available = Math.max(0, 8 - attachments.length);
    const selected = files.slice(0, available);
    const failures: string[] = [];
    setUploadingAttachments(true);
    setError("");
    try {
      for (const file of selected) {
        if (!askAttachmentSupported(file)) {
          failures.push(`${file.name}: Ask attachments currently support text and code files.`);
          continue;
        }
        const maxBytes = transport ? 32 * 1024 : ASK_ATTACHMENT_MAX_BYTES;
        if (file.size > maxBytes) {
          failures.push(`${file.name}: Ask attachments are limited to ${transport ? "32 KiB" : "1 MiB"} per file.`);
          continue;
        }
        try {
          const evidence = transport?.upload
            ? await transport.upload(file)
            : (await uploadReviewEvidence(reviewId, file, "", "", revisionId)).evidence;
          setAttachments((current) => current.some((item) => item.id === evidence.id) ? current : [...current, evidence]);
        } catch (reason) {
          failures.push(`${file.name}: ${String(reason instanceof Error ? reason.message : reason)}`);
        }
      }
      if (files.length > available) failures.push("Ask supports at most 8 external attachments.");
    } finally {
      setUploadingAttachments(false);
      if (attachmentInputRef.current) attachmentInputRef.current.value = "";
    }
    if (failures.length > 0) setError(failures.join(" · "));
  };

  const pastedTextFile = (text: string) => new File([text], "pasted-text.txt", { type: "text/plain" });

  const handleComposerPaste = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    if (transport && !transport.upload) return;
    const clipboardFiles = Array.from(event.clipboardData.files ?? []);
    if (clipboardFiles.length > 0) {
      event.preventDefault();
      void attachExternalFiles(clipboardFiles);
      return;
    }
    const text = event.clipboardData.getData("text/plain");
    if (text.length >= ASK_LARGE_PASTE_THRESHOLD) {
      event.preventDefault();
      void attachExternalFiles([pastedTextFile(text)]);
    }
  };

  const copyPrompt = async (id: string, text: string) => {
    if (!navigator.clipboard?.writeText) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopiedPromptId(id);
      window.setTimeout(() => {
        setCopiedPromptId((current) => current === id ? "" : current);
      }, 1400);
    } catch {
      // Clipboard permission failures should not disturb the conversation.
    }
  };

  const ask = async (raw: string, attachedPaths: string[] = mentionedPaths, attachedEvidence: ReviewEvidence[] = attachments) => {
    const question = raw.trim();
    if (!question || asking) return;
    const activeDriver = driver;
    const activeModel = model;
    if (!transport) {
      // Local friction metric: did this Ask need a hand-picked provider/model?
      activeReviewFriction()?.ask(
        window.localStorage.getItem(driverStorageKey) !== null || window.localStorage.getItem(modelStorageKey) !== null,
      );
    }
    const activeAttachments = [...attachedEvidence];
    setAsking(true);
    setError("");
    setBody("");
    setMentionedPaths([]);
    setAttachments([]);
    setMentionDraft(null);
    setMentionIndex(0);
    setPendingTurn({
      question,
      driver: activeDriver,
      model: activeModel,
      mentionedPaths: attachedPaths,
      attachments: activeAttachments,
      status: "starting",
      elapsedMs: 0,
      error: "",
      answer: "",
    });
    const input: ReviewAskInput = {
      revision_number: revisionNumber,
      ...(baseRevisionNumber != null && baseRevisionNumber !== revisionNumber
        ? { base_revision_number: baseRevisionNumber }
        : {}),
      body: question,
      ...(activeConversationId ? { conversation_id: activeConversationId } : {}),
      ...(attachedPaths.length > 0 ? { mentioned_paths: attachedPaths } : {}),
      ...(activeAttachments.length > 0 ? { evidence_ids: activeAttachments.map((item) => item.id) } : {}),
      ...(activeDriver !== "configured" ? { driver: activeDriver } : {}),
      ...(activeModel ? { model: activeModel } : {}),
      ...(scope?.context ? { context: scope.context } : {}),
      ...(scope?.target_unit_key ? { target_unit_key: scope.target_unit_key } : {}),
      ...(scope?.path ? { path: scope.path } : {}),
      ...(scope?.symbol ? { symbol: scope.symbol } : {}),
      ...(scope?.side ? { side: scope.side } : {}),
      ...(scope?.start_line ? { start_line: scope.start_line } : {}),
      ...(scope?.end_line ? { end_line: scope.end_line } : {}),
      ...(scope?.selected_text ? { selected_text: scope.selected_text } : {}),
    };
    const controller = new AbortController();
    askAbortRef.current = controller;
    const onEvent = (event: ReviewAskStreamEvent) => {
      if (controller.signal.aborted) return;
      if (event.type === "accepted") {
        setPendingTurn((current) => current ? { ...current, status: "working" } : current);
      } else if (event.type === "working") {
        setPendingTurn((current) => current ? { ...current, status: "working", elapsedMs: event.elapsed_ms } : current);
      } else if (event.type === "delta") {
        setPendingTurn((current) => current ? { ...current, status: "working", answer: current.answer + event.text } : current);
      }
    };
    try {
      const result = await (transport
        ? transport.stream(input, onEvent, controller.signal)
        : streamReviewAsk(reviewId, input, onEvent, controller.signal));
      // A stopped turn already settled in stopAsk; a late reply must not revive it.
      if (controller.signal.aborted) return;
      setMessages((current) => [...current, result.human, result.assistant]);
      const settledConversationId = result.conversation_id ?? activeConversationId;
      noteConversationTurn(settledConversationId, question);
      setLatest((current) => Math.max(current, result.latest_revision_number));
      setPendingTurn(null);
    } catch (reason) {
      if (controller.signal.aborted) return;
      const detail = String(reason instanceof Error ? reason.message : reason);
      setPendingTurn((current) => current
        ? { ...current, status: "failed", error: detail }
        : current);
    } finally {
      if (askAbortRef.current === controller) {
        askAbortRef.current = null;
        setAsking(false);
      }
    }
  };

  /** Cancel the in-flight Ask and keep the question plus any streamed answer as a stopped turn. */
  const stopAsk = () => {
    const controller = askAbortRef.current;
    if (!controller) return;
    askAbortRef.current = null;
    controller.abort();
    const turn = pendingTurn;
    if (turn) {
      stoppedTurnsRef.current += 1;
      const id = `${STOPPED_TURN_PREFIX}${stoppedTurnsRef.current}`;
      const shared = {
        review_id: reviewId,
        revision_id: "",
        revision_number: transport ? null : revisionNumber,
        intent: "question" as const,
        scope: LOCAL_TURN_SCOPE,
        action: {},
        created_at: new Date().toISOString(),
        conversation_id: activeConversationId,
      };
      setMessages((current) => [
        ...current,
        {
          ...shared,
          id: `${id}:human`,
          role: "human",
          body: turn.question,
          references: [
            ...turn.mentionedPaths.map((path) => ({ path })),
            ...turn.attachments.map((attachment) => ({ evidence_id: attachment.id, label: attachment.title })),
          ],
        },
        { ...shared, id: `${id}:assistant`, role: "assistant", body: turn.answer, references: [] },
      ]);
    }
    setPendingTurn(null);
    setAsking(false);
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void ask(body);
  };

  const sendHandoff = async (message: ReviewAskMessage) => {
    const action = message.action;
    if (!action.operation_id || !action.instruction_hash) return;
    setError("");
    try {
      const result = await sendReviewAskHandoff(reviewId, message.id, {
        operation_id: action.operation_id,
        instruction_hash: action.instruction_hash,
        expected_revision_id: message.action.expected_revision_id ?? message.revision_id,
        expected_tree_fingerprint: message.action.expected_tree_fingerprint ?? "",
        expected_source_fingerprint: message.action.expected_source_fingerprint ?? "",
      });
      setHandoffs((current) => ({ ...current, [message.id]: result }));
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    }
  };

  let previousRevision: number | null | undefined = undefined;

  return (
    <section className="relative flex h-full min-h-0 flex-col bg-background" aria-label={transport ? "Code chat" : "Review chat"}>
      <header className="flex h-11 shrink-0 items-center gap-2 border-b border-violet-800/60 bg-violet-950/55 px-3 shadow-[inset_0_-1px_0_rgba(139,92,246,0.08)]">
        <div className="flex h-6 w-6 items-center justify-center rounded-full bg-violet-950/70 text-violet-300">
          <Sparkles size={13} aria-hidden="true" />
        </div>
        <div className="min-w-0">
          <div className="text-label font-medium leading-4 text-neutral-200">{transport ? "Code chat" : "Review chat"}</div>
          <div className="max-w-[180px] truncate text-micro leading-3 text-muted-foreground">
            {activeConversationId
              ? conversations.find((item) => item.id === activeConversationId)?.title || (transport ? "Current worktree" : `Snapshot ${revisionNumber}`)
              : transport ? "Current worktree · file focused" : `Snapshot ${revisionNumber}`}
          </div>
        </div>
        <span className="flex-1" />
        {loading && (
          <span className="flex items-center gap-1 text-micro text-muted-foreground" aria-label="Syncing conversation">
            <LoaderCircle size={10} className="animate-spin motion-reduce:animate-none" />
            Syncing
          </span>
        )}
        <UiButton
          type="button"
          variant="ghost"
          size="icon"
          onClick={() => setHistoryOpen((current) => !current)}
          aria-label="Ask history"
          aria-expanded={historyOpen}
          className={historyOpen ? "h-6 w-6 bg-violet-950/50 text-violet-200" : "h-6 w-6"}
        >
          <History size={13} />
        </UiButton>
        <UiButton
          type="button"
          variant="ghost"
          size="icon"
          onClick={() => void startConversation()}
          disabled={asking || loading}
          aria-label="New Ask conversation"
          className="h-6 w-6"
        >
          <Plus size={13} />
        </UiButton>
        <UiButton type="button" variant="ghost" size="icon" onClick={onClose} aria-label="Close Ask" className="h-6 w-6">
          <X size={13} />
        </UiButton>
      </header>

      {historyOpen && (
        <div className="absolute left-2 right-2 top-12 z-40 max-h-[min(360px,70vh)] overflow-y-auto rounded-xl border border-border bg-background/98 p-1.5 shadow-2xl backdrop-blur" aria-label="Recent Ask conversations">
          <div className="flex items-center gap-2 px-2 py-1.5">
            <span className="text-caption font-semibold text-neutral-200">Recent conversations</span>
            <span className="ml-auto text-micro text-muted-foreground">{conversations.length}</span>
          </div>
          {conversations.length === 0 ? (
            <div className="px-2 py-6 text-center text-caption text-muted-foreground">No previous conversations yet.</div>
          ) : conversations.slice(0, 12).map((conversation) => (
            <UiButton
              unstyled
              type="button"
              key={conversation.id}
              onClick={() => void openConversation(conversation.id)}
              aria-label={`Open conversation: ${conversation.title}`}
              className={`flex w-full min-w-0 items-start gap-2 rounded-lg px-2 py-2 text-left transition hover:bg-muted ${conversation.id === activeConversationId ? "bg-violet-950/25" : ""}`}
            >
              <div className="min-w-0 flex-1">
                <div className="truncate text-label font-medium text-neutral-200">{conversation.title}</div>
                <div className="mt-0.5 truncate text-micro text-muted-foreground">{conversationOriginLabel(conversation.origin)}</div>
              </div>
              <div className="flex shrink-0 items-center gap-1 pt-0.5 text-micro text-muted-foreground">
                {conversation.id === activeConversationId && <Check size={10} className="text-violet-300" />}
                <span>{conversationAge(conversation.updated_at)}</span>
              </div>
            </UiButton>
          ))}
          <div className="mt-1 border-t border-border px-1 pt-1">
            <UiButton type="button" variant="ghost" size="compact" onClick={() => void startConversation()} disabled={asking || loading} className="w-full justify-start">
              <Plus size={11} /> New conversation
            </UiButton>
          </div>
        </div>
      )}

      {transport && scope?.path && (
        <div className="shrink-0 border-b border-border bg-muted/30 px-3 py-2" aria-label="Code Ask focus">
          <div className="flex min-w-0 items-start gap-2">
            <FileCode2 size={13} className="mt-0.5 shrink-0 text-muted-foreground" aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <div className="text-micro font-semibold uppercase tracking-wider text-muted-foreground">
                {scope.selected_text ? "Selected code" : scope.symbol || scope.start_line ? "File location" : "Focused file"}
              </div>
              <div className="truncate font-mono text-caption text-foreground" title={sourceScope}>{sourceScope}</div>
              <div className="mt-0.5 text-caption text-muted-foreground">Answers can reference related project files.</div>
            </div>
            {(scope.symbol || scope.start_line) && (
              <UiButton type="button" variant="ghost" size="icon" onClick={onClearScope} aria-label="Focus on whole file" className="h-6 w-6 shrink-0">
                <X size={12} />
              </UiButton>
            )}
          </div>
        </div>
      )}

      {!transport && visibleLatest > revisionNumber && (
        <div className="flex shrink-0 items-center gap-2 border-b border-sky-900/50 bg-sky-950/20 px-3 py-2 text-caption text-sky-200">
          <span>Snapshot {visibleLatest} available</span>
          <UiButton type="button" variant="ghost" size="compact" onClick={onSwitchLatest} className="ml-auto h-6 px-2">
            Switch to latest
          </UiButton>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        {!loading && messages.length === 0 && !pendingTurn && (
          <div className="mx-auto max-w-[340px] py-8 text-center">
            <div className="mx-auto flex h-8 w-8 items-center justify-center rounded-full border border-border bg-muted/70 text-violet-300">
              <Sparkles size={14} />
            </div>
            <div className="mt-3 text-sm font-medium text-neutral-200">{transport ? "Ask about this code" : "Ask, challenge, or request a change"}</div>
            <p className="mx-auto mt-1 max-w-[290px] text-caption leading-4 text-muted-foreground">
              {transport ? "Ask about the selected code and explore it as a conversation." : "Discuss this review like a chat. Ask why something changed, check an implementation, or request a concrete fix."}
            </p>
            <div className="mt-4 flex flex-wrap justify-center gap-1.5">
              {(transport ? CODE_SUGGESTIONS : SUGGESTIONS).map((suggestion) => (
                <UiButton
                  key={suggestion}
                  type="button"
                  variant="outline"
                  disabled={asking}
                  onClick={() => void ask(suggestion)}
                  className="h-auto rounded-full px-2.5 py-1.5 text-caption"
                >
                  {suggestion}
                </UiButton>
              ))}
            </div>
          </div>
        )}

        <div className="space-y-3">
          {messages.map((message) => {
            const revision = message.revision_number;
            const showDivider = revision !== previousRevision;
            previousRevision = revision;
            const action = message.action;
            const localHandoff = handoffs[message.id];
            const handoffState = localHandoff?.state ?? action.handoff?.state ?? "prepared";
            const delivery = action.delivery;
            const canSend = action.kind === "agent_handoff"
              && message.revision_number === visibleLatest
              && handoffState !== "sent"
              && handoffState !== "dispatching"
              && handoffState !== "uncertain"
              && Boolean(delivery?.supported);
            const destination = delivery?.label || delivery?.host || "author";
            return (
              <div key={message.id}>
                {showDivider && revision != null && (
                  <div className="my-3 flex items-center gap-2 text-micro uppercase tracking-[0.12em] text-muted-foreground">
                    <span className="h-px flex-1 bg-muted" />
                    <span>Snapshot {revision}</span>
                    <span className="h-px flex-1 bg-muted" />
                  </div>
                )}
                {message.role === "human" ? (
                  <div className="group ml-10 flex items-start justify-end gap-1">
                    <UiButton unstyled
                      type="button"
                      onClick={() => void copyPrompt(message.id, message.body)}
                      aria-label={copiedPromptId === message.id ? "Copied prompt" : "Copy prompt"}
                      title={copiedPromptId === message.id ? "Copied" : "Copy prompt"}
                      className={`mt-1 flex h-6 shrink-0 items-center justify-center rounded-md transition focus:opacity-100 group-hover:opacity-100 ${copiedPromptId === message.id ? "w-auto gap-1 px-1.5 text-emerald-400 opacity-100" : "w-6 text-muted-foreground opacity-0 hover:bg-muted hover:text-neutral-300"}`}
                    >
                      {copiedPromptId === message.id ? <Check size={11} /> : <Copy size={11} />}
                      {copiedPromptId === message.id && <span className="text-micro">Copied</span>}
                    </UiButton>
                    <div className="max-w-[92%] rounded-2xl rounded-br-md border border-border bg-muted/75 px-3 py-2">
                      <div className="flex flex-wrap items-center justify-end gap-1">
                        {message.references.map((reference, index) => reference.path ? (
                          <UiButton unstyled
                            key={`${reference.path}:${index}`}
                            type="button"
                            title={reference.path}
                            onClick={() => onOpenReference(reference)}
                            className="inline-flex max-w-full items-center gap-1 rounded-md border border-violet-900/60 bg-violet-950/30 px-1.5 py-0.5 font-mono text-micro text-violet-200 hover:bg-violet-950/50"
                          >
                            <FileCode2 size={9} />
                            <span className="truncate">{fileName(reference.path)}</span>
                          </UiButton>
                        ) : reference.evidence_id ? (
                          <span
                            key={`${reference.evidence_id}:${index}`}
                            title={reference.label || "External attachment"}
                            className="inline-flex max-w-full items-center gap-1 rounded-md border border-neutral-700/70 bg-neutral-800/45 px-1.5 py-0.5 text-micro text-neutral-300"
                          >
                            <FileText size={9} className="text-violet-400" />
                            <span className="truncate">{reference.label || "Attachment"}</span>
                          </span>
                        ) : null)}
                        <span className="whitespace-pre-wrap text-label leading-5 text-foreground">{message.body}</span>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="mr-1 flex items-start gap-2">
                    <div className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-violet-950/70 text-violet-300">
                      <Sparkles size={10} />
                    </div>
                    <div className="min-w-0 flex-1 pt-0.5">
                      <div className="whitespace-pre-wrap text-label leading-5 text-neutral-200">{message.body}</div>
                      {isStoppedTurn(message) && (
                        <div className="mt-1 flex items-center gap-1 text-micro text-muted-foreground">
                          <Square size={8} aria-hidden="true" />
                          {message.body ? "Stopped" : "Stopped before an answer arrived"}
                        </div>
                      )}
                      {message.references.length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {message.references.map((reference, index) => reference.kind === "project_knowledge" ? (
                            <ProjectKnowledgeChip key={`${reference.entry_id ?? "knowledge"}:${index}`} reference={reference} />
                          ) : (
                            <UiButton unstyled
                              key={`${reference.path}:${reference.start_line}:${index}`}
                              type="button"
                              onClick={() => onOpenReference(reference)}
                              className="rounded-full border border-border bg-muted/80 px-2 py-1 font-mono text-micro text-sky-300 transition hover:text-sky-200"
                            >
                              {referenceLabel(reference)}
                            </UiButton>
                          ))}
                        </div>
                      )}
                      {action.kind === "agent_handoff" && action.handoff_instruction && (
                        <div className="mt-3 overflow-hidden rounded-lg border border-violet-900/50 bg-violet-950/10">
                          <div className="flex items-center gap-2 border-b border-violet-900/30 px-2.5 py-2">
                            <span className="text-micro font-semibold uppercase tracking-[0.12em] text-violet-300">Proposed change</span>
                            <span className="ml-auto truncate text-micro text-muted-foreground">to {destination}</span>
                          </div>
                          <div className="whitespace-pre-wrap px-2.5 py-2 text-caption leading-4 text-neutral-300">
                            {action.handoff_instruction}
                          </div>
                          <div className="flex items-center gap-2 border-t border-violet-900/25 px-2.5 py-2">
                            <UiButton
                              type="button"
                              variant="default"
                              disabled={!canSend}
                              onClick={() => void sendHandoff(message)}
                              className="h-7 px-3 text-caption"
                              tooltip={!delivery?.supported ? delivery?.reason : undefined}
                            >
                              {handoffState === "dispatching"
                                ? <LoaderCircle size={12} className="animate-spin motion-reduce:animate-none" />
                                : <Send size={12} />}
                              {deliveryLabel(handoffState)}
                            </UiButton>
                            {handoffState === "sent" && (
                              <span className="text-micro text-emerald-400">Sent to {destination}; a new snapshot will appear when captured.</span>
                            )}
                            {!delivery?.supported && delivery?.reason && (
                              <span className="text-micro text-muted-foreground">{delivery.reason}</span>
                            )}
                            {(localHandoff?.message || action.handoff?.last_error) && handoffState !== "sent" && (
                              <span className="text-micro text-amber-400">
                                {localHandoff?.message || action.handoff?.last_error}
                              </span>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            );
          })}

          {pendingTurn && (
            <>
              <div className="group ml-10 flex items-start justify-end gap-1">
                <UiButton unstyled
                  type="button"
                  onClick={() => void copyPrompt("pending", pendingTurn.question)}
                  aria-label={copiedPromptId === "pending" ? "Copied prompt" : "Copy prompt"}
                  title={copiedPromptId === "pending" ? "Copied" : "Copy prompt"}
                  className={`mt-1 flex h-6 shrink-0 items-center justify-center rounded-md transition focus:opacity-100 group-hover:opacity-100 ${copiedPromptId === "pending" ? "w-auto gap-1 px-1.5 text-emerald-400 opacity-100" : "w-6 text-muted-foreground opacity-0 hover:bg-muted hover:text-neutral-300"}`}
                >
                  {copiedPromptId === "pending" ? <Check size={11} /> : <Copy size={11} />}
                  {copiedPromptId === "pending" && <span className="text-micro">Copied</span>}
                </UiButton>
                <div className="max-w-[92%] rounded-2xl rounded-br-md border border-border bg-muted/75 px-3 py-2">
                  <div className="flex flex-wrap items-center justify-end gap-1">
                    {pendingTurn.mentionedPaths.map((path) => (
                      <span key={path} title={path} className="inline-flex max-w-full items-center gap-1 rounded-md border border-violet-900/60 bg-violet-950/30 px-1.5 py-0.5 font-mono text-micro text-violet-200">
                        <FileCode2 size={9} />
                        <span className="truncate">{fileName(path)}</span>
                      </span>
                    ))}
                    {pendingTurn.attachments.map((attachment) => (
                      <span key={attachment.id} title={attachment.title} className="inline-flex max-w-full items-center gap-1 rounded-md border border-neutral-700/70 bg-neutral-800/45 px-1.5 py-0.5 text-micro text-neutral-300">
                        <FileText size={9} className="text-violet-400" />
                        <span className="truncate">{attachment.title}</span>
                      </span>
                    ))}
                    <span className="whitespace-pre-wrap text-label leading-5 text-foreground">{pendingTurn.question}</span>
                  </div>
                </div>
              </div>
              <div className="mr-1 flex items-start gap-2" aria-live="polite">
                <div className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-violet-950/70 text-violet-300">
                  {pendingTurn.status === "failed"
                    ? <X size={10} />
                    : <Sparkles size={10} className={pendingTurn.answer ? "" : pendingTurn.status === "working" ? "animate-pulse motion-reduce:animate-none" : ""} />}
                </div>
                <div className="min-w-0 flex-1 rounded-lg border border-neutral-900 bg-muted/20 px-2.5 py-2">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-caption font-medium text-neutral-300">
                      {DRIVER_LABELS[pendingTurn.driver]}
                      {pendingTurn.model ? ` · ${pendingTurn.model}` : ""}
                    </span>
                    {pendingTurn.status !== "failed" && pendingTurn.elapsedMs > 0 && (
                      <span className="ml-auto tabular-nums text-micro text-muted-foreground">
                        {Math.max(1, Math.floor(pendingTurn.elapsedMs / 1000))}s
                      </span>
                    )}
                  </div>
                  {pendingTurn.status === "failed" ? (
                    <div className="mt-1.5 flex items-start gap-2">
                      <span className="min-w-0 flex-1 text-caption leading-4 text-rose-300">{pendingTurn.error}</span>
                      <UiButton
                        type="button"
                        variant="ghost"
                        size="compact"
                        onClick={() => void ask(pendingTurn.question, pendingTurn.mentionedPaths, pendingTurn.attachments)}
                        className="h-6 shrink-0 px-2"
                      >
                        <RotateCcw size={10} />
                        Retry
                      </UiButton>
                    </div>
                  ) : pendingTurn.answer ? (
                    <div className="mt-1.5 whitespace-pre-wrap text-label leading-5 text-neutral-200">
                      {pendingTurn.answer}
                      <span className="ml-0.5 inline-block h-3 w-px animate-pulse bg-violet-400 align-middle motion-reduce:animate-none" aria-hidden="true" />
                    </div>
                  ) : (
                    <div className="mt-1.5 flex items-center gap-2 text-caption text-muted-foreground">
                      <LoaderCircle size={11} className="animate-spin motion-reduce:animate-none" />
                      <span>{pendingTurn.status === "starting" ? "Starting…" : transport ? "Working on this code…" : "Working on this snapshot…"}</span>
                    </div>
                  )}
                </div>
              </div>
            </>
          )}
          <div ref={endRef} />
        </div>

        {error && (
          <div className="mt-3 rounded-lg border border-rose-900/60 bg-rose-950/20 px-2.5 py-2 text-caption text-rose-300">
            {error}
          </div>
        )}
      </div>

      <form onSubmit={submit} className="shrink-0 border-t border-border bg-background/95 p-2.5">
        <div
          className={`relative rounded-lg border p-2 shadow-sm transition focus-within:border-neutral-700 focus-within:bg-muted/50 ${draggingAttachment ? "border-violet-700/70 bg-violet-950/20" : "border-border bg-muted/35"}`}
          onDragEnter={(event) => {
            if ((transport && !transport.upload) || !event.dataTransfer.types.includes("Files")) return;
            event.preventDefault();
            setDraggingAttachment(true);
          }}
          onDragOver={(event) => {
            if ((transport && !transport.upload) || !event.dataTransfer.types.includes("Files")) return;
            event.preventDefault();
            event.dataTransfer.dropEffect = "copy";
            setDraggingAttachment(true);
          }}
          onDragLeave={(event) => {
            // Stay visible only while the pointer moves to another element inside
            // the composer. A dragenter/dragleave depth count drifted whenever an
            // element under the pointer was replaced mid-drag (e.g. Stop turning
            // back into Send): its dragleave never bubbles here, so the overlay
            // stuck on after the drag left, was dropped elsewhere, or was cancelled.
            const next = event.relatedTarget;
            if (next instanceof Node && event.currentTarget.contains(next)) return;
            setDraggingAttachment(false);
          }}
          onDrop={(event) => {
            setDraggingAttachment(false);
            if ((transport && !transport.upload) || !event.dataTransfer.types.includes("Files")) return;
            event.preventDefault();
            const files = Array.from(event.dataTransfer.files ?? []);
            if (files.length > 0) void attachExternalFiles(files);
          }}
        >
          {draggingAttachment && (
            <div className="pointer-events-none absolute inset-1 z-10 flex items-center justify-center rounded-lg border border-dashed border-violet-700/70 bg-background/80 text-caption font-medium text-violet-200 backdrop-blur-sm">
              Drop to attach to this chat
            </div>
          )}
          {((!transport && sourceScope) || mentionedPaths.length > 0 || attachments.length > 0) && (
            <div className="mb-1.5 flex max-w-full flex-wrap items-center gap-1.5">
              {!transport && sourceScope && (
                <div className="flex min-w-0 max-w-full items-center gap-1.5 rounded-md border border-sky-900/60 bg-sky-950/20 px-2 py-1 font-mono text-micro text-sky-200" title={sourceScope}>
                  <span className="truncate">{sourceScope}</span>
                  {(!transport || scope?.symbol || scope?.start_line) && <UiButton type="button" variant="ghost" size="icon" onClick={onClearScope} aria-label="Remove Ask context" className="h-4 w-4 shrink-0 text-sky-500 hover:bg-transparent hover:text-sky-200">
                    <X size={10} />
                  </UiButton>}
                </div>
              )}
              {mentionedPaths.map((path) => (
                <div key={path} className="flex min-w-0 max-w-full items-center gap-1 rounded-md border border-violet-900/60 bg-violet-950/30 px-1.5 py-1 font-mono text-micro text-violet-200" title={path}>
                  <FileCode2 size={9} className="shrink-0" />
                  <span className="truncate">{fileName(path)}</span>
                  <UiButton
                    type="button"
                    variant="ghost"
                    size="icon"
                    onClick={() => setMentionedPaths((current) => current.filter((item) => item !== path))}
                    aria-label={`Remove ${path}`}
                    className="h-4 w-4 shrink-0 text-violet-500 hover:bg-transparent hover:text-violet-200"
                  >
                    <X size={9} />
                  </UiButton>
                </div>
              ))}
              {attachments.map((attachment) => (
                <div key={attachment.id} className="flex min-w-0 max-w-full items-center gap-1 rounded-md border border-neutral-700/70 bg-neutral-800/45 px-1.5 py-1 text-micro text-neutral-300" title={attachment.title}>
                  <FileText size={9} className="shrink-0 text-violet-400" />
                  <span className="max-w-40 truncate">{attachment.title}</span>
                  <UiButton
                    type="button"
                    variant="ghost"
                    size="icon"
                    onClick={() => setAttachments((current) => current.filter((item) => item.id !== attachment.id))}
                    aria-label={`Remove ${attachment.title}`}
                    className="h-4 w-4 shrink-0 text-muted-foreground hover:bg-transparent hover:text-neutral-200"
                  >
                    <X size={9} />
                  </UiButton>
                </div>
              ))}
            </div>
          )}
          {mentionDraft && (
            <div
              ref={mentionPickerRef}
              className="absolute bottom-full left-0 right-0 z-20 mb-1 overflow-hidden rounded-lg border border-border bg-muted/95 shadow-[0_-8px_28px_rgba(0,0,0,0.22)] backdrop-blur"
            >
              <div className="flex items-center gap-2 px-2.5 pb-1 pt-2">
                <FileCode2 size={11} className="shrink-0 text-violet-400" />
                <span className="text-micro font-medium text-muted-foreground">{transport ? "Files in this project" : "Files in this change"}</span>
                <span className="min-w-0 truncate font-mono text-micro text-violet-300">
                  @{mentionDraft.query}
                </span>
                <span className="ml-auto rounded border border-border bg-background/40 px-1 py-0.5 font-mono text-micro text-muted-foreground">Esc</span>
              </div>
              <div className="max-h-52 overflow-y-auto px-1 pb-1 pt-0.5" role="listbox" aria-label={transport ? "Project files" : "Changed files"}>
              {mentionOptions.length > 0 ? mentionOptions.map((path, index) => (
                <UiButton
                  unstyled
                  key={path}
                  type="button"
                  role="option"
                  aria-selected={index === mentionIndex}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => selectMention(path)}
                  className={`group/file flex w-full min-w-0 items-center gap-2 rounded-lg px-2 py-1.5 text-left transition ${index === mentionIndex ? "bg-violet-950/35 text-foreground ring-1 ring-inset ring-violet-900/35" : "text-neutral-400 hover:bg-neutral-800/55 hover:text-neutral-200"}`}
                >
                  <FileCode2 size={10} className={`shrink-0 ${index === mentionIndex ? "text-violet-300" : "text-muted-foreground group-hover/file:text-neutral-400"}`} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-caption font-medium leading-4">{fileName(path)}</span>
                    <span className="block truncate font-mono text-micro leading-3 text-muted-foreground">{path}</span>
                  </span>
                  {index === mentionIndex && <span className="shrink-0 text-micro text-violet-400">attach</span>}
                </UiButton>
              )) : (
                <div className="px-2 py-3 text-center text-caption text-muted-foreground">No files match @{mentionDraft.query}</div>
              )}
              </div>
            </div>
          )}
          <UiTextarea unstyled
            ref={textareaRef}
            rows={2}
            value={body}
            onPaste={handleComposerPaste}
            onChange={(event) => {
              const next = event.target.value;
              setBody(next);
              setMentionDraft(mentionDraftAt(next, event.target.selectionStart ?? next.length));
              setMentionIndex(0);
            }}
            onClick={(event) => {
              const target = event.currentTarget;
              setMentionDraft(mentionDraftAt(target.value, target.selectionStart ?? target.value.length));
            }}
            onKeyUp={(event) => {
              if (["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(event.key)) return;
              const target = event.currentTarget;
              setMentionDraft(mentionDraftAt(target.value, target.selectionStart ?? target.value.length));
            }}
            onKeyDown={(event) => {
              if (mentionDraft) {
                if (event.key === " ") {
                  setMentionDraft(null);
                  setMentionIndex(0);
                  return;
                }
                if (event.key === "ArrowDown" && mentionOptions.length > 0) {
                  event.preventDefault();
                  setMentionIndex((current) => (current + 1) % mentionOptions.length);
                  return;
                }
                if (event.key === "ArrowUp" && mentionOptions.length > 0) {
                  event.preventDefault();
                  setMentionIndex((current) => (current - 1 + mentionOptions.length) % mentionOptions.length);
                  return;
                }
                if ((event.key === "Enter" || event.key === "Tab") && mentionOptions[mentionIndex]) {
                  event.preventDefault();
                  selectMention(mentionOptions[mentionIndex]);
                  return;
                }
                if (event.key === "Escape") {
                  event.preventDefault();
                  setMentionDraft(null);
                  setMentionIndex(0);
                  return;
                }
              }
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                if (!asking) void ask(body);
              }
            }}
            placeholder={transport ? "Ask about this code…" : scope?.context
              ? "Ask about this context or request a change…"
              : scope?.path
                ? "Ask about this code or request a change…"
                : "Ask a question or request a change…"}
            aria-label={transport ? "Ask Code" : "Ask Review"}
            className="min-h-[44px] max-h-[132px] w-full resize-none bg-transparent px-1 py-1.5 text-label leading-5 text-foreground outline-none placeholder:text-muted-foreground"
          />
          <div className="mt-1.5 flex items-center gap-1.5">
            {(!transport || transport.upload) && <UiInput
              unstyled
              ref={attachmentInputRef}
              type="file"
              multiple
              tabIndex={-1}
              className="hidden"
              aria-label="External file picker"
              accept="text/*,.md,.mdx,.json,.jsonl,.yaml,.yml,.toml,.xml,.csv,.tsv,.py,.js,.jsx,.ts,.tsx,.go,.rs,.java,.kt,.cpp,.c,.h,.hpp,.cs,.rb,.php,.swift,.sql,.sh,.html,.css,.scss,.vue,.svelte,.graphql,.proto,.diff,.patch,.log,.ini,.cfg,.conf"
              onChange={(event) => void attachExternalFiles(Array.from(event.currentTarget.files ?? []))}
            />}
            {(!transport || transport.upload) && <UiButton
              type="button"
              variant="ghost"
              size="icon"
              disabled={uploadingAttachments || asking}
              aria-label="Attach external file"
              tooltip="Attach external file"
              onClick={() => attachmentInputRef.current?.click()}
              className="h-7 w-7 shrink-0 rounded-full"
            >
              {uploadingAttachments ? <LoaderCircle size={12} className="animate-spin motion-reduce:animate-none" /> : <Plus size={14} />}
            </UiButton>}
            <span className="flex-1" />
            <details className="relative text-caption" data-testid="ask-provider-options">
              <summary className="cursor-pointer select-none rounded px-2 py-1 text-muted-foreground hover:bg-muted hover:text-neutral-200">
                Provider
              </summary>
              <div className="absolute bottom-9 right-0 z-30 flex min-w-64 gap-1.5 rounded-md border border-border bg-popover p-2 shadow-lg">
                <AskPicker
                  label="Ask AI driver"
                  value={driver}
                  options={(allowedDrivers ?? (Object.keys(DRIVER_LABELS) as ReviewAskDriver[])).map((item) => ({
                    value: item,
                    label: DRIVER_LABELS[item],
                  }))}
                  onChange={(value) => {
                    const next = value as ReviewAskDriver;
                    if (!transport) activeReviewFriction()?.askProviderChanged();
                    setDriver(next);
                    window.localStorage.setItem(driverStorageKey, next);
                    const nextModels = modelsByDriver[next] ?? defaultDriverModels(next, models);
                    if (!model || !nextModels.includes(model)) {
                      const nextModel = next === "opencode" ? (nextModels[0] ?? "") : "";
                      setModel(nextModel);
                      if (nextModel) window.localStorage.setItem(modelStorageKey, nextModel);
                      else window.localStorage.removeItem(modelStorageKey);
                    }
                  }}
                />
                <AskPicker
                  label="Ask AI model"
                  value={model}
                  options={[
                    ...(driver === "opencode" ? [] : [{ value: "", label: "Default model" }]),
                    ...visibleModels.map((item) => ({ value: item, label: item })),
                  ]}
                  onChange={(value) => {
                    if (!transport) activeReviewFriction()?.askProviderChanged();
                    setModel(value);
                    if (value) window.localStorage.setItem(modelStorageKey, value);
                    else window.localStorage.removeItem(modelStorageKey);
                  }}
                />
              </div>
            </details>
            {asking ? (
              <UiButton
                type="button"
                variant="default"
                onClick={stopAsk}
                aria-label="Stop Ask"
                className="h-8 w-8 shrink-0 rounded-full bg-neutral-100 text-neutral-950 shadow-sm hover:bg-neutral-200"
              >
                <Square size={11} fill="currentColor" strokeWidth={0} />
              </UiButton>
            ) : (
              <UiButton
                type="submit"
                variant="default"
                disabled={body.trim() === ""}
                aria-label="Send Ask question"
                className="h-8 w-8 shrink-0 rounded-full bg-neutral-100 text-neutral-950 shadow-sm hover:bg-neutral-200 disabled:cursor-not-allowed disabled:bg-neutral-800 disabled:text-neutral-600"
              >
                <ArrowUp size={14} strokeWidth={2.4} />
              </UiButton>
            )}
          </div>
        </div>
      </form>
    </section>
  );
}
