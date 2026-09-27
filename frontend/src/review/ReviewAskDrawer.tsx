import { ArrowUp, Check, ChevronDown, Cpu, LoaderCircle, RotateCcw, Send, Sparkles, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import { fetchReviewAsk, sendReviewAskHandoff, streamReviewAsk } from "./reviewApi";
import type {
  ReviewAskDriver,
  ReviewAskHandoffResult,
  ReviewAskInput,
  ReviewAskMessage,
  ReviewAskReference,
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
}

interface PendingAskTurn {
  question: string;
  driver: ReviewAskDriver;
  model: string;
  status: "starting" | "working" | "failed";
  elapsedMs: number;
  error: string;
}

interface ReviewAskDrawerProps {
  reviewId: string;
  revisionNumber: number;
  baseRevisionNumber?: number;
  latestRevisionNumber: number;
  scope?: ReviewAskSelection | null;
  onClearScope(): void;
  onClose(): void;
  onSwitchLatest(): void;
  onOpenReference(reference: ReviewAskReference): void;
}

const SUGGESTIONS = [
  "Walk me through this change",
  "What should I review carefully?",
  "Could this be simpler?",
];

const ASK_DRIVER_KEY = "lemoncrow.review.ask.driver";
const ASK_MODEL_KEY = "lemoncrow.review.ask.model";

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
  const menuClass = "review-menu absolute bottom-full z-50 mb-1.5 min-w-[190px] max-w-[280px] p-1 "
    + (align === "right" ? "right-0" : "left-0");
  return (
    <details data-review-dropdown className="group relative">
      <summary
        className="flex h-7 max-w-[150px] cursor-pointer list-none items-center gap-1.5 rounded-full border border-neutral-800 bg-neutral-900/70 px-2.5 text-[10px] font-medium text-neutral-300 transition hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100 [&::-webkit-details-marker]:hidden"
        aria-label={label}
      >
        <Cpu size={11} className="shrink-0 text-neutral-500" />
        <span className="truncate">{selected}</span>
        <ChevronDown size={11} className="shrink-0 text-neutral-600 transition-transform group-open:rotate-180" />
      </summary>
      <div className={menuClass}>
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            onClick={(event) => {
              onChange(option.value);
              event.currentTarget.closest("details")?.removeAttribute("open");
            }}
            className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[10px] text-neutral-300 transition hover:bg-neutral-800/80 hover:text-neutral-100"
          >
            <span className="min-w-0 flex-1 truncate">{option.label}</span>
            {option.value === value && <Check size={11} className="shrink-0 text-neutral-400" />}
          </button>
        ))}
      </div>
    </details>
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

function deliveryLabel(state: string): string {
  if (state === "sent") return "Change sent";
  if (state === "dispatching") return "Sending change…";
  if (state === "uncertain") return "Delivery uncertain";
  if (state === "failed") return "Retry change";
  if (state === "blocked") return "Change blocked";
  return "Send change";
}

