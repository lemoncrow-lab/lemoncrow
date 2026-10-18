import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";
import { Textarea as UiTextarea } from "../components/ui/textarea";
/**
 * A comment thread, drawn inside the diff at the line its anchor points to.
 *
 * Mounted through `@pierre/diffs`' `renderAnnotation` slot: the viewer owns the
 * row it sits in, this component owns everything inside it, and the only thing
 * that crosses between them is a marker key. No line number is read back out
 * (spec §5.4) — the position on screen is a consequence of the server's anchor,
 * never a source for it.
 *
 * The composer is the same component in its empty state, so "leave a comment"
 * and "read a comment" look like one thing in one place instead of a popover
 * that has to be positioned twice.
 *
 * Tailwind classes are correct here, unlike in `lineMarks.DECORATION_CSS`: the
 * React `renderAnnotation` slot mounts this subtree in the **light DOM** and
 * slots it into the viewer, so the app's stylesheet reaches it. Only the line
 * decorations, which are stamped onto nodes *inside* the shadow root, need CSS
 * injected through `unsafeCSS`. Verified in a browser — the card's root reports
 * `getRootNode() instanceof ShadowRoot === false`.
 */

import { useEffect, useRef, useState } from "react";
import { Check, LoaderCircle, MessageSquareText, PencilLine, Reply, RotateCcw, Sparkles, X } from "lucide-react";

import "./reviewUi.css";

import {
  annotationIdentityColor,
  KIND_ACCENTS,
  KIND_LABELS,
  KIND_ORDER,
  annotationIdentityLabel,
  annotationProducerDetail,
  annotationProducerTitle,
  anchorAccent,
  annotationSource,
  isHumanJudgment,
  locationLabel,
  repliesFor,
} from "./annotationModel";
import { BelowThresholdList } from "./findingThreshold";
import type { Annotation, AnnotationKind, FindingDismissalReason, FindingDisposition } from "./types";
import { useReviewCorrectionsContext } from "./reviewCorrections";
import { actionProvenance, isActionInFlight, type ActionTone } from "./reviewActionsModel";
import { PublishedFeedbackControls } from "./feedbackSync";
import { ThreadSummary } from "./ThreadSummary";

const ACTION_TONE: Record<ActionTone, string> = {
  progress: "border-sky-900/55 bg-sky-950/15 text-sky-300",
  done: "border-emerald-900/55 bg-emerald-950/15 text-emerald-300",
  warning: "border-amber-900/55 bg-amber-950/15 text-amber-300",
  muted: "border-border/70 text-muted-foreground",
};

export interface AnnotationCardProps {
  comments: Annotation[];
  /** Every annotation in the review, so replies can be found for each comment. */
  all: Annotation[];
  label: string;
  composing: boolean;
  busy: boolean;
  /** Whether this composer is attached to a current ReviewTarget. */
  targetMarkAvailable?: boolean;
  draftBody?: string;
  draftKind?: AnnotationKind;
  draftMarkTarget?: boolean;
  /** Changes whenever the draft's anchor moves, so the composer re-takes focus. */
  draftFocusKey?: string;
  onDraftChange?(fields: { body?: string; kind?: AnnotationKind; markTarget?: boolean }): void;
  onAsk?(): void;
  onSuggestEdit?(): void;
  onEditSource?(): void;
  onSubmit(body: string, kind: AnnotationKind, parentId: string, markTarget: boolean): void;
  onCancel(): void;
  onResolve(id: string): void;
  onReopen(id: string): void;
  onFindingDisposition?(id: string, disposition: FindingDisposition, reason?: FindingDismissalReason): void;
  onFindingRequestChange?(comment: Annotation): void;
  onFindingFix?(comment: Annotation): void;
  onFindingAsk?(comment: Annotation): void;
}

