import { useState, type Ref } from "react";
import { CircleHelp, Copy, Download, MoreHorizontal, Send } from "lucide-react";

import type { DiffOverflow, DiffStylePreference } from "./diffModel";
import { REVIEW_CODE_THEMES, type ReviewCodeTheme } from "./reviewCodeTheme";
import type { ReaderOrder } from "./readerModel";
import type { ReviewPrimaryAction } from "./reviewPrimaryAction";

interface ReviewActionMenuProps {
  busy: boolean;
  readOnly: boolean;
  historical: boolean;
  allowRefresh: boolean;
  showFinish: boolean;
  reviewId: string;
  workspacePath?: string;
  primaryAction: ReviewPrimaryAction;
  order: ReaderOrder;
  codeTheme: ReviewCodeTheme;
  diffStyle: DiffStylePreference;
  diffOverflow: DiffOverflow;
  contextOpen: boolean;
  focusMode: boolean;
  feedbackTriggerRef?: Ref<HTMLButtonElement>;
  onRefresh(): void;
  onFinish(): void;
  onDownloadPatch(): void;
  onPrimaryAction(): void;
  onOrderChange(order: ReaderOrder): void;
  onCodeThemeChange(theme: ReviewCodeTheme): void;
  onDiffStyleChange(preference: DiffStylePreference): void;
  onToggleDiffOverflow(): void;
  onToggleContext(): void;
  onToggleFocus(): void;
  onShowShortcuts(): void;
  onOpenFeedback(): void;
}

type CopyState = "idle" | "copied" | "failed";

