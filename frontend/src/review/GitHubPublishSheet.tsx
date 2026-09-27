import { useCallback, useEffect, useRef, useState } from "react";
import { ExternalLink, X } from "lucide-react";

import "./reviewUi.css";
import { trapModalTab } from "./focusTrap";
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
  const closeRef = useRef<HTMLButtonElement>(null);
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
    closeRef.current?.focus();
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
    <div
      className="absolute inset-0 z-[70] flex justify-end bg-black/45"
      role="dialog"
      aria-modal="true"
      aria-labelledby="github-publish-title"
      onKeyDown={(event) => {
        if (trapModalTab(event)) return;
        if (event.key === "Escape") {
          event.stopPropagation();
          onClose();
        }
      }}
    >
      <section className="review-sheet flex h-full w-[min(500px,100%)] flex-col border-y-0 border-r-0">
        <div className="flex items-start gap-3 border-b border-neutral-800/80 px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 id="github-publish-title" className="text-[13px] font-semibold text-neutral-100">
              Publish to GitHub{preview ? ` · PR #${preview.pr.number}` : ""}
            </h2>
            <p className="mt-0.5 text-[10px] leading-4 text-neutral-500">
              Posted as one review under your own <code>gh</code> login. Nothing is sent until you publish.
            </p>
          </div>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close publish to GitHub" className="review-icon-button">
            <X size={14} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto px-4 py-4 text-[11px] text-neutral-300">
          {busy === "preview" && <p className="text-neutral-500">Asking GitHub what it will accept…</p>}
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
              {preview.empty && !preview.stale && <p className="mb-3 text-neutral-500">Nothing new to publish.</p>}
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
                  <p className="mb-1 text-[10px] text-neutral-500">GitHub only accepts inline comments on lines it shows in the diff.</p>
                  <ul className="space-y-1">
                    {preview.folded.map((item) => (
                      <li key={item.annotation_id}>
                        <span className="font-mono text-neutral-400">{item.location}</span>
                        <span className="text-neutral-500"> · {item.reason}</span>
                        <div className="whitespace-pre-wrap text-neutral-300">{item.body}</div>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-neutral-800/80 bg-neutral-950/95 px-4 py-3">
          {preview && (
            <a href={preview.pr.url} target="_blank" rel="noreferrer" className="review-toolbar-button mr-auto">
              View PR
            </a>
          )}
          <button type="button" disabled={busy !== ""} onClick={result ? onClose : () => void load()} className="review-toolbar-button">
            {result ? "Done" : "Refresh preview"}
          </button>
          {!result && (
            <button type="button" disabled={busy !== "" || blocked} onClick={() => void publish()} className="review-toolbar-button-primary">
              {busy === "publish" ? "Publishing…" : "Publish to GitHub"}
            </button>
          )}
        </div>
      </section>
    </div>
  );
}
