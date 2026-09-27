import { Clock3 } from "lucide-react";

import {
  fallbackReviewEnvironment,
  reviewEnvironmentDescription,
  reviewEnvironmentLabel,
  type ReviewEnvironment,
} from "./reviewEnvironment";
import { webHref } from "./safeLinks";
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
  const story = overview.change_story?.length
    ? `${overview.change_story.join(" · ")}${(overview.change_story_more ?? 0) > 0 ? ` · +${overview.change_story_more} more` : ""}`
    : overview.title || "Review";
  const authorship = authorshipOverride;
  const resolvedEnvironment = environment ?? fallbackReviewEnvironment("local");
  const localSource = resolvedEnvironment.review.local_workspace
    ? overview.session.range_mode === "working_tree"
      ? "Working tree"
      : overview.session.range_mode === "staged"
        ? "Staged changes"
        : overview.session.range_mode === "commit_range"
          ? "Commit range"
          : "Local source"
    : "";
  const linkedChanges = (overview.change?.relations ?? [])
    .filter(
      (item) =>
        ["parent", "previous", "next", "depends_on", "stack"].includes(item.relation) &&
        (item.target_change_ref || item.external_id || item.title),
    )
    .slice(0, 2);
  const linkedChangeLabels = linkedChanges.map((item) => {
    const target = item.target_change_ref || item.title || item.external_id;
    return (item.relation === "depends_on" ? "depends on" : item.relation) + " " + target;
  });
  const linkedChangeLabel = linkedChangeLabels.join(" · ");
  const workItem = (overview.change?.work_items ?? [])[0];
  const workItemLabel = workItem ? (workItem.external_id || workItem.title || "Work item") : "";
  const workspaceContext = contextOverride?.label
    ? contextOverride
    : resolvedEnvironment.review.local_workspace && overview.workspace_root
      ? { label: overview.workspace_root, title: `Workspace ${overview.workspace_root}` }
      : null;

  return (
    <div className="review-reader-identity">
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
          {localSource && (
            <span className="review-reader-source-context font-medium text-neutral-500" title={`Reviewing ${localSource}`}>
              {localSource}
            </span>
          )}
          {authorship && (authorship.prefix || authorship.label) && (
            <span className={localSource ? "ml-2 border-l border-neutral-800 pl-2" : ""}>
              {authorship.prefix && <span className="review-reader-author">{authorship.prefix} </span>}
              <span className="review-reader-author font-medium text-neutral-400">{authorship.label}</span>
            </span>
          )}
          {linkedChangeLabel && (
            <span
              className="review-reader-source-context ml-2 max-w-[220px] truncate border-l border-neutral-800 pl-2 font-medium text-neutral-500"
              title={linkedChangeLabel}
              aria-label={"Related Change: " + linkedChangeLabel}
            >
              {linkedChangeLabel}
            </span>
          )}
          {workItem && (
            webHref(workItem.url) ? (
              <a
                href={webHref(workItem.url)}
                target="_blank"
                rel="noreferrer"
                className="review-reader-source-context ml-2 max-w-[180px] truncate border-l border-neutral-800 pl-2 font-medium text-neutral-500 hover:text-neutral-300 hover:underline"
                title={workItem.title || workItem.external_id}
                aria-label={`Open associated work item ${workItemLabel}`}
              >
                {workItemLabel}
              </a>
            ) : (
              <span
                className="review-reader-source-context ml-2 max-w-[180px] truncate border-l border-neutral-800 pl-2 font-medium text-neutral-500"
                title={workItem.title || workItem.external_id}
              >
                {workItemLabel}
              </span>
            )
          )}
          <span
            className="review-reader-mode-context ml-2 border-l border-neutral-800 pl-2 font-medium text-neutral-500"
            title={reviewEnvironmentDescription(resolvedEnvironment)}
            aria-label={"Review environment: " + reviewEnvironmentLabel(resolvedEnvironment.mode)}
          >
            {reviewEnvironmentLabel(resolvedEnvironment.mode)}
          </span>
          {workspaceContext?.label && (
            <span
              className="review-reader-repo-context ml-2 max-w-[220px] truncate border-l border-neutral-800 pl-2 font-mono text-[10px] text-neutral-600"
              title={workspaceContext.title || workspaceContext.label}
              aria-label={contextOverride?.label ? `Review context: ${workspaceContext.label}` : `Review workspace: ${workspaceContext.label}`}
            >
              {workspaceContext.label}
            </span>
          )}
        </span>
      )}
      <button
        type="button"
        onClick={onShowHistory}
        className="review-compact-action review-reader-revision"
        aria-label={`Snapshot ${overview.revision.revision_number}; open Snapshot history and compare`}
        title="Snapshot history and compare"
      >
        <Clock3 size={12} />
        <span className="font-mono text-[10px]">Snapshot {overview.revision.revision_number}</span>
      </button>
    </div>
  );
}