export default function ReviewAskDrawer({
  reviewId,
  revisionNumber,
  baseRevisionNumber,
  latestRevisionNumber,
  scope,
  onClearScope,
  onClose,
  onSwitchLatest,
  onOpenReference,
}: ReviewAskDrawerProps) {
  const [messages, setMessages] = useState<ReviewAskMessage[]>([]);
  const [latest, setLatest] = useState(latestRevisionNumber);
  const [body, setBody] = useState("");
  const [loading, setLoading] = useState(true);
  const [asking, setAsking] = useState(false);
  const [pendingTurn, setPendingTurn] = useState<PendingAskTurn | null>(null);
  const [error, setError] = useState("");
  const [driver, setDriver] = useState<ReviewAskDriver>(() => {
    const saved = window.localStorage.getItem(ASK_DRIVER_KEY);
    return saved === "claude"
      || saved === "codex"
      || saved === "opencode"
      || saved === "ollama"
      || saved === "openai"
      || saved === "litellm"
      ? saved
      : "configured";
  });
  const [model, setModel] = useState(() => window.localStorage.getItem(ASK_MODEL_KEY) ?? "");
  const [models, setModels] = useState<string[]>([]);
  const [modelsByDriver, setModelsByDriver] = useState<Partial<Record<ReviewAskDriver, string[]>>>({});
  const [handoffs, setHandoffs] = useState<Record<string, ReviewAskHandoffResult>>({});
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError("");
    void fetchReviewAsk(reviewId)
      .then((result) => {
        if (!live) return;
        setMessages(result.messages);
        setLatest(Math.max(latestRevisionNumber, result.latest_revision_number));
        setModels(result.ai?.models ?? []);
        setModelsByDriver(result.ai?.models_by_driver ?? {});
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
  }, [reviewId, latestRevisionNumber]);

  useEffect(() => {
    setLatest((current) => Math.max(current, latestRevisionNumber));
  }, [latestRevisionNumber]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages.length, pendingTurn?.status]);

  const visibleLatest = Math.max(latest, latestRevisionNumber);
  const sourceScope = useMemo(() => scopeLabel(scope), [scope]);
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
    window.localStorage.setItem(ASK_MODEL_KEY, nextModel);
  }, [driver, model, models, modelsByDriver]);

  const ask = async (raw: string) => {
    const question = raw.trim();
    if (!question || asking) return;
    const activeDriver = driver;
    const activeModel = model;
    setAsking(true);
    setError("");
    setBody("");
    setPendingTurn({
      question,
      driver: activeDriver,
      model: activeModel,
      status: "starting",
      elapsedMs: 0,
      error: "",
    });
    const input: ReviewAskInput = {
      revision_number: revisionNumber,
      ...(baseRevisionNumber != null && baseRevisionNumber !== revisionNumber
        ? { base_revision_number: baseRevisionNumber }
        : {}),
      body: question,
      ...(activeDriver !== "configured" ? { driver: activeDriver } : {}),
      ...(activeModel ? { model: activeModel } : {}),
      ...(scope?.context ? { context: scope.context } : {}),
      ...(scope?.target_unit_key ? { target_unit_key: scope.target_unit_key } : {}),
      ...(scope?.path ? { path: scope.path } : {}),
      ...(scope?.symbol ? { symbol: scope.symbol } : {}),
      ...(scope?.side ? { side: scope.side } : {}),
      ...(scope?.start_line ? { start_line: scope.start_line } : {}),
      ...(scope?.end_line ? { end_line: scope.end_line } : {}),
    };
    try {
      const result = await streamReviewAsk(reviewId, input, (event) => {
        if (event.type === "accepted") {
          setPendingTurn((current) => current ? { ...current, status: "working" } : current);
        } else if (event.type === "working") {
          setPendingTurn((current) => current
            ? { ...current, status: "working", elapsedMs: event.elapsed_ms }
            : current);
        }
      });
      setMessages((current) => [...current, result.human, result.assistant]);
      setLatest((current) => Math.max(current, result.latest_revision_number));
      setPendingTurn(null);
    } catch (reason) {
      const detail = String(reason instanceof Error ? reason.message : reason);
      setPendingTurn((current) => current
        ? { ...current, status: "failed", error: detail }
        : current);
    } finally {
      setAsking(false);
    }
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
    <section className="flex h-full min-h-0 flex-col bg-neutral-950" aria-label="Review chat">
      <header className="flex h-11 shrink-0 items-center gap-2 border-b border-neutral-800 px-3">
        <div className="flex h-6 w-6 items-center justify-center rounded-full bg-violet-950/70 text-violet-300">
          <Sparkles size={13} aria-hidden="true" />
        </div>
        <div className="min-w-0">
          <div className="text-[11px] font-medium leading-4 text-neutral-200">Review chat</div>
          <div className="text-[9px] leading-3 text-neutral-600">Snapshot {revisionNumber}</div>
        </div>
        <span className="flex-1" />
        {loading && (
          <span className="flex items-center gap-1 text-[9px] text-neutral-600" aria-label="Syncing conversation">
            <LoaderCircle size={10} className="animate-spin motion-reduce:animate-none" />
            Syncing
          </span>
        )}
        <button type="button" onClick={onClose} aria-label="Close Ask" className="review-icon-button h-6 w-6">
          <X size={13} />
        </button>
      </header>

      {visibleLatest > revisionNumber && (
        <div className="flex shrink-0 items-center gap-2 border-b border-sky-900/50 bg-sky-950/20 px-3 py-2 text-[10px] text-sky-200">
          <span>Snapshot {visibleLatest} available</span>
          <button type="button" onClick={onSwitchLatest} className="review-compact-action ml-auto h-6 px-2">
            Switch to latest
          </button>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        {!loading && messages.length === 0 && !pendingTurn && (
          <div className="mx-auto max-w-[340px] py-8 text-center">
            <div className="mx-auto flex h-8 w-8 items-center justify-center rounded-full border border-neutral-800 bg-neutral-900/70 text-violet-300">
              <Sparkles size={14} />
            </div>
            <div className="mt-3 text-sm font-medium text-neutral-200">Ask, challenge, or request a change</div>
            <p className="mx-auto mt-1 max-w-[290px] text-[10px] leading-4 text-neutral-500">
              Discuss this review like a chat. Ask why something changed, check an implementation, or request a concrete fix.
            </p>
            <div className="mt-4 flex flex-wrap justify-center gap-1.5">
              {SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  disabled={asking}
                  onClick={() => void ask(suggestion)}
                  className="rounded-full border border-neutral-800 bg-neutral-900/50 px-2.5 py-1.5 text-[10px] text-neutral-300 transition hover:border-neutral-700 hover:bg-neutral-900"
                >
                  {suggestion}
                </button>
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
                  <div className="my-3 flex items-center gap-2 text-[9px] uppercase tracking-[0.12em] text-neutral-700">
                    <span className="h-px flex-1 bg-neutral-900" />
                    <span>Snapshot {revision}</span>
                    <span className="h-px flex-1 bg-neutral-900" />
                  </div>
                )}
                {message.role === "human" ? (
                  <div className="ml-10 flex justify-end">
                    <div className="max-w-[92%] rounded-2xl rounded-br-md border border-neutral-800 bg-neutral-900/75 px-3 py-2">
                      <div className="whitespace-pre-wrap text-[11px] leading-5 text-neutral-100">{message.body}</div>
                    </div>
                  </div>
                ) : (
                  <div className="mr-1 flex items-start gap-2">
                    <div className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-violet-950/70 text-violet-300">
                      <Sparkles size={10} />
                    </div>
                    <div className="min-w-0 flex-1 pt-0.5">
                      <div className="whitespace-pre-wrap text-[11px] leading-5 text-neutral-200">{message.body}</div>
                      {message.references.length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {message.references.map((reference, index) => (
                            <button
                              key={`${reference.path}:${reference.start_line}:${index}`}
                              type="button"
                              onClick={() => onOpenReference(reference)}
                              className="rounded-full border border-neutral-800 bg-neutral-900/80 px-2 py-1 font-mono text-[9px] text-sky-300 transition hover:border-sky-900/70 hover:text-sky-200"
                            >
                              {referenceLabel(reference)}
                            </button>
                          ))}
                        </div>
                      )}
                      {action.kind === "agent_handoff" && action.handoff_instruction && (
                        <div className="mt-3 overflow-hidden rounded-xl border border-violet-900/50 bg-violet-950/10">
                          <div className="flex items-center gap-2 border-b border-violet-900/30 px-2.5 py-2">
                            <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-violet-300">Proposed change</span>
                            <span className="ml-auto truncate text-[9px] text-neutral-500">to {destination}</span>
                          </div>
                          <div className="whitespace-pre-wrap px-2.5 py-2 text-[10px] leading-4 text-neutral-300">
                            {action.handoff_instruction}
                          </div>
                          <div className="flex items-center gap-2 border-t border-violet-900/25 px-2.5 py-2">
                            <button
                              type="button"
                              disabled={!canSend}
                              onClick={() => void sendHandoff(message)}
                              className="review-toolbar-button-primary h-7 px-2 text-[10px]"
                              title={!delivery?.supported ? delivery?.reason : undefined}
                            >
                              {handoffState === "dispatching"
                                ? <LoaderCircle size={12} className="animate-spin motion-reduce:animate-none" />
                                : <Send size={12} />}
                              {deliveryLabel(handoffState)}
                            </button>
                            {handoffState === "sent" && (
                              <span className="text-[9px] text-emerald-400">Sent to {destination}; a new snapshot will appear when captured.</span>
                            )}
                            {!delivery?.supported && delivery?.reason && (
                              <span className="text-[9px] text-neutral-600">{delivery.reason}</span>
                            )}
                            {(localHandoff?.message || action.handoff?.last_error) && handoffState !== "sent" && (
                              <span className="text-[9px] text-amber-400">
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
              <div className="ml-10 flex justify-end">
                <div className="max-w-[92%] rounded-2xl rounded-br-md border border-neutral-800 bg-neutral-900/75 px-3 py-2">
                  <div className="whitespace-pre-wrap text-[11px] leading-5 text-neutral-100">{pendingTurn.question}</div>
                </div>
              </div>
              <div className="mr-1 flex items-start gap-2" aria-live="polite">
                <div className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-violet-950/70 text-violet-300">
                  {pendingTurn.status === "failed"
                    ? <X size={10} />
                    : <Sparkles size={10} className={pendingTurn.status === "working" ? "animate-pulse motion-reduce:animate-none" : ""} />}
                </div>
                <div className="min-w-0 flex-1 rounded-xl border border-neutral-900 bg-neutral-900/20 px-2.5 py-2">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-[10px] font-medium text-neutral-300">
                      {DRIVER_LABELS[pendingTurn.driver]}
                      {pendingTurn.model ? ` · ${pendingTurn.model}` : ""}
                    </span>
                    {pendingTurn.status !== "failed" && pendingTurn.elapsedMs > 0 && (
                      <span className="ml-auto tabular-nums text-[9px] text-neutral-600">
                        {Math.max(1, Math.floor(pendingTurn.elapsedMs / 1000))}s
                      </span>
                    )}
                  </div>
                  {pendingTurn.status === "failed" ? (
                    <div className="mt-1.5 flex items-start gap-2">
                      <span className="min-w-0 flex-1 text-[10px] leading-4 text-rose-300">{pendingTurn.error}</span>
                      <button
                        type="button"
                        onClick={() => void ask(pendingTurn.question)}
                        className="review-compact-action h-6 shrink-0 px-2"
                      >
                        <RotateCcw size={10} />
                        Retry
                      </button>
                    </div>
                  ) : (
                    <div className="mt-1.5 flex items-center gap-2 text-[10px] text-neutral-500">
                      <LoaderCircle size={11} className="animate-spin motion-reduce:animate-none" />
                      <span>{pendingTurn.status === "starting" ? "Starting…" : "Working on this snapshot…"}</span>
                    </div>
                  )}
                </div>
              </div>
            </>
          )}
          <div ref={endRef} />
        </div>

        {error && (
          <div className="mt-3 rounded-lg border border-rose-900/60 bg-rose-950/20 px-2.5 py-2 text-[10px] text-rose-300">
            {error}
          </div>
        )}
      </div>

      <form onSubmit={submit} className="shrink-0 border-t border-neutral-800 bg-neutral-950/95 p-2.5">
        <div className="rounded-xl border border-neutral-800 bg-neutral-900/35 p-2 shadow-sm transition focus-within:border-neutral-700 focus-within:bg-neutral-900/50">
          {sourceScope && (
            <div className="mb-1.5 flex max-w-full items-center gap-1.5">
              <div className="flex min-w-0 max-w-full items-center gap-1.5 rounded-full border border-sky-900/60 bg-sky-950/20 px-2 py-1 font-mono text-[9px] text-sky-200" title={sourceScope}>
                <span className="truncate">{sourceScope}</span>
                <button type="button" onClick={onClearScope} aria-label="Remove Ask context" className="shrink-0 text-sky-500 hover:text-sky-200">
                  <X size={10} />
                </button>
              </div>
            </div>
          )}
          <textarea
            rows={2}
            value={body}
            onChange={(event) => setBody(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                if (!asking) void ask(body);
              }
            }}
            placeholder={scope?.context
              ? "Ask about this context or request a change…"
              : scope?.path
                ? "Ask about this code or request a change…"
                : "Ask a question or request a change…"}
            aria-label="Ask Review"
            className="min-h-[44px] max-h-[132px] w-full resize-none bg-transparent px-1 py-1.5 text-[11px] leading-5 text-neutral-100 outline-none placeholder:text-neutral-600"
          />
          <div className="mt-1.5 flex items-center gap-1.5">
            <AskPicker
              label="Ask AI driver"
              value={driver}
              options={(Object.keys(DRIVER_LABELS) as ReviewAskDriver[]).map((item) => ({
                value: item,
                label: DRIVER_LABELS[item],
              }))}
              onChange={(value) => {
                const next = value as ReviewAskDriver;
                setDriver(next);
                window.localStorage.setItem(ASK_DRIVER_KEY, next);
                const nextModels = modelsByDriver[next] ?? defaultDriverModels(next, models);
                if (!model || !nextModels.includes(model)) {
                  const nextModel = next === "opencode" ? (nextModels[0] ?? "") : "";
                  setModel(nextModel);
                  if (nextModel) window.localStorage.setItem(ASK_MODEL_KEY, nextModel);
                  else window.localStorage.removeItem(ASK_MODEL_KEY);
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
                setModel(value);
                if (value) window.localStorage.setItem(ASK_MODEL_KEY, value);
                else window.localStorage.removeItem(ASK_MODEL_KEY);
              }}
            />
            <span className="flex-1" />
            <span className="hidden text-[9px] text-neutral-700 sm:inline">Enter to send · Shift+Enter for newline</span>
            <button
              type="submit"
              disabled={asking || body.trim() === ""}
              aria-label="Send Ask question"
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-neutral-100 text-neutral-950 shadow-sm transition hover:bg-white disabled:cursor-not-allowed disabled:bg-neutral-800 disabled:text-neutral-600"
            >
              <ArrowUp size={14} strokeWidth={2.4} />
            </button>
          </div>
        </div>
      </form>
    </section>
  );
}