function Composer({
  label,
  busy,
  parentId,
  targetMarkAvailable,
  initialBody = "",
  initialKind = "comment",
  initialMarkTarget = true,
  focusKey = "",
  onDraftChange,
  onAsk,
  onSuggestEdit,
  onEditSource,
  onSubmit,
  onCancel,
}: {
  focusKey?: string;
  label: string;
  busy: boolean;
  parentId: string;
  targetMarkAvailable: boolean;
  initialBody?: string;
  initialKind?: AnnotationKind;
  initialMarkTarget?: boolean;
  onDraftChange?(fields: { body?: string; kind?: AnnotationKind; markTarget?: boolean }): void;
  onAsk?(): void;
  onSuggestEdit?(): void;
  onEditSource?(): void;
  onSubmit(body: string, kind: AnnotationKind, parentId: string, markTarget: boolean): void;
  onCancel(): void;
}) {
  const [body, setBody] = useState(initialBody);
  const [kind, setKind] = useState<AnnotationKind>(initialKind);
  const [markTarget, setMarkTarget] = useState(initialMarkTarget);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // `autoFocus` only fires on mount. A drag-selection in the diff finishes on
  // the gutter after the composer mounted (and re-anchors it when the range
  // grows), so take focus again once the pointer interaction has settled.
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      const input = inputRef.current;
      if (!input || document.activeElement === input) return;
      input.focus({ preventScroll: true });
      // A composer opened from a target header anchors at the end of its
      // change, which can be off-screen; bring it into view without the jump
      // a focus scroll makes.
      (input.closest(".review-composer") ?? input).scrollIntoView?.({ block: "nearest" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [focusKey]);
  const canMarkTarget = targetMarkAvailable && parentId === "" && kind === "request_change";
  const submitLabel = parentId
    ? "Reply"
    : kind === "request_change"
      ? "Request changes"
      : kind === "suggestion"
        ? "Add suggestion"
        : "Add comment";
  const placeholder = parentId
    ? "Write a reply…"
    : kind === "request_change"
      ? "What needs to change?"
      : kind === "suggestion"
        ? "What do you suggest?"
        : "Leave feedback…";
  const submit = () => {
    if (busy || body.trim() === "") return;
    onSubmit(body, kind, parentId, canMarkTarget && markTarget);
  };
  return (
    <div className="review-composer" aria-busy={busy}>
      <div className="review-composer-heading">
        <MessageSquareText size={16} className="text-neutral-400" aria-hidden="true" />
        <span>Draft · {label}</span>
      </div>
      <UiTextarea
        unstyled
        ref={inputRef}
        autoFocus
        rows={2}
        aria-label={parentId ? "Reply text" : "Comment text"}
        aria-keyshortcuts="Control+Enter Meta+Enter"
        value={body}
        onChange={(event) => {
          setBody(event.target.value);
          if (parentId === "") onDraftChange?.({ body: event.target.value });
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            // Escape cancels an IME candidate; it must never reach the draft.
            if (event.nativeEvent.isComposing) return;
            event.preventDefault();
            // Escape is a reflex key: typed text is discarded only on purpose.
            if (body.trim() !== "" && !window.confirm("Discard this unsent comment?")) return;
            onCancel();
            return;
          }
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            submit();
          }
        }}
        placeholder={placeholder}
        className="review-composer-input"
      />
      <div className="review-composer-options" role="group" aria-label="Feedback type">
        {KIND_ORDER.filter((option) => option !== "suggestion" || !(onSuggestEdit || onEditSource)).map((option) => (
          <UiButton unstyled
            key={option}
            type="button"
            aria-pressed={kind === option}
            onClick={() => {
              setKind(option);
              if (parentId === "") onDraftChange?.({ kind: option });
            }}
            className={`review-composer-kind ${
              kind === option ? KIND_ACCENTS[option] : "border-transparent text-neutral-400 hover:bg-neutral-800/50 hover:text-foreground"
            }`}
          >
            {KIND_LABELS[option]}
          </UiButton>
        ))}
        {!parentId && onAsk && (
          <UiButton unstyled
            type="button"
            onClick={onAsk}
            className="review-composer-kind border-violet-900/70 text-violet-300"
            tooltip="Ask LemonCrow about the selected code without creating review feedback"
          >
            <Sparkles size={11} aria-hidden="true" />
            Ask
          </UiButton>
        )}
        {!parentId && (onSuggestEdit || onEditSource) && (
          <div className="review-composer-source-actions" role="group" aria-label="Source actions">
            {onSuggestEdit && (
              <UiButton unstyled
                type="button"
                onClick={onSuggestEdit}
                className="review-composer-kind review-composer-kind-source border-violet-900/70 text-violet-300"
                tooltip="Save a concrete patch without changing the working source"
              >
                <PencilLine size={11} aria-hidden="true" />
                Suggest edit
              </UiButton>
            )}
            {onEditSource && (
              <UiButton unstyled
                type="button"
                onClick={onEditSource}
                className="review-composer-kind review-composer-kind-source border-sky-900/70 text-sky-300"
                tooltip="Edit the trusted working source through the guarded proposal path"
              >
                Edit source
              </UiButton>
            )}
          </div>
        )}
      </div>
        {canMarkTarget && (
          <label className="review-composer-mark">
            <UiInput unstyled
              type="checkbox"
              checked={markTarget}
              onChange={(event) => {
                setMarkTarget(event.target.checked);
                if (parentId === "") onDraftChange?.({ markTarget: event.target.checked });
              }}
              className="accent-rose-500"
            />
            Also mark target needs changes
          </label>
        )}
      <div className="review-composer-footer">
        <span className="review-composer-shortcut"><kbd>Ctrl/⌘</kbd> + <kbd>Enter</kbd> to add</span>
        <UiButton size="xs" type="button" onClick={onCancel}>
          <X size={14} aria-hidden="true" />
          Cancel
        </UiButton>
        <UiButton
          variant="default"
          size="xs"
          type="button"
          disabled={busy || body.trim() === ""}
          onClick={submit}
        >
          {busy ? <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <Check size={14} aria-hidden="true" />}
          {busy ? "Saving…" : submitLabel}
        </UiButton>
      </div>
    </div>
  );
}

