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
import { Check, LoaderCircle, MessageSquareText, Reply, RotateCcw, X } from "lucide-react";

import "./reviewUi.css";

import {
  KIND_ACCENTS,
  KIND_LABELS,
  KIND_ORDER,
  SOURCE_ACCENTS,
  SOURCE_LABELS,
  anchorAccent,
  annotationSource,
  isHumanJudgment,
  locationLabel,
  repliesFor,
} from "./annotationModel";
import type { Annotation, AnnotationKind } from "./types";

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
  onSuggestEdit?(): void;
  onEditSource?(): void;
  onSubmit(body: string, kind: AnnotationKind, parentId: string, markTarget: boolean): void;
  onCancel(): void;
  onResolve(id: string): void;
  onReopen(id: string): void;
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
      if (input && document.activeElement !== input) input.focus({ preventScroll: true });
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
        <span>{label}</span>
      </div>
      <textarea
        ref={inputRef}
        autoFocus
        rows={3}
        aria-label={parentId ? "Reply text" : "Comment text"}
        aria-keyshortcuts="Control+Enter Meta+Enter"
        value={body}
        onChange={(event) => {
          setBody(event.target.value);
          if (parentId === "") onDraftChange?.({ body: event.target.value });
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            onCancel();
            return;
          }
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            submit();
          }
        }}
        placeholder={placeholder}
        className="review-field review-composer-input"
      />
      <div className="review-composer-options" role="group" aria-label="Feedback type">
        {KIND_ORDER.filter((option) => option !== "suggestion" || !(onSuggestEdit || onEditSource)).map((option) => (
          <button
            key={option}
            type="button"
            aria-pressed={kind === option}
            onClick={() => {
              setKind(option);
              if (parentId === "") onDraftChange?.({ kind: option });
            }}
            className={`review-composer-kind ${
              kind === option ? KIND_ACCENTS[option] : "border-transparent text-neutral-400 hover:bg-neutral-800/50 hover:text-neutral-100"
            }`}
          >
            {KIND_LABELS[option]}
          </button>
        ))}
        {!parentId && (onSuggestEdit || onEditSource) && (
          <div className="review-composer-source-actions" role="group" aria-label="Source actions">
            {onSuggestEdit && (
              <button
                type="button"
                onClick={onSuggestEdit}
                className="review-composer-kind review-composer-kind-source border-violet-900/70 text-violet-300"
                title="Save a concrete patch without changing the working source"
              >
                Suggest edit
              </button>
            )}
            {onEditSource && (
              <button
                type="button"
                onClick={onEditSource}
                className="review-composer-kind review-composer-kind-source border-sky-900/70 text-sky-300"
                title="Edit the trusted working source through the guarded proposal path"
              >
                Edit source
              </button>
            )}
          </div>
        )}
      </div>
        {canMarkTarget && (
          <label className="review-composer-mark">
            <input
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
        <span className="review-composer-shortcut"><kbd>Ctrl/⌘</kbd> + <kbd>Enter</kbd> to send</span>
        <button type="button" onClick={onCancel} className="review-toolbar-button">
          <X size={14} aria-hidden="true" />
          Cancel
        </button>
        <button
          type="button"
          disabled={busy || body.trim() === ""}
          onClick={submit}
          className="review-toolbar-button-primary"
        >
          {busy ? <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <Check size={14} aria-hidden="true" />}
          {busy ? "Saving…" : submitLabel}
        </button>
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
}: {
  comment: Annotation;
  replies: Annotation[];
  busy: boolean;
  onResolve(id: string): void;
  onReopen(id: string): void;
  onReply(id: string): void;
}) {
  const resolved = comment.state === "resolved";
  const source = annotationSource(comment);
  const human = isHumanJudgment(comment);
  const sourceDetail = comment.source_id || comment.created_by;
  const blocking = human && comment.kind === "request_change" && !resolved;
  return (
    <article className={`review-thread ${blocking ? "review-thread-blocking" : ""} ${resolved ? "review-thread-resolved" : ""}`}>
      <div className="review-thread-heading">
        <span className={`review-thread-source ${SOURCE_ACCENTS[source].split(" ").at(-1) ?? "text-neutral-400"}`}>{SOURCE_LABELS[source]}</span>
        {sourceDetail && <span className="review-thread-author">{sourceDetail}</span>}
        {human && comment.kind !== "comment" && (
          <span className={`rounded-md border px-1.5 py-0.5 ${KIND_ACCENTS[comment.kind]}`}>{KIND_LABELS[comment.kind]}</span>
        )}
        {!comment.file_level && comment.end_line > comment.start_line && (
          <span className="review-thread-symbol font-mono">{locationLabel(comment.start_line, comment.end_line)}</span>
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
        {resolved && <span className="inline-flex items-center gap-1 text-emerald-400"><Check size={13} aria-hidden="true" />Resolved</span>}
      </div>
      <div className="review-thread-content">
      {comment.title && <div className="review-thread-title">{comment.title}</div>}
      <div className="review-thread-body">{comment.body}</div>
      {/*
        How this comment got to this line remains visible on every card, but it
        follows the human message instead of interrupting it. The relocation
        contract is trust metadata; the comment itself is the thing to read.
      */}
      <div
        title={comment.anchor_detail}
        className={`review-thread-anchor ${anchorAccent(comment)}`}
      >
        anchor: {comment.anchor_method_label || comment.anchor_method}
      </div>
      {human && comment.author_response === "addressed" && !resolved && (
        <div className="w-fit rounded-md border border-sky-900/55 bg-sky-950/15 px-2 py-1 text-[10px] font-medium text-sky-300">
          marked addressed · re-review
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
      {comment.confidence != null && (
        <div className="text-[11px] text-neutral-400">confidence {Math.round(comment.confidence * 100)}%</div>
      )}
      {human && comment.author_response === "addressed" && !resolved && comment.author_response_source_id && (
        <div className="font-mono text-[10px] text-sky-500/80">
          addressed by {comment.author_response_source_id}; only you can resolve this comment
        </div>
      )}
      {replies.length > 0 && (
        <div className="review-thread-replies">
          {replies.map((reply) => (
            <div key={reply.id} className="review-thread-reply">
              <div className="mb-1 flex items-center gap-2 text-[12px] font-medium text-neutral-400">
                <span>{reply.created_by}</span>
                {reply.kind !== "comment" && (
                  <span className={`rounded-md border px-1.5 py-0.5 text-[10px] ${KIND_ACCENTS[reply.kind]}`}>{KIND_LABELS[reply.kind]}</span>
                )}
              </div>
              <div className="review-thread-body">{reply.body}</div>
            </div>
          ))}
        </div>
      )}
      {human && (
        <div className="review-thread-actions">
          <button
            type="button"
            disabled={busy}
            onClick={() => onReply(comment.id)}
            className="review-compact-action"
          >
            <Reply size={14} aria-hidden="true" />
            Reply
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => (resolved ? onReopen(comment.id) : onResolve(comment.id))}
            className="review-compact-action"
          >
            {resolved ? <RotateCcw size={14} aria-hidden="true" /> : <Check size={14} aria-hidden="true" />}
            {resolved ? "Reopen" : "Resolve"}
          </button>
        </div>
      )}
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
  onSuggestEdit,
  onEditSource,
  onSubmit,
  onCancel,
  onResolve,
  onReopen,
}: AnnotationCardProps) {
  const [replyTo, setReplyTo] = useState("");
  return (
    <div className="review-annotations">
      {comments.map((comment) => (
        <Comment
          key={comment.id}
          comment={comment}
          replies={repliesFor(all, comment.id)}
          busy={busy}
          onResolve={onResolve}
          onReopen={onReopen}
          onReply={setReplyTo}
        />
      ))}
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
export function OrphanedComments({ comments }: { comments: Annotation[] }) {
  if (comments.length === 0) return null;
  return (
    <div className="border-b border-amber-900/50 bg-amber-950/15 px-3 py-2.5">
      <div className="review-kicker text-amber-300">
        {comments.length === 1 ? "1 comment lost its anchor" : `${comments.length} comments lost their anchor`}
      </div>
      {comments.map((comment) => (
        <div key={comment.id} className="mt-2 rounded-md border border-amber-900/35 bg-amber-950/10 px-3 py-2 text-[11px] text-amber-200/80">
          <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] text-amber-300/75">
            <span>{SOURCE_LABELS[annotationSource(comment)]}</span>
            <span>{comment.file_level ? "file" : locationLabel(comment.start_line, comment.end_line)}</span>
            <span>{comment.state}</span>
          </div>
          {comment.title && <div className="pt-0.5 font-sans font-semibold text-amber-100">{comment.title}</div>}
          <div className="whitespace-pre-wrap font-sans">{comment.body}</div>
          <div className="font-mono text-[10px] text-amber-200/50">
            {comment.anchor_method_label || comment.anchor_method}: {comment.anchor_detail || "no reason recorded"}
          </div>
        </div>
      ))}
    </div>
  );
}
