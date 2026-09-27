import { useRef, useState, type FormEvent, type ReactNode, type Ref } from "react";
import {
  ArrowLeft,
  MessageSquareText,
  Network,
  Send,
  ShieldCheck,
  Sparkles,
  Workflow,
  X,
} from "lucide-react";

import type { DiffOverflow, DiffStylePreference } from "./diffModel";
import type { ReviewCodeTheme } from "./reviewCodeTheme";
import type { ReaderOrder } from "./readerModel";
import type { ReviewPrimaryAction } from "./reviewPrimaryAction";
import { trapModalTab } from "./focusTrap";
import type { ReviewEnvironment } from "./reviewEnvironment";
import ReviewIdentityBar from "./ReviewIdentityBar";
import ReviewActionMenu from "./ReviewActionMenu";
import type { ReviewOverview, ReviewProgress } from "./types";

interface ReaderHeaderProps {
  overview: ReviewOverview;
  progress: ReviewProgress;
  diffStyle: DiffStylePreference;
  diffOverflow: DiffOverflow;
  order: ReaderOrder;
  focusMode: boolean;
  contextOpen: boolean;
  busy: boolean;
  busyLabel?: string;
  readOnly: boolean;
  revisionAdvanced: boolean;
  finishButtonRef?: Ref<HTMLButtonElement>;
  codeTheme: ReviewCodeTheme;
  commentCount: number;
  openCommentCount: number;
  commentState?: "loading" | "ready" | "failed";
  askOpen?: boolean;
  mapOpen?: boolean;
  primaryAction: ReviewPrimaryAction;
  authorshipOverride?: { prefix: string; label: string };
  contextOverride?: { label: string; title?: string };
  environment?: ReviewEnvironment;
  allowRefresh?: boolean;
  extensionActions?: ReactNode;
  historical: boolean;
  onCodeThemeChange(theme: ReviewCodeTheme): void;
  onShowDirectory(): void;
  onShowComments(): void;
  onShowAsk?(): void;
  onShowMap?(): void;
  onShowHistory(): void;
  onPrimaryAction(): void;
  onDownloadPatch(): void;
  onFinish(): void;
  onRefresh(): void;
  onOrderChange(order: ReaderOrder): void;
  onDiffStyleChange(preference: DiffStylePreference): void;
  onToggleDiffOverflow(): void;
  onToggleContext(): void;
  onToggleFocus(): void;
  onShowShortcuts(): void;
  onShowOverview(): void;
  onPublishGitHub?: () => void;
  /** Compact Development action; omitted when the Change has no Development surface. */
  development?: ReaderDevelopmentSummary | null;
  developmentOpen?: boolean;
  onShowDevelopment?(): void;
  /** Review readiness control; hidden for historical Snapshots. */
  readiness?: { label: string; detail: string; text: string; className: string } | null;
  onShowReadiness?(): void;
}

export interface ReaderDevelopmentSummary {
  /** Contributors frozen with the displayed Snapshot. */
  contributors: number;
  /** Development events recorded after the Snapshot's watermark. */
  newerEvents: number;
  newerFiles: number;
  /** An agent is working on this Change right now. */
  active: boolean;
  label: string;
}

const FEEDBACK_MAIL = "contact@lemoncrow.com";

