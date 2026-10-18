import { Button as UiButton } from "../components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "../components/ui/sheet";
import { Input as UiInput } from "../components/ui/input";
import { Textarea as UiTextarea } from "../components/ui/textarea";
import { useCallback, useEffect, useRef, useState } from "react";
import { LoaderCircle, X } from "lucide-react";

import { reviewHref } from "./reviewApi";
import type { ReviewActionRoute, ReviewActionRouteTier } from "./reviewActionsApi";
import {
  SPLIT_CANCELLABLE,
  SPLIT_COLLECTABLE,
  cancelReviewSplit,
  collectReviewSplit,
  fetchReviewSplits,
  previewReviewSplit,
  startReviewSplit,
  type ReviewSplitListing,
  type ReviewSplitPreview,
  type ReviewSplitRecord,
} from "./reviewSplitApi";

const TIER_LABELS: Record<ReviewActionRouteTier, string> = {
  exact_session: "Authoring session",
  preferred_agent: "Preferred agent",
  prompt_fallback: "Copy the split prompt",
  reviewer_apply: "Apply in your checkout",
};

const STATUS_TEXT: Record<string, string> = {
  dispatching: "Sending",
  awaiting_branches: "Waiting for the agent's branches",
  uncertain: "Delivery uncertain",
  collecting: "Collecting",
  completed: "Split into linked Reviews",
  failed: "Failed",
  cancelled: "Cancelled",
};

function routeTarget(route: ReviewActionRoute): string {
  if (route.mode === "copy") return "you hand it to any agent";
  return route.label || route.target_ref || route.host;
}

function primaryLabel(preview: ReviewSplitPreview): string {
  const route = preview.route;
  if (!route) return "Request split";
  if (route.mode === "copy") return "Copy prompt and record handoff";
  return `Send to ${route.label || route.target_ref || route.host}`;
}

async function writeClipboard(text: string): Promise<void> {
  if (!navigator.clipboard?.writeText) throw new Error("the clipboard is unavailable in this browser");
  await navigator.clipboard.writeText(text);
}

function SplitRecordRow({
  record,
  collectAvailable,
  collectReason,
  busy,
  onCollect,
  onCancel,
}: {
  record: ReviewSplitRecord;
  collectAvailable: boolean;
  collectReason: string;
  busy: boolean;
  onCollect(record: ReviewSplitRecord): void;
  onCancel(record: ReviewSplitRecord): void;
}) {
  const collectable = SPLIT_COLLECTABLE.has(record.status);
  return (
    <li className="rounded-md border border-border/70 px-2 py-1.5" data-testid="review-split-record">
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 text-neutral-200">
          {`${record.plan.layers.length} stacked layers · ${STATUS_TEXT[record.status] ?? record.status}`}
        </span>
        {collectable && (
          <UiButton
            variant="outline"
            size="xs"
            type="button"
            disabled={busy || !collectAvailable}
            title={collectAvailable ? "Turn the agent's branches into linked Reviews" : collectReason}
            onClick={() => onCollect(record)}
          >
            Collect branches
          </UiButton>
        )}
        {SPLIT_CANCELLABLE.has(record.status) && (
          <UiButton variant="ghost" size="xs" type="button" disabled={busy} onClick={() => onCancel(record)}>
            Cancel
          </UiButton>
        )}
      </div>
      {record.message && <div className="mt-0.5 text-caption text-muted-foreground">{record.message}</div>}
      {collectable && !collectAvailable && collectReason && (
        <div className="mt-0.5 text-caption text-amber-300">{collectReason}</div>
      )}
      {record.children.length > 0 && (
        <ul className="mt-1 space-y-0.5">
          {record.children.map((child) => (
            <li key={child.review_id} className="truncate text-caption">
              <a className="text-sky-300 hover:underline" href={reviewHref(child.review_id)}>
                {child.title || `Layer ${child.layer}`}
              </a>
              <span className="ml-1 font-mono text-muted-foreground">{child.branch}</span>
              {child.extra_paths.length > 0 && (
                <span className="ml-1 text-amber-300">{`also changes ${child.extra_paths.length} file${child.extra_paths.length === 1 ? "" : "s"} of other layers`}</span>
              )}
            </li>
          ))}
        </ul>
      )}
      {record.migration && (
        <div className="mt-0.5 text-caption text-muted-foreground">
          {`${record.migration.carried} judgment${record.migration.carried === 1 ? "" : "s"} carried on identical targets`}
          {record.migration.not_carried_total > 0 ? ` · ${record.migration.not_carried_total} not carried (target changed or not in a layer)` : ""}
        </div>
      )}
    </li>
  );
}