function Comment({
  comment,
  replies,
  busy,
  onResolve,
  onReopen,
  onReply,
  onFindingDisposition,
  onFindingRequestChange,
  onFindingFix,
  onFindingAsk,
}: {
  comment: Annotation;
  replies: Annotation[];
  busy: boolean;
  onResolve(id: string): void;
  onReopen(id: string): void;
  onReply(id: string): void;
  onFindingDisposition?(id: string, disposition: FindingDisposition, reason?: FindingDismissalReason): void;
  onFindingRequestChange?(comment: Annotation): void;
  onFindingFix?(comment: Annotation): void;
  onFindingAsk?(comment: Annotation): void;
}) {
  const resolved = comment.state === "resolved";
  const [dismissing, setDismissing] = useState(false);
  const source = annotationSource(comment);
  const human = isHumanJudgment(comment);
  const sourceDetail = annotationProducerDetail(comment);
  const identityColor = annotationIdentityColor(comment);
  const aiFinding = source === "ai_review";
  const findingDisposition = comment.finding_disposition || (comment.anchored ? "open" : "superseded_stale");
  const blocking = human && comment.kind === "request_change" && !resolved;
  // Which controlled action addressed this finding, if any. Fix routes through
  // that preview (exact author -> preferred agent -> copied prompt) when the
  // reader has a writable current Snapshot; the action never sets a disposition.
  const corrections = useReviewCorrectionsContext();
  const latestAction = corrections?.byFinding.get(comment.id);
  const fixInFlight = latestAction ? isActionInFlight(latestAction) : false;
  const provenance = latestAction ? actionProvenance(latestAction) : null;
  const fix = corrections?.canFix
    ? () => corrections.openFix([comment.id])
    : onFindingFix
      ? () => onFindingFix(comment)
      : undefined;
  return (
    <article className={`review-thread review-thread-src-${source} ${blocking ? "review-thread-blocking" : ""} ${resolved ? "review-thread-resolved" : ""}`}>
      <div className="review-thread-heading">
        <span className={`review-thread-source ${identityColor}`}>{annotationIdentityLabel(comment)}</span>
        {sourceDetail && <span className="review-thread-author" title={annotationProducerTitle(comment)}>{sourceDetail}</span>}
        {human && comment.kind !== "comment" && (
          <span className={`rounded-md border px-1.5 py-0.5 ${KIND_ACCENTS[comment.kind]}`}>{KIND_LABELS[comment.kind]}</span>
        )}
        {comment.origin_revision_number != null && (
          <span
            className="review-thread-symbol font-mono"
            title={`Created on Snapshot ${comment.origin_revision_number}`}
          >
            {comment.relation_to_revision === "inherited" ? "from " : ""}Snapshot {comment.origin_revision_number}
          </span>
        )}
        {!comment.file_level && comment.end_line > comment.start_line && (
          <span className="review-thread-symbol font-mono">{locationLabel(comment.start_line, comment.end_line)}</span>
        )}
        {comment.owning_change && (
          <span
            className="review-thread-symbol"
            title={`${comment.owning_change.reason}. Feedback and fixes for this code belong to that Change.`}
            data-testid="annotation-owning-change"
          >
            belongs to {comment.owning_change.change_ref.slice(0, 10)}
          </span>
        )}
        {comment.symbol ? (
          <span className="review-thread-symbol">in {comment.symbol}</span>
        ) : (
          comment.origin_symbol && (
            <span className="review-thread-symbol">originally in {comment.origin_symbol}</span>
          )
        )}
        <span className="flex-1" />
        {comment.created_at && <time dateTime={comment.created_at} className="review-thread-date">{new Date(comment.created_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</time>}
        {resolved && (
          <span className="inline-flex items-center gap-1 text-emerald-400">
            <Check size={13} aria-hidden="true" />
            Resolved{comment.resolved_revision_number != null ? ` · Snapshot ${comment.resolved_revision_number}` : ""}
          </span>
        )}
      </div>
      <div className="review-thread-content">
      {comment.title && <div className="review-thread-title">{comment.title}</div>}
      <div className="review-thread-body">{comment.body}</div>
      {/*
        Anchor provenance is useful only when the comment had to be re-found.
        An unchanged file is the normal case and does not deserve UI chrome.
        Surface relocation uncertainty; keep exact/unchanged anchoring silent.
      */}
      {!comment.anchor_exact && (
        <div
          title={comment.anchor_detail}
          className={`review-thread-anchor ${anchorAccent(comment)}`}
        >
          Re-anchored · {comment.anchor_method_label || comment.anchor_method}
        </div>
      )}
      {human && comment.author_response === "addressed" && !resolved && (
        <div className="w-fit rounded-md border border-sky-900/55 bg-sky-950/15 px-2 py-1 text-caption font-medium text-sky-300">
          marked addressed{comment.author_addressed_revision_number != null ? ` in Snapshot ${comment.author_addressed_revision_number}` : ""} · re-review
        </div>
      )}
      {comment.evidence && comment.evidence.length > 0 && (
        <details className="review-thread-evidence">
          <summary className="cursor-pointer">evidence · {comment.evidence.length}</summary>
          <ul className="pl-4 pt-1">
            {comment.evidence.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </details>
      )}
      {comment.rule_refs && comment.rule_refs.length > 0 && (
        <details className="review-thread-evidence">
          <summary className="cursor-pointer">review rules · {comment.rule_refs.length}</summary>
          <ul className="pl-4 pt-1">
            {comment.rule_refs.map((item) => <li key={item} className="break-all font-mono">{item}</li>)}
          </ul>
        </details>
      )}
      {aiFinding && (comment.agent_host || comment.agent_model || comment.confidence != null) && (
        <details className="review-thread-evidence">
          <summary className="cursor-pointer">Run details</summary>
          <div className="pt-1 text-caption text-muted-foreground">
            {comment.agent_host && <div>Provider: {comment.agent_host}</div>}
            {comment.agent_model && <div>Model: {comment.agent_model}</div>}
            {comment.confidence != null && <div>Internal confidence: {Math.round(comment.confidence * 100)}%</div>}
          </div>
        </details>
      )}
      {human && comment.author_response === "addressed" && !resolved && comment.author_response_source_id && (
        <div className="font-mono text-caption text-sky-500/80">
          addressed by {comment.author_response_source_id}; only you can resolve this comment
        </div>
      )}
      {replies.length > 0 && (
        <div className="review-thread-replies">
          {replies.map((reply) => (
            <div key={reply.id} className="review-thread-reply">
              <div className="mb-1 flex items-center gap-2 text-body font-medium text-neutral-400">
                <span className={annotationIdentityColor(reply)}>{annotationIdentityLabel(reply)}</span>
                <span title={annotationProducerTitle(reply)}>{annotationProducerDetail(reply)}</span>
                {reply.kind !== "comment" && (
                  <span className={`rounded-md border px-1.5 py-0.5 text-caption ${KIND_ACCENTS[reply.kind]}`}>{KIND_LABELS[reply.kind]}</span>
                )}
              </div>
              <div className="review-thread-body">{reply.body}</div>
            </div>
          ))}
        </div>
      )}
      {replies.length >= 2 && comment.review_id && (
        <ThreadSummary reviewId={comment.review_id} threadId={comment.id} messages={[comment, ...replies]} />
      )}
      {latestAction && provenance && (
        <div
          className={`flex w-fit flex-wrap items-center gap-2 rounded-md border px-2 py-1 text-caption font-medium ${ACTION_TONE[provenance.tone]}`}
          data-testid="finding-action-provenance"
          title={latestAction.events.map((event) => `${event.kind}${event.detail ? `: ${event.detail}` : ""}`).join("\n")}
        >
          <span>{provenance.text}</span>
          {fixInFlight && corrections && (
            <UiButton
              variant="ghost"
              size="compact"
              className="h-5"
              type="button"
              disabled={busy}
              onClick={() => corrections.cancel(latestAction.id)}
            >
              Cancel
            </UiButton>
          )}
        </div>
      )}
      {aiFinding && (
        <div className="review-thread-actions" aria-label="AI finding actions">
          <span className="mr-1 text-caption text-muted-foreground">
            {findingDisposition === "confirmed_by_reviewer" ? "Confirmed"
              : findingDisposition === "dismissed" ? "Dismissed"
              : findingDisposition === "fixed" ? "Fixed"
              : findingDisposition === "superseded_stale" ? "Needs revalidation"
              : "Open"}
          </span>
          {findingDisposition !== "dismissed" && findingDisposition !== "fixed" && findingDisposition !== "superseded_stale" && (
            <>
              {onFindingRequestChange && (
                <UiButton
                  variant="ghost"
                  size="compact"
                  className="h-6"
                  type="button"
                  disabled={busy}
                  onClick={() => onFindingRequestChange(comment)}
                >
                  Request change
                </UiButton>
              )}
              <UiButton
                variant="ghost"
                size="compact"
                className="h-6"
                type="button"
                disabled={busy}
                onClick={() => setDismissing((current) => !current)}
                aria-expanded={dismissing}
              >
                Dismiss
              </UiButton>
              {fix && (
                <UiButton
                  variant="ghost"
                  size="compact"
                  className="h-6"
                  type="button"
                  disabled={busy || fixInFlight}
                  onClick={fix}
                >
                  Fix
                </UiButton>
              )}
              {onFindingAsk && (
                <UiButton
                  variant="ghost"
                  size="compact"
                  className="h-6"
                  type="button"
                  disabled={busy}
                  onClick={() => onFindingAsk(comment)}
                >
                  Ask
                </UiButton>
              )}
            </>
          )}
          {findingDisposition === "dismissed" && onFindingDisposition && (
            <UiButton
              variant="ghost"
              size="compact"
              className="h-6"
              type="button"
              disabled={busy}
              onClick={() => onFindingDisposition(comment.id, "open")}
            >
              Reopen
            </UiButton>
          )}
        </div>
      )}
      {aiFinding && dismissing && onFindingDisposition && (
        <div className="mt-1 flex flex-wrap items-center gap-1 rounded-md border border-border/80 px-2 py-1.5" role="group" aria-label="Dismiss finding reason">
          <span className="mr-1 text-caption text-muted-foreground">Why dismiss?</span>
          {([
            ["incorrect", "Incorrect"],
            ["accepted_risk", "Accepted risk"],
            ["duplicate", "Duplicate"],
            ["not_applicable", "Not applicable"],
          ] as const).map(([reason, label]) => (
            <UiButton
              key={reason}
              variant="ghost"
              size="compact"
              className="h-6"
              type="button"
              disabled={busy}
              onClick={() => {
                setDismissing(false);
                onFindingDisposition(comment.id, "dismissed", reason);
              }}
            >
              {label}
            </UiButton>
          ))}
        </div>
      )}
      {human && (
        <div className="review-thread-actions">
          <UiButton
            variant="ghost"
            size="compact"
            className="h-6"
            type="button"
            disabled={busy}
            onClick={() => onReply(comment.id)}
          >
            <Reply size={13} aria-hidden="true" />
            Reply
          </UiButton>
          <UiButton
            variant="ghost"
            size="compact"
            className="h-6"
            type="button"
            disabled={busy}
            onClick={() => (resolved ? onReopen(comment.id) : onResolve(comment.id))}
          >
            {resolved ? <RotateCcw size={13} aria-hidden="true" /> : <Check size={13} aria-hidden="true" />}
            {resolved ? "Reopen" : "Resolve"}
          </UiButton>
        </div>
      )}
      {human && !comment.parent_id && <PublishedFeedbackControls comment={comment} busy={busy} />}
      </div>
    </article>
  );
}
export default function AnnotationCard({
  comments,
  all,
  label,
  composing,
  busy,
  targetMarkAvailable = false,
  draftBody = "",
  draftKind = "comment",
  draftMarkTarget = true,
  draftFocusKey = "",
  onDraftChange,
  onAsk,
  onSuggestEdit,
  onEditSource,
  onSubmit,
  onCancel,
  onResolve,
  onReopen,
  onFindingDisposition,
  onFindingRequestChange,
  onFindingFix,
  onFindingAsk,
}: AnnotationCardProps) {
  const [replyTo, setReplyTo] = useState("");
  return (
    <div className="review-annotations">
      <BelowThresholdList
        items={comments}
        render={(comment) => (
          <Comment
            key={comment.id}
            comment={comment}
            replies={repliesFor(all, comment.id)}
            busy={busy}
            onResolve={onResolve}
            onReopen={onReopen}
            onReply={setReplyTo}
            onFindingDisposition={onFindingDisposition}
            onFindingRequestChange={onFindingRequestChange}
            onFindingFix={onFindingFix}
            onFindingAsk={onFindingAsk}
          />
        )}
      />
      {/*
        The composer is keyed to the comment it answers because React would
        otherwise keep one instance across a change of parent: a top-level draft
        still sitting in the textarea when the reviewer clicks Reply would be
        posted, with the kind they picked for it, against a thread they only
        meant to answer. Remounting discards the wrong draft instead of moving
        it somewhere it was never written for.
      */}
      {(composing || replyTo !== "") && (
        <Composer
          key={replyTo || "root"}
          label={replyTo ? "Reply" : label}
          busy={busy}
          parentId={replyTo}
          targetMarkAvailable={targetMarkAvailable}
          initialBody={replyTo ? "" : draftBody}
          initialKind={replyTo ? "comment" : draftKind}
          initialMarkTarget={replyTo ? true : draftMarkTarget}
          focusKey={replyTo || draftFocusKey}
          onDraftChange={replyTo ? undefined : onDraftChange}
          onAsk={replyTo ? undefined : onAsk}
          onSuggestEdit={replyTo ? undefined : onSuggestEdit}
          onEditSource={replyTo ? undefined : onEditSource}
          onSubmit={(body, kind, parentId, markTarget) => {
            setReplyTo("");
            onSubmit(body, kind, parentId, markTarget);
          }}
          onCancel={() => {
            // Cancelling a reply must not discard an unrelated top-level draft.
            if (replyTo) {
              setReplyTo("");
              return;
            }
            onCancel();
          }}
        />
      )}
    </div>
  );
}
/**
 * The comments on this file that no longer point anywhere, with their reasons.
 *
 * Listed rather than drawn, always. An orphan rendered on its last known line
 * would be the silent relocation the ladder refuses to perform — the reader
 * would take a stale number for a claim.
 *
 * The reason comes from the server's own rung label, never from a sentence
 * written here: "more than one place fit" is true of an orphan and false of a
 * comment on a binary file we never looked at, and one hard-coded explanation
 * over both is a claim about which of them happened.
 *
 * Each row carries the same identification `Discussion` gives an anchored one —
 * source, location, state, title. Losing the anchor is exactly when the reader
 * needs it: with no line to weigh the claim against, whether a machine or a
 * colleague wrote it, and whether it is still open, is all that is left to
 * judge it by.
 */
export function OrphanedComments({
  comments,
  onSetState,
}: {
  comments: Annotation[];
  /** Resolve/reopen only; the anchor stays lost. Absent when the reader cannot take judgments. */
  onSetState?(id: string, state: "open" | "resolved"): void;
}) {
  if (comments.length === 0) return null;
  return (
    <div className="border-b border-amber-900/50 bg-amber-950/15 px-3 py-2.5">
      <div className="review-kicker text-amber-300">
        {comments.length === 1 ? "1 comment lost its anchor" : `${comments.length} comments lost their anchor`}
      </div>
      <BelowThresholdList items={comments} render={(comment) => (
        <div key={comment.id} className="mt-2 rounded-md border border-amber-900/35 bg-amber-950/10 px-3 py-2 text-label text-amber-200/80">
          <div className="flex flex-wrap items-center gap-2 font-mono text-caption text-amber-300/75">
            <span className={annotationIdentityColor(comment)}>{annotationIdentityLabel(comment)}</span>
            <span>{comment.file_level ? "file" : locationLabel(comment.start_line, comment.end_line)}</span>
            <span>{comment.state}</span>
            <span>not anchored</span>
          </div>
          {comment.title && <div className="pt-0.5 font-sans font-semibold text-amber-100">{comment.title}</div>}
          <div className="whitespace-pre-wrap font-sans">{comment.body}</div>
          <div className="font-mono text-caption text-amber-200/50">
            {comment.anchor_method_label || comment.anchor_method}: {comment.anchor_detail || "no reason recorded"}
          </div>
          {onSetState && comment.state !== "obsolete" && (
            <UiButton
              type="button"
              variant="ghost"
              size="compact"
              className="mt-1.5"
              onClick={() => onSetState(comment.id, comment.state === "resolved" ? "open" : "resolved")}
            >
              {comment.state === "resolved" ? "Reopen" : "Resolve"}
            </UiButton>
          )}
        </div>
      )} />
    </div>
  );
}
