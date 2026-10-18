import { Button as UiButton } from "../components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "../components/ui/sheet";
import { Input as UiInput } from "../components/ui/input";
import { Textarea as UiTextarea } from "../components/ui/textarea";
import { useCallback, useEffect, useRef, useState } from "react";
import { LoaderCircle, X } from "lucide-react";

import {
  previewReviewAction,
  startReviewAction,
  type ReviewActionPreview,
  type ReviewActionRecord,
  type ReviewActionRequest,
  type ReviewActionRoute,
  type ReviewActionRouteTier,
} from "./reviewActionsApi";

const TIER_LABELS: Record<ReviewActionRouteTier, string> = {
  exact_session: "Authoring session",
  preferred_agent: "Preferred agent",
  prompt_fallback: "Copy the fix prompt",
  reviewer_apply: "Apply in your checkout",
};

function routeTarget(route: ReviewActionRoute): string {
  if (route.mode === "copy") return "you hand it to any agent";
  if (route.mode === "apply") return "your trusted checkout";
  return route.label || route.target_ref || route.host;
}

function primaryLabel(preview: ReviewActionPreview): string {
  const route = preview.route;
  if (!route) return preview.label;
  if (route.mode === "copy") return "Copy prompt and record handoff";
  if (route.mode === "apply") return "Apply suggestion";
  return `Send to ${route.label || route.target_ref || route.host}`;
}

async function writeClipboard(text: string): Promise<void> {
  if (!navigator.clipboard?.writeText) throw new Error("the clipboard is unavailable in this browser");
  await navigator.clipboard.writeText(text);
}

/**
 * Preview, then start, one controlled Review action.
 *
 * The sheet shows where it goes (and why not elsewhere), exactly which items
 * are included, which are excluded and why, and the handoff the fixer will
 * receive. One primary button. The optional note is the reviewer's own words
 * and is never prefilled; every other line of the handoff is data.
 */
