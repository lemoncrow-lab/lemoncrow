import { Button as UiButton } from "../components/ui/button";
import { Dialog, DialogBody, DialogClose, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { Input as UiInput } from "../components/ui/input";
import { Textarea as UiTextarea } from "../components/ui/textarea";
import { useRef, useState, type FormEvent, type ReactNode, type Ref } from "react";
import { createPortal } from "react-dom";
import {
  ArrowLeft,
  FileDiff,
  FileText,
  MessageSquareText,
  Network,
  ShieldCheck,
  Sparkles,
  Workflow,
  X,
  type LucideIcon,
} from "lucide-react";

import type { DiffOverflow, DiffStylePreference } from "./diffModel";
import { authenticatedFetch } from "./reviewApi";
import type { ReviewCodeTheme } from "./reviewCodeTheme";
import { finishAvailableInMenu, type ReviewPrimaryAction } from "./reviewPrimaryAction";
import type { ReviewEnvironment } from "./reviewEnvironment";
import ReviewIdentityBar from "./ReviewIdentityBar";
import { revisionShortLabel } from "./revisionLabel";
import ReviewActionMenu from "./ReviewActionMenu";
import type { ReviewPaletteAction } from "./ReviewCommandPalette";
import type { ReviewLandingReadiness, ReviewOverview, ReviewProgress } from "./types";

interface ReaderHeaderProps {
  overview: ReviewOverview;
  progress: ReviewProgress;
  diffStyle: DiffStylePreference;
  diffOverflow: DiffOverflow;
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
  /** Which projection/mode of the Review shell is showing. */
  view?: ReviewView;
  /** Persistent categories earned by this Change. Guided Review is never one. */
  availableViews?: readonly ReviewPersistentView[];
  primaryAction: ReviewPrimaryAction;
  hidePrimaryAction?: boolean;
  authorshipOverride?: { prefix: string; label: string };
  contextOverride?: { label: string; title?: string };
  environment?: ReviewEnvironment;
  allowRefresh?: boolean;
  extensionActions?: ReactNode;
  /** Unified feedback action and destination dropdown; replaces the primary header action. */
  handoffControls?: ReactNode;
  snapshotControls?: ReactNode;
  historical: boolean;
  onCodeThemeChange(theme: ReviewCodeTheme): void;
  onShowDirectory(): void;
  backToReviewHref?: string;
  /** Where Back returns when another surface (Code) opened this Review. */
  returnTo?: { href: string; label: string };
  /**
   * The in-Review backtrail's newest departure. It takes the existing Back
   * slot (no new control) and returns to that exact Review location.
   */
  backtrail?: { label: string; onBack(): void } | null;
  onShowComments(): void;
  onShowAsk?(): void;
  onViewChange?(view: ReviewPersistentView): void;
  onShowHistory(): void;
  onPrimaryAction(): void;
  onDownloadPatch(): void;
  onFinish(): void;
  onRefresh(): void;
  onDiffStyleChange(preference: DiffStylePreference): void;
  onToggleDiffOverflow(): void;
  onToggleContext(): void;
  onToggleFocus(): void;
  onShowShortcuts(): void;
  onShowOverview(): void;
  onPublishGitHub?: () => void;
  /** Expert correction actions (Fix all, CI, tests, …) reachable from the More menu. */
  correctionActions?: ReviewPaletteAction[];
  /** Compact Development action; omitted when the Change has no Development surface. */
  development?: ReaderDevelopmentSummary | null;
  developmentOpen?: boolean;
  onShowDevelopment?(): void;
  /** Review readiness control; hidden for historical Snapshots. */
  readiness?: { label: string; detail: string; text: string; className: string; shortText?: string } | null;
  /** Current landing readiness; the change-set header label follows it. */
  landing?: ReviewLandingReadiness | null;
  onShowReadiness?(): void;
  /** Degraded analysis signals; carried by the Status control, the single status authority. */
  uncertainSignals?: number;
  /**
   * Phone composition: the tools (Activity, Comments, Status, Ask) and the
   * primary action leave the header for this sticky bottom dock, so no primary
   * mobile action is ever found by horizontal scrolling. Undefined on desktop.
   */
  compactDock?: { target: HTMLElement | null; navigation?: ReactNode };
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

/** Guided Review is transient URL-backed navigation, not a persistent category. */
export type ReviewView = "brief" | "tour" | "implementation" | "map";
export type ReviewPersistentView = Exclude<ReviewView, "tour">;

const REVIEW_VIEWS: { value: ReviewPersistentView; label: string; hint: string; icon: LucideIcon }[] = [
  { value: "brief", label: "Brief", hint: "What changed, how it was verified, and where to look", icon: FileText },
  { value: "implementation", label: "Code", hint: "The exact Snapshot change, target by target", icon: FileDiff },
  { value: "map", label: "Map", hint: "What the change reaches and what it relies on", icon: Network },
];

const FEEDBACK_MAIL = "contact@lemoncrow.com";

type FeedbackState = "idle" | "sending" | "sent" | "failed";

/** Escape hatch when the server could not deliver a wish (telemetry off, or
 *  the sink refused it): hand the same text to the user's mail client. */
function feedbackMailto(message: string, contact: string): string {
  const body = [
    message,
    "",
    contact ? `Contact email: ${contact}` : "Contact email: not provided",
    "",
    "Sent from LemonCrow Review.",
  ].join("\n");
  return `mailto:${FEEDBACK_MAIL}?subject=${encodeURIComponent("LemonCrow Review feedback")}&body=${encodeURIComponent(body)}`;
}

export default function ReaderHeader({
  overview,
  progress,
  diffStyle,
  diffOverflow,
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
  view = "implementation",
  availableViews = ["brief", "implementation", "map"],
  primaryAction,
  hidePrimaryAction = false,
  authorshipOverride,
  contextOverride,
  environment,
  allowRefresh = true,
  extensionActions,
  handoffControls,
  snapshotControls,
  historical,
  onCodeThemeChange,
  onShowDirectory,
  backToReviewHref,
  returnTo,
  backtrail,
  onShowComments,
  onShowAsk,
  onViewChange,
  onShowHistory,
  onPrimaryAction,
  onDownloadPatch,
  onFinish,
  onRefresh,
  onDiffStyleChange,
  onToggleDiffOverflow,
  onToggleContext,
  onToggleFocus,
  onShowShortcuts,
  onShowOverview,
  onPublishGitHub,
  correctionActions,
  development,
  developmentOpen = false,
  onShowDevelopment,
  readiness,
  landing,
  onShowReadiness,
  uncertainSignals = 0,
  compactDock,
}: ReaderHeaderProps) {
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackText, setFeedbackText] = useState("");
  const [feedbackEmail, setFeedbackEmail] = useState("");
  const [feedbackState, setFeedbackState] = useState<FeedbackState>("idle");
  const feedbackTriggerRef = useRef<HTMLButtonElement>(null);
  const feedbackTextRef = useRef<HTMLTextAreaElement>(null);

  const closeFeedback = () => {
    setFeedbackOpen(false);
    setFeedbackText("");
    setFeedbackEmail("");
    setFeedbackState("idle");
  };

  const changeId = overview.session.ref || `r/${overview.session.id}`;
  const showFinishInMenu = finishAvailableInMenu({
    sessionStatus: overview.session.status,
    historical,
    revisionAdvanced,
    action: primaryAction,
  });

  const sendProductFeedback = async (event: FormEvent) => {
    event.preventDefault();
    const text = feedbackText.trim();
    if (!text || feedbackState === "sending") return;
    setFeedbackState("sending");
    try {
      const response = await authenticatedFetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          email: feedbackEmail.trim(),
          source: "review",
        }),
      });
      if (!response.ok) {
        setFeedbackState("failed");
        return;
      }
      const body = (await response.json().catch(() => null)) as { delivered?: string } | null;
      setFeedbackState(body?.delivered === "remote" ? "sent" : "failed");
    } catch {
      setFeedbackState("failed");
    }
  };

  const docked = Boolean(compactDock);
  const uncertain = uncertainSignals;
  const tools = (
    <>
      {docked && compactDock?.navigation}
      {development && onShowDevelopment && (
        <UiButton
          variant="ghost"
          size="compact"
          type="button"
          onClick={onShowDevelopment}
          aria-pressed={developmentOpen}
          aria-label={development.label}
          tooltip={development.label}
          className={developmentOpen ? "border-sky-800/70 bg-sky-950/30 text-sky-200" : ""}
          data-testid="reader-development-action"
        >
          <Workflow size={13} aria-hidden="true" />
          <span className="review-tool-label hidden lg:inline">Activity</span>
          {development.contributors > 0 && (
            <span className="font-mono text-caption text-neutral-400">{development.contributors}</span>
          )}
          {development.newerEvents > 0 && (
            <span className="review-live-dot" data-active={development.active ? "true" : "false"} aria-hidden="true" />
          )}
        </UiButton>
      )}
      <UiButton
        variant="ghost"
        size="compact"
        type="button"
        onClick={onShowComments}
        aria-label={commentState === "ready"
          ? `Review comments: ${openCommentCount} open, ${commentCount} total`
          : commentState === "loading" ? "Review comments: loading" : "Review comments: unavailable"}
        tooltip={commentState === "failed" ? "Comments unavailable" : "Comments"}
      >
        <MessageSquareText size={13} />
        <span className="review-tool-label hidden lg:inline">Comments</span>
        {commentState === "ready" && openCommentCount > 0 && <span className="font-mono text-caption text-neutral-400">{openCommentCount}</span>}
        {commentState !== "ready" && <span className="font-mono text-caption text-muted-foreground">—</span>}
      </UiButton>
      {readiness && onShowReadiness && (
        <UiButton
          variant="ghost"
          size="compact"
          type="button"
          onClick={onShowReadiness}
          aria-label={`Review Status: ${readiness.label}${uncertain > 0 ? ` · ${uncertain} uncertain signal${uncertain === 1 ? "" : "s"}` : ""}`}
          tooltip={readiness.detail}
          className={`review-reader-readiness ${readiness.className}`}
          data-review-status-authority=""
        >
          <ShieldCheck size={13} aria-hidden="true" />
          <span>{docked ? readiness.shortText ?? readiness.text : readiness.text}</span>
          {uncertain > 0 && <span className="text-caption text-amber-400" aria-hidden="true">· {docked ? `${uncertain}?` : `${uncertain} uncertain`}</span>}
        </UiButton>
      )}
      {onShowAsk && (
        <UiButton
          variant="ghost"
          size="compact"
          type="button"
          onClick={onShowAsk}
          aria-pressed={askOpen}
          aria-label="Ask about this review"
          tooltip="Ask about this change"
          className={askOpen ? "border-violet-800/70 bg-violet-950/30 text-violet-200" : ""}
        >
          <Sparkles size={13} />
          <span className="review-tool-label hidden lg:inline">Ask</span>
        </UiButton>
      )}
      {extensionActions}
    </>
  );
  const commitActions = (snapshotControls || handoffControls || (!handoffControls && !hidePrimaryAction)) ? (
    <div className="review-reader-commit-actions">
      {snapshotControls}
      {handoffControls}
      {!handoffControls && !hidePrimaryAction && (
        <UiButton
          variant="default"
          ref={finishButtonRef}
          type="button"
          disabled={busy || primaryAction.disabled}
          onClick={onPrimaryAction}
          tooltip={primaryAction.detail ?? ""}
          data-finish={primaryAction.kind === "finish" || undefined}
          className="px-3 review-reader-primary"
        >
          {busyLabel ?? primaryAction.label}
        </UiButton>
      )}
    </div>
  ) : null;

  return (
    <header className="review-shell shrink-0 border-b border-border/80 bg-background">
      <div className="sr-only" aria-live="polite">
        <span>{progress.target_count} review areas</span>
        <span>{progress.reviewed} reviewed</span>
        <span>{progress.changed_since_review} changed</span>
        <span>{progress.needs_changes} needs changes</span>
        <span>{progress.unknown} unknown</span>
        <span>· {revisionShortLabel(overview.revision)}</span>
      </div>
      <div className="review-reader-toolbar">
        {backToReviewHref ? (
          <UiButton asChild variant="ghost" size="icon" tooltip="Back to full review">
            <a href={backToReviewHref} aria-label="Back to full review"><ArrowLeft size={15} /></a>
          </UiButton>
        ) : backtrail ? (
          <UiButton
            variant="ghost"
            size="icon"
            type="button"
            onClick={backtrail.onBack}
            aria-label={`Back to ${backtrail.label}`}
            tooltip={`Back to ${backtrail.label}`}
            data-testid="review-backtrail"
          >
            <ArrowLeft size={15} />
          </UiButton>
        ) : returnTo ? (
          <UiButton asChild variant="ghost" size="icon" tooltip={returnTo.label}>
            <a href={returnTo.href} aria-label={returnTo.label}><ArrowLeft size={15} /></a>
          </UiButton>
        ) : (
          <UiButton
            variant="ghost"
            size="icon"
            type="button"
            onClick={onShowDirectory}
            aria-label="Back to reviews"
            tooltip="All reviews"
          >
            <ArrowLeft size={15} />
          </UiButton>
        )}

        <ReviewIdentityBar
          overview={overview}
          focusMode={focusMode}
          authorshipOverride={authorshipOverride}
          contextOverride={contextOverride}
          environment={environment}
          landing={landing}
          onShowOverview={onShowOverview}
          onShowHistory={onShowHistory}
        />

        <nav className="flex shrink-0 items-center gap-1" aria-label="Review workspace">
          {onViewChange && (
            <div role="group" aria-label="Review views" className="review-reader-views mr-1 inline-flex overflow-hidden rounded-md border border-border/80">
              {REVIEW_VIEWS.filter(({ value }) => availableViews.includes(value)).map(({ value, label, hint, icon: Icon }) => (
                <UiButton
                  key={value}
                  variant="ghost"
                  size="compact"
                  type="button"
                  onClick={() => onViewChange(value)}
                  aria-pressed={view === value || (view === "tour" && value === "brief")}
                  aria-label={label}
                  tooltip={hint}
                  className={`rounded-none border-0 ${(view === value || (view === "tour" && value === "brief")) ? "bg-accent text-foreground" : "text-neutral-400"}`}
                >
                  <Icon size={13} aria-hidden="true" />
                  <span className="review-tool-label hidden lg:inline">{label}</span>
                </UiButton>
              ))}
            </div>
          )}
          {!docked && tools}
        </nav>

        {!docked && commitActions}

        <div className="review-reader-menu-slot">
        <ReviewActionMenu
          busy={busy}
          readOnly={readOnly}
          historical={historical}
          allowRefresh={allowRefresh}
          showFinish={showFinishInMenu}
          nextAction={handoffControls ? primaryAction : undefined}
          onNextAction={onPrimaryAction}
          reviewId={changeId}
          workspacePath={environment?.review.local_workspace === false ? "" : overview.workspace_root ?? ""}
          codeTheme={codeTheme}
          diffStyle={diffStyle}
          diffOverflow={diffOverflow}
          contextOpen={contextOpen}
          focusMode={focusMode}
          feedbackTriggerRef={feedbackTriggerRef}
          onRefresh={onRefresh}
          onFinish={onFinish}
          onDownloadPatch={onDownloadPatch}
          onCodeThemeChange={onCodeThemeChange}
          onDiffStyleChange={onDiffStyleChange}
          onToggleDiffOverflow={onToggleDiffOverflow}
          onToggleContext={onToggleContext}
          onToggleFocus={onToggleFocus}
          onShowShortcuts={onShowShortcuts}
          onShowHistory={onShowHistory}
          onOpenFeedback={() => setFeedbackOpen(true)}
          settingsHref={!environment || environment.mode === "local" ? "/settings/ai" : "/settings"}
          onPublishGitHub={onPublishGitHub}
          correctionActions={correctionActions}
        />
        </div>
      </div>
      {docked && compactDock?.target && createPortal(
        <>
          {commitActions}
          <div className="review-reader-tools" role="group" aria-label="Review tools">{tools}</div>
        </>,
        compactDock.target,
      )}

      <Dialog open={feedbackOpen} onOpenChange={(open) => { if (!open) closeFeedback(); }}>
        <DialogContent
          className="max-w-md"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            feedbackTextRef.current?.focus();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            feedbackTriggerRef.current?.focus();
          }}
        >
          <form onSubmit={sendProductFeedback}>
            <DialogHeader>
              <div className="min-w-0 flex-1">
                <DialogTitle>Make a wish</DialogTitle>
                <p className="mt-0.5 text-caption text-muted-foreground">What would make Review better?</p>
              </div>
              <DialogClose asChild>
                <UiButton variant="ghost" size="icon" aria-label="Close feedback">
                  <X size={14} />
                </UiButton>
              </DialogClose>
            </DialogHeader>
            <DialogBody>
              {feedbackState === "sent" ? (
                <p className="text-body leading-5 text-emerald-400">Sent. Thank you — it’s in the wish backlog.</p>
              ) : (
                <>
                  <UiTextarea
                    uiSize="xs"
                    ref={feedbackTextRef}
                    required
                    value={feedbackText}
                    onChange={(event) => setFeedbackText(event.target.value)}
                    aria-label="Feedback"
                    rows={6}
                    placeholder="What worked, what was confusing, or what should change?"
                    className="min-h-0 text-body leading-5"
                  />
                  <label className="mt-3 block text-caption text-muted-foreground">
                    Email <span className="text-muted-foreground">optional</span>
                    <UiInput
                      uiSize="xs"
                      type="email"
                      value={feedbackEmail}
                      onChange={(event) => setFeedbackEmail(event.target.value)}
                      aria-label="Feedback contact email"
                      placeholder="you@example.com"
                      className="mt-1.5 text-body"
                    />
                  </label>
                  <p className="mt-1.5 text-caption leading-4 text-muted-foreground">
                    Add your email only if you’re okay with the developer contacting you about this feedback.
                  </p>
                  {feedbackState === "failed" && (
                    <p className="mt-3 text-caption leading-4 text-amber-300">
                      Couldn’t reach LemonCrow. Your wish is still here —{" "}
                      <a className="underline" href={feedbackMailto(feedbackText.trim(), feedbackEmail.trim())}>
                        email it instead
                      </a>
                      .
                    </p>
                  )}
                </>
              )}
            </DialogBody>
            <DialogFooter>
              <DialogClose asChild>
                <UiButton variant="outline" type="button">
                  {feedbackState === "sent" ? "Close" : "Cancel"}
                </UiButton>
              </DialogClose>
              {feedbackState !== "sent" && (
                <UiButton
                  variant="default"
                  type="submit"
                  disabled={!feedbackText.trim() || feedbackState === "sending"}
                >
                  {feedbackState === "sending" ? "Sending…" : "Send wish"}
                </UiButton>
              )}
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </header>
  );
}
