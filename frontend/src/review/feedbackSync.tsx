/**
 * Published feedback (P12-AUTO item 11): a comment that is on the provider is
 * changed through the provider, never silently in LemonCrow alone. The Reader
 * loads each comment's provider identity and latest sync state once; the
 * comment card shows it and offers edit / withdraw through the provider, and
 * on a visible `sync_failed` retry or discard. An unpublished comment shows
 * nothing here: its edits stay local.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { CloudOff, LoaderCircle, UploadCloud } from "lucide-react";

import { Button as UiButton } from "../components/ui/button";
import { Textarea as UiTextarea } from "../components/ui/textarea";
import {
  fetchFeedbackSync,
  syncPublishedComment,
  type CommentSyncView,
  type FeedbackSyncOp,
} from "./automationApi";
import type { Annotation } from "./types";

export interface FeedbackSyncContextValue {
  comments: Readonly<Record<string, CommentSyncView>>;
  pending: string;
  errors: Readonly<Record<string, string>>;
  sync(annotationId: string, op: FeedbackSyncOp, body?: string): Promise<boolean>;
}

const FeedbackSyncContext = createContext<FeedbackSyncContextValue | null>(null);

export const FeedbackSyncProvider = FeedbackSyncContext.Provider;

export function useFeedbackSyncContext(): FeedbackSyncContextValue | null {
  return useContext(FeedbackSyncContext);
}

/** One load per Review (and after each sync); a failed load shows no sync claims. */
export function useFeedbackSync({
  reviewId,
  enabled,
  onChanged,
}: {
  reviewId: string;
  enabled: boolean;
  /** The local comment changed (the provider confirmed): reload comments. */
  onChanged?(): void;
}): FeedbackSyncContextValue {
  const [comments, setComments] = useState<Record<string, CommentSyncView>>({});
  const [pending, setPending] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const load = useCallback(async () => {
    if (!enabled || !reviewId) return;
    try {
      setComments((await fetchFeedbackSync(reviewId)).comments);
    } catch {
      setComments({});
    }
  }, [enabled, reviewId]);
  useEffect(() => { void load(); }, [load]);
  const sync = useCallback(async (annotationId: string, op: FeedbackSyncOp, body = "") => {
    setPending(annotationId);
    setErrors((current) => ({ ...current, [annotationId]: "" }));
    try {
      const result = await syncPublishedComment(annotationId, op, body);
      await load();
      if (result.local_changed) onChanged?.();
      return result.sync.state !== "sync_failed";
    } catch (reason) {
      setErrors((current) => ({ ...current, [annotationId]: String(reason instanceof Error ? reason.message : reason) }));
      return false;
    } finally {
      setPending("");
    }
  }, [load, onChanged]);
  return useMemo(() => ({ comments, pending, errors, sync }), [comments, pending, errors, sync]);
}

function providerLabel(view: CommentSyncView): string {
  return (view.sync?.provider || "github") === "github" ? "GitHub" : view.sync?.provider || "the provider";
}

export function PublishedFeedbackControls({ comment, busy }: { comment: Annotation; busy: boolean }) {
  const context = useFeedbackSyncContext();
  const view = context?.comments[comment.id];
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(comment.body);
  if (!context || !view || (!view.published && !view.sync)) return null;
  const provider = providerLabel(view);
  const record = view.sync;
  const inFlight = context.pending === comment.id;
  const error = context.errors[comment.id] || "";
  const failed = record?.state === "sync_failed";
  const folded = view.published?.kind === "review";
  return (
    <div className="review-thread-actions flex-wrap" aria-label="Published feedback" data-testid="published-feedback">
      <span className="inline-flex items-center gap-1 text-caption text-muted-foreground">
        {failed ? <CloudOff size={12} className="text-rose-300" aria-hidden="true" /> : <UploadCloud size={12} aria-hidden="true" />}
        {failed
          ? `Sync to ${provider} failed`
          : record?.state === "synced"
            ? `Updated on ${provider}`
            : record?.state === "corrected"
              ? `Corrected on ${provider} by a follow-up comment`
              : record?.state === "sync_pending"
                ? `Updating ${provider}…`
                : `Published on ${provider}`}
      </span>
      {failed && record && (
        <span role="alert" className="basis-full text-caption text-rose-300">
          {record.error || "The provider refused the change."} The comment here is unchanged.
        </span>
      )}
      {failed ? (
        <>
          <UiButton variant="ghost" size="compact" className="h-6" type="button" disabled={busy || inFlight} onClick={() => void context.sync(comment.id, "retry")}>
            Retry
          </UiButton>
          <UiButton variant="ghost" size="compact" className="h-6" type="button" disabled={busy || inFlight} onClick={() => void context.sync(comment.id, "discard")}>
            Discard change
          </UiButton>
        </>
      ) : view.published && comment.state !== "obsolete" && !editing && (
        <>
          <UiButton
            variant="ghost"
            size="compact"
            className="h-6"
            type="button"
            disabled={busy || inFlight}
            onClick={() => { setDraft(comment.body); setEditing(true); }}
          >
            {folded ? `Correct on ${provider}` : `Edit on ${provider}`}
          </UiButton>
          <UiButton variant="ghost" size="compact" className="h-6" type="button" disabled={busy || inFlight} onClick={() => void context.sync(comment.id, "withdraw")}>
            {folded ? `Withdraw with a follow-up` : `Withdraw from ${provider}`}
          </UiButton>
        </>
      )}
      {inFlight && <LoaderCircle size={12} className="animate-spin text-muted-foreground" aria-label="Syncing" />}
      {editing && (
        <form
          className="basis-full space-y-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            void context.sync(comment.id, "edit", draft.trim()).then((ok) => { if (ok) setEditing(false); });
          }}
        >
          <UiTextarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            aria-label={`Edited comment for ${provider}`}
            className="min-h-16 text-label"
          />
          <div className="flex gap-1">
            <UiButton size="xs" type="submit" disabled={busy || inFlight || !draft.trim() || draft.trim() === comment.body}>
              {folded ? `Post correction on ${provider}` : `Update on ${provider}`}
            </UiButton>
            <UiButton size="xs" variant="ghost" type="button" disabled={inFlight} onClick={() => setEditing(false)}>Cancel</UiButton>
          </div>
        </form>
      )}
      {error && <span role="alert" className="basis-full text-caption text-rose-300">{error}</span>}
    </div>
  );
}