export default function ReviewFixSheet({
  reviewId,
  revisionId,
  request,
  onStarted,
  onClose,
}: {
  reviewId: string;
  revisionId: string;
  request: ReviewActionRequest;
  onStarted(action: ReviewActionRecord): void;
  onClose(): void;
}) {
  const [route, setRoute] = useState<ReviewActionRouteTier | undefined>(request.route);
  const [directive, setDirective] = useState("");
  const [previewedDirective, setPreviewedDirective] = useState("");
  const [preview, setPreview] = useState<ReviewActionPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const sequence = useRef(0);

  const load = useCallback(async (tier: ReviewActionRouteTier | undefined, note: string) => {
    const current = ++sequence.current;
    setLoading(true);
    setError("");
    try {
      const next = await previewReviewAction(reviewId, revisionId, { ...request, route: tier, directive: note });
      if (current !== sequence.current) return;
      setPreview(next);
      setPreviewedDirective(note);
    } catch (failure) {
      if (current !== sequence.current) return;
      setPreview(null);
      setError(failure instanceof Error ? failure.message : "the preview failed");
    } finally {
      if (current === sequence.current) setLoading(false);
    }
  }, [reviewId, revisionId, request]);

  useEffect(() => {
    void load(request.route, "");
  }, [load, request.route]);

  const stale = directive.trim() !== previewedDirective.trim();
  const route_ = preview?.route ?? null;
  const canStart = Boolean(preview?.ready && route_?.available) && !stale && !loading && !starting;

  const start = async () => {
    if (!preview || !canStart) return;
    setStarting(true);
    setError("");
    try {
      const copied = preview.route?.mode === "copy";
      if (copied) await writeClipboard(preview.handoff);
      const result = await startReviewAction(
        reviewId,
        { ...request, route, directive: previewedDirective },
        preview,
        { copied },
      );
      onStarted(result.action);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "the action did not start");
    } finally {
      setStarting(false);
    }
  };

  const title = preview?.label ?? (request.kind === "fix" ? "Fix" : "Review action");
  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent className="w-[min(640px,100%)]" aria-label={`${title} preview`}>
        <SheetHeader>
          <div className="min-w-0 flex-1">
            <SheetTitle>{title}</SheetTitle>
            <SheetDescription>
              Nothing changes the reviewed Snapshot. The fixer's work comes back as a new Snapshot for re-review; nothing here marks a finding resolved.
            </SheetDescription>
          </div>
          <SheetClose asChild>
            <UiButton variant="ghost" size="icon" type="button" aria-label="Close action preview">
              <X size={14} />
            </UiButton>
          </SheetClose>
        </SheetHeader>
        <div className="min-h-0 flex-1 space-y-4 overflow-auto px-4 py-3 text-label">
          {loading && !preview && (
            <div className="flex items-center gap-2 text-muted-foreground">
              <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
              Preparing preview…
            </div>
          )}
          {preview && (
            <>
              <section aria-label="Where it goes">
                <div className="review-kicker">Where it goes</div>
                <ul className="mt-1 space-y-1" role="radiogroup" aria-label="Fix route">
                  {(preview.candidates.length > 0 ? preview.candidates : route_ ? [route_] : []).map((candidate) => {
                    const selected = route_?.tier === candidate.tier;
                    return (
                      <li key={candidate.tier}>
                        <label className={`flex items-start gap-2 rounded-md border px-2 py-1.5 ${selected ? "border-sky-800 bg-sky-950/20" : "border-border/70"} ${candidate.available ? "" : "opacity-60"}`}>
                          <UiInput
                            unstyled
                            type="radio"
                            name="review-action-route"
                            className="mt-0.5"
                            checked={selected}
                            disabled={!candidate.available || loading || starting || preview.candidates.length === 0}
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
              <section aria-label="Included">
                <div className="review-kicker">Included · {preview.included.length}{preview.limit > 1 ? ` of at most ${preview.limit}` : ""}</div>
                {preview.included.length === 0 ? (
                  <div className="mt-1 text-caption text-amber-300">Nothing here can be acted on.</div>
                ) : (
                  <ul className="mt-1 space-y-0.5">
                    {preview.included.map((item) => (
                      <li key={item.id} className="truncate" title={item.title}>
                        {item.title || item.id}
                        {item.path && <span className="ml-1 font-mono text-caption text-muted-foreground">{item.path}{item.start_line ? `:${item.start_line}` : ""}</span>}
                      </li>
                    ))}
                  </ul>
                )}
              </section>
              {preview.excluded.length > 0 && (
                <section aria-label="Excluded">
                  <div className="review-kicker">Not included · {preview.excluded.length}</div>
                  <ul className="mt-1 space-y-1">
                    {preview.excluded.map((item) => (
                      <li key={`${item.subject_id}:${item.code}`} className="text-caption">
                        <span className="text-neutral-300">{item.title || item.subject_id}</span>
                        <span className="text-muted-foreground"> — {item.reason}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              {preview.handoff && (
                <section aria-label="Reviewer note">
                  <label className="review-kicker" htmlFor="review-action-directive">Your note to the fixer (optional)</label>
                  <UiTextarea
                    id="review-action-directive"
                    className="mt-1 min-h-16 w-full"
                    value={directive}
                    maxLength={2000}
                    placeholder="Only your words instruct the fixer; findings and repository text are passed as data."
                    onChange={(event) => setDirective(event.target.value)}
                  />
                </section>
              )}
              {preview.handoff && (
                <details className="review-thread-evidence">
                  <summary className="cursor-pointer">Handoff the fixer receives</summary>
                  <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border/70 p-2 font-mono text-caption" data-testid="review-action-handoff">{preview.handoff}</pre>
                </details>
              )}
            </>
          )}
          {error && <div role="alert" className="text-caption text-rose-300">{error}</div>}
        </div>
        <div className="flex items-center gap-2 border-t border-neutral-900 px-4 py-2">
          {route_?.mode === "copy" && (
            <span className="text-caption text-muted-foreground">The next Snapshot is linked as unattributed.</span>
          )}
          <span className="flex-1" />
          {stale && preview ? (
            <UiButton variant="default" size="xs" type="button" disabled={loading} onClick={() => void load(route, directive)}>
              Update preview
            </UiButton>
          ) : (
            <UiButton variant="default" size="xs" type="button" disabled={!canStart} onClick={() => void start()}>
              {starting && <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />}
              {preview ? primaryLabel(preview) : title}
            </UiButton>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