/**
 * Ask the authoring agent to split this Change into stacked Changes, one per
 * cohort (Phase 12 item 13), with the same route, preview and handoff rules as
 * Fix. The original Review stays open; collected layers become linked Reviews.
 */
export default function ReviewSplitSheet({
  reviewId,
  revisionId,
  onClose,
}: {
  reviewId: string;
  revisionId: string;
  onClose(): void;
}) {
  const [route, setRoute] = useState<ReviewActionRouteTier | undefined>(undefined);
  const [directive, setDirective] = useState("");
  const [previewedDirective, setPreviewedDirective] = useState("");
  const [preview, setPreview] = useState<ReviewSplitPreview | null>(null);
  const [listing, setListing] = useState<ReviewSplitListing | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sequence = useRef(0);

  const load = useCallback(async (tier: ReviewActionRouteTier | undefined, note: string) => {
    const current = ++sequence.current;
    setLoading(true);
    setError("");
    try {
      const [nextListing, nextPreview] = await Promise.all([
        fetchReviewSplits(reviewId),
        previewReviewSplit(reviewId, revisionId, { route: tier, directive: note }),
      ]);
      if (current !== sequence.current) return;
      setListing(nextListing);
      setPreview(nextPreview);
      setPreviewedDirective(note);
    } catch (failure) {
      if (current !== sequence.current) return;
      setPreview(null);
      setError(failure instanceof Error ? failure.message : "the split preview failed");
    } finally {
      if (current === sequence.current) setLoading(false);
    }
  }, [reviewId, revisionId]);

  useEffect(() => {
    void load(undefined, "");
  }, [load]);

  const stale = directive.trim() !== previewedDirective.trim();
  const route_ = preview?.route ?? null;
  const canStart = Boolean(preview?.ready && route_?.available) && !stale && !loading && !busy;

  const run = async (work: () => Promise<unknown>, fallback: string) => {
    setBusy(true);
    setError("");
    try {
      await work();
      await load(route, previewedDirective);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : fallback);
    } finally {
      setBusy(false);
    }
  };

  const start = () => run(async () => {
    if (!preview || !canStart) return;
    const copied = preview.route?.mode === "copy";
    if (copied) await writeClipboard(preview.handoff);
    await startReviewSplit(reviewId, { route, directive: previewedDirective }, preview, { copied });
  }, "the split did not start");

  const splits = listing?.splits ?? [];
  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent className="w-[min(640px,100%)]" aria-label="Request split preview">
        <SheetHeader>
          <div className="min-w-0 flex-1">
            <SheetTitle>Request split</SheetTitle>
            <SheetDescription>
              The authoring agent builds one stacked branch per cohort. Collected layers become Reviews linked to this one; this Review stays open and nothing here closes it.
            </SheetDescription>
          </div>
          <SheetClose asChild>
            <UiButton variant="ghost" size="icon" type="button" aria-label="Close split preview">
              <X size={14} />
            </UiButton>
          </SheetClose>
        </SheetHeader>
        <div className="min-h-0 flex-1 space-y-4 overflow-auto px-4 py-3 text-label">
          {loading && !preview && (
            <div className="flex items-center gap-2 text-muted-foreground">
              <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
              Planning the split…
            </div>
          )}
          {splits.length > 0 && (
            <section aria-label="Split requests">
              <div className="review-kicker">Requests</div>
              <ul className="mt-1 space-y-1">
                {splits.map((record) => (
                  <SplitRecordRow
                    key={record.id}
                    record={record}
                    busy={busy}
                    collectAvailable={Boolean(listing?.collect_available)}
                    collectReason={listing?.collect_reason ?? ""}
                    onCollect={(item) => void run(() => collectReviewSplit(reviewId, item.id, item.plan.digest), "the branches were not collected")}
                    onCancel={(item) => void run(() => cancelReviewSplit(reviewId, item.id), "the split was not cancelled")}
                  />
                ))}
              </ul>
            </section>
          )}
          {preview && !preview.ready && preview.reason && (
            <div className="text-caption text-amber-300" data-testid="review-split-refused">{preview.reason}</div>
          )}
          {preview?.plan && (
            <>
              <section aria-label="Layers">
                <div className="review-kicker">{`Stacked layers · ${preview.plan.layers.length}`}</div>
                <ol className="mt-1 space-y-1">
                  {preview.plan.layers.map((layer) => (
                    <li key={layer.branch} data-testid="review-split-layer">
                      <div className="flex items-baseline gap-2">
                        <span className="min-w-0 flex-1 truncate text-neutral-200">{`${layer.index}. ${layer.title}`}</span>
                        <span className="shrink-0 text-caption text-muted-foreground">{`${layer.paths.length} file${layer.paths.length === 1 ? "" : "s"}`}</span>
                      </div>
                      <div className="truncate font-mono text-caption text-muted-foreground" title={layer.paths.join("\n")}>
                        {layer.branch}
                      </div>
                    </li>
                  ))}
                </ol>
              </section>
              {preview.route && (
                <section aria-label="Where it goes">
                  <div className="review-kicker">Where it goes</div>
                  <ul className="mt-1 space-y-1" role="radiogroup" aria-label="Split route">
                    {(preview.candidates.length > 0 ? preview.candidates : [preview.route]).map((candidate) => {
                      const selected = route_?.tier === candidate.tier;
                      return (
                        <li key={candidate.tier}>
                          <label className={`flex items-start gap-2 rounded-md border px-2 py-1.5 ${selected ? "border-sky-800 bg-sky-950/20" : "border-border/70"} ${candidate.available ? "" : "opacity-60"}`}>
                            <UiInput
                              unstyled
                              type="radio"
                              name="review-split-route"
                              className="mt-0.5"
                              checked={selected}
                              disabled={!candidate.available || loading || busy || preview.candidates.length === 0}
                              onChange={() => {
                                setRoute(candidate.tier);
                                void load(candidate.tier, directive);
                              }}
                            />
                            <span className="min-w-0 flex-1">
                              <span className="block text-neutral-200">{TIER_LABELS[candidate.tier]}</span>
                              <span className="block text-caption text-muted-foreground">
                                {candidate.available ? routeTarget(candidate) : candidate.reason || "unavailable"}
                              </span>
                            </span>
                          </label>
                        </li>
                      );
                    })}
                  </ul>
                </section>
              )}
              <section aria-label="Reviewer note">
                <label className="review-kicker" htmlFor="review-split-directive">Your note to the agent (optional)</label>
                <UiTextarea
                  id="review-split-directive"
                  className="mt-1 min-h-16 w-full"
                  value={directive}
                  maxLength={2000}
                  placeholder="Only your words instruct the agent; cohort titles and file paths are passed as data."
                  onChange={(event) => setDirective(event.target.value)}
                />
              </section>
              {preview.handoff && (
                <details className="review-thread-evidence">
                  <summary className="cursor-pointer">Handoff the agent receives</summary>
                  <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border/70 p-2 font-mono text-caption" data-testid="review-split-handoff">{preview.handoff}</pre>
                </details>
              )}
            </>
          )}
          {error && <div role="alert" className="text-caption text-rose-300">{error}</div>}
        </div>
        <div className="flex items-center gap-2 border-t border-neutral-900 px-4 py-2">
          {preview && !preview.collect_available && (
            <span className="min-w-0 truncate text-caption text-muted-foreground" title={preview.collect_reason}>
              Collect the branches from your local Review.
            </span>
          )}
          <span className="flex-1" />
          {stale && preview ? (
            <UiButton variant="default" size="xs" type="button" disabled={loading} onClick={() => void load(route, directive)}>
              Update preview
            </UiButton>
          ) : (
            <UiButton variant="default" size="xs" type="button" disabled={!canStart} onClick={() => void start()}>
              {busy && <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />}
              {preview ? primaryLabel(preview) : "Request split"}
            </UiButton>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
