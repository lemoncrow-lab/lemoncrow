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

import { useState } from "react";

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
  onSubmit,
  onCancel,
}: {
  label: string;
  busy: boolean;
  parentId: string;
  targetMarkAvailable: boolean;
  onSubmit(body: string, kind: AnnotationKind, parentId: string, markTarget: boolean): void;
  onCancel(): void;
}) {
  const [body, setBody] = useState("");
  const [kind, setKind] = useState<AnnotationKind>("comment");
  const [markTarget, setMarkTarget] = useState(true);
  const canMarkTarget = targetMarkAvailable && parentId === "" && kind === "request_change";
  const submit = () => {
    if (busy || body.trim() === "") return;
    onSubmit(body, kind, parentId, canMarkTarget && markTarget);
  };
  return (
    <div className="flex flex-col gap-2">
      <div className="font-mono text-[10px] uppercase tracking-widest text-neutral-500">{label}</div>
      <textarea
        autoFocus
        rows={3}
        value={body}
        onChange={(event) => setBody(event.target.value)}
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
        placeholder="What does the reviewer need to know?"
        className="w-full resize-y border border-neutral-800 bg-neutral-950 p-2 font-sans text-[12px] text-neutral-200 outline-none focus:border-neutral-600"
      />
      <div className="flex flex-wrap items-center gap-1.5">
        {KIND_ORDER.map((option) => (
          <button
            key={option}
            type="button"
            aria-pressed={kind === option}
            onClick={() => setKind(option)}
            className={`border px-2 py-0.5 text-[11px] ${
              kind === option ? KIND_ACCENTS[option] : "border-neutral-800 text-neutral-500"
            }`}
          >
            {KIND_LABELS[option]}
          </button>
        ))}
        {canMarkTarget && (
          <label className="flex items-center gap-1.5 px-1 text-[10px] text-rose-300/80">
            <input
              type="checkbox"
              checked={markTarget}
              onChange={(event) => setMarkTarget(event.target.checked)}
              className="accent-rose-500"
            />
            Mark target needs changes
          </label>
        )}
        <span className="flex-1" />
        <button
          type="button"
          onClick={onCancel}
          className="border border-neutral-800 px-2 py-0.5 text-[11px] text-neutral-400"
        >
          Cancel (esc)
        </button>
        <button
          type="button"
          disabled={busy || body.trim() === ""}
          onClick={submit}
          className="border border-sky-800 px-2 py-0.5 text-[11px] text-sky-300 disabled:opacity-40"
        >
          Save
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
  return (
    <div className={`flex flex-col gap-1 ${resolved ? "opacity-60" : ""}`}>
      <div className="flex items-center gap-2 font-mono text-[10px]">
        <span className={`border px-1 ${SOURCE_ACCENTS[source]}`}>{SOURCE_LABELS[source]}</span>
        {human && <span className={`border px-1 ${KIND_ACCENTS[comment.kind]}`}>{KIND_LABELS[comment.kind]}</span>}
        {sourceDetail && <span className="text-neutral-500">{sourceDetail}</span>}
        {comment.symbol ? (
          <span className="truncate text-neutral-600">in {comment.symbol}</span>
        ) : (
          comment.origin_symbol && (
            <span className="truncate text-neutral-600">originally in {comment.origin_symbol}</span>
          )
        )}
        {resolved && <span className="text-neutral-500">resolved</span>}
        {human && comment.author_response === "addressed" && !resolved && (
          <span className="border border-sky-800 px-1 text-sky-300">
            author says addressed · re-review
          </span>
        )}
      </div>
      {/*
        How this comment got to this line, on every card. Without it a
        heuristic re-find is indistinguishable from an untouched file, which
        is the one thing the relocation ladder must never let happen.
      */}
      <div
        title={comment.anchor_detail}
        className={`w-fit border px-1 font-mono text-[10px] ${anchorAccent(comment)}`}
      >
        anchor: {comment.anchor_method_label || comment.anchor_method}
      </div>
      {comment.title && <div className="font-sans text-[12px] font-semibold text-neutral-100">{comment.title}</div>}
      <div className="whitespace-pre-wrap font-sans text-[12px] text-neutral-200">{comment.body}</div>
      {comment.evidence && comment.evidence.length > 0 && (
        <details className="font-mono text-[10px] text-neutral-500">
          <summary className="cursor-pointer">evidence · {comment.evidence.length}</summary>
          <ul className="pl-4 pt-1">
            {comment.evidence.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </details>
      )}
      {comment.confidence != null && (
        <div className="font-mono text-[10px] text-neutral-600">confidence {Math.round(comment.confidence * 100)}%</div>
      )}
      {human && comment.author_response === "addressed" && !resolved && comment.author_response_source_id && (
        <div className="font-mono text-[10px] text-sky-500/80">
          claimed by {comment.author_response_source_id}; only you can resolve this comment
        </div>
      )}
      {replies.map((reply) => (
        <div key={reply.id} className="border-l border-neutral-800 pl-2">
          <div className="font-mono text-[10px] text-neutral-500">{reply.created_by}</div>
          <div className="whitespace-pre-wrap font-sans text-[12px] text-neutral-300">{reply.body}</div>
        </div>
      ))}
      {human && (
        <div className="flex gap-1.5">
          <button
            type="button"
            disabled={busy}
            onClick={() => onReply(comment.id)}
            className="text-[11px] text-neutral-500 hover:text-neutral-300 disabled:opacity-40"
          >
            Reply
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => (resolved ? onReopen(comment.id) : onResolve(comment.id))}
            className="text-[11px] text-neutral-500 hover:text-neutral-300 disabled:opacity-40"
          >
            {resolved ? "Reopen" : "Resolve"}
          </button>
        </div>
      )}
    </div>
  );
}
export default function AnnotationCard({
  comments,
  all,
  label,
  composing,
  busy,
  targetMarkAvailable = false,
  onSubmit,
  onCancel,
  onResolve,
  onReopen,
}: AnnotationCardProps) {
  const [replyTo, setReplyTo] = useState("");
  return (
    <div className="my-1 flex flex-col gap-3 border border-neutral-800 bg-neutral-900/70 p-2.5">
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
          onSubmit={(body, kind, parentId, markTarget) => {
            setReplyTo("");
            onSubmit(body, kind, parentId, markTarget);
          }}
          onCancel={() => {
            setReplyTo("");
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
    <div className="border-b border-amber-900/60 bg-amber-950/20 px-3 py-2">
      <div className="font-mono text-[10px] uppercase tracking-widest text-amber-300">
        {comments.length === 1 ? "1 comment lost its anchor" : `${comments.length} comments lost their anchor`}
      </div>
      {comments.map((comment) => (
        <div key={comment.id} className="pt-1.5 text-[11px] text-amber-200/80">
          <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] text-amber-300/80">
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