export default function ReviewActionMenu({
  busy,
  readOnly,
  historical,
  allowRefresh,
  showFinish,
  reviewId,
  workspacePath = "",
  primaryAction,
  order,
  codeTheme,
  diffStyle,
  diffOverflow,
  contextOpen,
  focusMode,
  feedbackTriggerRef,
  onRefresh,
  onFinish,
  onDownloadPatch,
  onPrimaryAction,
  onOrderChange,
  onCodeThemeChange,
  onDiffStyleChange,
  onToggleDiffOverflow,
  onToggleContext,
  onToggleFocus,
  onShowShortcuts,
  onOpenFeedback,
}: ReviewActionMenuProps) {
  const [copyIdState, setCopyIdState] = useState<CopyState>("idle");
  const [copyWorkspaceState, setCopyWorkspaceState] = useState<CopyState>("idle");

  const copyText = (value: string, setState: (state: CopyState) => void) => {
    const write = navigator.clipboard?.writeText;
    if (!write) {
      setState("failed");
      return;
    }
    void Promise.resolve(write.call(navigator.clipboard, value)).then(
      () => {
        setState("copied");
        window.setTimeout(() => setState("idle"), 1200);
      },
      () => setState("failed"),
    );
  };

  return (
    <details data-review-dropdown className="relative shrink-0">
      <summary
        aria-label="Review actions"
        className="review-icon-button cursor-pointer list-none [&::-webkit-details-marker]:hidden"
      >
        <MoreHorizontal size={14} />
      </summary>
      <div className="review-menu absolute right-0 top-full z-50 mt-2 w-64 max-h-[calc(100dvh-80px)] overflow-y-auto">
        <div className="px-2.5 pb-1.5 pt-2">
          <div className="review-kicker">Review</div>
        </div>
        {allowRefresh && (
          <button type="button" disabled={busy || readOnly} onClick={onRefresh} className="review-menu-item">
            Refresh local revision
          </button>
        )}
        {showFinish && (
          <button type="button" disabled={busy || historical} onClick={onFinish} className="review-menu-item">
            Finish review…
          </button>
        )}
        <button type="button" onClick={() => copyText(reviewId, setCopyIdState)} className="review-menu-item">
          <Copy size={12} />
          Copy review ID
          {copyIdState === "copied" && <span className="ml-auto text-emerald-400">Copied</span>}
          {copyIdState === "failed" && <span className="ml-auto text-amber-300">Copy failed</span>}
        </button>
        {workspacePath && (
          <button
            type="button"
            onClick={() => copyText(workspacePath, setCopyWorkspaceState)}
            className="review-menu-item"
            title={workspacePath}
          >
            <Copy size={12} />
            Copy workspace path
            {copyWorkspaceState === "copied" && <span className="ml-auto text-emerald-400">Copied</span>}
            {copyWorkspaceState === "failed" && <span className="ml-auto text-amber-300">Copy failed</span>}
          </button>
        )}
        <button
          type="button"
          onClick={onDownloadPatch}
          className="review-menu-item"
          title="Download the exact frozen Review patch; apply it with git apply."
        >
          <Download size={12} />
          Download review patch
          <span className="ml-auto font-mono text-[10px] text-neutral-600">git apply</span>
        </button>
        {primaryAction.kind === "send_feedback" && (
          <button
            type="button"
            disabled={busy || primaryAction.disabled}
            onClick={onPrimaryAction}
            className="review-menu-item"
            title="Opens the handoff preview. Nothing is sent automatically."
          >
            <Send size={12} />
            Preview & send to agent
            <span className="ml-auto text-[10px] text-neutral-600">manual</span>
          </button>
        )}

        <div className="my-1 border-t border-neutral-900" />
        <label className="block px-2.5 py-2 text-neutral-500">
          <span className="review-kicker mb-1.5 block">Review order</span>
          <select
            aria-label="Review order"
            disabled={busy}
            value={order}
            onChange={(event) => onOrderChange(event.target.value as ReaderOrder)}
            className="review-field h-8 w-full py-0 disabled:opacity-40"
          >
            <option value="recommended">Recommended</option>
            <option value="file">File order</option>
          </select>
        </label>
        <label className="block px-2.5 pb-2 text-neutral-500">
          <span className="review-kicker mb-1.5 block">Appearance</span>
          <select
            aria-label="Code theme"
            value={codeTheme}
            onChange={(event) => onCodeThemeChange(event.target.value as ReviewCodeTheme)}
            className="review-field h-8 w-full py-0 text-[10px]"
          >
            {REVIEW_CODE_THEMES.map((option) => (
              <option key={option.id} value={option.id}>{option.label}</option>
            ))}
          </select>
        </label>

        <div className="my-1 border-t border-neutral-900" />
        <label className="block px-2.5 py-2 text-neutral-500">
          <span className="review-kicker mb-1.5 block">Diff view</span>
          <select
            aria-label="Diff view"
            value={diffStyle}
            onChange={(event) => onDiffStyleChange(event.target.value as DiffStylePreference)}
            className="review-field h-8 w-full py-0 text-[10px]"
          >
            <option value="auto">Auto · responsive</option>
            <option value="split">Split</option>
            <option value="unified">Unified</option>
          </select>
          <span className="mt-1 block text-[9px] leading-4 text-neutral-700">
            Auto uses unified when the code area is tight and split when it is wide enough.
          </span>
        </label>
        <button type="button" onClick={onToggleDiffOverflow} className="review-menu-item">
          Code lines <span className="ml-auto text-neutral-500">{diffOverflow === "wrap" ? "Wrap" : "One line"} · w</span>
        </button>
        <button type="button" onClick={onToggleContext} className="review-menu-item">
          {contextOpen ? "Close" : "Open"} context
        </button>
        <button type="button" onClick={onToggleFocus} className="review-menu-item">
          {focusMode ? "Exit" : "Enter"} focus mode
        </button>
        <button type="button" onClick={onShowShortcuts} className="review-menu-item">
          Keyboard shortcuts <span className="ml-auto font-mono text-neutral-600">?</span>
        </button>
        <button
          ref={feedbackTriggerRef}
          type="button"
          onClick={onOpenFeedback}
          aria-label="Send LemonCrow feedback"
          className="review-menu-item border-t border-neutral-900"
        >
          <CircleHelp size={12} />
          Product feedback
        </button>
      </div>
    </details>
  );
}
