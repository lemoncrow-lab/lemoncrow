import { useState } from "react";
import { Clock3, Copy } from "lucide-react";

import {
  fallbackReviewEnvironment,
  reviewEnvironmentDescription,
  reviewEnvironmentLabel,
  type ReviewEnvironment,
} from "./reviewEnvironment";
import type { ReviewOverview } from "./types";

interface ReviewIdentityBarProps {
  overview: ReviewOverview;
  focusMode: boolean;
  authorshipOverride?: { prefix: string; label: string };
  contextOverride?: { label: string; title?: string };
  environment?: ReviewEnvironment;
  onShowOverview(): void;
  onShowHistory(): void;
}

export default function ReviewIdentityBar({
  overview,
  focusMode,
  authorshipOverride,
  contextOverride,
  environment,
  onShowOverview,
  onShowHistory,
}: ReviewIdentityBarProps) {
  const [copyIdState, setCopyIdState] = useState<"idle" | "copied" | "failed">("idle");
  const story = overview.change_story?.length
    ? `${overview.change_story.join(" · ")}${(overview.change_story_more ?? 0) > 0 ? ` · +${overview.change_story_more} more` : ""}`
    : overview.title || "Review";
  const changeId = overview.session.ref || `r/${overview.session.id}`;
  const authorship = authorshipOverride ?? (overview.revision.provenance_model
    ? { prefix: overview.revision.provenance_certainty === "exact" ? "agent" : "agent signal", label: overview.revision.provenance_model }
    : overview.session.actor_type === "agent"
      ? { prefix: "", label: "agent" }
      : { prefix: "", label: "local" });
  const resolvedEnvironment = environment ?? fallbackReviewEnvironment("local");
  const localSource = resolvedEnvironment.review.local_workspace
    ? overview.session.range_mode === "working_tree"
      ? "Working tree"
      : overview.session.range_mode === "staged"
        ? "Staged changes"
        : overview.session.source_ref || (overview.session.range_mode === "commit_range" ? "Commit range" : "Local source")
    : "";

  const copyReviewId = () => {
    const write = navigator.clipboard?.writeText;
    if (!write) {
      setCopyIdState("failed");
      return;
    }
    void Promise.resolve(write.call(navigator.clipboard, changeId)).then(
      () => {
        setCopyIdState("copied");
        window.setTimeout(() => setCopyIdState("idle"), 1200);
      },
      () => setCopyIdState("failed"),
    );
  };

  return (
    <div className="review-reader-identity">
      {!focusMode && (
        <button
          type="button"
          onClick={copyReviewId}
          aria-label={`Copy Review ID ${changeId}`}
          title={`Copy Review ID: ${changeId}`}
          className="review-reader-id group"
        >
          <span>{changeId}</span>
          <Copy size={10} className="shrink-0 text-neutral-700 transition-colors group-hover:text-neutral-50" />
          {copyIdState === "copied" && <span className="font-sans font-medium text-emerald-400">Copied</span>}
          {copyIdState === "failed" && <span className="font-sans font-medium text-amber-300">Copy failed</span>}
        </button>
      )}
      <button
        type="button"
        onClick={onShowOverview}
        aria-label="Open change overview"
        className="review-reader-title"
        title={`${story} · Open change overview`}
      >
        {focusMode ? "Review" : story}
      </button>
      {!focusMode && (
        <span className="review-reader-authorship">
          {authorship.prefix && <span className="review-reader-author">{authorship.prefix} </span>}
          <span className="review-reader-author font-medium text-neutral-400">{authorship.label}</span>
          {localSource && (
            <span className="review-reader-source-context ml-2 border-l border-neutral-800 pl-2 font-medium text-neutral-500" title={`Reviewing ${localSource}`}>
              {localSource}
            </span>
          )}
          <span
            className="review-reader-mode-context ml-2 border-l border-neutral-800 pl-2 font-medium text-neutral-500"
            title={reviewEnvironmentDescription(resolvedEnvironment)}
            aria-label={`Review environment: ${reviewEnvironmentLabel(resolvedEnvironment.mode)}`}
          >
            {reviewEnvironmentLabel(resolvedEnvironment.mode)}
          </span>
          {contextOverride?.label && (
            <span
              className="review-reader-repo-context ml-2 max-w-[220px] truncate border-l border-neutral-800 pl-2 font-mono text-[10px] text-neutral-600"
              title={contextOverride.title || contextOverride.label}
              aria-label={`Review context: ${contextOverride.label}`}
            >
              {contextOverride.label}
            </span>
          )}
        </span>
      )}
      <button
        type="button"
        onClick={onShowHistory}
        className="review-compact-action review-reader-revision"
        aria-label={`Revision ${overview.revision.revision_number}; open revision compare and history`}
        title="Revision history and compare"
      >
        <Clock3 size={12} />
        <span className="font-mono text-[10px]">rev {overview.revision.revision_number}</span>
      </button>
    </div>
  );
}
