import { Button as UiButton } from "../components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu";
import { useState, type Ref } from "react";
import { CircleHelp, Copy, Download, EllipsisVertical, History, Send, Settings } from "lucide-react";

import type { DiffOverflow, DiffStylePreference } from "./diffModel";
import { REVIEW_CODE_THEMES, type ReviewCodeTheme } from "./reviewCodeTheme";
import type { ReviewPrimaryAction } from "./reviewPrimaryAction";
import type { ReviewPaletteAction } from "./ReviewCommandPalette";

interface ReviewActionMenuProps {
  busy: boolean;
  readOnly: boolean;
  historical: boolean;
  allowRefresh: boolean;
  showFinish: boolean;
  nextAction?: ReviewPrimaryAction;
  onNextAction?(): void;
  reviewId: string;
  workspacePath?: string;
  codeTheme: ReviewCodeTheme;
  diffStyle: DiffStylePreference;
  diffOverflow: DiffOverflow;
  contextOpen: boolean;
  focusMode: boolean;
  /** Stable element to return focus to once a menu-opened dialog (e.g. feedback) closes. */
  feedbackTriggerRef?: Ref<HTMLButtonElement>;
  onRefresh(): void;
  onFinish(): void;
  onDownloadPatch(): void;
  onCodeThemeChange(theme: ReviewCodeTheme): void;
  onDiffStyleChange(preference: DiffStylePreference): void;
  onToggleDiffOverflow(): void;
  onToggleContext(): void;
  onToggleFocus(): void;
  onShowShortcuts(): void;
  /** Snapshot history and compare: the header's Snapshot chip is hidden on phones. */
  onShowHistory?: () => void;
  onOpenFeedback(): void;
  settingsHref?: string;
  /** Present only for a Review opened from a GitHub pull request. */
  onPublishGitHub?: () => void;
  /** Expert correction actions; unavailable ones stay listed with their reason. */
  correctionActions?: ReviewPaletteAction[];
}

type CopyState = "idle" | "copied" | "failed";

const DIFF_STYLE_LABEL: Record<DiffStylePreference, string> = {
  auto: "Auto",
  split: "Split",
  unified: "Unified",
};