export default function ReaderHeader({
  overview,
  progress,
  diffStyle,
  diffOverflow,
  order,
  focusMode,
  contextOpen,
  busy,
  busyLabel,
  readOnly,
  revisionAdvanced,
  finishButtonRef,
  codeTheme,
  commentCount,
  openCommentCount,
  commentState = "ready",
  askOpen = false,
  mapOpen = false,
  primaryAction,
  authorshipOverride,
  contextOverride,
  environment,
  allowRefresh = true,
  extensionActions,
  historical,
  onCodeThemeChange,
  onShowDirectory,
  onShowComments,
  onShowAsk,
  onShowMap,
  onShowHistory,
  onPrimaryAction,
  onDownloadPatch,
  onFinish,
  onRefresh,
  onOrderChange,
  onDiffStyleChange,
  onToggleDiffOverflow,
  onToggleContext,
  onToggleFocus,
  onShowShortcuts,
  onShowOverview,
  onPublishGitHub,
  development,
  developmentOpen = false,
  onShowDevelopment,
  readiness,
  onShowReadiness,
}: ReaderHeaderProps) {
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackText, setFeedbackText] = useState("");
  const [feedbackEmail, setFeedbackEmail] = useState("");
  const feedbackTriggerRef = useRef<HTMLButtonElement>(null);

  const closeFeedback = () => {
    setFeedbackOpen(false);
    window.requestAnimationFrame(() => feedbackTriggerRef.current?.focus());
  };

  const changeId = overview.session.ref || `r/${overview.session.id}`;
  const showFinishInMenu = overview.session.status === "open"
    && !historical
    && !revisionAdvanced
    && primaryAction.kind !== "finish";

  const sendProductFeedback = (event: FormEvent) => {
    event.preventDefault();
    const text = feedbackText.trim();
    if (!text) return;
    const contact = feedbackEmail.trim();
    const body = [
      text,
      "",
      contact ? `Contact email: ${contact}` : "Contact email: not provided",
      "",
      "Sent from LemonCrow Review.",
    ].join("\n");
    window.location.href = `mailto:${FEEDBACK_MAIL}?subject=${encodeURIComponent("LemonCrow Review feedback")}&body=${encodeURIComponent(body)}`;
    closeFeedback();
  };

  return (
    <header className="review-shell shrink-0 border-b border-neutral-800/80 bg-neutral-950">
      <div className="sr-only" aria-live="polite">
        <span>{progress.target_count} review areas</span>
        <span>{progress.reviewed} reviewed</span>
        <span>{progress.changed_since_review} changed</span>
        <span>{progress.needs_changes} needs changes</span>
        <span>{progress.unknown} unknown</span>
        <span>· Snapshot {overview.revision.revision_number}</span>
      </div>
      <div className="review-reader-toolbar">
        <button
          type="button"
          onClick={onShowDirectory}
          aria-label="Back to reviews"
          title="All reviews"
          className="review-icon-button"
        >
          <ArrowLeft size={15} />
        </button>

        <ReviewIdentityBar
          overview={overview}
          focusMode={focusMode}
          authorshipOverride={authorshipOverride}
          contextOverride={contextOverride}
          environment={environment}
          onShowOverview={onShowOverview}
          onShowHistory={onShowHistory}
        />

        <span className="review-reader-progress" aria-hidden="true">{progress.reviewed}<span> / {progress.target_count} reviewed</span></span>
        <nav className="flex shrink-0 items-center gap-1" aria-label="Review workspace">
          {readiness && onShowReadiness && (
            <button
              type="button"
              onClick={onShowReadiness}
              aria-label={`Review readiness: ${readiness.label}`}
              title={readiness.detail}
              className={`review-compact-action review-reader-readiness ${readiness.className}`}
            >
              <ShieldCheck size={13} aria-hidden="true" />
              <span>{readiness.text}</span>
            </button>
          )}
          <button
            type="button"
            onClick={onShowComments}
            aria-label={commentState === "ready"
              ? `Review comments: ${openCommentCount} open, ${commentCount} total`
              : commentState === "loading" ? "Review comments: loading" : "Review comments: unavailable"}
            title={commentState === "failed" ? "Comments unavailable" : "Comments"}
            className="review-compact-action"
          >
            <MessageSquareText size={13} />
            <span className="hidden lg:inline">Comments</span>
            {commentState === "ready" && openCommentCount > 0 && <span className="font-mono text-[10px] text-neutral-400">{openCommentCount}</span>}
            {commentState !== "ready" && <span className="font-mono text-[10px] text-neutral-600">—</span>}
          </button>
          {development && onShowDevelopment && (
            <button
              type="button"
              onClick={onShowDevelopment}
              aria-pressed={developmentOpen}
              aria-label={development.label}
              title={development.label}
              className={`review-compact-action ${developmentOpen ? "border-sky-800/70 bg-sky-950/30 text-sky-200" : ""}`}
              data-testid="reader-development-action"
            >
              <Workflow size={13} aria-hidden="true" />
              <span className="hidden lg:inline">Development</span>
              {development.contributors > 0 && (
                <span className="font-mono text-[10px] text-neutral-400">{development.contributors}</span>
              )}
              {development.newerEvents > 0 && (
                <span className="review-live-dot" data-active={development.active ? "true" : "false"} aria-hidden="true" />
              )}
            </button>
          )}
          {onShowMap && (
            <button
              type="button"
              onClick={onShowMap}
              aria-pressed={mapOpen}
              aria-label="Review map"
              title="System impact map"
              className={`review-compact-action ${mapOpen ? "border-cyan-800/70 bg-cyan-950/30 text-cyan-200" : ""}`}
            >
              <Network size={13} />
              <span className="hidden lg:inline">Map</span>
            </button>
          )}
          <button
            type="button"
            onClick={onShowAsk}
            aria-pressed={askOpen}
            aria-label="Ask about this review"
            title="Ask about this change"
            className={`review-compact-action ${askOpen ? "border-violet-800/70 bg-violet-950/30 text-violet-200" : ""}`}
          >
            <Sparkles size={13} />
            <span className="hidden lg:inline">Ask</span>
          </button>
          {extensionActions}
        </nav>

        <button
          ref={finishButtonRef}
          type="button"
          disabled={busy || primaryAction.disabled}
          onClick={onPrimaryAction}
          title={primaryAction.detail}
          className="review-toolbar-button-primary review-reader-primary"
        >
          {!busyLabel && primaryAction.kind === "send_feedback" && <Send size={13} aria-hidden="true" />}
          {busyLabel ?? primaryAction.label}
        </button>

        <ReviewActionMenu
          busy={busy}
          readOnly={readOnly}
          historical={historical}
          allowRefresh={allowRefresh}
          showFinish={showFinishInMenu}
          reviewId={changeId}
          workspacePath={environment?.review.local_workspace === false ? "" : overview.workspace_root ?? ""}
          primaryAction={primaryAction}
          order={order}
          codeTheme={codeTheme}
          diffStyle={diffStyle}
          diffOverflow={diffOverflow}
          contextOpen={contextOpen}
          focusMode={focusMode}
          feedbackTriggerRef={feedbackTriggerRef}
          onRefresh={onRefresh}
          onFinish={onFinish}
          onDownloadPatch={onDownloadPatch}
          onPrimaryAction={onPrimaryAction}
          onOrderChange={onOrderChange}
          onCodeThemeChange={onCodeThemeChange}
          onDiffStyleChange={onDiffStyleChange}
          onToggleDiffOverflow={onToggleDiffOverflow}
          onToggleContext={onToggleContext}
          onToggleFocus={onToggleFocus}
          onShowShortcuts={onShowShortcuts}
          onOpenFeedback={() => setFeedbackOpen(true)}
          onPublishGitHub={onPublishGitHub}
        />
      </div>

      {feedbackOpen && (
        <div
          className="review-sheet-backdrop items-center justify-center px-4"
          role="dialog"
          aria-modal="true"
          aria-label="LemonCrow feedback"
          onKeyDown={(event) => {
            if (trapModalTab(event)) return;
            if (event.key === "Escape") {
              event.stopPropagation();
              closeFeedback();
            }
          }}
        >
          <form onSubmit={sendProductFeedback} className="review-sheet w-full max-w-md rounded-[7px] p-4">
            <div className="flex items-center gap-3">
              <div className="min-w-0 flex-1">
                <div className="text-[12px] font-semibold text-neutral-100">Send feedback</div>
                <div className="mt-0.5 text-[10px] text-neutral-500">What would make Review better?</div>
              </div>
              <button type="button" onClick={closeFeedback} aria-label="Close feedback" className="review-icon-button">
                <X size={14} />
              </button>
            </div>
            <textarea
              autoFocus
              required
              value={feedbackText}
              onChange={(event) => setFeedbackText(event.target.value)}
              aria-label="Feedback"
              rows={6}
              placeholder="What worked, what was confusing, or what should change?"
              className="review-field mt-4 w-full resize-y leading-5"
            />
            <label className="mt-3 block text-[10px] text-neutral-500">
              Email <span className="text-neutral-700">optional</span>
              <input
                type="email"
                value={feedbackEmail}
                onChange={(event) => setFeedbackEmail(event.target.value)}
                aria-label="Feedback contact email"
                placeholder="you@example.com"
                className="review-field mt-1.5 w-full"
              />
            </label>
            <p className="mt-1.5 text-[10px] leading-4 text-neutral-600">
              Add your email only if you’re okay with the developer contacting you about this feedback.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" onClick={closeFeedback} className="review-toolbar-button">Cancel</button>
              <button type="submit" disabled={!feedbackText.trim()} className="review-toolbar-button-primary">Send email</button>
            </div>
          </form>
        </div>
      )}
    </header>
  );
}
