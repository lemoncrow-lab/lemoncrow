import { Button as UiButton } from "../components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "../components/ui/sheet";
import { useCallback, useEffect, useState } from "react";
import { ExternalLink, X } from "lucide-react";

import "./reviewUi.css";
import { previewGitHubPublish, publishGitHubReview } from "./reviewApi";
import type { GitHubPublishPreview, GitHubPublishResult } from "./types";

interface GitHubPublishSheetProps {
  reviewId: string;
  onClose(): void;
  /** Called once GitHub holds the review, so the Reader can refresh its feedback state. */
  onPublished?: () => void;
}

const EVENT_LABEL: Record<GitHubPublishPreview["event"], string> = {
  APPROVE: "Approve",
  REQUEST_CHANGES: "Request changes",
  COMMENT: "Comment",
};

function span(start: number, line: number): string {
  return start === line ? `L${line}` : `L${start}-L${line}`;
}

/** Preview, then post, this Review's comments and verdict to its GitHub pull request. */
export default function GitHubPublishSheet({ reviewId, onClose, onPublished }: GitHubPublishSheetProps) {
  const [preview, setPreview] = useState<GitHubPublishPreview | null>(null);
  const [result, setResult] = useState<GitHubPublishResult | null>(null);
  const [busy, setBusy] = useState<"preview" | "publish" | "">("preview");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setBusy("preview");
    setError("");
    try {
      setPreview(await previewGitHubPublish(reviewId));
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  }, [reviewId]);

  useEffect(() => {
    void load();
  }, [load]);

  const publish = async () => {
    if (!preview || busy) return;
    setBusy("publish");
    setError("");
    try {
      setResult(await publishGitHubReview(reviewId, preview));
      onPublished?.();
    } catch (reason) {
      setError(String(reason instanceof Error ? reason.message : reason));
    } finally {
      setBusy("");
    }
  };

  const blocked = !preview || preview.stale || preview.empty;

  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent>
        <SheetHeader>
          <div className="min-w-0 flex-1">
            <SheetTitle>
              Publish to GitHub{preview ? ` · PR #${preview.pr.number}` : ""}
            </SheetTitle>
            <SheetDescription>
              Posted as one review under your own <code>gh</code> login. Nothing is sent until you publish.
            </SheetDescription>
          </div>
          <SheetClose asChild>
            <UiButton variant="ghost" size="icon" type="button" aria-label="Close publish to GitHub">
              <X size={14} />
            </UiButton>
          </SheetClose>
        </SheetHeader>

        <div className="min-h-0 flex-1 overflow-auto px-4 py-4 text-label text-neutral-300">
          {busy === "preview" && <p className="text-muted-foreground">Asking GitHub what it will accept…</p>}
          {error && <p role="alert" className="mb-3 text-amber-300">{error}</p>}
          {result && (
            <p className="mb-3 text-emerald-300">
              {result.adopted ? "Already on GitHub" : "Published"}: {EVENT_LABEL[result.event]}, {result.inline} inline
              {result.folded > 0 ? `, ${result.folded} in the review body` : ""}.{" "}
              <a href={result.html_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 underline">
                Open on GitHub <ExternalLink size={11} />
              </a>
            </p>
          )}
          {preview && !result && (
            <>
              {preview.stale && (
                <p className="mb-3 text-amber-300">
                  The pull request has new commits. Run <code>lc review --pr {preview.pr.number}</code> to refresh, then publish.
                </p>
              )}
              {preview.empty && !preview.stale && <p className="mb-3 text-muted-foreground">Nothing new to publish.</p>}
              {preview.notices.map((notice) => (
                <p key={notice} className="mb-2 text-amber-200">{notice}</p>
              ))}
              <div className="review-kicker mb-1">Verdict</div>
              <p className="mb-3">{EVENT_LABEL[preview.event]}{preview.summary ? ` — ${preview.summary}` : ""}</p>
              <div className="review-kicker mb-1">Inline comments ({preview.inline.length})</div>
              <ul className="mb-3 space-y-1">
                {preview.inline.map((item) => (
                  <li key={item.annotation_id}>
                    <span className="font-mono text-neutral-400">{item.path}:{span(item.start_line, item.line)}{item.side === "LEFT" ? " (old)" : ""}</span>
                    <div className="whitespace-pre-wrap text-neutral-300">{item.body}</div>
                  </li>
                ))}
              </ul>
              {preview.folded.length > 0 && (
                <>
                  <div className="review-kicker mb-1">In the review body ({preview.folded.length})</div>
                  <p className="mb-1 text-caption text-muted-foreground">GitHub only accepts inline comments on lines it shows in the diff.</p>
                  <ul className="space-y-1">
                    {preview.folded.map((item) => (
                      <li key={item.annotation_id}>
                        <span className="font-mono text-neutral-400">{item.location}</span>
                        <span className="text-muted-foreground"> · {item.reason}</span>
                        <div className="whitespace-pre-wrap text-neutral-300">{item.body}</div>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-border/80 bg-background/95 px-4 py-3">
          {preview && (
            <UiButton asChild className="mr-auto">
              <a href={preview.pr.url} target="_blank" rel="noreferrer">
                View PR
              </a>
            </UiButton>
          )}
          <UiButton type="button" disabled={busy !== ""} onClick={result ? onClose : () => void load()}>
            {result ? "Done" : "Refresh preview"}
          </UiButton>
          {!result && (
            <UiButton variant="default" type="button" disabled={busy !== "" || blocked} onClick={() => void publish()} className="px-3">
              {busy === "publish" ? "Publishing…" : "Publish to GitHub"}
            </UiButton>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