export default function ReviewActionMenu({
  busy,
  readOnly,
  historical,
  allowRefresh,
  showFinish,
  nextAction,
  onNextAction,
  reviewId,
  workspacePath = "",
  codeTheme,
  diffStyle,
  diffOverflow,
  contextOpen,
  focusMode,
  feedbackTriggerRef,
  onRefresh,
  onFinish,
  onDownloadPatch,
  onCodeThemeChange,
  onDiffStyleChange,
  onToggleDiffOverflow,
  onToggleContext,
  onToggleFocus,
  onShowShortcuts,
  onShowHistory,
  onOpenFeedback,
  settingsHref = "/settings",
  onPublishGitHub,
  correctionActions = [],
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

  const codeThemeLabel = REVIEW_CODE_THEMES.find((option) => option.id === codeTheme)?.label ?? codeTheme;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <UiButton ref={feedbackTriggerRef} variant="ghost" size="icon" aria-label="Review actions">
          <EllipsisVertical size={14} />
        </UiButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64 max-h-[calc(100dvh-80px)]">
        <DropdownMenuLabel>Review</DropdownMenuLabel>
        {nextAction && nextAction.kind !== "review_target" && onNextAction && (
          <DropdownMenuItem disabled={busy || nextAction.disabled} onSelect={onNextAction}>
            {nextAction.label}
          </DropdownMenuItem>
        )}
        {allowRefresh && (
          <DropdownMenuItem disabled={busy || readOnly} onSelect={onRefresh}>
            Update from current source
          </DropdownMenuItem>
        )}
        {showFinish && (
          <DropdownMenuItem disabled={busy || historical} onSelect={onFinish}>
            Finish review…
          </DropdownMenuItem>
        )}
        {onShowHistory && (
          <DropdownMenuItem onSelect={onShowHistory}>
            <History size={12} />
            Snapshot history
          </DropdownMenuItem>
        )}
        {onPublishGitHub && (
          <DropdownMenuItem
            disabled={busy || historical}
            onSelect={(event) => {
              event.preventDefault();
              onPublishGitHub();
            }}
          >
            <Send size={12} />
            Publish to GitHub…
          </DropdownMenuItem>
        )}
        {correctionActions.length > 0 && (
          <DropdownMenuSub>
            <DropdownMenuSubTrigger>Fix &amp; corrections</DropdownMenuSubTrigger>
            <DropdownMenuSubContent className="w-72">
              {correctionActions.map((action) => (
                <DropdownMenuItem
                  key={action.id}
                  className="items-start"
                  disabled={action.disabled}
                  onSelect={() => action.run()}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block">{action.label}</span>
                    {action.detail && <span className="block text-caption text-muted-foreground">{action.detail}</span>}
                  </span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        )}
        <DropdownMenuItem
          onSelect={(event) => {
            event.preventDefault();
            copyText(reviewId, setCopyIdState);
          }}
        >
          <Copy size={12} />
          Copy Change ID
          {copyIdState === "copied" && <span className="ml-auto text-emerald-400">Copied</span>}
          {copyIdState === "failed" && <span className="ml-auto text-amber-300">Copy failed</span>}
        </DropdownMenuItem>
        {workspacePath && (
          <DropdownMenuItem
            className="items-start"
            onSelect={(event) => {
              event.preventDefault();
              copyText(workspacePath, setCopyWorkspaceState);
            }}
          >
            <Copy size={12} className="mt-0.5 shrink-0" />
            <span className="min-w-0 flex-1">
              <span className="block">Copy workspace path</span>
              <span className="block truncate text-caption text-muted-foreground">{workspacePath}</span>
            </span>
            {copyWorkspaceState === "copied" && <span className="shrink-0 text-emerald-400">Copied</span>}
            {copyWorkspaceState === "failed" && <span className="shrink-0 text-amber-300">Copy failed</span>}
          </DropdownMenuItem>
        )}
        <DropdownMenuItem className="items-start" onSelect={onDownloadPatch}>
          <Download size={12} className="mt-0.5 shrink-0" />
          <span className="min-w-0 flex-1">
            <span className="block">Download Snapshot patch</span>
            <span className="block text-caption text-muted-foreground">Apply with git apply</span>
          </span>
        </DropdownMenuItem>

        <DropdownMenuSeparator />
        <DropdownMenuSub>
          <DropdownMenuSubTrigger>
            Appearance
            <span className="ml-auto pl-3 text-caption text-muted-foreground">{codeThemeLabel}</span>
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent>
            <DropdownMenuRadioGroup value={codeTheme} onValueChange={(value) => onCodeThemeChange(value as ReviewCodeTheme)}>
              {REVIEW_CODE_THEMES.map((option) => (
                <DropdownMenuRadioItem key={option.id} value={option.id}>{option.label}</DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuSubContent>
        </DropdownMenuSub>

        <DropdownMenuSeparator />
        <DropdownMenuSub>
          <DropdownMenuSubTrigger>
            Diff view
            <span className="ml-auto pl-3 text-caption text-muted-foreground">{DIFF_STYLE_LABEL[diffStyle]}</span>
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent>
            <DropdownMenuRadioGroup value={diffStyle} onValueChange={(value) => onDiffStyleChange(value as DiffStylePreference)}>
              <DropdownMenuRadioItem value="auto">Auto · responsive</DropdownMenuRadioItem>
              <DropdownMenuRadioItem value="split">Split</DropdownMenuRadioItem>
              <DropdownMenuRadioItem value="unified">Unified</DropdownMenuRadioItem>
            </DropdownMenuRadioGroup>
          </DropdownMenuSubContent>
        </DropdownMenuSub>
        <DropdownMenuItem
          onSelect={(event) => {
            event.preventDefault();
            onToggleDiffOverflow();
          }}
        >
          Code lines <span className="ml-auto text-muted-foreground">{diffOverflow === "wrap" ? "Wrap" : "One line"} · w</span>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={onToggleContext}>
          {contextOpen ? "Close" : "Open"} context
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={onToggleFocus}>
          {focusMode ? "Exit" : "Enter"} focus mode
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={onShowShortcuts}>
          Keyboard shortcuts <span className="ml-auto font-mono text-muted-foreground">?</span>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <a href={settingsHref}>
            <Settings size={12} />
            Settings
          </a>
        </DropdownMenuItem>
        <DropdownMenuItem className="items-start" onSelect={onOpenFeedback} aria-label="Send LemonCrow feedback">
          <CircleHelp size={12} className="mt-0.5 shrink-0" />
          <span className="min-w-0 flex-1">
            <span className="block">Make a wish</span>
            <span className="block text-caption text-muted-foreground">Tell LemonCrow what to build next</span>
          </span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
